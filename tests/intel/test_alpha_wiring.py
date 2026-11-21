"""情报接线测试（Alpha Discovery V1）：全部离线、注入 fetch。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from research.intel.ablation import evaluate as evaluate_ablation
from research.intel.contracts import IntelDigest
from research.intel.features import compute as compute_features
from research.intel.feeds import FeedItem, parse_feed
from research.intel.news_llm import LLMConfig, extract, rule_extract
from research.intel.pipeline import IntelPipelineConfig, collect
from research.intel.polymarket_fetch import (
    direction_from_question,
    parse_gamma,
    select_btc_markets,
    to_events,
)
from research.market.contracts import DataQuality, MarketState

D = Decimal
NOW = 1_795_000_000_000
HOUR = 3_600_000


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_rss(items: list[tuple[str, str, str, str]]) -> str:
    parts = ["<?xml version='1.0'?><rss version='2.0'><channel>"]
    for title, link, pub, desc in items:
        parts.append(f"<item><title>{title}</title><link>{link}</link>"
                     f"<pubDate>{pub}</pubDate><description>{desc}</description></item>")
    parts.append("</channel></rss>")
    return "".join(parts)


def fake_gamma() -> list[dict]:
    def market(mid, q, days, tokens, vol=10000.0):
        return {"id": mid, "question": q, "endDate": _iso(NOW + int(days * 24 * HOUR)),
                "volume24hr": vol, "clobTokenIds": json.dumps(tokens),
                "active": True, "closed": False}

    return [
        market("m1", "Will Bitcoin be above $56,000 on November 21?", 0.6, ["111", "222"], 200000),
        market("m2", "Will Bitcoin dip to $52,000 November 16-22?", 1.6, ["333", "444"], 150000),
        market("m3", "Will Bitcoin dip to $55,000 by December 31, 2026?", 40, ["555", "666"], 700000),
        market("m4", "Will Manchester City FC win on 2026-11-21?", 0.2, ["777", "888"], 900000),
    ]


def fake_book() -> dict:
    return {"bids": [{"price": "0.60"}, {"price": "0.62"}],
            "asks": [{"price": "0.66"}, {"price": "0.64"}],
            "liquidity": "50000"}


def test_parse_rss_and_atom() -> None:
    rss = fake_rss([("BTC ETF inflow 3.2B", "https://x/1", "Fri, 21 Nov 2026 12:00:00 GMT",
                    "3.2 billion inflow")])
    items = parse_feed(rss, "test")
    assert len(items) == 1 and items[0].published_ms is not None

    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>Fed cuts rates</title><link href="https://x/2"/>
      <updated>2026-11-21T12:00:00Z</updated><summary>25bps</summary></entry></feed>"""
    items2 = parse_feed(atom, "atom")
    assert items2 and items2[0].link == "https://x/2"


def test_rule_extract_direction() -> None:
    pos = rule_extract(FeedItem("s", "ETF approval inflow", "", NOW, ""))
    neg = rule_extract(FeedItem("s", "exchange hack ban", "", NOW, ""))
    assert pos.direction == "bullish" and neg.direction == "bearish"
    assert pos.extraction_method == "rules"


def test_llm_unavailable_returns_none() -> None:
    assert extract(FeedItem("s", "t", "", NOW, ""), LLMConfig(api_key="")) is None


def test_llm_parses_strict_json() -> None:
    def fake_post(url, key, body, timeout):
        return {"choices": [{"message": {"content": '{"direction":"bearish","confidence":0.8,'
                                            '"horizon_hours":12,"event_type":"macro",'
                                            '"evidence":"hawkish"}'}}]}

    ev = extract(FeedItem("s", "t", "", NOW, ""), LLMConfig(api_key="k"), fetch=fake_post)
    assert ev is not None and ev.direction == "bearish" and ev.confidence == D("0.8")

    def bad_post(url, key, body, timeout):
        return {"choices": [{"message": {"content": "not json"}}]}

    assert extract(FeedItem("s", "t", "", NOW, ""), LLMConfig(api_key="k"), fetch=bad_post) is None


