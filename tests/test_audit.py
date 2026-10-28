"""The audit's two load-bearing claims, tested directly.

1. A bar containing both the stop and the target must resolve to the stop
   under the default assumption. This is the whole difference between
   CascadeFader's published +132% and the +469% a naive horizon analysis
   reported for the same signals.
2. A signal fills on the NEXT bar's open. Acting inside the bar that produced
   the signal is look-ahead, and look-ahead is how every backtest lies.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.audit.pathsim import Exit, simulate  # noqa: E402
from research.audit.strategies import CascadeFader  # noqa: E402

HOUR = 3600 * 1000


def _candles(prices: list[tuple[float, float, float, float, float]]) -> list[dict]:
    """(open, high, low, close, volume) tuples, one hour apart."""
    return [
        {"t": 1_700_000_000_000 + i * HOUR, "o": o, "h": h, "l": l, "c": c, "v": v}
        for i, (o, h, l, c, v) in enumerate(prices)
    ]


def test_a_bar_with_both_stop_and_target_resolves_to_the_stop_by_default():
    """The Freqle lesson, as a test."""
    candles = _candles([
        (100, 101, 99, 100, 10),      # 0: warmup
        (100, 101, 99, 100, 10),      # 1: signal here
        (100, 112, 90, 105, 10),      # 2: contains BOTH +12% and -10%
    ])
    entries = {1: "long"}
    rule = Exit(take_profit=0.10, stop_loss=-0.08)

    worst = simulate(candles, entries, rule, worst_case=True)
    best = simulate(candles, entries, rule, worst_case=False)

    assert worst.closed == 1
    assert worst.fills[0].reason == "stop_loss", "a bar with both must stop out by default"
    # the stop fills AT the stop price (92), not at the bar's low (90)
    assert worst.fills[0].gross_pct == pytest.approx(-0.08, abs=0.001)
    assert best.fills[0].reason == "take_profit", "and the optimistic read is reported beside it"
    assert best.fills[0].gross_pct == pytest.approx(0.10, abs=0.001)


def test_the_two_assumptions_are_both_reported_never_one_alone():
    """A strategy that only survives the optimistic read must be visible as such."""
    candles = _candles([(100, 101, 99, 100, 10)] * 2 + [(100, 130, 85, 120, 10)])
    rule = Exit(take_profit=0.25, stop_loss=-0.12)
    worst = simulate(candles, {1: "long"}, rule, worst_case=True)
    best = simulate(candles, {1: "long"}, rule, worst_case=False)
    assert worst.fills[0].reason == "stop_loss"
    assert best.fills[0].reason == "take_profit"
    assert worst.fills[0].net_pct < 0 < best.fills[0].net_pct


def test_a_signal_fills_on_the_next_bars_open_not_its_own():
    """Look-ahead is how a backtest flatters its author."""
    candles = _candles([
        (100, 101, 99, 100, 10),      # 0
        (100, 101, 99, 100, 10),      # 1: signal bar, close 100
        (90, 92, 88, 91, 10),         # 2: next bar opens at 90
        (91, 93, 90, 92, 10),
    ])
    res = simulate(candles, {1: "long"}, Exit(timeout_bars=1), worst_case=True)
    assert res.closed == 1
    assert res.fills[0].entry_price == 90.0, "filled at the next open, not the signal close"
    assert res.fills[0].entry_i == 2


def test_a_signal_that_never_resolves_is_counted_not_dropped():
    """An unresolved signal is a fact about the data window."""
    candles = _candles([(100, 101, 99, 100, 10)] * 5)
    res = simulate(candles, {4: "long"}, Exit(timeout_bars=50), worst_case=True)
    assert res.closed == 0
    assert res.open_signals == 1, "the window ran out; that must be visible"


def test_funding_is_charged_at_each_8h_boundary_crossed():
    """Carry is a cash flow, not a rounding error."""
    candles = _candles([(100, 101, 99, 100, 10)] * 40)
    funding = [{"t": 1_700_000_000_000 + 8 * HOUR, "rate": 0.001}]
    res = simulate(candles, {1: "long"}, Exit(timeout_bars=30),
                   funding_series=funding, worst_case=True)
    assert res.closed == 1
    # held across one 8h boundary as a long: pay 0.1%
    assert res.fills[0].funding_pct == pytest.approx(-0.001, abs=0.0002)
    # and a short on the same boundary collects it
    res_short = simulate(candles, {1: "short"}, Exit(timeout_bars=30),
                         funding_series=funding, worst_case=True)
    assert res_short.fills[0].funding_pct == pytest.approx(0.001, abs=0.0002)


def test_cascade_fader_fires_on_a_deep_recovered_wick_with_volume():
    """The ported signal: >=8% wick, >40% recovered, >2x volume."""
    strat = CascadeFader()
    # 720 bars of flat quiet candles to establish the volume baseline,
    # then one deep wick that recovers on heavy volume.
    flat = [(100, 100.5, 99.5, 100, 10)] * 730
    cascade = [(100, 100.5, 91, 97, 40)]     # wick 9%, recovered 6/9 = 67%, volume 4x
    candles = _candles(flat + cascade + [(97, 98, 96, 97, 10)] * 3)
    signals = strat.signals(candles)
    assert 731 in signals, "the bar after the cascade bar should carry the entry"
    assert signals[731] == "long"


def test_cascade_fader_ignores_a_wick_that_never_recovers():
    """A falling knife is not a cascade."""
    strat = CascadeFader()
    flat = [(100, 100.5, 99.5, 100, 10)] * 730
    knife = [(100, 100.5, 88, 89, 40)]       # wick 12%, recovered 1/12 = 8%
    candles = _candles(flat + knife + [(89, 90, 88, 89, 10)] * 3)
    assert 731 not in strat.signals(candles)


def test_cascade_fader_ignores_a_deep_drop_on_normal_volume():
    """No panic signature, no cascade."""
    strat = CascadeFader()
    flat = [(100, 100.5, 99.5, 100, 10)] * 730
    quiet = [(100, 100.5, 91, 97, 12)]       # wick 9%, recovered, but only 1.2x volume
    candles = _candles(flat + quiet + [(97, 98, 96, 97, 10)] * 3)
    assert 731 not in strat.signals(candles)
