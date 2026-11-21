"""双账本测试（Sprint 4.2）：实验盘与验证盘完全隔离。

规则：
  - shadow 意图只能进实验盘（shadow book），永远不能写验证盘；
  - 非 shadow（VERIFIED）意图只能进验证盘；
  - 两本账的现金、持仓、手续费、资金费互不影响；
  - 实验盘的交易不改变策略的 VERIFIED 权限。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from research.paper.broker import PaperBroker
from research.runtime.metrics import max_drawdown, summarize
from research.strategy.contracts import TradeIntent

D = Decimal
BASE = 1_795_000_000_000


def _intent(shadow: bool, iid: str = "i1", side: str = "LONG") -> TradeIntent:
    return TradeIntent(
        intent_id=iid, decision_ts_ms=BASE, symbol="BTC/USDT", side=side,
        strategy_id="trend_follow", strategy_version="v1",
        entry_ref_price=D("100"), stop_price=D("95") if side == "LONG" else D("105"),
        take_profit_price=D("110") if side == "LONG" else D("90"),
        expected_holding_ms=3_600_000, confidence=D("0.7"), reason="test",
        shadow=shadow,
    )


def test_shadow_intent_is_rejected_by_verified_book(tmp_path: Path) -> None:
    verified = PaperBroker(tmp_path / "verified.sqlite", starting_cash=D("1000"))
    assert verified.submit(_intent(shadow=True), D("100")) is None
    assert verified.open_positions() == []


def test_books_are_isolated(tmp_path: Path) -> None:
    shadow = PaperBroker(tmp_path / "shadow.sqlite", starting_cash=D("1000"), shadow_mode="shadow_only")
    verified = PaperBroker(tmp_path / "verified.sqlite", starting_cash=D("1000"))
    shadow.submit(_intent(shadow=True), D("100"))
    assert shadow.cash() != verified.cash()
    assert len(shadow.open_positions()) == 1
    assert verified.open_positions() == []


def test_funding_is_charged_to_long(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         fee_bps=D("0"), slippage_bps=D("0"),
                         funding_bps_per_8h=D("10"), shadow_mode="shadow_only")     # 10bps / 8h
    broker.submit(_intent(shadow=True), D("100"))
    closed = broker.expire(BASE + 8 * 3_600_000, {"BTC/USDT": D("100")})
    assert len(closed) == 1
    # 8 小时多头资金费 = 100 * 10bps = 0.1 USDT
    assert closed[0].pnl == pytest.approx(D("-0.1"), abs=D("0.01"))


def test_funding_is_received_by_short(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         fee_bps=D("0"), slippage_bps=D("0"),
                         funding_bps_per_8h=D("10"), shadow_mode="shadow_only")
    broker.submit(_intent(shadow=True, side="SHORT"), D("100"))
    closed = broker.expire(BASE + 8 * 3_600_000, {"BTC/USDT": D("100")})
    assert len(closed) == 1 and closed[0].pnl > 0


def test_max_drawdown() -> None:
    assert max_drawdown([D("1000"), D("1100"), D("900"), D("1000")]) == pytest.approx(
        D("0.1818"), abs=D("0.001"))
    assert max_drawdown([]) == D("0")
    assert max_drawdown([D("1000")]) == D("0")


def test_summarize_counts_and_realized(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         fee_bps=D("0"), slippage_bps=D("0"), shadow_mode="shadow_only")
    broker.submit(_intent(shadow=True), D("100"))
    broker.mark_bar("BTC/USDT", D("120"), D("120"), D("120"), BASE + 1000)
    summary = summarize(broker, label="shadow")
    assert summary["label"] == "shadow"
    assert summary["orders"] == 1
    assert summary["closed"] == 1
    assert summary["win_rate"] == "1.000"
    assert D(summary["realized_pnl"]) > 0