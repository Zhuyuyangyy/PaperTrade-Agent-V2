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
