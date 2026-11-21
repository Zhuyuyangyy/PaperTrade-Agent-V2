"""Polymarket 情报测试（Sprint 3）：先筛"配研究"，再谈概率。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from research.cross_market.market_mapping import MarketMapping
from research.intel.polymarket import assess_market

D = Decimal
NOW = 1_795_000_000_000


def _iso(hours: float) -> str:
    dt = datetime.fromtimestamp(NOW / 1000, tz=timezone.utc) + timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _m(**over) -> MarketMapping:
    base = MarketMapping(
        market_id="m", underlying_asset="BTC", event_threshold=100000.0,
        event_comparator="ABOVE", event_expiry=_iso(6), resolution_source="uma",
        target_horizons=("5m",), primary_horizon="5m",
    )
    return replace(base, **over) if over else base


def _book(**over) -> dict:
    b = {"yes_bid": "0.605", "yes_ask": "0.615", "prev_yes_mid": "0.58",
         "liquidity_usd": "50000"}
    b.update(over)
    return b


def test_short_horizon_liquid_market_is_researchable() -> None:
    ev = assess_market(_m(), _book(), NOW)
    assert ev.researchable is True and ev.horizon_match is True
    assert ev.data_quality == "OK"
    assert ev.yes_mid == D("0.61")
    assert ev.delta == D("0.61") - D("0.58")
    assert ev.spread_bps is not None and D("150") < ev.spread_bps < D("180")
    assert ev.advisory_only is True


def test_long_dated_market_fails_horizon_match() -> None:
    ev = assess_market(_m(event_expiry=_iso(24 * 200)), _book(), NOW)
    assert ev.researchable is False and ev.horizon_match is False
    assert any("horizon_mismatch" in r for r in ev.reasons)


def test_wide_spread_blocks() -> None:
    ev = assess_market(_m(), _book(yes_bid="0.40", yes_ask="0.70"), NOW)
    assert ev.data_quality == "WIDE_SPREAD" and ev.researchable is False


def test_low_liquidity_blocks() -> None:
    ev = assess_market(_m(), _book(liquidity_usd="100"), NOW)
    assert ev.data_quality == "LOW_LIQUIDITY" and ev.researchable is False


def test_missing_book_is_unknown() -> None:
    ev = assess_market(_m(), None, NOW)
    assert ev.data_quality == "UNKNOWN" and ev.researchable is False
    assert ev.yes_mid is None


def test_expired_market_is_not_researchable() -> None:
    ev = assess_market(_m(event_expiry=_iso(-1)), _book(), NOW)
    assert ev.researchable is False
    assert any("expired" in r for r in ev.reasons)