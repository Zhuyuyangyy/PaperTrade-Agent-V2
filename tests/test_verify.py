"""Tests for the strategy falsification engine.

Two layers: unit tests for each instrument (spec validation, data loading,
RSI, metrics, barrier reach, control determinism) and an end-to-end test that
runs the CLI on synthetic candles and checks the report says what the data
implies. Nothing here touches the network or the runtime databases.
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.verify import Spec, SpecError, verify  # noqa: E402
from research.verify import control, data, metrics, report  # noqa: E402
from research.verify import _evaluate_criteria  # noqa: E402
from research.audit.pathsim import Exit, simulate  # noqa: E402
from research.verify.signals import random_signals, rsi, rule_signals  # noqa: E402
from research.verify.__main__ import main  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def make_candles(n: int = 300, seed: int = 7, start: float = 100.0) -> list[dict]:
    """Deterministic synthetic OHLC: a mild upward drift with fat tails, so
    both barriers get touched sometimes and neither dominates."""
    rng = random.Random(seed)
    out, px = [], start
    t0 = 1_700_000_000_000
    for i in range(n):
        px *= 1.0 + rng.gauss(0.0004, 0.01)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.004))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.003)))
        l = min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.003)))
        out.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": 1000.0})
    return out


def write_csv(path: Path, rows: list[dict], cols: list[str]) -> Path:
    import csv

    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return path


# --------------------------------------------------------------------------
# spec validation
# --------------------------------------------------------------------------

class TestSpec:
    def test_minimal_valid(self):
        s = Spec.from_dict({
            "name": "t", "symbol": "btcusdt", "interval": "1h",
            "exit": {"take_profit": 0.03, "stop_loss": -0.08},
            "signals": {"kind": "rule", "rule": "rsi_below"},
        })
        assert s.symbol == "BTCUSDT"
        assert s.fill_model == "taker"

    def test_missing_field_names_it(self):
        with pytest.raises(SpecError, match="exit"):
            Spec.from_dict({"name": "t", "symbol": "X", "interval": "1h", "signals": {"kind": "random"}})

    def test_positive_stop_loss_rejected(self):
        with pytest.raises(SpecError, match="stop_loss 必须是负数"):
            Spec.from_dict({
                "name": "t", "symbol": "X", "interval": "1h",
                "exit": {"stop_loss": 0.08}, "signals": {"kind": "random"},
            })

    def test_exit_must_declare_something(self):
        with pytest.raises(SpecError, match="至少要声明一个退出条件"):
            Spec.from_dict({
                "name": "t", "symbol": "X", "interval": "1h",
                "exit": {}, "signals": {"kind": "random"},
            })

    def test_funding_requires_futures(self):
        with pytest.raises(SpecError, match="funding 只对 futures"):
            Spec.from_dict({
                "name": "t", "symbol": "X", "interval": "1h",
                "venue": "spot", "funding": True,
                "exit": {"timeout_bars": 5}, "signals": {"kind": "random"},
            })

    def test_fee_multiples_must_include_baseline(self):
        with pytest.raises(SpecError, match="必须包含 1"):
            Spec.from_dict({
                "name": "t", "symbol": "X", "interval": "1h",
                "exit": {"timeout_bars": 5}, "signals": {"kind": "random"},
                "fee_multiples": [2, 5],
            })

    def test_bad_fill_model(self):
        with pytest.raises(SpecError, match="fill_model"):
            Spec.from_dict({
                "name": "t", "symbol": "X", "interval": "1h",
                "fill_model": "magic",
                "exit": {"timeout_bars": 5}, "signals": {"kind": "random"},
            })

    def test_exit_signals_csv_maps_to_bars(self, tmp_path):
        """A strategy whose exit logic is itself a signal: bar index -> reason,
        filled at the close (pathsim contract)."""
        candles = make_candles(60)
        p = write_csv(tmp_path / "x.csv", [
            {"t": str(candles[5]["t"]), "reason": "funding_reversal"},
            {"t": str(candles[9]["t"])},                      # default reason
            {"t": str(candles[0]["t"] - 10_000_000)},         # before window: skipped
        ], ["t", "reason"])
        sigs = data.load_exit_signals_csv(p, candles)
        assert sigs.get(5) == "funding_reversal"
        assert sigs.get(9) == "signal_exit"
        assert len(sigs) == 2

    def test_exit_signals_reach_the_simulator(self):
        """With an exit signal on every bar, every trade exits by signal at the
        close -- the reason must appear in the by_reason bookkeeping."""
        candles = make_candles(400, seed=8)
        d = dict(BASE_SPEC)
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        every_bar = {i: "my_reason" for i in range(len(candles))}
        r = verify(spec, candles, entries, exit_signals=every_bar)
        assert "my_reason" in r["runs"]["worst_case"]["by_reason"]

    def test_roundtrip_dict(self):
        d = {
            "name": "t", "symbol": "X", "interval": "1h",
            "exit": {"take_profit": 0.02, "stop_loss": -0.05, "timeout_bars": 10},
            "signals": {"kind": "rule", "rule": "rsi_below", "period": 7, "threshold": 25},
            "control": {"enabled": False},
        }
        assert Spec.from_dict(d).to_dict()["signals"]["period"] == 7


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------

class TestData:
    def test_epoch_ms_and_iso_both_parse(self):
        assert data._to_ms("1700000000000") == 1_700_000_000_000
        assert data._to_ms("1700000000") == 1_700_000_000_000
        assert data._to_ms("2023-11-14 22:13:20") == 1_700_000_000_000

    def test_unparseable_timestamp_raises(self):
        with pytest.raises(data.CandleError, match="无法解析时间戳"):
            data._to_ms("not-a-time")

    def test_candles_csv_happy_path_and_sort(self, tmp_path):
        p = write_csv(tmp_path / "k.csv", [
            {"t": "2023-01-02", "o": "2", "h": "3", "l": "1", "c": "2.5"},
            {"t": "2023-01-01", "o": "1", "h": "2", "l": "0.5", "c": "1.5"},
        ], ["t", "o", "h", "l", "c"])
        cs = data.load_candles_csv(p)
        assert len(cs) == 2 and cs[0]["t"] < cs[1]["t"]

    def test_inconsistent_ohlc_rejected(self, tmp_path):
        p = write_csv(tmp_path / "bad.csv", [
            {"t": "2023-01-01", "o": "1", "h": "0.5", "l": "0.4", "c": "1"},
        ], ["t", "o", "h", "l", "c"])
        with pytest.raises(data.CandleError, match="OHLC 不自洽"):
            data.load_candles_csv(p)

    def test_signals_map_to_first_bar_at_or_after(self, tmp_path):
        candles = make_candles(50)
        p = write_csv(tmp_path / "s.csv", [
            {"t": str(candles[0]["t"]), "side": "long"},     # maps to bar 0
            {"t": str(candles[0]["t"] + 1), "side": "short"},  # still bar 0 (first >= t is bar 1? no: bar 0 t < signal)
            {"t": str(candles[-1]["t"]), "side": "long"},    # after last usable bar -> skipped
        ], ["t", "side"])
        entries, stats = data.load_signals_csv(p, candles)
        # signal 1 maps to bar 1 (first bar with t >= signal), signal 2 same bar
        assert stats["total"] == 3
        assert stats["skipped_after_end"] == 1
        assert 1 in entries

    def test_signals_default_side_long(self, tmp_path):
        candles = make_candles(10)
        p = write_csv(tmp_path / "s.csv", [{"t": str(candles[2]["t"])}], ["t"])
        entries, _ = data.load_signals_csv(p, candles)
        assert all(s == "long" for s in entries.values())


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

class TestSignals:
    def test_rsi_warmup_and_bounds(self):
        vals = rsi([float(i) for i in range(50)], 14)
        assert vals[:14] == [None] * 14
        assert all(0 <= v <= 100 for v in vals[14:])

    def test_rsi_monotonic_up_is_100_down_is_0(self):
        up = rsi([float(i) for i in range(40)], 14)
        down = rsi([float(100 - i) for i in range(40)], 14)
        assert up[-1] == pytest.approx(100.0)
        assert down[-1] == pytest.approx(0.0)

    def test_rule_signals_no_lookahead_shape(self):
        candles = make_candles(400, seed=3)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=30)
        assert entries
        assert all(0 <= i < len(candles) - 1 for i in entries)
        assert all(s == "long" for s in entries.values())

    def test_random_signals_deterministic_and_in_range(self):
        candles = make_candles(200)
        a = random_signals(candles, 20, seed=42)
        b = random_signals(candles, 20, seed=42)
        assert a == b
        assert all(30 <= i <= len(candles) - 2 for i in a)

    def test_random_signals_rejects_tiny_window(self):
        with pytest.raises(ValueError, match="数据窗口太短"):
            random_signals(make_candles(10), 5, seed=1)


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

class TestMetrics:
    def test_known_series(self):
        st = metrics.equity_stats([0.10, -0.05])
        assert st["n"] == 2
        assert st["sum_pct"] == pytest.approx(5.0)
        assert st["mean_pct"] == pytest.approx(2.5)
        assert st["win_pct"] == pytest.approx(50.0)
        assert st["profit_factor"] == pytest.approx(2.0)
        assert st["max_dd_pct"] == pytest.approx(-5.0)

    def test_empty_series_is_zeroed_not_crash(self):
        st = metrics.equity_stats([])
        assert st["n"] == 0 and st["profit_factor"] is None

    def test_all_losses_has_zero_pf_not_none(self):
        """Gross profit 0 with gross losses > 0 is a PF of 0, not undefined;
        None is reserved for 'no losses at all' where the ratio explodes."""
        st = metrics.equity_stats([-0.01, -0.02])
        assert st["profit_factor"] == pytest.approx(0.0)
        assert st["win_pct"] == 0.0


# --------------------------------------------------------------------------
# end-to-end engine
# --------------------------------------------------------------------------

BASE_SPEC = {
    "name": "unit-test-strategy",
    "symbol": "TESTUSDT",
    "interval": "1h",
    "fee_rate": 0.0004,
    "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 12},
    "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 35},
    "control": {"enabled": True, "n_random": 100, "seed": 42},
    "fee_multiples": [1, 2, 5],
    "falsify_if": {"min_fills": 10},
}


class TestVerifyEngine:
    def test_battery_runs_and_shape_is_right(self):
        candles = make_candles(1500, seed=11)
        spec = Spec.from_dict(BASE_SPEC)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        result = verify(spec, candles, entries)
        assert result["n_signals"] == len(entries)
        for case in ("worst_case", "best_case"):
            assert result["runs"][case]["stats"]["n"] > 0
        assert len(result["cost_sensitivity"]) == 3
        assert result["barrier_reach"]["n"] > 0
        assert result["control"]["selectivity"] in ("proven", "refuted", "unproven")
        assert result["verdict"]["outcome"] in ("FALSIFIED", "NOT_FALSIFIED", "INCONCLUSIVE")

    def test_worst_case_never_better_than_best(self):
        candles = make_candles(1500, seed=5)
        spec = Spec.from_dict(BASE_SPEC)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        assert r["runs"]["worst_case"]["stats"]["sum_pct"] <= r["runs"]["best_case"]["stats"]["sum_pct"]

    def test_empty_entries_rejected(self):
        spec = Spec.from_dict(BASE_SPEC)
        with pytest.raises(ValueError, match="没有入场信号"):
            verify(spec, make_candles(50), {})

    def test_falsification_rule_fires_when_declared(self):
        """A strategy that loses at 2x fees must be FALSIFIED when the spec
        says so -- and the check list must quote the exact numbers."""
        candles = make_candles(1500, seed=11)
        d = dict(BASE_SPEC)
        d["falsify_if"] = {"net_negative_at_fee_multiple": 2, "min_fills": 10}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        rules = {c["rule"]: c for c in r["verdict"]["checks"]}
        if r["verdict"]["outcome"] == "FALSIFIED":
            assert rules["net_negative_at_fee_multiple=2"]["triggered"]
            assert "edge 付不起 toll" in rules["net_negative_at_fee_multiple=2"]["detail"]

    def test_insufficient_samples_is_inconclusive_not_falsified(self):
        """Too few fills must not be dressed up as a verdict."""
        candles = make_candles(120, seed=2)  # short window -> few signals
        d = dict(BASE_SPEC)
        d["falsify_if"] = {"min_fills": 10_000}
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        assert r["verdict"]["outcome"] == "INCONCLUSIVE"

    def test_zero_fill_window_is_inconclusive_never_falsified(self):
        """A window where nothing ever closed has no measured net: sum_pct
        is 0.0 because nothing traded, not because the strategy broke even.
        The declared net-negative rules must stay silent there -- falsifying
        a strategy that never traded is the failure mode min_fills exists
        to prevent, and '无证据不算输' cuts both ways."""
        candles = make_candles(60)
        d = dict(BASE_SPEC)
        d["falsify_if"] = {"min_fills": 5, "worst_case_net_negative": True,
                           "net_negative_at_fee_multiple": 2}
        spec = Spec.from_dict(d)
        r = verify(spec, candles, {len(candles) - 1: "long"})  # last bar: nothing can close
        assert r["runs"]["worst_case"]["stats"]["n"] == 0
        v = r["verdict"]
        assert v["outcome"] == "INCONCLUSIVE"
        assert not any(c.get("triggered") for c in v["checks"])
        assert any(c.get("insufficient") and c.get("scope") == "全窗口"
                   for c in v["checks"])

    def test_control_disabled_leaves_no_control_section(self):
        candles = make_candles(800, seed=9)
        d = dict(BASE_SPEC)
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        assert r["control"] is None
        assert "没有对照组" in report.render(r)

    def test_barrier_reach_detects_untouchable_barriers(self):
        """A TP at +50% on quiet candles: reachability must call it out."""
        candles = make_candles(400, seed=4)
        d = dict(BASE_SPEC)
        d["exit"] = {"take_profit": 0.5, "stop_loss": -0.4, "timeout_bars": 6}
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        br = r["barrier_reach"]
        assert br["n"] > 0
        assert br["reached_tp_pct"] < 10.0
        assert "彩票" in report.render(r)

    def test_maker_unfilled_entries_are_counted(self):
        """Quiet candles + maker limit: signals that never get crossed must
        surface as unfilled, not vanish."""
        candles = make_candles(600, seed=6)
        d = dict(BASE_SPEC)
        d["fill_model"] = "maker"
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        total = r["runs"]["worst_case"]["fills"] + r["runs"]["worst_case"]["unfilled"]
        assert total + r["runs"]["worst_case"]["open_signals"] == len(entries)


# --------------------------------------------------------------------------
# walk-forward
# --------------------------------------------------------------------------

def _dip_recovery_candles(n: int, seed: int = 3, period: int = 15,
                          dip: float = 0.015, rec: float = 0.005) -> list[dict]:
    """Flat base with periodic deep dips that recover: dip-buying WINS here."""
    rng = random.Random(seed)
    out, px = [], 100.0
    t0 = 1_700_000_000_000
    for i in range(n):
        phase = i % period
        px *= 1.0 + (-dip if phase < 3 else rec) + rng.gauss(0.0, 0.002)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.004))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.003)))
        l = min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.003)))
        out.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": 1000.0})
    return out


def _grind_down_candles(n: int, seed: int = 5, drift: float = -0.0002,
                        vol: float = 0.006) -> list[dict]:
    """Mild persistent decline: dip entries time out at a small loss, forever."""
    rng = random.Random(seed)
    out, px = [], 100.0
    t0 = 1_700_000_000_000
    for i in range(n):
        px *= 1.0 + drift + rng.gauss(0.0, vol)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.004))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, vol)))
        l = min(o, c) * (1.0 - abs(rng.gauss(0.0, vol)))
        out.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": 1000.0})
    return out


def _overfit_candles() -> list[dict]:
    """The overfitting story in one series: the edge is real for 1000 bars,
    then the regime grinds lower and the same entries bleed. Measured on this
    series (seeded, deterministic): full window +87.1%, in-sample +163.5%,
    out-of-sample -76.4% -- the full window PASSES worst_case_net_negative
    while the out-of-sample segment fails it, so a FALSIFIED verdict here can
    only have come from the walk-forward propagation rule."""
    return _dip_recovery_candles(1000) + _grind_down_candles(500)


def _free_fall_candles(n: int) -> list[dict]:
    """Monotonic -1%/bar crash: every dip entry loses at EVERY parameter
    combination -- the grid has no positive cell anywhere, which is a
    stronger statement than knife-edge and deserves its own sentence."""
    out, px = [], 100.0
    t0 = 1_700_000_000_000
    for i in range(n):
        px *= 0.99
        o = px * 1.001
        c = px
        out.append({"t": t0 + i * 3_600_000, "o": o, "h": o, "l": px * 0.999,
                    "c": c, "v": 1000.0})
    return out


class TestWalkForward:
    def test_single_fold_rejected(self):
        with pytest.raises(SpecError, match="至少为 2"):
            Spec.from_dict({
                "name": "t", "symbol": "X", "interval": "1h",
                "exit": {"timeout_bars": 5}, "signals": {"kind": "random"},
                "walk_forward": {"folds": 1},
            })

    def test_default_disabled_and_declaring_block_enables(self):
        off = Spec.from_dict({
            "name": "t", "symbol": "X", "interval": "1h",
            "exit": {"timeout_bars": 5}, "signals": {"kind": "random"},
        })
        assert off.walk_forward.enabled is False
        on = Spec.from_dict({
            "name": "t", "symbol": "X", "interval": "1h",
            "exit": {"timeout_bars": 5}, "signals": {"kind": "random"},
            "walk_forward": {"folds": 4},
        })
        assert on.walk_forward.enabled is True and on.walk_forward.folds == 4
        assert on.to_dict()["walk_forward"] == {"enabled": True, "folds": 4}

    def test_window_too_short_for_folds_is_refused(self):
        candles = make_candles(60)
        d = dict(BASE_SPEC)
        d["walk_forward"] = {"folds": 100}
        spec = Spec.from_dict(d)
        with pytest.raises(ValueError, match="不够切"):
            verify(spec, candles, {30: "long"})

    def test_split_partitions_every_signal_exactly_once(self):
        candles = make_candles(1200, seed=21)
        d = dict(BASE_SPEC)
        d["walk_forward"] = {"folds": 4}
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        wf = r["walk_forward"]
        assert wf["folds"] == 4 and wf["fold_bars"] == 300 and wf["boundary_bar"] == 900
        isb, oosb = wf["in_sample"], wf["out_sample"]
        assert isb and oosb
        assert isb["n_signals"] + oosb["n_signals"] == len(entries)
        # both segments run the SAME instruments, not a reduced copy
        for b in (isb, oosb):
            assert len(b["cost_sensitivity"]) == 3
            assert b["barrier_reach"]["n"] > 0
            assert b["runs"]["worst_case"]["stats"]["n"] > 0
        assert r["walk_forward"] is not None

    def test_oos_control_draws_only_from_the_oos_segment(self):
        candles = make_candles(1200, seed=21)
        d = dict(BASE_SPEC)
        d["walk_forward"] = {"folds": 4}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        wf = r["walk_forward"]
        # the in-sample control stays inside the in-sample segment: a control
        # reaching into the out-of-sample fold would book that fold's random
        # losses as "control" and inflate in-sample selectivity
        assert wf["in_sample"]["control"]["bar_range"] == [30, 899]
        # the out-of-sample control is confined to the out-of-sample segment
        assert wf["out_sample"]["control"]["bar_range"][0] == 900
        assert wf["out_sample"]["control"]["bar_range"][1] == 1198

    def test_oos_falsification_falsifies_the_whole_strategy(self):
        """Full window passes the declared rule, out-of-sample fails it: the
        FALSIFIED verdict can only come from the propagation rule."""
        candles = _overfit_candles()
        d = dict(BASE_SPEC)
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45}
        d["control"] = {"enabled": False}
        d["walk_forward"] = {"folds": 3}
        d["falsify_if"] = {"worst_case_net_negative": True, "min_fills": 10}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        r = verify(spec, candles, entries)
        v = r["verdict"]
        assert v["outcome"] == "FALSIFIED"
        triggered = [c for c in v["checks"] if c.get("triggered")]
        assert triggered and all(c["scope"] == "样本外" for c in triggered)
        assert "样本外段（第 1000 根起）" in triggered[0]["detail"]
        assert "样本外" in v["reason"]
        # the standalone segment verdicts say the same thing
        assert r["walk_forward"]["out_sample"]["verdict"]["outcome"] == "FALSIFIED"

    def test_oos_insufficient_is_inconclusive_never_a_pass(self):
        candles = make_candles(150, seed=2)  # short window -> few signals
        d = dict(BASE_SPEC)
        d["walk_forward"] = {"folds": 5}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        v = r["verdict"]
        assert v["outcome"] == "INCONCLUSIVE"
        assert not any(c.get("triggered") for c in v["checks"])
        oos_ins = [c for c in v["checks"]
                   if c.get("insufficient") and c.get("scope") == "样本外"]
        assert oos_ins and "样本外段" in oos_ins[0]["detail"]

    def test_no_oos_signals_is_inconclusive_with_the_reason(self):
        candles = make_candles(100)
        d = dict(BASE_SPEC)
        d["walk_forward"] = {"folds": 2}
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        r = verify(spec, candles, {5: "long"})  # the only signal is in-sample
        wf = r["walk_forward"]
        assert wf["in_sample"] is not None and wf["out_sample"] is None
        v = r["verdict"]
        assert v["outcome"] == "INCONCLUSIVE"
        note = [c for c in v["checks"] if c.get("scope") == "样本外"]
        assert note and "没有任何入场信号" in note[0]["detail"]

    def test_zero_fill_oos_fold_is_inconclusive_never_falsified(self):
        """The out-of-sample fold's only signal sits on the last bar: the
        segment has signals but ZERO fills. Its net is unmeasured, so the
        declared net-negative rules must not fire there -- a FALSIFIED
        verdict would override this fold's own min_fills insufficient check
        and kill the strategy on no evidence at all."""
        candles = make_candles(60)
        d = dict(BASE_SPEC)
        d["walk_forward"] = {"folds": 2}
        d["falsify_if"] = {"min_fills": 5, "worst_case_net_negative": True,
                           "net_negative_at_fee_multiple": 2}
        spec = Spec.from_dict(d)
        r = verify(spec, candles, {59: "long"})  # boundary=30 -> the only signal is OOS
        wf = r["walk_forward"]
        assert wf["out_sample"] is not None
        assert wf["out_sample"]["runs"]["worst_case"]["stats"]["n"] == 0
        v = r["verdict"]
        assert v["outcome"] == "INCONCLUSIVE"
        assert not any(c.get("triggered") for c in v["checks"])
        oos_ins = [c for c in v["checks"]
                   if c.get("insufficient") and c.get("scope") == "样本外"]
        assert oos_ins and "样本外段" in oos_ins[0]["detail"]

    def test_report_shows_both_segments_and_the_tuning_disclosure(self):
        candles = _overfit_candles()
        d = dict(BASE_SPEC)
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45}
        d["control"] = {"enabled": False}
        d["walk_forward"] = {"folds": 3}
        d["falsify_if"] = {"worst_case_net_negative": True, "min_fills": 10}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        text = report.render(verify(spec, candles, entries))
        assert "## 5. Walk-forward（样本内 / 样本外）" in text
        assert "样本内（前 2 折）" in text and "样本外（最后 1 折）" in text
        assert "## 7. 事前证伪规则判定" in text
        assert "| 规则 | 范围 | 结果 | 明细 |" in text
        assert "无法验证策略参数是否曾用全窗口数据调优" in text
        assert "只在看得见的地方成立的 edge 不是 edge" in text
        assert "不在本引擎射程内" not in text  # stale once walk-forward runs

    def test_report_without_walk_forward_keeps_the_old_disclosure(self):
        candles = make_candles(800, seed=9)
        d = dict(BASE_SPEC)
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        text = report.render(verify(spec, candles, entries))
        assert "## 5. Walk-forward" not in text
        assert "## 7. 事前证伪规则判定" in text  # criteria renumbered, still last
        assert "walk-forward 与参数敏感性不在本引擎射程内" in text


# --------------------------------------------------------------------------
# parameter sensitivity grid
# --------------------------------------------------------------------------

class TestParamGrid:
    def _grid_spec(self, **over):
        d = dict(BASE_SPEC)
        d["control"] = {"enabled": False}
        d["param_grid"] = {"axes": {"take_profit": [0.02, 0.03, 0.04],
                                    "stop_loss": [-0.06, -0.08, -0.10]}}
        d.update(over)
        return Spec.from_dict(d)

    def test_declaring_block_enables_and_roundtrips(self):
        on = self._grid_spec()
        assert on.param_grid.enabled is True
        rt = on.to_dict()["param_grid"]
        assert rt["axes"]["take_profit"] == [0.02, 0.03, 0.04]
        assert rt["max_cells"] == 64
        off = Spec.from_dict({
            "name": "t", "symbol": "X", "interval": "1h",
            "exit": {"timeout_bars": 5}, "signals": {"kind": "random"},
        })
        assert off.param_grid.enabled is False
        assert off.to_dict()["param_grid"]["axes"] == {}

    def test_axis_validation_errors(self):
        base = {"name": "t", "symbol": "X", "interval": "1h",
                "exit": {"timeout_bars": 5}, "signals": {"kind": "random"}}
        with pytest.raises(SpecError, match="参数名必须是"):
            Spec.from_dict({**base, "param_grid": {"axes": {"rsi_period": [7, 14]}}})
        with pytest.raises(SpecError, match="至少 2 个值"):
            Spec.from_dict({**base, "param_grid": {"axes": {"take_profit": [0.03]}}})
        with pytest.raises(SpecError, match="最多支持 2 个参数轴"):
            Spec.from_dict({**base, "param_grid": {"axes": {
                "take_profit": [0.02, 0.03], "stop_loss": [-0.06, -0.08],
                "timeout_bars": [6, 12]}}})
        with pytest.raises(SpecError, match="take_profit 的值必须是正数"):
            Spec.from_dict({**base, "param_grid": {"axes": {"take_profit": [-0.02, 0.03]}}})
        with pytest.raises(SpecError, match="stop_loss 的值必须是负数"):
            Spec.from_dict({**base, "param_grid": {"axes": {"stop_loss": [0.06, -0.08]}}})
        with pytest.raises(SpecError, match="有重复"):
            Spec.from_dict({**base, "param_grid": {"axes": {"take_profit": [0.03, 0.03]}}})
        with pytest.raises(SpecError, match="max_cells"):
            Spec.from_dict({**base, "param_grid": {
                "axes": {"take_profit": [0.01, 0.02, 0.03, 0.04]}, "max_cells": 3}})
        with pytest.raises(SpecError, match="≥1 的整数"):
            Spec.from_dict({**base, "param_grid": {"axes": {"timeout_bars": [6, 12.5]}}})

    def test_grid_positive_ratio_min_validated(self):
        base = {"name": "t", "symbol": "X", "interval": "1h",
                "exit": {"timeout_bars": 5}, "signals": {"kind": "random"}}
        with pytest.raises(SpecError, match="grid_positive_ratio_min"):
            Spec.from_dict({**base, "falsify_if": {"grid_positive_ratio_min": 1.5}})
        with pytest.raises(SpecError, match="grid_positive_ratio_min"):
            Spec.from_dict({**base, "falsify_if": {"grid_positive_ratio_min": "half"}})

    def test_grid_covers_every_combo_and_the_base_cell_reproduces_the_headline(self):
        """The cell matching the declared exit must reproduce the headline
        worst-case number EXACTLY -- the grid moves parameters, never the
        measurement convention."""
        candles = make_candles(1200, seed=21)
        spec = self._grid_spec()
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        g = r["param_grid"]["scopes"][0]
        assert g["n_cells"] == 9 and len(g["cells"]) == 9
        combos = {(c["params"]["take_profit"], c["params"]["stop_loss"]) for c in g["cells"]}
        assert len(combos) == 9
        base_cell = next(c for c in g["cells"]
                         if c["params"] == {"take_profit": 0.03, "stop_loss": -0.08})
        assert base_cell["net_sum_pct"] == pytest.approx(
            r["runs"]["worst_case"]["stats"]["sum_pct"])
        # the summary is a recount of the cells, not a second source of truth
        traded = [c for c in g["cells"] if c["fills"] > 0]
        assert g["cells_with_fills"] == len(traded)
        assert g["positive_cells"] == sum(1 for c in traded if c["net_sum_pct"] > 0)
        assert g["positive_ratio"] == pytest.approx(g["positive_cells"] / len(traded))

    def test_declared_rule_fires_and_undeclared_rule_invents_nothing(self):
        """Same series, same grid: with the rule declared a knife-edge grid
        falsifies; without it the grid is still measured but no threshold
        appears out of thin air."""
        candles = _grind_down_candles(800)
        d = dict(BASE_SPEC)
        d["control"] = {"enabled": False}
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45}
        d["param_grid"] = {"axes": {"take_profit": [0.01, 0.03, 0.05],
                                    "stop_loss": [-0.04, -0.08]}}
        d["falsify_if"] = {"min_fills": 10, "grid_positive_ratio_min": 0.5}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        r = verify(spec, candles, entries)
        g = r["param_grid"]["scopes"][0]
        checks = [c for c in r["verdict"]["checks"]
                  if c["rule"] == "grid_positive_ratio_min"]
        assert checks and all(c["triggered"] for c in checks)
        assert r["verdict"]["outcome"] == "FALSIFIED"
        assert f"{g['positive_cells']}/{g['cells_with_fills']}" in checks[0]["detail"]
        assert "曲线拟合不是 edge" in checks[0]["detail"]
        d2 = dict(d)
        d2["falsify_if"] = {"min_fills": 10}
        r2 = verify(Spec.from_dict(d2), candles, entries)
        assert not any(c["rule"] == "grid_positive_ratio_min"
                       for c in r2["verdict"]["checks"])
        assert r2["param_grid"]["scopes"][0]["positive_ratio"] == g["positive_ratio"]

    def test_grid_follows_the_walk_forward_segments(self):
        """The grid runs per segment when walk-forward is on; a knife-edge
        OUT-OF-SAMPLE grid falsifies the whole strategy, and the in-sample
        segment verdict sees its own grid."""
        candles = _overfit_candles()
        d = dict(BASE_SPEC)
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45}
        d["control"] = {"enabled": False}
        d["walk_forward"] = {"folds": 3}
        d["param_grid"] = {"axes": {"take_profit": [0.01, 0.03, 0.05],
                                    "stop_loss": [-0.04, -0.08]}}
        d["falsify_if"] = {"min_fills": 10, "grid_positive_ratio_min": 0.5}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        r = verify(spec, candles, entries)
        pg = r["param_grid"]
        assert [g["scope"] for g in pg["scopes"]] == ["样本内", "样本外"]
        is_g, oos_g = pg["scopes"]
        # the seeded series: dip-buying works in-sample and bleeds out-of-sample
        assert is_g["positive_cells"] > 0
        assert oos_g["positive_cells"] < is_g["positive_cells"]
        v = r["verdict"]
        oos_checks = [c for c in v["checks"]
                      if c["rule"] == "grid_positive_ratio_min" and c["scope"] == "样本外"]
        assert oos_checks and oos_checks[0]["triggered"]
        assert "样本外段（第 1000 根起）" in oos_checks[0]["detail"]
        # the segment's own verdict must agree with the criteria table
        assert r["walk_forward"]["out_sample"]["verdict"]["outcome"] == "FALSIFIED"

    def test_grid_where_nothing_trades_is_inconclusive(self):
        """A grid with no fills anywhere carries no evidence: INCONCLUSIVE,
        never a pass and never a falsification."""
        candles = make_candles(40)
        spec = self._grid_spec(falsify_if={"min_fills": 10, "grid_positive_ratio_min": 0.5})
        r = verify(spec, candles, {len(candles) - 1: "long"})  # last bar: nothing can close
        g = r["param_grid"]["scopes"][0]
        assert g["cells_with_fills"] == 0 and g["positive_ratio"] is None
        v = r["verdict"]
        assert v["outcome"] == "INCONCLUSIVE"
        assert not any(c.get("triggered") for c in v["checks"])
        ins = [c for c in v["checks"]
               if c["rule"] == "grid_positive_ratio_min" and c.get("insufficient")]
        assert ins and "无法评估参数敏感性" in ins[0]["detail"]

    def test_zero_positive_grid_says_the_edge_does_not_exist(self):
        """0 positive cells is not 'knife-edge' -- the honest sentence is
        that no combination in the declared space makes money."""
        candles = _free_fall_candles(200)
        d = dict(BASE_SPEC)
        d["control"] = {"enabled": False}
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45}
        d["param_grid"] = {"axes": {"take_profit": [0.01, 0.03, 0.05],
                                    "stop_loss": [-0.04, -0.08]}}
        d["falsify_if"] = {"min_fills": 10, "grid_positive_ratio_min": 0.5}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        r = verify(spec, candles, entries)
        g = r["param_grid"]["scopes"][0]
        assert g["positive_cells"] == 0 and g["cells_with_fills"] == 6
        check = next(c for c in r["verdict"]["checks"]
                     if c["rule"] == "grid_positive_ratio_min")
        assert check["triggered"]
        assert "没有任何参数组合赚钱" in check["detail"]
        assert "曲线拟合" not in check["detail"]  # 0 cells is not knife-edge
        text = report.render(r)
        assert "全部 6 格均为负" in text

    def test_report_renders_heatmap_and_renumbers_sections(self):
        candles = _overfit_candles()
        d = dict(BASE_SPEC)
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45}
        d["control"] = {"enabled": False}
        d["walk_forward"] = {"folds": 3}
        d["param_grid"] = {"axes": {"take_profit": [0.01, 0.03, 0.05],
                                    "stop_loss": [-0.04, -0.08]}}
        d["falsify_if"] = {"min_fills": 10, "grid_positive_ratio_min": 0.5}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        text = report.render(verify(spec, candles, entries))
        assert "## 6. 参数敏感性网格" in text
        assert "## 7. 事前证伪规则判定" in text
        assert "## 8. 诊断：死因与处方" in text
        assert "## 9. 未建模事项" in text
        # the diagnosis must name a cause and prescribe something numeric
        assert "主死因：" in text and "处方（按优先级" in text
        assert "take_profit ↓ ＼ stop_loss →" in text
        assert "**样本内（前 2 折）**" in text and "**样本外（最后 1 折）**" in text
        # seeded numbers (real execution): in-sample 6/6 positive, oos 2/6
        assert "6 格有成交 · 6 格净收益为正（100%）" in text
        assert "6 格有成交 · 2 格净收益为正（33%）" in text
        assert "只有 2/6 格为正" in text  # the flag quotes the measured ratio
        assert "曲线拟合不是 edge" in text  # the declared rule fired -> the flag shows
        # grid off: no section 6, criteria still 7, old disclosure intact
        d2 = dict(d)
        d2.pop("param_grid")
        d2.pop("walk_forward")
        d2["falsify_if"] = {"min_fills": 10}
        text2 = report.render(verify(Spec.from_dict(d2), candles, entries))
        assert "## 6. 参数敏感性网格" not in text2
        assert "## 7. 事前证伪规则判定" in text2
        assert "walk-forward 与参数敏感性不在本引擎射程内" in text2


# --------------------------------------------------------------------------
# control group
# --------------------------------------------------------------------------

class TestControl:
    def test_mirrors_dominant_side(self):
        candles = make_candles(400)
        spec = Spec.from_dict(BASE_SPEC)
        ctrl = control.random_control(
            candles, _exit_rule(), spec, {0: "short", 1: "short", 2: "long"},
            n_random=20, seed=1,
        )
        assert ctrl["side"] == "short"

    def test_identical_books_compare_as_unproven(self):
        nets = [0.01, -0.02, 0.03, 0.0, -0.01, 0.02, -0.03, 0.01]
        st = metrics.equity_stats(nets)
        ctrl = {"stats": metrics.equity_stats(list(reversed(nets))), "n_random": 8, "seed": 1}
        out = control.compare(st, ctrl, seed=1)
        assert out["selectivity"] == "unproven"
        assert out["diff_ci"] is not None

    def test_control_book_keeps_the_accounting_identity(self):
        """The control book answers to the same identity as the strategy
        books: fills + unfilled + open == n_random. A random entry the replay
        never resolved is a fact about the window -- dropping it would quietly
        inflate the control's apparent fill rate."""
        candles = make_candles(600, seed=6)
        spec = Spec.from_dict(BASE_SPEC)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        ctrl = r["control"]
        # seeded repro: 88 distinct random entries, 87 closed, 1 never resolved
        assert ctrl["open_signals"] >= 1
        assert ctrl["fills"] + ctrl["unfilled"] + ctrl["open_signals"] == ctrl["n_random"]

    def test_control_side_tie_is_deterministic_across_hash_seeds(self):
        """A 50/50 long/short book has no dominant side, so the mirrored side
        must not depend on the process's string-hash seed (set iteration
        order). Otherwise the same spec + data + seed yields a DIFFERENT
        control group on every run."""
        import os
        import subprocess

        code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from research.verify import Spec, control
