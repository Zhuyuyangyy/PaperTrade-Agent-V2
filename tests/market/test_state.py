"""状态机测试（Sprint 1）：三种 regime、UNKNOWN、冲突、数据质量、确定性。"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Iterable

import pytest

from research.market.contracts import Candle, TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.market.demo import demo_as_of_ms, synthetic_1m
from research.market.klines import resample
from research.market.state import analyze

D = Decimal
BASE = 1_795_000_000_000 // TF_4H_MS * TF_4H_MS


def _dec(x: float) -> Decimal:
    return Decimal(f"{x:.6f}")


def _ramp(n: int, start: float, step: float, tf: int, base: int = BASE) -> list[Candle]:
    out = []
    price = start
    for i in range(n):
        o = price
        price += step
        c = price
        out.append(Candle(base + i * tf, _dec(o), _dec(max(o, c) + 0.5),
                          _dec(min(o, c) - 0.5), _dec(c), D("1"), tf))
    return out


def _views(base_1m: Iterable[Candle]) -> dict[str, list[Candle]]:
    base = list(base_1m)
    return {name: resample(base, ms)[0] for name, ms in
            (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS))}


def _as_of(views: dict[str, list[Candle]]) -> int:
    return max(c.close_ms for cs in views.values() for c in cs) + 1_000


def test_demo_regimes_are_distinguishable_and_explained() -> None:
    as_of = demo_as_of_ms()
    up = analyze("BTC/USDT", _views(synthetic_1m("trend_up")), as_of)
    rg = analyze("BTC/USDT", _views(synthetic_1m("range")), as_of)
    hv = analyze("BTC/USDT", _views(synthetic_1m("high_vol")), as_of)

    assert up.trend == "TREND_UP"
    assert up.momentum == "MOMENTUM_UP"
    assert rg.trend == "RANGE"
    assert hv.volatility == "HIGH_VOL"
    assert hv.risk in ("RISK_HIGH", "RISK_EXTREME")

    for s in (up, rg, hv):
        assert s.evidence, "每个判断必须有证据"
        assert s.invalidations, "每个判断必须写明失效条件"
        assert s.data_quality is not None
        assert s.data_quality.gaps == ()
        assert len(s.timeframes) == 4
        assert D("0.05") <= s.confidence <= D("0.95")


def test_insufficient_data_is_unknown_not_guessed() -> None:
    views = {"1h": _ramp(50, 100, 0.2, TF_1H_MS)}
    state = analyze("BTC/USDT", views, _as_of(views))
    assert state.trend == "UNKNOWN"
    assert "不足" in state.reason
    assert state.confidence <= D("0.20")
    assert state.invalidations and "补齐" in state.invalidations[0]


def test_missing_1h_is_unknown() -> None:
    views = {"5m": _ramp(100, 100, 0.01, TF_5M_MS)}
    state = analyze("BTC/USDT", views, _as_of(views))
    assert state.trend == "UNKNOWN" and state.volatility == "UNKNOWN"
    assert state.risk == "RISK_HIGH"


def test_1h_up_vs_4h_down_is_a_recorded_contradiction() -> None:
    # 构造数据（1h 上行、4h 下行）只用于触发一致性校验，不代表真实市场
    views = {
        "1h": _ramp(300, 100, 0.5, TF_1H_MS),
        "4h": _ramp(70, 250, -1.0, TF_4H_MS, base=BASE + 300 * TF_1H_MS),
    }
    state = analyze("BTC/USDT", views, _as_of(views))
    codes = {c.code for c in state.contradictions}
    assert "1h_up_vs_4h_down" in codes
    assert state.confidence < D("0.85")
    assert any("冲突" in state.reason for _ in [0])


def test_gap_lowers_quality_and_raises_risk() -> None:
    candles = _ramp(250, 100, 0.2, TF_1H_MS)
    del candles[100]
    views = {"1h": candles}
    state = analyze("BTC/USDT", views, _as_of(views))
    assert state.data_quality.gaps
    assert state.data_quality.complete is False
    assert state.risk in ("RISK_HIGH", "RISK_EXTREME")


def test_stale_feed_is_extreme_risk() -> None:
    candles = _ramp(250, 100, 0.2, TF_1H_MS)
    as_of = candles[-1].close_ms + 3 * TF_1H_MS
    state = analyze("BTC/USDT", {"1h": candles}, as_of)
    assert state.data_quality.stale is True
    assert state.risk == "RISK_EXTREME"
    assert state.confidence <= D("0.40")


def test_unclosed_candles_are_ignored() -> None:
    candles = _ramp(210, 100, 0.2, TF_1H_MS)
    last = candles[-1]
    as_of = last.open_ms + 60_000          # 最后一根还没收盘
    state = analyze("BTC/USDT", {"1h": candles}, as_of)
    assert state.timeframes["1h"].last_closed_ms == candles[-2].open_ms
    assert state.timeframes["1h"].bars == 209


def test_analysis_is_deterministic() -> None:
    views = _views(synthetic_1m("trend_up"))
    a = analyze("BTC/USDT", views, demo_as_of_ms()).to_dict()
    b = analyze("BTC/USDT", views, demo_as_of_ms()).to_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_state_is_json_serializable_with_decimal_strings() -> None:
    views = _views(synthetic_1m("trend_up"))
    payload = analyze("BTC/USDT", views, demo_as_of_ms()).to_dict()
    text = json.dumps(payload, ensure_ascii=False)
    assert '"trend": "TREND_UP"' in text
    assert isinstance(payload["confidence"], str)
    assert isinstance(payload["evidence"][0]["value"], (str, type(None)))


def test_invalid_state_rejected() -> None:
    views = _views(synthetic_1m("range"))
    state = analyze("BTC/USDT", views, demo_as_of_ms())
    with pytest.raises(ValueError):
        type(state)(symbol=state.symbol, as_of_ms=state.as_of_ms, trend="BULLISH",
                    volatility=state.volatility, momentum=state.momentum,
                    risk=state.risk, confidence=state.confidence, reason="")