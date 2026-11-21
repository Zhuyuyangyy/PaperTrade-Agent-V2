"""策略与注册表测试（Sprint 2）：适用状态、方向、弃权原因、影子/排除。"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from research.market.contracts import DataQuality, MarketState
from research.strategy.contracts import StrategySpec
from research.strategy.registry import DEFAULT_SPECS, select
from research.strategy.strategies import breakout_confirm, range_revert, trend_follow

D = Decimal
BASE = 1_795_000_000_000


def _features(**over) -> dict:
    base = {
        "close": D("100"),
        "atr_abs": D("1"),
        "atr_pct": D("0.01"),
        "ema20": D("101"),
        "ema60": D("100.5"),
        "ema200": D("100"),
        "range_high": D("101"),
        "range_low": D("99"),
        "range_mid": D("100"),
        "breakout_ref_high": D("100.5"),
        "breakout_ref_low": D("99.5"),
        "volume_z": D("1.5"),
    }
    base.update(over)
    return base


def _state(trend="RANGE", vol="NORMAL_VOL", momentum="FLAT", risk="RISK_NORMAL",
           features=None, stale=False, contiguous=True) -> MarketState:
    dq = DataQuality(timeframes_seen=("1h",), critical_stale=stale, contiguous=contiguous)
    return MarketState(
        symbol="BTC/USDT", as_of_ms=BASE, trend=trend, volatility=vol, momentum=momentum,
        risk=risk, confidence=D("0.6"), reason="test", data_quality=dq,
        features=features or _features(),
    )


def test_trend_follow_sides_and_levels() -> None:
    sig, code = trend_follow(_state(trend="TREND_UP", momentum="MOMENTUM_UP"))
    assert code == "TREND_FOLLOW_LONG" and sig is not None and sig.side == "LONG"
    assert sig.stop_price < D("100") < sig.take_profit_price

    sig, code = trend_follow(_state(trend="TREND_DOWN", momentum="MOMENTUM_DOWN",
                                    features=_features(ema20=D("99"), ema60=D("99.5"),
                                                       ema200=D("100"))))
    assert code == "TREND_FOLLOW_SHORT" and sig is not None and sig.side == "SHORT"
    assert sig.take_profit_price < D("100") < sig.stop_price


def test_trend_follow_abstains_on_momentum_conflict_and_bad_state() -> None:
    assert trend_follow(_state(trend="TREND_UP", momentum="MOMENTUM_DOWN"))[1] == "MOMENTUM_CONFLICT"
    assert trend_follow(_state(trend="RANGE"))[1] == "TREND_NOT_APPLICABLE"
    assert trend_follow(_state(trend="TREND_UP", stale=True))[1] == "CRITICAL_STALE"
    assert trend_follow(_state(trend="TREND_UP", contiguous=False))[1] == "NON_CONTIGUOUS"


def test_range_revert_edges() -> None:
    low_sig, code = range_revert(_state(features=_features(close=D("99.1"))))
    assert code == "RANGE_REVERT_LONG" and low_sig is not None
    assert low_sig.stop_price < D("99.1") < low_sig.take_profit_price

    high_sig, code = range_revert(_state(features=_features(close=D("100.9"))))
    assert code == "RANGE_REVERT_SHORT" and high_sig is not None

    assert range_revert(_state(features=_features(close=D("100"))))[1] == "NOT_AT_RANGE_EDGE"


def test_breakout_needs_price_and_volume() -> None:
    sig, code = breakout_confirm(_state(trend="RANGE", features=_features(close=D("101.2"))))
    assert code == "BREAKOUT_LONG" and sig is not None

    assert breakout_confirm(_state(
        trend="RANGE", features=_features(close=D("101.2"), volume_z=D("0.3"))
    ))[1] == "BREAKOUT_WITHOUT_VOLUME"
    assert breakout_confirm(_state(trend="RANGE", features=_features(close=D("100.2"))))[1] == "NO_BREAKOUT"


def test_registry_prefers_priority_and_records_all_candidates() -> None:
    # 区间放宽到 99..103，close=101.2 处于中间（range_revert 弃权），
    # 但已突破前高 100.5 → 只有 breakout 出手
    state = _state(trend="RANGE", features=_features(close=D("101.2"), range_high=D("103")))
    sel = select(state)
    assert sel.decision == "LONG"
    assert sel.intent is not None and sel.intent.strategy_id == "breakout_confirm"
    assert sel.intent.shadow is True
    assert len(sel.candidates) == 3
    assert any(c.strategy_id == "range_revert" and not c.eligible for c in sel.candidates)


def test_falsified_strategy_is_excluded() -> None:
    specs = tuple(replace(s, verification="FALSIFIED") if s.strategy_id == "breakout_confirm"
                  else s for s in DEFAULT_SPECS)
    sel = select(_state(trend="RANGE", features=_features(close=D("101.2"), range_high=D("103"))), specs)
    assert sel.decision == "HOLD"
    assert any(c.reason == "STRATEGY_FALSIFIED" for c in sel.candidates)


def test_verified_strategy_is_not_shadow() -> None:
    specs = tuple(replace(s, verification="VERIFIED", verified_by="test-report")
                  if s.strategy_id == "breakout_confirm" else s for s in DEFAULT_SPECS)
    sel = select(_state(trend="RANGE", features=_features(close=D("101.2"), range_high=D("103"))), specs)
    assert sel.intent is not None and sel.intent.shadow is False


def test_hold_reason_prefers_data_problem() -> None:
    sel = select(_state(trend="TREND_UP", stale=True))
    assert sel.decision == "HOLD"
    assert "陈旧" in sel.hold_reason
    sel2 = select(_state(trend="UNKNOWN"))
    assert "HOLD" in sel2.hold_reason


def test_custom_spec_state_filter() -> None:
    spec = StrategySpec("trend_follow", "v1", "趋势跟随", ("TREND_UP",), ("HIGH_VOL",),
                        ("LONG",))
    assert select(_state(trend="TREND_UP", vol="NORMAL_VOL"), (spec,)).decision == "HOLD"
    assert select(_state(trend="TREND_UP", vol="HIGH_VOL"), (spec,)).decision == "LONG"