from research.audit.pathsim import Exit
spec = Spec.from_dict({{'name': 't', 'symbol': 'X', 'interval': '1h',
    'exit': {{'take_profit': 0.03}}, 'signals': {{'kind': 'random'}}}})
rule = Exit(take_profit=0.03)
tied = {{i: ('long' if i % 2 == 0 else 'short') for i in range(40)}}
candles = [{{'t': 1_700_000_000_000 + i * 3_600_000, 'o': 100.0, 'h': 101.0,
            'l': 99.0, 'c': 100.0, 'v': 1.0}} for i in range(200)]
print(control.random_control(candles, rule, spec, tied, n_random=20, seed=42)['side'])
"""
        sides = set()
        for hash_seed in ("1", "2", "5"):
            env = dict(os.environ, PYTHONHASHSEED=hash_seed)
            out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                                 capture_output=True, text=True, timeout=120)
            assert out.returncode == 0, out.stderr
            sides.add(out.stdout.strip())
        assert len(sides) == 1, f"对照组镜像方向随 PYTHONHASHSEED 变化：{sides}"


    def test_trailing_exit_is_labelled_trailing_not_stop_loss(self):
        """Once the trail arms it is a profit-taking mechanism: labelling its
        exit 'stop_loss' made winning givebacks read as losing stops in the
        by-reason bookkeeping (the FreqBot audit exposed this). The Fill.reason
        contract has always listed 'trailing'; now the code keeps it."""
        candles = make_candles(400, seed=31)
        d = dict(BASE_SPEC)
        d["exit"] = {"stop_loss": -0.08, "trailing": 0.01, "trailing_offset": 0.005,
                     "timeout_bars": 24}
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        by = r["runs"]["worst_case"]["by_reason"]
        assert "trailing" in by, f"移动止损止盈被记成了别的原因: {sorted(by)}"
        assert by["trailing"]["n"] > 0

    def test_hard_stop_still_labelled_stop_loss_when_trail_never_arms(self):
        """The label fix must not swallow real stops: without a trail the
        stop_loss reason is unchanged."""
        candles = make_candles(400, seed=32)
        d = dict(BASE_SPEC)
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        by = r["runs"]["worst_case"]["by_reason"]
        assert "trailing" not in by

    def test_spec_without_falsify_rules_is_not_falsified_with_a_saying(self):
        """Measured in full, verdict NOT_FALSIFIED, and the criteria table says
        no rules were declared -- the docstring used to claim 'unfalsifiable',
        which the engine never said."""
        candles = make_candles(1500, seed=33)  # >=30 fills so the default guard passes
        d = dict(BASE_SPEC)
        d.pop("falsify_if", None)
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        assert r["verdict"]["outcome"] == "NOT_FALSIFIED"
        assert "未声明" in report.render(r)


    def test_port_notes_render_as_section_zero(self):
        """A ported strategy's fidelity notes live in the spec, not in
        hand-edited markdown: regeneration must never lose them."""
        candles = make_candles(600, seed=34)
        d = dict(BASE_SPEC)
        d["port_notes"] = "信号层高保真；退出层一处近似：ROI 阶梯退化为固定 TP。"
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        text = report.render(r)
        assert "## 0. 移植说明" in text
        assert "ROI 阶梯退化为固定 TP" in text
        assert r["spec"]["port_notes"].startswith("信号层高保真")


class TestReplayHonestyFixes:
    """Regressions for the five issues an external review found (2026-10-30).
    Each of these passed the suite BEFORE the fix -- the tests below are the
    nails that keep them fixed."""

    def test_worst_case_is_never_better_than_best_case(self):
        """The inversion: a tie bar where an ARMED trailing stop (a profit
        level) beats the take-profit used to make worst_case pay MORE than
        best_case (+89.86% vs -27.22% in the FreqBot audit). The tie-break
        now picks by what the exit is worth for the side, not by slot."""
        candles = [
            {"t": 1, "o": 100, "h": 100.1, "l": 99.9, "c": 100.0, "v": 1},
            {"t": 2, "o": 100, "h": 99.65, "l": 99.4, "c": 99.45, "v": 1},  # tie bar
            {"t": 3, "o": 99.45, "h": 99.5, "l": 99.3, "c": 99.4, "v": 1},
        ]
        rule = Exit(take_profit=0.003, stop_loss=-0.015,
                    trailing=0.002, trailing_offset=0.005, timeout_bars=10)
        nets = {}
        for worst in (True, False):
            sim = simulate(candles, {0: "short"}, rule, symbol="T",
                           fee_rate=0.0, worst_case=worst)
            nets[worst] = sim.fills[0].net_pct
        assert nets[True] <= nets[False], (
            f"最坏口径 {nets[True]:+.4%} 好于最好口径 {nets[False]:+.4%}——倒挂")

    def test_worst_le_best_holds_over_random_sweep(self):
        """Property test: for randomized candles and rules (trailing on and
        off, both sides), worst-case net must never exceed best-case net."""
        rng = random.Random(99)
        checked = 0
        for _ in range(60):
            n = rng.randint(40, 120)
            candles, px = [], 100.0
            for i in range(n):
                px *= 1.0 + rng.gauss(0, 0.02)
                o = px
                c = px * (1 + rng.gauss(0, 0.01))
                h = max(o, c) * (1 + abs(rng.gauss(0, 0.008)))
                l = min(o, c) * (1 - abs(rng.gauss(0, 0.008)))
                candles.append({"t": i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": 1})
            side = rng.choice(["long", "short"])
            rule = Exit(
                take_profit=rng.choice([None, 0.01, 0.03]),
                stop_loss=rng.choice([None, -0.02, -0.05]),
                trailing=rng.choice([None, 0.005, 0.01]),
                trailing_offset=rng.choice([None, 0.005, 0.01]),
                timeout_bars=rng.randint(2, 20),
            )
            if rule.take_profit is None and rule.stop_loss is None and rule.trailing is None:
                continue
            entries = {rng.randint(5, n - 3): side}
            worst_sim = simulate(candles, entries, rule, symbol="T", fee_rate=0.0004, worst_case=True)
            best_sim = simulate(candles, entries, rule, symbol="T", fee_rate=0.0004, worst_case=False)
            w = sum(f.net_pct for f in worst_sim.fills)
            b = sum(f.net_pct for f in best_sim.fills)
            assert w <= b + 1e-9, f"倒挂: worst {w:+.4%} > best {b:+.4%}"
            assert len(worst_sim.fills) == len(best_sim.fills)
            checked += 1
        assert checked >= 40

    def test_fee_bps_column_is_round_trip(self):
        """The simulator deducts fee_rate*2 per trade; the report's
        'round-trip fee' column used to show the one-side number --
        understating every toll by exactly half."""
        candles = make_candles(800, seed=41)
        d = dict(BASE_SPEC)
        d["fee_rate"] = 0.0005
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        row = r["cost_sensitivity"][0]
        assert row["fee_bps"] == pytest.approx(10.0)  # 5bps/side -> 10bps round trip

    def test_drawdown_is_chronological_with_a_disclosed_bound(self):
        """max_dd_pct must be the realized (time-ordered) drawdown; the
        losses-first replay is a separate, clearly-labelled bound."""
        nets = [0.2, -0.1, 0.2, -0.1]
        st = metrics.equity_stats(nets)
        # chronological: 1.2 -> 1.08 -> 1.296 -> 1.1664, peak 1.296 -> DD -10%
        assert st["max_dd_pct"] == pytest.approx(-10.0)
        # losses-first: 0.9 -> 0.81 -> 0.972 -> 1.1664, peak 1.0 -> DD -19%
        assert st["max_dd_worst_order_pct"] == pytest.approx(-19.0)
        assert st["max_dd_worst_order_pct"] <= st["max_dd_pct"]

    def test_control_rule_fires_when_significantly_worse_too(self):
        """'CI includes zero' used to let an interval entirely BELOW zero
        (strategy significantly WORSE than random) pass the rule. Not being
        proven better is the trigger; being proven worse is not a pass."""
        spec = Spec.from_dict({
            **BASE_SPEC,
            "falsify_if": {"control_lift_ci_includes_zero": True, "min_fills": 1},
        })
        runs = {
            "worst_case": {"stats": metrics.equity_stats([-0.01] * 50)},
            "best_case": {"stats": metrics.equity_stats([-0.01] * 50)},
        }
        ctrl = {"diff_pct": -0.30, "diff_ci": [-0.50, -0.10], "selectivity": "refuted"}
        checks = _evaluate_criteria(spec, runs, [], ctrl, scope="全窗口")
        rule = next(c for c in checks if c["rule"] == "control_lift_ci_includes_zero")
        assert rule["triggered"]
        assert "显著差于随机入场" in rule["detail"]

    def test_min_fills_gates_every_other_rule(self):
        """Below the spec's own declared evidence bar, nothing else is
        evaluated: a net rule firing on 8 fills would override the very
        insufficient-verdict the spec asked for."""
        candles = make_candles(300, seed=42)  # few signals
        d = dict(BASE_SPEC)
        d["falsify_if"] = {"min_fills": 10_000, "worst_case_net_negative": True}
        d["control"] = {"enabled": False}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        r = verify(spec, candles, entries)
        assert r["verdict"]["outcome"] == "INCONCLUSIVE"
        assert [c["rule"] for c in r["verdict"]["checks"]] == ["min_fills"]


class TestCustomerJourneyFixes:
    """Frictions found by a blind customer run (2026-10-30): a real exchange
    CSV, a real freqtrade strategy, and only the public docs. Each fix here
    is a place the journey used to die."""

    def _write(self, path, header, rows):
        import csv
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
        return path

    def test_candle_csv_accepts_exchange_column_names(self, tmp_path):
        """A Binance/freqtrade export uses date/open/high/low/close/volume.
        Demanding a literal `t` rejected every real customer file."""
        rows = [[1_700_000_000_000 + i * 3_600_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10]
                for i in range(50)]
        strict = self._write(tmp_path / "a.csv", ["t", "o", "h", "l", "c", "v"], rows)
        loose = self._write(tmp_path / "b.csv",
                            ["date", "open", "high", "low", "close", "volume"], rows)
        assert data.load_candles_csv(strict) == data.load_candles_csv(loose)

    def test_candle_csv_accepts_timestamp_alias_and_extra_columns(self, tmp_path):
        rows = [[1_700_000_000_000 + i * 3_600_000, 100, 101, 99, 100.5, 10]
                for i in range(50)]
        p = self._write(tmp_path / "c.csv",
                        ["open_time", "open", "high", "low", "close", "quote_volume", "trades"],
                        rows)
        candles = data.load_candles_csv(p)
        assert len(candles) == 50 and candles[0]["t"] == 1_700_000_000_000

    def test_signal_csv_accepts_date_column_and_buy_sell(self, tmp_path):
        """Customer signal files arrive as date/direction with buy/sell."""
        candles = make_candles(80, seed=51)
        p = self._write(tmp_path / "s.csv",
                        ["date", "direction"],
                        [[1_700_000_000_000 + i * 3_600_000, "buy" if i % 2 else "sell"]
                         for i in range(5, 70)])
        entries, stats = data.load_signals_csv(p, candles)
        assert stats["mapped"] > 0
        assert set(entries.values()) <= {"long", "short"}

    def test_signals_option_with_rule_spec_is_a_hard_error(self, tmp_path):
        """The dangerous one: --signals silently ignored under a rule spec
        meant the customer's own signals never got tested while the report
        audited the built-in rule. That is buying the wrong answer."""
        candles = make_candles(300, seed=52)
        kp = self._write(tmp_path / "k.csv", ["t", "o", "h", "l", "c", "v"],
                         [[1_700_000_000_000 + i * 3_600_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10]
                          for i in range(300)])
        sp = self._write(tmp_path / "s.csv", ["t", "side"],
                         [[1_700_000_000_000 + i * 3_600_000, "long"] for i in range(10, 200, 5)])
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps({**BASE_SPEC, "control": {"enabled": False}}), encoding="utf-8")
        rc = main(["--spec", str(spec_path), "--candles", str(kp), "--signals", str(sp)])
        assert rc == 2  # SpecError -> usage error, never a quiet wrong-answer report

    def test_spec_relative_signal_path_resolves_against_spec_dir(self, tmp_path):
        """signals.path inside a spec resolves against the SPEC's directory
        (a spec folder travels together), not the engine's cwd."""
        candles = make_candles(300, seed=53)
        kp = self._write(tmp_path / "k.csv", ["t", "o", "h", "l", "c", "v"],
                         [[1_700_000_000_000 + i * 3_600_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10]
                          for i in range(300)])
        self._write(tmp_path / "s.csv", ["t", "side"],
                    [[1_700_000_000_000 + i * 3_600_000, "long"] for i in range(10, 200, 5)])
        d = dict(BASE_SPEC)
        d["signals"] = {"kind": "csv", "path": "s.csv"}
        d["control"] = {"enabled": False}
        (tmp_path / "spec.json").write_text(json.dumps(d), encoding="utf-8")
        out = tmp_path / "r.md"
        rc = main(["--spec", str(tmp_path / "spec.json"), "--candles", str(kp), "--out", str(out)])
        assert rc == 0
        assert "证伪报告" in out.read_text(encoding="utf-8")

    def test_verdict_reason_says_each_scope_once(self):
        """Four rules firing on the OOS segment used to repeat the segment
        prefix four times -- a broken record readers skim past."""
        candles = _overfit_candles()
        d = dict(BASE_SPEC)
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45}
        d["control"] = {"enabled": False}
        d["walk_forward"] = {"folds": 3}
        d["falsify_if"] = {"worst_case_net_negative": True,
                           "net_negative_at_fee_multiple": 2, "min_fills": 10}
        spec = Spec.from_dict(d)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        r = verify(spec, candles, entries)
        if r["verdict"]["outcome"] == "FALSIFIED":
            reason = r["verdict"]["reason"]
            assert reason.count("样本外段：") <= 1, "scope 前缀重复了"


