"""Buy-and-hold baseline: does the strategy beat simply holding the coin?

A dip-buy book in a rising market can show a positive net that is nothing but
the coin's own drift. These tests pin the baseline's arithmetic (one round
trip of fees, exposure from the worst-case fills, log per-bar drift, short
mirror), its per-segment scope under walk-forward, the renderers, and that it
never touches the verdict unless the spec opts in.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.verify import Spec, SpecError, verify  # noqa: E402
from research.verify import report  # noqa: E402
from research.verify.html_report import render_html  # noqa: E402

T0 = 1_700_000_000_000


def _bar(i: int, o: float, c: float, wick: float = 0.0005) -> dict:
    return {"t": T0 + i * 3_600_000, "o": o, "c": c,
            "h": max(o, c) * (1 + wick), "l": min(o, c) * (1 - wick), "v": 1000.0}


def _trend(n: int, drift: float, noise: float = 0.001, seed: int = 11) -> list[dict]:
    """Steady trend with a little noise; open = previous close (no gaps)."""
    rng = random.Random(seed)
    out, px = [], 100.0
    for i in range(n):
        c = px * (1 + drift + rng.gauss(0.0, noise))
        out.append(_bar(i, px, c))
        px = c
    return out


def _zigzag_up(n: int) -> list[dict]:
    """Period-4 pattern: two +2% bars, then two -1.8% bars. Net drift is
    small but positive; a strategy that enters right before the up bars has
    real timing, not drift."""
    out, px = [], 100.0
    for i in range(n):
        step = 0.02 if i % 4 in (1, 2) else -0.018
        c = px * (1 + step)
        out.append(_bar(i, px, c))
        px = c
    return out


def _spec(**over) -> Spec:
    d = {
        "name": "bh", "symbol": "TESTUSDT", "interval": "1h",
        "fee_rate": 0.0004,
        "exit": {"timeout_bars": 3},
        "signals": {"kind": "random"},
        "control": {"enabled": False},
        "falsify_if": {"worst_case_net_negative": True, "min_fills": 10},
    }
    for k, v in over.items():
        d[k] = v
    return Spec.from_dict(d)


def _random_entries(n_bars: int, k: int, side: str = "long", seed: int = 5) -> dict[int, str]:
    rng = random.Random(seed)
    return {i: side for i in sorted(rng.sample(range(5, n_bars - 10), k))}


def _expected_matched(candles, bh, side, fee_rate):
    lo, hi = bh["bar_range"]
    ratio = candles[hi]["c"] / candles[lo]["o"]
    log_gross = math.log(ratio) if side == "long" else -math.log(ratio)
    n = hi - lo + 1
    return (log_gross - 2 * fee_rate) * bh["bars_in_market"] / n * 100


# --------------------------------------------------------------------------


def test_uptrend_random_long_is_positive_but_does_not_beat_drift():
    """The @Cryptolaz case: random long entries in a rising market make
    money, and the money is the drift -- each trade pays a round trip the
    holder pays once."""
    candles = _trend(600, drift=0.002)
    entries = _random_entries(len(candles), 60)
    res = verify(_spec(), candles, entries)
    bh = res["buy_and_hold"]
    assert res["runs"]["worst_case"]["stats"]["sum_pct"] > 0          # looks like an edge
    assert bh["side"] == "long" and not bh["mixed_sides"]
    assert bh["bar_range"] == [0, len(candles) - 1]
    assert bh["beats_buy_and_hold"] is False                          # it is drift
    assert bh["diff_pct"] < 0
    assert bh["strategy_net_pct"] == res["runs"]["worst_case"]["stats"]["sum_pct"]
    assert bh["matched_bh_pct"] == pytest.approx(_expected_matched(candles, bh, "long", 0.0004))
    # exposure from the worst-case fills: timeout 3 -> 4 bars per trade
    assert bh["fills"] == res["runs"]["worst_case"]["stats"]["n"]
    assert bh["bars_in_market"] == 4 * bh["fills"]
    assert bh["exposure"] == pytest.approx(bh["bars_in_market"] / len(candles))
    # informational by default: verdict untouched
    assert res["verdict"]["outcome"] == "NOT_FALSIFIED"
    assert not any(c["rule"] == "net_below_buy_and_hold" for c in res["verdict"]["checks"])


def test_timed_entries_beat_buy_and_hold():
    candles = _zigzag_up(400)
    entries = {i: "long" for i in range(8, 390, 4)}   # fills at open of the first up bar
    res = verify(_spec(exit={"timeout_bars": 1}), candles, entries)
    bh = res["buy_and_hold"]
    assert bh["bh_gross_pct"] > 0                     # the coin itself rose
    assert bh["beats_buy_and_hold"] is True
    assert bh["diff_pct"] > 0
    assert bh["bars_in_market"] == 2 * bh["fills"]


def test_short_book_mirrors_to_short_and_hold():
    candles = _trend(600, drift=-0.002, seed=13)
    entries = _random_entries(len(candles), 60, side="short")
    res = verify(_spec(), candles, entries)
    bh = res["buy_and_hold"]
    ratio = candles[-1]["c"] / candles[0]["o"]
    assert bh["side"] == "short"
    assert bh["bh_gross_pct"] == pytest.approx((1 - ratio) * 100)  # short-and-hold, positive
    assert bh["bh_gross_pct"] > 0
    assert bh["matched_bh_pct"] == pytest.approx(_expected_matched(candles, bh, "short", 0.0004))
    assert res["runs"]["worst_case"]["stats"]["sum_pct"] > 0
    assert bh["beats_buy_and_hold"] is False          # shorting a falling coin at random is drift too
    md = report.render(res)
    assert "做空持有" in md and "基线镜像为做空持有" in md


def test_mixed_book_uses_dominant_side_and_discloses_it():
    candles = _trend(400, drift=0.001)
    entries = _random_entries(len(candles), 40)
    for i in list(entries)[:10]:
        entries[i] = "short"
    res = verify(_spec(), candles, entries)
    bh = res["buy_and_hold"]
    assert bh["side"] == "long" and bh["mixed_sides"] is True
    assert "多空混合" in report.render(res)


def test_fees_are_one_round_trip_on_the_baseline():
    candles = _trend(500, drift=0.001)
    entries = _random_entries(len(candles), 40)
    free = verify(_spec(fee_rate=0.0), candles, entries)["buy_and_hold"]
    paid = verify(_spec(fee_rate=0.001), candles, entries)["buy_and_hold"]
    assert free["bh_net_pct"] == pytest.approx(free["bh_gross_pct"])
    assert paid["bh_gross_pct"] == pytest.approx(free["bh_gross_pct"])
    assert paid["bh_net_pct"] == pytest.approx(paid["bh_gross_pct"] - 0.2)   # 2 * 10bps
    assert paid["fee_round_trip_pct"] == pytest.approx(0.2)
    # the matched baseline is charged the same round trip, scaled by exposure
    assert free["matched_bh_pct"] - paid["matched_bh_pct"] == pytest.approx(0.2 * paid["exposure"])


def test_walk_forward_segments_get_their_own_baseline():
    candles = _trend(600, drift=0.001)
    entries = _random_entries(len(candles), 80)
    res = verify(_spec(walk_forward={"enabled": True, "folds": 3}), candles, entries)
    wf = res["walk_forward"]
    b = wf["boundary_bar"]
    assert wf["in_sample"]["buy_and_hold"]["bar_range"] == [0, b - 1]
    assert wf["out_sample"]["buy_and_hold"]["bar_range"] == [b, len(candles) - 1]
    oos = wf["out_sample"]["buy_and_hold"]
    assert oos["strategy_net_pct"] == wf["out_sample"]["runs"]["worst_case"]["stats"]["sum_pct"]
    md = report.render(res)
    assert "样本内（第 0 →" in md and f"样本外（第 {b:,} →" in md


def test_no_fills_is_no_evidence():
    candles = _trend(200, drift=0.001)
    entries = {len(candles) - 1: "long"}               # window ends before the fill bar
    res = verify(_spec(), candles, entries)
    bh = res["buy_and_hold"]
    assert bh["fills"] == 0 and bh["exposure"] == 0
    assert bh["beats_buy_and_hold"] is None and bh["diff_pct"] is None


def test_opt_in_criterion_off_by_default_and_fires_when_declared():
    candles = _trend(600, drift=0.002)
    entries = _random_entries(len(candles), 60)
    base = verify(_spec(), candles, entries)
    off = verify(_spec(falsify_if={"worst_case_net_negative": True, "min_fills": 10,
                                   "net_below_buy_and_hold": False}), candles, entries)
    on = verify(_spec(falsify_if={"worst_case_net_negative": True, "min_fills": 10,
                                  "net_below_buy_and_hold": True}), candles, entries)
    assert base["verdict"] == off["verdict"]
    assert base["verdict"]["outcome"] == "NOT_FALSIFIED"
    assert on["verdict"]["outcome"] == "FALSIFIED"
    fired = [c for c in on["verdict"]["checks"] if c["rule"] == "net_below_buy_and_hold"]
    assert fired and fired[0]["triggered"]
    assert "跑不赢同等敞口的买入持有" in report.render(on)


def test_opt_in_criterion_respects_min_fills():
    candles = _trend(300, drift=0.002)
    entries = _random_entries(len(candles), 5)
    res = verify(_spec(falsify_if={"min_fills": 30, "net_below_buy_and_hold": True}),
                 candles, entries)
    assert res["verdict"]["outcome"] == "INCONCLUSIVE"


def test_opt_in_criterion_must_be_boolean():
    with pytest.raises(SpecError, match="net_below_buy_and_hold"):
        _spec(falsify_if={"net_below_buy_and_hold": "yes"})


def test_markdown_and_html_share_the_same_sentences():
    candles = _trend(400, drift=0.002)
    entries = _random_entries(len(candles), 40)
    res = verify(_spec(), candles, entries)
    lines = report.buy_and_hold_lines(res)
    assert lines and any("仅作信息展示" in ln for ln in lines)
    md = report.render(res)
    html = render_html(res)
    assert "## 4b. 买入持有基线" in md and "<h2>4b. 买入持有基线" in html
    for ln in lines:
        assert f"- {ln}" in md
    assert "未跑赢同敞口买入持有" in html
