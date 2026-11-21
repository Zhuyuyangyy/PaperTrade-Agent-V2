"""指标口径测试（Sprint 1）：标准定义 + 与输入等长 + 前导 None。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from research.market.indicators import (
    adx,
    atr,
    bollinger,
    ema,
    macd,
    percentile_rank,
    rsi,
    slope_bps,
    sma,
    volume_z,
)

D = Decimal


def test_sma_ema_known_values() -> None:
    values = [D(i) for i in range(1, 6)]
    assert sma(values, 3) == [None, None, D(2), D(3), D(4)]
    # EMA(3): seed=2, k=0.5 → 3, 4
    assert ema(values, 3) == [None, None, D(2), D(3), D(4)]


def test_ema_reacts_faster_than_long_sma_after_jump() -> None:
    # 5 根 20 的权重在 EMA(5) 里比在 SMA(10) 里更高
    values = [D(10)] * 10 + [D(20)] * 5
    e = ema(values, 5)[-1]
    s = sma(values, 10)[-1]
    assert e is not None and s is not None and e > s


def test_rsi_monotonic_up_is_100() -> None:
    values = [D(100 + i) for i in range(30)]
    series = rsi(values, 14)
    assert series[14] == D(100) and series[-1] == D(100)


def test_rsi_monotonic_down_is_0() -> None:
    values = [D(200 - i) for i in range(30)]
    assert rsi(values, 14)[-1] == D(0)


def test_atr_constant_range() -> None:
    highs = [D(101)] * 30
    lows = [D(99)] * 30
    closes = [D(100)] * 30
    a = atr(highs, lows, closes, 14)
    assert a[14] == D(2) and a[-1] == D(2)


def test_adx_high_in_clean_trend_low_in_chop() -> None:
    up = [D(100 + i) for i in range(80)]
    highs = [v + D(1) for v in up]
    lows = [v - D(1) for v in up]
    series, plus_di, minus_di = adx(highs, lows, up, 14)
    assert series[-1] is not None and series[-1] > D(50)
    assert plus_di[-1] is not None and minus_di[-1] is not None
    assert plus_di[-1] > minus_di[-1]

    flat = [D(100)] * 80
    fh = [v + D(0.5) for v in flat]
    fl = [v - D(0.5) for v in flat]
    chop, _, _ = adx(fh, fl, flat, 14)
    assert chop[-1] is not None and chop[-1] < D(20)


def test_macd_positive_in_uptrend() -> None:
    values = [D(100 + i) for i in range(80)]
    line, sig, hist = macd(values)
    assert line[-1] is not None and line[-1] > 0
    assert sig[-1] is not None and len(hist) == len(values)


def test_volume_z_spike() -> None:
    vols = [D(100)] * 30 + [D(500)]
    z = volume_z(vols, 20)
    assert z[-1] is not None and z[-1] > D(3)


def test_bollinger_constant_series_has_zero_width() -> None:
    values = [D(100)] * 30
    mid, upper, lower = bollinger(values, 20)
    assert mid[-1] == upper[-1] == lower[-1] == D(100)


def test_slope_bps() -> None:
    values = [D(100), D(101), D(102), D(103), D(110)]
    s = slope_bps(values, 5)
    assert s[-1] == D(1000)


def test_percentile_rank() -> None:
    hist = [D(1), D(2), D(3), D(4)]
    assert percentile_rank(hist, D(2)) == D("0.5")
    assert percentile_rank([], D(1)) is None


def test_series_length_always_matches_input() -> None:
    values = [D(100 + i) for i in range(10)]
    highs = [v + 1 for v in values]
    lows = [v - 1 for v in values]
    assert len(ema(values, 20)) == 10
    assert len(rsi(values, 14)) == 10
    assert len(atr(highs, lows, values, 14)) == 10
    assert len(macd(values)[2]) == 10


def test_bad_period_rejected() -> None:
    with pytest.raises(ValueError):
        ema([D(1), D(2)], 0)
    with pytest.raises(ValueError):
        macd([D(1)], fast=26, slow=12)