class TestCompare:
    """Multi-strategy comparison mode: the declared ranking rule, the matrix,
    and the CLI guards that keep it honest."""

    def _spec_file(self, tmp_path: Path, name: str, threshold: int) -> Path:
        d = dict(BASE_SPEC)
        d["name"] = name
        d["funding"] = False
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": threshold}
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(d), encoding="utf-8")
        return p

    def _candles_file(self, tmp_path: Path, n: int = 1500) -> Path:
        p = tmp_path / "candles.csv"
        write_csv(p, make_candles(n, seed=11), ["t", "o", "h", "l", "c", "v"])
        return p

    def test_compare_needs_two_specs(self):
        from research.verify.__main__ import main

        with pytest.raises(SystemExit):
            main(["--compare", "only-one.json"])

    def test_compare_rejects_demo_and_spec(self):
        from research.verify.__main__ import main

        with pytest.raises(SystemExit):
            main(["--compare", "a.json", "b.json", "--demo"])
        with pytest.raises(SystemExit):
            main(["--compare", "a.json", "b.json", "--spec", "c.json"])

    def test_rank_key_tier_beats_numbers(self):
        """A FALSIFIED strategy with a beautiful book still ranks BELOW an
        INCONCLUSIVE one with a poor book: the tier IS the honesty order."""
        from research.verify.compare import rank_key

        good_book = {
            "verdict": {"outcome": "FALSIFIED"},
            "control": {"diff_ci": [0.5, 0.9], "selectivity": "proven"},
            "walk_forward": {"out_sample": {"runs": {"worst_case": {"stats": {"sum_pct": 50.0}}}}},
            "runs": {"worst_case": {"stats": {"sum_pct": 50.0, "n": 100}},
                     "best_case": {"stats": {"sum_pct": 50.0}}},
        }
        poor_book = {
            "verdict": {"outcome": "INCONCLUSIVE"},
            "control": {"diff_ci": [-0.2, 0.1], "selectivity": "unproven"},
            "walk_forward": {"out_sample": {"runs": {"worst_case": {"stats": {"sum_pct": -5.0}}}}},
            "runs": {"worst_case": {"stats": {"sum_pct": -5.0, "n": 100}},
                     "best_case": {"stats": {"sum_pct": -5.0}}},
        }
        assert rank_key(poor_book) > rank_key(good_book)

    def test_render_states_the_rule_before_the_numbers(self):
        from research.verify.compare import render_comparison

        def res(name, outcome, ci):
            return {
                "spec": {"name": name, "symbol": "TESTUSDT", "interval": "1h",
                         "venue": "futures", "fee_rate": 0.0004, "fill_model": "taker",
                         "funding": False},
                "window": {"from": "2023-01-01 00:00", "to": "2023-06-01 00:00", "bars": 3600},
                "verdict": {"outcome": outcome, "reason": f"{name} 的理由", "checks": []},
                "runs": {"worst_case": {"stats": {"sum_pct": 1.0, "n": 42}},
                         "best_case": {"stats": {"sum_pct": 1.0, "n": 42}}},
                "control": {"diff_ci": ci, "selectivity": "unproven", "diff_pct": 0.0},
                "walk_forward": {"out_sample": {"runs": {"worst_case": {"stats": {"sum_pct": -1.0}}}}},
            }

        md = render_comparison([res("alpha", "INCONCLUSIVE", [-0.1, 0.2]),
                                res("beta", "FALSIFIED", [-0.4, -0.1])])
        # rule section precedes the matrix
        assert md.index("排名规则") < md.index("对比矩阵")
        # the matrix lists both strategies, ranked by tier (alpha first)
        assert md.index("alpha") < md.index("beta")
        # each strategy's verdict reason is quoted, with provenance
        assert "alpha 的理由" in md and "beta 的理由" in md
        assert "TESTUSDT" in md and "3600 根" in md

    def test_end_to_end(self, tmp_path):
        from research.verify.__main__ import main

        a = self._spec_file(tmp_path, "cmp-alpha", 30)
        b = self._spec_file(tmp_path, "cmp-beta", 45)
        candles = self._candles_file(tmp_path)
        out = tmp_path / "cmp.md"
        rc = main(["--compare", str(a), str(b),
                   "--candles", str(candles), "--out", str(out), "--json"])
        assert rc == 0
        md = out.read_text(encoding="utf-8")
        assert "多策略对比" in md and "cmp-alpha" in md and "cmp-beta" in md
        # --json in compare mode is a LIST (one entry per strategy); single
        # mode stays a dict -- the two must never be confused downstream
        payload = json.loads((tmp_path / "cmp.json").read_text(encoding="utf-8"))
        assert isinstance(payload, list) and len(payload) == 2

    def test_single_spec_json_stays_a_dict(self, tmp_path):
        from research.verify.__main__ import main

        a = self._spec_file(tmp_path, "solo", 30)
        candles = self._candles_file(tmp_path)
        out = tmp_path / "solo.md"
        rc = main(["--spec", str(a), "--candles", str(candles),
                   "--out", str(out), "--json"])
        assert rc == 0
        payload = json.loads((tmp_path / "solo.json").read_text(encoding="utf-8"))
        assert isinstance(payload, dict) and "verdict" in payload


