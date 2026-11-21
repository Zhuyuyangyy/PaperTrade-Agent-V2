"""确定性风控测试（Sprint 2）：逐项触发、顺序、市场级门禁。"""
from __future__ import annotations

from decimal import Decimal

from research.market.contracts import DataQuality, MarketState
from research.risk.engine import AccountSnapshot, RiskLimits, evaluate, evaluate_market_gate
from research.strategy.contracts import TradeIntent

D = Decimal
BASE = 1_795_000_000_000


def _state(*, stale=False, contiguous=True, risk="RISK_NORMAL", atr_pct=D("0.01")) -> MarketState:
    dq = DataQuality(timeframes_seen=("1h",), critical_stale=stale, contiguous=contiguous)
    return MarketState(
        symbol="BTC/USDT", as_of_ms=BASE, trend="TREND_UP", volatility="NORMAL_VOL",
        momentum="MOMENTUM_UP", risk=risk, confidence=D("0.6"), reason="test",
        data_quality=dq, features={"atr_pct": atr_pct},
    )


def _intent() -> TradeIntent:
    return TradeIntent(
        intent_id="abc", decision_ts_ms=BASE, symbol="BTC/USDT", side="LONG",
        strategy_id="trend_follow", strategy_version="v1",
        entry_ref_price=D("100"), stop_price=D("98"), take_profit_price=D("104"),
        expected_holding_ms=3_600_000, confidence=D("0.7"), reason="test",
    )


def _account(**over) -> AccountSnapshot:
    base = dict(cash=D("1000"), gross_exposure=D("0"), open_positions=0,
                realized_pnl_today=D("0"))
    base.update(over)
    return AccountSnapshot(**base)


def test_allow_when_everything_is_fine() -> None:
    d = evaluate(_intent(), _state(), _account(), RiskLimits())
    assert d.decision == "ALLOW" and d.notional == D("100")
    assert all(d.checks.values())


def test_kill_switch_freezes() -> None:
    d = evaluate(_intent(), _state(), _account(), RiskLimits(kill_switch=True))
    assert d.decision == "FREEZE" and "KILL_SWITCH" in d.codes


def test_stale_freezes_and_gap_denies() -> None:
    d1 = evaluate(_intent(), _state(stale=True), _account(), RiskLimits())
    assert d1.decision == "FREEZE" and "DATA_STALE" in d1.codes
    d2 = evaluate(_intent(), _state(contiguous=False), _account(), RiskLimits())
    assert d2.decision == "DENY" and "DATA_GAPS" in d2.codes


def test_extreme_risk_denies() -> None:
    d = evaluate(_intent(), _state(risk="RISK_EXTREME"), _account(), RiskLimits())
    assert d.decision == "DENY" and "RISK_EXTREME" in d.codes


def test_spread_denies() -> None:
    d = evaluate(_intent(), _state(), _account(), RiskLimits(), spread_bps=D("30"))
    assert d.decision == "DENY" and "SPREAD_TOO_WIDE" in d.codes


def test_cost_gate_denies_when_atr_too_small() -> None:
    d = evaluate(_intent(), _state(atr_pct=D("0.001")), _account(), RiskLimits())
    assert d.decision == "DENY" and "COST_TOO_HIGH" in d.codes


def test_cost_gate_denies_when_atr_missing() -> None:
    d = evaluate(_intent(), _state(atr_pct=None), _account(), RiskLimits())
    assert d.decision == "DENY" and "COST_UNKNOWN" in d.codes


def test_position_and_exposure_limits() -> None:
    d1 = evaluate(_intent(), _state(), _account(open_positions=3), RiskLimits())
    assert d1.decision == "DENY" and "MAX_POSITIONS" in d1.codes
    d2 = evaluate(_intent(), _state(), _account(gross_exposure=D("300")), RiskLimits())
    assert d2.decision == "DENY" and "EXPOSURE_LIMIT" in d2.codes


def test_daily_loss_freezes() -> None:
    d = evaluate(_intent(), _state(), _account(realized_pnl_today=D("-25")), RiskLimits())
    assert d.decision == "FREEZE" and "DAILY_LOSS" in d.codes


def test_insufficient_cash_denies() -> None:
    d = evaluate(_intent(), _state(), _account(cash=D("50")), RiskLimits())
    assert d.decision == "DENY" and "INSUFFICIENT_CASH" in d.codes


def test_check_order_is_data_first() -> None:
    """数据门必须在其它检查之前短路：不能因为成本好看就放行陈旧数据。"""
    d = evaluate(_intent(), _state(stale=True, atr_pct=D("0.5")), _account(), RiskLimits())
    assert d.codes == ("DATA_STALE",)


def test_market_gate_without_intent() -> None:
    assert evaluate_market_gate(_state(), RiskLimits()) is None
    g = evaluate_market_gate(_state(stale=True), RiskLimits())
    assert g is not None and g.decision == "FREEZE" and "DATA_STALE" in g.codes
    g2 = evaluate_market_gate(_state(contiguous=False), RiskLimits())
    assert g2 is not None and g2.decision == "DENY" and "DATA_GAPS" in g2.codes