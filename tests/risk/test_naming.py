"""Sprint 2.1：ATR 代理不得冒充 EV（期望收益）。

0.5×ATR% 只说明"有波动空间"，不说明"策略有优势"。命名必须诚实，
否则下一步会把"波动够大"误读成"期望为正"。
"""
from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

from research.market.contracts import DataQuality, MarketState
from research.risk.engine import AccountSnapshot, RiskDecision, RiskLimits, evaluate
from research.strategy.contracts import TradeIntent

D = Decimal
BASE = 1_795_000_000_000


def _state(atr_pct=D("0.01")) -> MarketState:
    dq = DataQuality(timeframes_seen=("1h",), critical_stale=False, contiguous=True)
    return MarketState(symbol="BTC/USDT", as_of_ms=BASE, trend="TREND_UP",
                       volatility="NORMAL_VOL", momentum="MOMENTUM_UP", risk="RISK_NORMAL",
                       confidence=D("0.6"), reason="test", data_quality=dq,
                       features={"atr_pct": atr_pct})


def _intent() -> TradeIntent:
    return TradeIntent("i", BASE, "BTC/USDT", "LONG", "trend_follow", "v1",
                       D("100"), D("98"), D("104"), 3_600_000, D("0.7"), "test")


def _account() -> AccountSnapshot:
    return AccountSnapshot(cash=D("1000"), gross_exposure=D("0"), open_positions=0,
                           realized_pnl_today=D("0"))


def test_decision_exposes_volatility_budget_not_ev() -> None:
    d = evaluate(_intent(), _state(), _account(), RiskLimits())
    assert isinstance(d, RiskDecision)
    assert d.decision == "ALLOW"
    assert d.volatility_budget_bps == D("50"), "0.5 × 1% = 50bps 波动预算"
    assert "波动预算" in d.reason
    assert "EV" not in d.reason.upper(), "不得再把 ATR 代理称作 EV"


def test_limits_are_named_for_volatility_budget() -> None:
    names = {f.name for f in fields(RiskLimits)}
    assert "min_volatility_budget_bps" in names
    assert "min_ev_bps" not in names
    assert "volatility_budget_bps" in {f.name for f in fields(RiskDecision)}


def test_rejection_message_says_budget_not_edge() -> None:
    d = evaluate(_intent(), _state(atr_pct=D("0.001")), _account(), RiskLimits())
    assert d.decision == "DENY" and "COST_TOO_HIGH" in d.codes
    assert "波动预算" in d.reason
    assert "EV" not in d.reason.upper()