class TestHtmlReport:
    """The HTML renderer: self-contained, every markdown section present,
    charts drawn from real numbers, and zero drift from the markdown text."""

    def _result(self):
        candles = make_candles(1500, seed=11)
        spec = Spec.from_dict(BASE_SPEC)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        result = verify(spec, candles, entries)
        # the CLI injects repro; a direct verify() call does not. Add a
        # realistic one so the report's section 9 is exercised too.
        result["repro"] = {"command": "python -m research.verify --spec s.json",
                           "spec": "s.json", "candles": "c.csv", "signals": "rule:rsi_below"}
        return result

    def test_structure_and_all_sections(self):
        from research.verify.html_report import render_html

        html = render_html(self._result())
        assert html.startswith("<!DOCTYPE html>")
        assert 'class="badge' in html
        for section in ("1. Path-aware 回放", "2. 成本敏感性", "3. 障碍可达性",
                        "4. 随机入场对照组", "5. Walk-forward", "6. 参数敏感性网格",
                        "7. 事前证伪规则判定", "8. 诊断：死因与处方",
                        "9. 未建模事项", "10. 复现命令"):
            assert section in html or section.replace("## ", "") in html, section

    def test_self_contained_no_external_resources(self):
        from research.verify.html_report import render_html

        html = render_html(self._result())
        # no stylesheets, scripts, fonts, or images fetched from elsewhere
        for bad in ("<script", "<link", "@import", "src=\"http", "href=\"http://fonts"):
            assert bad not in html, bad
        # the only external hrefs are the footer's two deliberate links
        import re

        hrefs = re.findall(r'href="(http[^"]+)"', html)
        assert set(hrefs) <= {
            "https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/",
            "https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2",
        }

    def test_charts_are_drawn_from_real_numbers(self):
        from research.verify.html_report import render_html

        html = render_html(self._result())
        # equity curves: two polylines (worst + best), each with points
        assert html.count("<polyline") >= 2
        assert "points=" in html
        # cost bars: one rect per fee multiple
        assert html.count("<rect") >= 3
        # grid heatmap: cells carry their net% as text, not colour alone
        assert "%</text>" in html

    def test_html_escaping(self):
        from research.verify.html_report import render_html

        result = self._result()
        result["spec"]["name"] = "<script>alert(1)</script>"
        html = render_html(result)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_no_drift_from_markdown_disclosures(self):
        """The markdown and HTML renderers share unmodeled_notes(); if that
        ever regresses to two hardcoded lists, this test fails."""
        from research.verify import report
        from research.verify.html_report import render_html

        html = render_html(self._result())
        for note in report.unmodeled_notes(self._result()):
            assert note in html

    def test_comparison_page(self):
        from research.verify.compare import render_comparison
        from research.verify.html_report import render_comparison_html

        r1 = self._result()
        r2 = self._result()
        r2["spec"]["name"] = "second-strategy"
        r2["verdict"]["outcome"] = "NOT_FALSIFIED"
        page = render_comparison_html([r1, r2])
        assert page.index("排名规则") < page.index("对比矩阵")
        assert page.count("<details") == 2
        assert "second-strategy" in page
        # and the markdown comparison still renders (both formats, same data)
        md = render_comparison([r1, r2])
        assert "second-strategy" in md and "排名规则" in md

    def test_out_html_extension_selects_html(self, tmp_path):
        from research.verify.__main__ import main

        a = tmp_path / "s.json"
        a.write_text(json.dumps(BASE_SPEC), encoding="utf-8")
        candles = tmp_path / "c.csv"
        write_csv(candles, make_candles(1500, seed=11), ["t", "o", "h", "l", "c", "v"])
        out = tmp_path / "r.html"
        rc = main(["--spec", str(a), "--candles", str(candles), "--out", str(out)])
        assert rc == 0
        text = out.read_text(encoding="utf-8")
        assert text.startswith("<!DOCTYPE html>")
        assert "<svg" in text


