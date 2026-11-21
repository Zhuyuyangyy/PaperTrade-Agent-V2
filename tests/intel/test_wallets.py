"""聪明钱研究测试（Sprint 3）：可复制性与偏差，而不是"抄作业"。"""
from __future__ import annotations

from decimal import Decimal

from research.intel.wallets import WalletTrade, analyze, scorecard

D = Decimal
NOW = 1_795_000_000_000


def _t(**over) -> WalletTrade:
    base = dict(wallet="w1", token="SOL", side="BUY", tx_ts_ms=NOW - 1_000,
                price_usd=D("100"), size_usd=D("1000"),
                realized_pnl_usd=D("50"), unrealized_pnl_usd=D("0"))
    base.update(over)
    return WalletTrade(**base)


def test_copyable_when_fast_and_small() -> None:
    sig = analyze([_t()], NOW, liquidity_usd=D("100000"))[0]
    assert sig.copyable is True and sig.reasons == ()
    assert sig.delay_ms == 1_000
    assert sig.advisory_only is True


def test_delay_makes_it_not_copyable() -> None:
    sig = analyze([_t(tx_ts_ms=NOW - 60_000)], NOW)[0]
    assert sig.copyable is False
    assert any("DELAY_TOO_LONG" in r for r in sig.reasons)


def test_size_relative_to_liquidity() -> None:
    sig = analyze([_t(size_usd=D("10000"))], NOW, liquidity_usd=D("100000"))[0]
    assert sig.copyable is False
    assert any("SIZE_TOO_LARGE" in r for r in sig.reasons)


def test_unrealized_only_is_not_a_track_record() -> None:
    sig = analyze([_t(realized_pnl_usd=D("0"), unrealized_pnl_usd=D("500"))], NOW)[0]
    assert sig.copyable is False
    assert "UNREALIZED_ONLY" in sig.reasons


def test_scorecard_flags_survivorship() -> None:
    only_winners = [_t(wallet="a"), _t(wallet="b")]
    sc = scorecard(only_winners)
    assert any("SURVIVORSHIP" in n for n in sc["notes"])
    mixed = only_winners + [_t(wallet="c", realized_pnl_usd=D("-30"))]
    sc2 = scorecard(mixed)
    assert not any("SURVIVORSHIP" in n for n in sc2["notes"])
    assert sc2["realized_pnl_usd"] == "70"


def test_unrealized_never_enters_realized_total() -> None:
    sc = scorecard([_t(realized_pnl_usd=D("10"), unrealized_pnl_usd=D("999"))])
    assert sc["realized_pnl_usd"] == "10"
    assert sc["unrealized_pnl_usd"] == "999"