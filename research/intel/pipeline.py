"""情报采集管线（Alpha Discovery V1）：真实 feeds + Polymarket + 钱包 → IntelDigest。

所有外部调用都可注入；单源失败只记 warning，不阻塞其它源，也不伪造数据。
DeepSeek 无 key 时自动回退规则抽取，并在 warnings 里写明 ``LLM_UNAVAILABLE``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .contracts import IntelDigest, NewsEvent, PredictionMarketEvent, SourceRef, WalletSignal
from .digest import build_digest
from .feeds import FeedItem, fetch_feeds
from .news import NewsStore
from .news_llm import ExtractedEvent, LLMConfig, extract, rule_extract
from .polymarket_fetch import fetch_books, fetch_gamma, select_btc_markets, to_events
from .wallets import WalletTrade, analyze as analyze_wallets

DEFAULT_FEEDS: tuple[tuple[str, str], ...] = (
    ("https://cointelegraph.com/rss", "cointelegraph"),
    ("https://decrypt.co/feed", "decrypt"),
    ("https://www.theblock.co/rss.xml", "theblock"),
)


@dataclass
class IntelPipelineConfig:
    feeds: tuple[tuple[str, str], ...] = DEFAULT_FEEDS
    max_days: float = 7.0
    min_volume: float = 0.0
    max_news: int = 40          # 每轮最多送 LLM 的条数（成本/延迟上限）
    llm: LLMConfig = field(default_factory=LLMConfig)
    fetch: object = None            # 新闻 HTTP fetch（注入）
    gamma_fetch: object = None      # Gamma fetch（注入）
    book_fetch: object = None       # CLOB book fetch（注入）
    llm_fetch: object = None        # DeepSeek fetch（注入）
    with_books: bool = True


def collect(now_ms: int, cfg: IntelPipelineConfig | None = None,
            wallet_trades: list[WalletTrade] | None = None) -> IntelDigest:
    cfg = cfg or IntelPipelineConfig()
    warnings: list[str] = []
    store = NewsStore()

    items, feed_warnings = fetch_feeds(cfg.feeds, fetch=cfg.fetch)
    warnings.extend(feed_warnings)
    # 按发布时间倒序，只送最新 N 条给 LLM（成本与延迟上限）
    items = sorted(items, key=lambda i: i.published_ms or 0, reverse=True)[:cfg.max_news]
    if not cfg.llm.available:
        warnings.append("LLM_UNAVAILABLE: no DEEPSEEK_API_KEY; falling back to rules")

    added = 0
    for item in items:
        ev = extract(item, cfg.llm, fetch=cfg.llm_fetch) if cfg.llm.available else None
        if ev is None:
            ev = rule_extract(item)
        event = store.add(
            headline=item.title,
            raw_text=item.summary or item.title,
            occurred_at_ms=item.published_ms or now_ms,
            first_seen_ms=now_ms,
            sources=(SourceRef(item.link or "", item.source, item.published_ms or now_ms,
                               now_ms, now_ms),),
            horizon_ms=ev.horizon_hours * 3_600_000,
            direction=ev.direction,
            confidence=ev.confidence,
            event_type=ev.event_type,
            extraction_method=ev.extraction_method,
        )
        if event is not None:
            added += 1
    if items and added == 0:
        warnings.append(f"NEWS_ALL_STALE: fetched={len(items)} none inside horizon")
    elif len(items) > added:
        warnings.append(f"NEWS_DEDUPED_OR_STALE: fetched={len(items)} kept={added}")

    markets: tuple[PredictionMarketEvent, ...] = ()
    try:
        gamma = fetch_gamma(cfg.gamma_fetch)
        selected = select_btc_markets(gamma, now_ms, cfg.max_days, cfg.min_volume)
        books: dict[str, dict] = {}
        if cfg.with_books and selected:
            books, book_warnings = fetch_books(selected, cfg.book_fetch)
            warnings.extend(book_warnings)
        markets = to_events(selected, books, now_ms)
    except Exception as exc:  # noqa: BLE001 - 市场源失败不阻塞新闻
        warnings.append(f"GAMMA_FAILED:{type(exc).__name__}")

    wallets: tuple[WalletSignal, ...] = ()
    if wallet_trades:
        wallets = analyze_wallets(wallet_trades, now_ms)
    else:
        warnings.append("WALLET_SOURCE_UNAVAILABLE: no wallet history source configured")

    return build_digest(now_ms, news=store.events(now_ms), markets=markets,
                        wallets=wallets, warnings=tuple(warnings))