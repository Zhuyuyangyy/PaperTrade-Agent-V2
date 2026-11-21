"""数据质量硬门（Sprint 2 前置）：逐周期陈旧、真实收盘时间、缺口、特征契约。

这些问题在 Sprint 1 的实现里存在：
  - 整体陈旧度用"所有周期里最新的一根"计算 → 5m 新鲜会掩盖 1h 陈旧，
    而趋势判断恰恰依赖 1h；
  - ``last_closed_ms`` 实际是 ``open_ms``，不是收盘时刻；
  - ``ensure_contiguous`` 存在但 ``analyze`` 只记录缺口，没有把"不可用"传给风险层；
  - ``MarketState`` 没有区间上下沿/突破参考价，策略层只能自己重算。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from research.market.contracts import Candle, TF_1H_MS, TF_5M_MS
from research.market.state import analyze

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


def _ramp(n: int, start: float, step: float, tf: int, base: int = BASE) -> list[Candle]:
    out, price = [], start
    for i in range(n):
        o = price
        price += step
        c = price
        out.append(Candle(base + i * tf, D(f"{o:.6f}"), D(f"{max(o, c) + 0.5:.6f}"),
                          D(f"{min(o, c) - 0.5:.6f}"), D(f"{c:.6f}"), D("1"), tf))
    return out


def test_stale_1h_is_not_masked_by_fresh_5m() -> None:
    hourly = _ramp(250, 100, 0.2, TF_1H_MS)                 # 最后一根 close 在 BASE+250h
    as_of = hourly[-1].close_ms + 4 * TF_1H_MS              # 1h 停了 4 小时
    fresh_5m = _ramp(12, 100, 0.01, TF_5M_MS, base=as_of - 60 * 60_000)
    state = analyze("BTC/USDT", {"5m": fresh_5m, "1h": hourly}, as_of)
    assert state.data_quality.critical_stale is True, "1h 陈旧必须被发现，不能被 5m 新鲜掩盖"
    assert state.risk == "RISK_EXTREME"
    assert state.confidence <= D("0.40")
    assert state.data_quality.per_timeframe["1h"]["stale"] is True
    assert state.data_quality.per_timeframe["5m"]["stale"] is False


def test_per_timeframe_age_uses_close_time() -> None:
    hourly = _ramp(250, 100, 0.2, TF_1H_MS)
    as_of = hourly[-1].close_ms + 30 * 60_000               # 收盘后 30 分钟
    state = analyze("BTC/USDT", {"1h": hourly}, as_of)
    last = state.timeframes["1h"]
    assert last.last_closed_ms == hourly[-1].close_ms, "last_closed_ms 必须是收盘时刻"
    assert last.age_ms == as_of - hourly[-1].close_ms
    assert state.data_quality.per_timeframe["1h"]["age_ms"] == last.age_ms
    assert last.stale is False


def test_critical_stale_does_not_freeze_a_healthy_5m_only_book() -> None:
    """非 1h 周期陈旧不应判成趋势数据陈旧（避免误伤）。"""
    hourly = _ramp(250, 100, 0.2, TF_1H_MS)
    as_of = hourly[-1].close_ms + 30 * 60_000
    aligned = (BASE // (TF_1H_MS * 4)) * (TF_1H_MS * 4)
    stale_4h = _ramp(70, 100, 0.5, TF_1H_MS * 4, base=aligned - 20 * TF_1H_MS * 4)
    state = analyze("BTC/USDT", {"1h": hourly, "4h": stale_4h}, as_of)
    assert state.data_quality.per_timeframe["4h"]["stale"] is True
    assert state.data_quality.critical_stale is False
    assert state.risk != "RISK_EXTREME"


def test_gaps_make_book_non_contiguous_without_raising() -> None:
    candles = _ramp(250, 100, 0.2, TF_1H_MS)
    del candles[100]
    state = analyze("BTC/USDT", {"1h": candles}, state_as_of(candles))
    assert state.data_quality.gaps
    assert state.data_quality.contiguous is False
    assert state.risk in ("RISK_HIGH", "RISK_EXTREME")
    assert any("缺口" in i for i in state.invalidations) or state.data_quality.gaps


def test_features_expose_range_and_breakout_levels() -> None:
    hourly = _ramp(250, 100, 0.2, TF_1H_MS)
    state = analyze("BTC/USDT", {"1h": hourly}, state_as_of(hourly))
    f = state.features
    for key in ("range_high", "range_low", "breakout_ref_high", "breakout_ref_low",
                "atr_abs", "ema20", "ema60", "distance_to_range_high_bps"):
        assert key in f, f"缺少策略所需特征: {key}"
    recent = hourly[-20:]
    assert f["range_high"] == max(c.high for c in recent)
    assert f["range_low"] == min(c.low for c in recent)
    prior = hourly[-21:-1]
    assert f["breakout_ref_high"] == max(c.close for c in prior)
    assert f["breakout_ref_low"] == min(c.close for c in prior)


def test_features_are_json_safe() -> None:
    hourly = _ramp(250, 100, 0.2, TF_1H_MS)
    payload = analyze("BTC/USDT", {"1h": hourly}, state_as_of(hourly)).to_dict()
    assert "features" in payload
    assert all(v is None or isinstance(v, str) for v in payload["features"].values())


def state_as_of(candles: list[Candle]) -> int:
    """决策时刻 = 最后一根收盘后 31 秒（超过默认 30s 收盘宽限）。"""
    return max(c.close_ms for c in candles) + 31_000