class TestSpecCarriedData:
    """A spec that carries its own candles/funding files travels as a folder
    and replays identically on a machine with no market cache."""

    def test_spec_candles_path_resolves_against_spec_dir(self, tmp_path):
        from research.verify.__main__ import main

        d = dict(BASE_SPEC)
        d["funding"] = False
        d["candles_path"] = "own_candles.csv"
        spec_p = tmp_path / "spec.json"
        spec_p.write_text(json.dumps(d), encoding="utf-8")
        write_csv(tmp_path / "own_candles.csv", make_candles(1200, seed=5), ["t", "o", "h", "l", "c", "v"])
        out = tmp_path / "r.md"
        rc = main(["--spec", str(spec_p), "--out", str(out)])
        assert rc == 0
        text = out.read_text(encoding="utf-8")
        assert "own_candles.csv（spec 自带，相对 spec 目录解析）" in text

    def test_spec_funding_path_is_applied(self, tmp_path):
        import csv

        from research.verify.__main__ import main

        d = dict(BASE_SPEC)
        d["venue"] = "futures"
        d["candles_path"] = "own_candles.csv"
        d["funding"] = True
        d["funding_path"] = "own_funding.csv"
        spec_p = tmp_path / "spec.json"
        spec_p.write_text(json.dumps(d), encoding="utf-8")
        write_csv(tmp_path / "own_candles.csv", make_candles(1200, seed=5), ["t", "o", "h", "l", "c", "v"])
        candles = make_candles(1200, seed=5)
        with (tmp_path / "own_funding.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["t", "rate"])
            for c in candles[::8]:
                w.writerow([c["t"], 0.0001])
        out = tmp_path / "r.md"
        rc = main(["--spec", str(spec_p), "--out", str(out), "--json"])
        assert rc == 0
        payload = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
        assert payload["funding_applied"] is True
        text = out.read_text(encoding="utf-8")
        assert "own_funding.csv（spec 自带资金费序列）" in text

    def test_funding_csv_bad_row_is_an_error(self, tmp_path):
        from research.verify import data

        p = tmp_path / "bad.csv"
        p.write_text("t,rate\n1,0.0001\nnot-a-number,0.0002\n", encoding="utf-8")
        with pytest.raises(data.CandleError):
            data.load_funding_csv(p)


class TestDiagnosis:
    """The diagnosis layer: cause taxonomy, priority order, and prescriptions
    that quote the customer's own numbers."""

    @staticmethod
    def _result(**over):
        base = {
            "verdict": {"outcome": "FALSIFIED", "reason": "", "checks": []},
            "runs": {"worst_case": {"stats": {"n": 100, "sum_pct": -5.0, "mean_pct": -0.05}},
                     "best_case": {"stats": {"n": 100, "sum_pct": -5.0, "mean_pct": -0.05}}},
            "control": {"diff_pct": -0.30, "diff_ci": [-0.50, -0.10], "selectivity": "refuted"},
            "cost_sensitivity": [{"fee_multiple": 1, "fee_bps": 8, "net_sum_pct": -5.0},
                                 {"fee_multiple": 2, "fee_bps": 16, "net_sum_pct": -12.0}],
            "walk_forward": {"in_sample": {"runs": {"worst_case": {"stats": {"sum_pct": 10.0}}}},
                             "out_sample": {"runs": {"worst_case": {"stats": {"sum_pct": 8.0}}}}},
            "barrier_reach": {"reached_tp_pct": 60.0},
        }
        base.update(over)
        return base

    def test_real_cases_classify_as_expected(self):
        """The published cases must classify the way their narratives say --
        this is the anti-drift guard for the whole taxonomy."""
        from research.verify.diagnosis import diagnose

        expected = {
            "docs/verify-sample-report.json": "randomness",
            "docs/cases/case1_rsi_dip_btc.json": "randomness",
            "docs/cases/case2_rsi_breakout_btc.json": "randomness",
            "docs/cases/case3_rsi_dip_eth.json": "randomness",
            "docs/cases/case4_rsi_dip_btc_maker.json": "randomness",
            "docs/cases/external-cascade-fader.json": "insufficient",
            "docs/cases/external-freqbot-fundingrate.json": "randomness",
            "docs/cases/external-grid-freqbot.json": "randomness",
        }
        import json

        for path, cause in expected.items():
            d = json.load(open(path, encoding="utf-8"))
            assert diagnose(d)["primary"] == cause, path

    def test_inconclusive_is_insufficient_not_a_death(self):
        from research.verify.diagnosis import diagnose

        d = diagnose(self._result(verdict={"outcome": "INCONCLUSIVE", "reason": "", "checks": []}))
        assert d["primary"] == "insufficient"
        assert any("样本" in p for p in d["prescriptions"])

    def test_refuted_is_randomness_with_negative_ci_evidence(self):
        from research.verify.diagnosis import diagnose

        d = diagnose(self._result())
        assert d["primary"] == "randomness"
        assert any("整体为负" in e for e in d["evidence"])

    def test_profitable_but_unproven_names_beta(self):
        from research.verify.diagnosis import diagnose

        r = self._result()
        r["control"] = {"diff_pct": 0.03, "diff_ci": [-0.27, 0.35], "selectivity": "unproven"}
        r["runs"]["worst_case"]["stats"]["sum_pct"] = 34.35
        d = diagnose(r)
        assert d["primary"] == "randomness"
        assert any("beta" in e for e in d["evidence"]), d["evidence"]
        assert any("beta" in p for p in d["prescriptions"])

    def test_proven_edge_killed_by_doubled_fees_is_cost(self):
        from research.verify.diagnosis import diagnose

        r = self._result()
        r["control"] = {"diff_pct": 0.40, "diff_ci": [0.15, 0.46], "selectivity": "proven"}
        r["runs"]["worst_case"]["stats"]["sum_pct"] = 20.0
        r["cost_sensitivity"] = [{"fee_multiple": 1, "fee_bps": 8, "net_sum_pct": 20.0},
                                 {"fee_multiple": 2, "fee_bps": 16, "net_sum_pct": -3.0}]
        d = diagnose(r)
        assert d["primary"] == "cost"
        assert any("maker" in p for p in d["prescriptions"])

    def test_losing_at_1x_without_selectivity_is_edge_absent(self):
        from research.verify.diagnosis import diagnose

        r = self._result(control=None)
        d = diagnose(r)
        assert d["primary"] == "edge_absent"

    def test_proven_but_collapses_out_of_sample_is_overfitting(self):
        from research.verify.diagnosis import diagnose

        r = self._result()
        r["control"] = {"diff_pct": 0.40, "diff_ci": [0.15, 0.46], "selectivity": "proven"}
        r["runs"]["worst_case"]["stats"]["sum_pct"] = 30.0
        r["cost_sensitivity"] = [{"fee_multiple": 1, "fee_bps": 8, "net_sum_pct": 30.0},
                                 {"fee_multiple": 2, "fee_bps": 16, "net_sum_pct": 28.0}]
        r["walk_forward"] = {"in_sample": {"runs": {"worst_case": {"stats": {"sum_pct": 30.0}}}},
                             "out_sample": {"runs": {"worst_case": {"stats": {"sum_pct": -8.0}}}}}
        d = diagnose(r)
        assert d["primary"] == "overfitting"
        assert any("砍自由度" in p for p in d["prescriptions"])

    def test_take_profit_never_reached_is_barrier(self):
        from research.verify.diagnosis import diagnose

        r = self._result()
        r["control"] = {"diff_pct": 0.40, "diff_ci": [0.15, 0.46], "selectivity": "proven"}
        r["runs"]["worst_case"]["stats"]["sum_pct"] = 30.0
        r["cost_sensitivity"] = [{"fee_multiple": 1, "fee_bps": 8, "net_sum_pct": 30.0},
                                 {"fee_multiple": 2, "fee_bps": 16, "net_sum_pct": 28.0}]
        r["walk_forward"] = None
        r["barrier_reach"] = {"reached_tp_pct": 2.0}
        d = diagnose(r)
        assert d["primary"] == "barrier"
        assert any("ATR" in p for p in d["prescriptions"])

    def test_survived_carries_the_absence_of_disproof_caveat(self):
        from research.verify.diagnosis import diagnose

        r = self._result(verdict={"outcome": "NOT_FALSIFIED", "reason": "", "checks": []},
                         control=None)
        d = diagnose(r)
        assert d["primary"] == "survived"
        assert any("未被证伪" in p for p in d["prescriptions"])

    def test_randomness_outranks_cost_when_both_fire(self):
        """Priority is part of the contract: an entry that cannot beat coin
        flips is the more fundamental failure, even if fees also hurt."""
        from research.verify.diagnosis import diagnose

        r = self._result()
        r["control"] = {"diff_pct": 0.03, "diff_ci": [-0.27, 0.35], "selectivity": "unproven"}
        r["runs"]["worst_case"]["stats"]["sum_pct"] = 34.35
        r["cost_sensitivity"] = [{"fee_multiple": 1, "fee_bps": 8, "net_sum_pct": 34.35},
                                 {"fee_multiple": 2, "fee_bps": 16, "net_sum_pct": -96.69}]
        d = diagnose(r)
        assert d["primary"] == "randomness"
        assert "cost" in d["tags"]
        assert any("toll" in p for p in d["prescriptions"])

    def test_diagnosis_is_in_the_verify_output(self):
        candles = make_candles(1500, seed=11)
        spec = Spec.from_dict(BASE_SPEC)
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=35)
        result = verify(spec, candles, entries)
        dx = result["diagnosis"]
        assert dx["primary"] in dict((c, l) for c, l in
                                     __import__("research.verify.diagnosis", fromlist=["CAUSES"]).CAUSES)
        assert dx["primary_label"] and isinstance(dx["prescriptions"], list)


