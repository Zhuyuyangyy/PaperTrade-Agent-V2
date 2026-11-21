"""Sprint 2.1：验证权限与选择顺序。

- VERIFIED 必须带验证来源（verified_by），普通运行参数不能凭一个字符串提升；
- 选择时 VERIFIED 优先于影子策略（即使影子策略的 priority 数字更小）。
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from research.market.contracts import DataQuality, MarketState
from research.strategy.contracts import StrategySpec
from research.strategy.registry import DEFAULT_SPECS, select

D = Decimal
BASE = 1_795_000_000_000


def _features(**over) -> dict:
    base = {
        "close": D("99.1"), "atr_abs": D("1"), "atr_pct": D("0.01"),
        "ema20": D("99.2"), "ema60": D("99.3"), "ema200": D("99.4"),
        "range_high": D("103"), "range_low": D("99"), "range_mid": D("101"),
        "breakout_ref_high": D("102"), "breakout_ref_low": D("99.5"),
        "volume_z": D("2.0"),
    }
    base.update(over)
    return base


def _state() -> MarketState:
    dq = DataQuality(timeframes_seen=("1h",), critical_stale=False, contiguous=True)
    return MarketState(symbol="BTC/USDT", as_of_ms=BASE, trend="RANGE", volatility="NORMAL_VOL",
                       momentum="FLAT", risk="RISK_NORMAL", confidence=D("0.6"), reason="test",
                       data_quality=dq, features=_features())


def test_verified_requires_a_source() -> None:
    with pytest.raises(ValueError, match="verified_by"):
        StrategySpec("x", "v1", "x", ("RANGE",), (), ("LONG",), verification="VERIFIED")


def test_verified_with_source_is_allowed() -> None:
    spec = StrategySpec("x", "v1", "x", ("RANGE",), (), ("LONG",), verification="VERIFIED",
                        verified_by="report-2026-11-21")
    assert spec.verification == "VERIFIED"


def test_selection_prefers_verified_over_higher_priority_shadow() -> None:
    """breakout(priority 20, UNTESTED) 与 range_revert(priority 30, VERIFIED) 同时出手时，
    必须执行已验证策略，而不是被影子策略抢占。"""
    specs = tuple(
        replace(s, verification="VERIFIED", verified_by="test-report")
        if s.strategy_id == "range_revert" else s
        for s in DEFAULT_SPECS
    )
    state = _state()
    sel = select(state, specs)
    assert sel.decision == "LONG", f"应执行 range_revert 的 LONG，实得 {sel.decision}"
    assert sel.intent is not None and sel.intent.strategy_id == "range_revert"
    assert sel.intent.shadow is False
    # 影子候选仍然被记录（可复盘），只是不抢占
    eligible = [c.strategy_id for c in sel.candidates if c.eligible]
    assert "breakout_confirm" in eligible


def test_shadow_still_selected_when_nothing_verified() -> None:
    sel = select(_state())
    assert sel.intent is not None and sel.intent.shadow is True