def test_polymarket_direction_and_selection() -> None:
    assert direction_from_question("Will Bitcoin be above $56,000 on November 21?") == "ABOVE"
    assert direction_from_question("Will Bitcoin dip to $52,000 November 16-22?") == "BELOW"
    assert direction_from_question("Will Alice win?") == "UNKNOWN"

    markets = parse_gamma(fake_gamma())
    selected = select_btc_markets(markets, NOW, max_days=7)
    assert len(selected) == 2
    assert all(m.comparator in ("ABOVE", "BELOW") for m in selected)


def test_to_events_flags_missing_book() -> None:
    selected = select_btc_markets(parse_gamma(fake_gamma()), NOW, max_days=7)
    events = to_events(selected, {}, NOW)
    assert events and all(e.data_quality == "UNKNOWN" for e in events)
    assert any("BOOK_UNAVAILABLE" in r for e in events for r in e.reasons)


def test_to_events_maps_book_and_direction() -> None:
    selected = select_btc_markets(parse_gamma(fake_gamma()), NOW, max_days=7)
    books = {m.yes_token: {"yes_bid": "0.615", "yes_ask": "0.625", "liquidity_usd": "50000"}
             for m in selected}
    events = to_events(selected, books, NOW)
    assert all(e.data_quality == "OK" for e in events)
    assert all(e.yes_mid == D("0.62") for e in events)
    assert {e.comparator for e in events} == {"ABOVE", "BELOW"}


def test_pipeline_collects_offline() -> None:
    cfg = IntelPipelineConfig(
        feeds=(("https://feed", "test"),),
        fetch=lambda url: fake_rss([("BTC ETF inflow", "https://x",
                                     "Fri, 21 Nov 2026 12:00:00 GMT", "inflow")]),
        gamma_fetch=lambda url: fake_gamma(),
        book_fetch=lambda url: fake_book(),
        with_books=True,
    )
    digest = collect(NOW, cfg)
    assert digest.news and digest.markets
    assert any("LLM_UNAVAILABLE" in w for w in digest.warnings)
    assert any("WALLET_SOURCE_UNAVAILABLE" in w for w in digest.warnings)


def test_pipeline_survives_bad_gamma() -> None:
    cfg = IntelPipelineConfig(
        feeds=(),
        gamma_fetch=lambda url: (_ for _ in ()).throw(RuntimeError("gamma down")),
        fetch=None, with_books=False,
    )
    digest = collect(NOW, cfg)
    assert any("GAMMA_FAILED" in w for w in digest.warnings)


def test_features_and_ablation() -> None:
    dq = DataQuality(timeframes_seen=("1h",))
    state = MarketState("BTC/USDT", NOW, "TREND_UP", "NORMAL_VOL", "MOMENTUM_UP",
                        "RISK_NORMAL", D("0.6"), "t", data_quality=dq,
                        features={"adx": D("30"), "rsi": D("60"), "atr_pct": D("0.01"),
                                  "volume_z": D("2")})
    feats = compute_features(state, IntelDigest(NOW))
    assert -1 <= feats.technical["bias"] <= 1
    assert feats.news["bias"] == 0.0

    points = [{"technical": 0.8, "news": 0.5, "prediction": 0, "wallet": 0,
               "microstructure": 0}] * 40
    labels = [10.0] * 40
    out = evaluate_ablation(points, labels,
                            {"T": {"technical": 1.0}, "T+N": {"technical": 0.7, "news": 0.3}})
    assert out["baseline"] == "T"
    assert out["variants"]["T"]["n"] == 40
    assert out["variants"]["T+N"]["verdict"] in ("SUPPORTED", "FALSIFIED", "INCONCLUSIVE")