def _exit_rule():
    from research.audit.pathsim import Exit

    return Exit(take_profit=0.03, stop_loss=-0.08, timeout_bars=12)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class TestCLI:
    def test_end_to_end_on_csv(self, tmp_path):
        candles = make_candles(1500, seed=11)
        kpath = write_csv(tmp_path / "k.csv", [
            {"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in candles
        ], ["t", "o", "h", "l", "c"])
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(BASE_SPEC), encoding="utf-8")
        out = tmp_path / "r.md"
        rc = main(["--spec", str(spec_path), "--candles", str(kpath), "--out", str(out), "--json"])
        assert rc == 0
        text = out.read_text(encoding="utf-8")
        assert "证伪报告" in text and "裁决" in text
        assert "## 4. 随机入场对照组" in text
        assert (tmp_path / "r.json").is_file()
        payload = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
        assert payload["verdict"]["outcome"] in ("FALSIFIED", "NOT_FALSIFIED", "INCONCLUSIVE")

    def test_cli_spec_with_walk_forward_renders_both_segments(self, tmp_path):
        candles = make_candles(1200, seed=21)
        kpath = write_csv(tmp_path / "k.csv", [
            {"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in candles
        ], ["t", "o", "h", "l", "c"])
        d = dict(BASE_SPEC)
        d["walk_forward"] = {"folds": 4}
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(d), encoding="utf-8")
        out = tmp_path / "r.md"
        rc = main(["--spec", str(spec_path), "--candles", str(kpath), "--out", str(out), "--json"])
        assert rc == 0
        text = out.read_text(encoding="utf-8")
        assert "## 5. Walk-forward（样本内 / 样本外）" in text
        payload = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
        assert payload["walk_forward"]["folds"] == 4
        assert payload["walk_forward"]["boundary_bar"] == 900

    def test_cli_spec_with_param_grid_renders_heatmap(self, tmp_path):
        candles = make_candles(1200, seed=21)
        kpath = write_csv(tmp_path / "k.csv", [
            {"t": c["t"], "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in candles
        ], ["t", "o", "h", "l", "c"])
        d = dict(BASE_SPEC)
        d["param_grid"] = {"axes": {"take_profit": [0.02, 0.03, 0.04],
                                    "stop_loss": [-0.06, -0.08, -0.10]}}
        spec_path = tmp_path / "spec.json"
        spec_path.write_text(json.dumps(d), encoding="utf-8")
        out = tmp_path / "r.md"
        rc = main(["--spec", str(spec_path), "--candles", str(kpath), "--out", str(out), "--json"])
        assert rc == 0
        text = out.read_text(encoding="utf-8")
        assert "## 6. 参数敏感性网格" in text
        payload = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
        assert payload["param_grid"]["scopes"][0]["n_cells"] == 9

    def test_bad_spec_exits_2_with_message(self, tmp_path, capsys):
        spec_path = tmp_path / "bad.json"
        spec_path.write_text('{"name": "x"}', encoding="utf-8")
        rc = main(["--spec", str(spec_path), "--candles", "nope.csv"])
        assert rc == 2
        assert "spec" in capsys.readouterr().err

    def test_missing_candles_exits_2(self, tmp_path):
        spec_path = tmp_path / "s.json"
        spec_path.write_text(json.dumps(BASE_SPEC), encoding="utf-8")
        rc = main(["--spec", str(spec_path), "--candles", str(tmp_path / "absent.csv")])
        assert rc == 2

    def test_no_spec_and_no_demo_is_usage_error(self):
        with pytest.raises(SystemExit):
            main([])

    def test_bundled_offline_demo_runs_without_cache(self, tmp_path):
        """The sales funnel: a fresh clone has no market cache (sqlite is
        gitignored), so --demo must work from the committed CSV bundle alone.
        If this breaks, every visitor who clones the repo hits a dead end."""
        from research.verify.__main__ import BUNDLED_CANDLES, BUNDLED_SPEC

        assert BUNDLED_CANDLES.is_file(), "演示 K 线 CSV 缺失"
        assert BUNDLED_SPEC.is_file(), "离线演示 spec 缺失"
        out = tmp_path / "offline.md"
        rc = main(["--demo", "--market", str(tmp_path / "absent.sqlite"), "--out", str(out)])
        assert rc == 0
        text = out.read_text(encoding="utf-8")
        assert "裁决" in text and "FALSIFIED" in text or "NOT_FALSIFIED" in text
        assert "不含资金费" in text  # the bundle ships no funding series; disclosed
        # the bundled run also exercises the parameter grid (seeded bundle,
        # deterministic): in-sample grid has no positive cell, oos has all
        assert "## 6. 参数敏感性网格" in text
        assert "全部 9 格均为负" in text
        assert "9 格净收益为正（100%）" in text
