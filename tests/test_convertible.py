"""The convertible audit's load-bearing claims:
1. a signal only acts on the NEXT day's close -- same-bar action is look-ahead
2. the control is random days on the SAME bonds, not a different universe
3. a signal whose horizon runs past the data is not a trade, and is not counted
4. cost is deducted per trade, so "excess over control" is net of the toll
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# akshare is a heavy, network-oriented dependency used only by the A-share
# data fetcher. The audit logic under test is pure and offline; when the
# fetcher's dependency is absent the tests skip rather than error the whole
# CI collection (which is exactly what happened before this guard).
pytest.importorskip("akshare", reason="akshare 未安装：跳过可转债审计测试（CI 最小依赖环境）")

from research.astock.discount import (  # noqa: E402
    COMMISSION,
    THRESHOLDS,
    BondResult,
    _forward_returns,
    audit_bond,
    summarize,
)
from research.astock.data import BondData  # noqa: E402


class _FakeData(BondData):
    """A BondData that serves a fixed premium series, no network."""

    def __init__(self, series: list[dict]):
        self._series = series

    def premium_series(self, bond_code, stock_code, conv_price):
        return self._series


def _series(prices: list[float], premium: float = 0.0) -> list[dict]:
    return [{"date": f"2024-01-{i+1:02d}", "bond": p, "premium": premium}
            for i, p in enumerate(prices)]


def test_a_signal_acts_on_the_next_day_not_the_signal_day():
    """Same rule as the Binance audit: the close that produced the signal is
    not a price you could have traded at."""
    series = _series([100.0, 101.0, 102.0, 103.0, 104.0])
    # entry is series[i+1] (the next day's close), exit is series[i+1+horizon]
    assert _forward_returns(series, 0, 1) == 102.0 / 101.0 - 1
    assert _forward_returns(series, 0, 2) == 103.0 / 101.0 - 1


def test_a_horizon_that_runs_past_the_data_is_not_a_trade():
    series = _series([100.0, 101.0, 102.0])
    assert _forward_returns(series, 1, 1) is None, "the window ran out; not a trade"
    assert _forward_returns(series, 2, 1) is None


def test_the_control_is_random_days_on_the_same_bonds():
    """A control drawn from another universe would be comparing two things."""
    # 40 days, every one in a -5% discount, alternating ±1%
    series = _series([100.0 + (1.0 if i % 2 else -1.0) for i in range(40)],
                     premium=-0.05)
    # (a signal day near the end has no full horizon left; the control pool
    #  excludes those, which is why the counts are equal by construction)
    data = _FakeData(series)
    row = {"bond": "123456", "bond_name": "测试转债", "stock": "600000",
           "stock_name": "测试正股", "conv_price": 10.0}
    result = audit_bond(data, row)
    assert result is not None
    assert result.days == 40
    # control uses the same horizon keys as the signals
    assert set(result.control) == {1, 3, 5}
    # the control draws the same NUMBER of entries as there are signal days;
    # a signal whose horizon runs past the data is filtered out of the trade
    # list, so the trade counts can be slightly lower -- the mean comparison
    # stays unbiased either way because both draw from the same distribution
    assert result.signals == 40
    for horizon in (1, 3, 5):
        assert len(result.control[horizon]) == result.signals
        assert len(result.trades[(THRESHOLDS[0], horizon)]) <= result.signals


def test_cost_is_deducted_per_trade():
    """'Excess over control' must be net of the toll, or it is a gross lie."""
    series = _series([100.0 + (0.5 if i % 2 else -0.5) for i in range(40)],
                     premium=-0.02)
    data = _FakeData(series)
    row = {"bond": "123456", "bond_name": "x", "stock": "600000",
           "stock_name": "y", "conv_price": 10.0}
    result = audit_bond(data, row)
    trades = result.trades[(THRESHOLDS[0], 1)]
    assert trades, "every day is a signal at -2%"
    # gross would be next_day/entry_day; net must be lower by the round-trip toll
    gross = 99.5 / 100.5 - 1
    assert trades[0] == pytest.approx(gross - 2 * COMMISSION)


def test_thresholds_are_nested_not_independent():
    """A deeper discount must select a subset of the shallower one's days."""
    prices = [100.0 + i for i in range(40)]
    series = [{"date": f"d{i}", "bond": p,
               "premium": -0.06 if i < 10 else (-0.02 if i < 25 else 0.0)}
              for i, p in enumerate(prices)]
    data = _FakeData(series)
    row = {"bond": "1", "bond_name": "x", "stock": "600000",
           "stock_name": "y", "conv_price": 10.0}
    result = audit_bond(data, row)
    wide = len(result.trades[(THRESHOLDS[0], 1)])
    deep = len(result.trades[(THRESHOLDS[-1], 1)])
    assert wide == 25, "days at -2% or deeper"
    assert deep == 10, "days at -5% or deeper"
    assert deep <= wide


def test_a_book_with_too_little_history_is_not_audited():
    """Thirty days decides nothing, whatever it says."""
    data = _FakeData(_series([100.0] * 10, premium=-0.05))
    row = {"bond": "1", "bond_name": "x", "stock": "600000",
           "stock_name": "y", "conv_price": 10.0}
    assert audit_bond(data, row) is None


def test_summary_reports_what_it_measured():
    result = BondResult(bond="1", name="x", days=100, signals=25)
    result.trades[(-0.01, 1)] = [0.001, -0.002, 0.003]
    result.trades[(-0.01, 3)] = []
    result.trades[(-0.01, 5)] = []
    result.control[1] = [0.0, 0.0, 0.0]
    text = summarize({"results": [result], "failed": [], "universe": 10})
    assert "覆盖 1/10" in text
    assert "信号" in text and "超额" in text


import pytest  # noqa: E402
