"""Polymarket 真实市场接线（Alpha Discovery V1）。

- Gamma ``/markets`` 拉真实市场；只保留 active、未关闭、BTC、到期在 1..7 天内；
- 方向映射：问题含 "above/reach/be above" → ABOVE；"dip/below/fall to" → BELOW；
  其它 → UNKNOWN（不进研究）。YES 概率上升在 BELOW 市场代表看跌，绝不当看涨；
- 盘口（CLOB ``/book``）可选：拿不到就标记 ``BOOK_UNAVAILABLE``，不猜概率；
- 视界匹配：剩余到期时间 / 5m <= 500 才可研究（与 market.registry 同一口径）。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone

from .contracts import PredictionMarketEvent

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"
BPS = Decimal("10000")
PRIMARY_HORIZON_MS = 300_000
MAX_HORIZON_RATIO = 500
UA = {"User-Agent": "PaperTrade-OpenAlpha/1.0 (research; read-only)"}


@dataclass(frozen=True)
class GammaMarket:
    market_id: str
    question: str
    end_ms: int
    volume24h: float
    yes_token: str
    no_token: str
    comparator: str        # ABOVE / BELOW / UNKNOWN
    active: bool
    closed: bool


def _fetch_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers=UA)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _tokens(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [str(t) for t in parsed] if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def direction_from_question(question: str) -> str:
    q = question.lower()
    if any(k in q for k in ("above", "reach", "be above", "hit $", "exceed")):
        return "ABOVE"
    if any(k in q for k in ("dip", "below", "fall to", "drop to", "under $")):
        return "BELOW"
    return "UNKNOWN"


def parse_gamma(payload) -> list[GammaMarket]:
    out: list[GammaMarket] = []
    for m in payload if isinstance(payload, list) else []:
        try:
            tokens = _tokens(m.get("clobTokenIds"))
            end_raw = str(m.get("endDate") or "")
            end_ms = int(datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                         .timestamp() * 1000) if end_raw else 0
            out.append(GammaMarket(
                market_id=str(m.get("id") or ""),
                question=str(m.get("question") or ""),
                end_ms=end_ms,
                volume24h=float(m.get("volume24hr") or 0),
                yes_token=tokens[0] if len(tokens) > 0 else "",
                no_token=tokens[1] if len(tokens) > 1 else "",
                comparator=direction_from_question(str(m.get("question") or "")),
                active=bool(m.get("active")),
                closed=bool(m.get("closed")),
            ))
        except (ValueError, TypeError):
            continue
    return out


def fetch_gamma(fetch=None, limit: int = 500, timeout: float = 20.0) -> list[GammaMarket]:
    fn = fetch or (lambda u: _fetch_json(u, timeout))
    payload = fn(f"{GAMMA_MARKETS}?limit={limit}&closed=false&order=volume24hr&ascending=false")
    return parse_gamma(payload)


def select_btc_markets(markets: list[GammaMarket], now_ms: int,
                       max_days: float = 7.0, min_volume: float = 0.0) -> list[GammaMarket]:
    horizon_ms = max_days * 86_400_000
    out = []
    for m in markets:
        if not m.active or m.closed or not m.yes_token or not m.no_token:
            continue
        if not (now_ms < m.end_ms <= now_ms + horizon_ms):
            continue
        q = m.question.lower()
        if "bitcoin" not in q and "btc" not in q:
            continue
        if m.comparator == "UNKNOWN":
            continue
        if m.volume24h < min_volume:
            continue
        out.append(m)
    return sorted(out, key=lambda m: m.volume24h, reverse=True)


def _mid_from_book(book: dict | None) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if not book:
        return None, None, None
    try:
        bid = Decimal(str(book["yes_bid"]))
        ask = Decimal(str(book["yes_ask"]))
    except (KeyError, ArithmeticError):
        return None, None, None
    if bid <= 0 or ask <= 0 or bid >= ask:
        return None, None, None
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid * BPS
    return mid, spread, Decimal(str(book.get("prev_yes_mid"))) if book.get("prev_yes_mid") else None


def to_events(markets: list[GammaMarket], books: dict[str, dict], now_ms: int,
              min_ratio: Decimal = Decimal(MAX_HORIZON_RATIO)) -> tuple[PredictionMarketEvent, ...]:
    events = []
    for m in markets:
        book = books.get(m.yes_token)
        mid, spread, prev = _mid_from_book(book)
        remaining = m.end_ms - now_ms
        ratio = Decimal(remaining) / Decimal(PRIMARY_HORIZON_MS)
        reasons = []
        horizon_ok = ratio <= min_ratio
        if not horizon_ok:
            reasons.append(f"horizon_mismatch:{ratio:.0f}")
        quality = "OK"
        if mid is None:
            quality = "UNKNOWN"
            reasons.append("BOOK_UNAVAILABLE")
        elif spread is not None and spread > Decimal("200"):
            quality = "WIDE_SPREAD"
            reasons.append(f"spread_too_wide:{spread:.0f}bps")
        events.append(PredictionMarketEvent(
            market_id=m.market_id,
            question=m.question,
            yes_mid=mid,
            prev_yes_mid=prev,
            delta=(mid - prev) if (mid is not None and prev is not None) else None,
            spread_bps=spread,
            liquidity_usd=Decimal(str(book.get("liquidity_usd"))) if book and book.get("liquidity_usd") else None,
            ends_at_ms=m.end_ms,
            resolution_source="polymarket",
            researchable=bool(horizon_ok and quality == "OK"),
            horizon_match=horizon_ok,
            reasons=tuple(reasons),
            data_quality=quality,
            advisory_only=True,
            comparator=m.comparator,
        ))
    return tuple(events)


def fetch_books(markets: list[GammaMarket], fetch=None, timeout: float = 12.0) -> tuple[dict[str, dict], list[str]]:
    """逐个 token 拉盘口；失败只记 warning，不伪造价格。"""
    fn = fetch or (lambda u: _fetch_json(u, timeout))
    books: dict[str, dict] = {}
    warnings: list[str] = []
    for m in markets:
        for token, role in ((m.yes_token, "yes"), (m.no_token, "no")):
            try:
                payload = fn(f"{CLOB_BOOK}?token_id={token}")
            except Exception:  # noqa: BLE001
                warnings.append(f"BOOK_FAILED:{m.market_id}:{role}")
                continue
            if role != "yes":
                continue
            try:
                bids = payload.get("bids") or []
                asks = payload.get("asks") or []
                bid = max((Decimal(str(x["price"])) for x in bids), default=None)
                ask = min((Decimal(str(x["price"])) for x in asks), default=None)
                books[token] = {
                    "yes_bid": bid, "yes_ask": ask,
                    "liquidity_usd": payload.get("liquidity") or payload.get("liquidityNum"),
                }
            except (KeyError, ArithmeticError, TypeError):
                warnings.append(f"BOOK_PARSE_FAILED:{m.market_id}")
    return books, warnings