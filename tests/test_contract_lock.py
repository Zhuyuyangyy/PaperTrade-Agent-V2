"""The contract lock: pre-declared rules may not move mid-iteration.

An agent (or a human) iterating on a strategy has a structural temptation to
quietly relax the rule that just fired. The lock turns that into exit 99 with
a field-level diff -- the run is void as evidence, whatever its numbers say.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _spec_doc(**over) -> dict:
    base = {
        "name": "lock-test", "symbol": "TESTUSDT", "interval": "1h", "venue": "futures",
        "fee_rate": 0.0004, "funding": False,
        "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 12},
        "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30},
        "falsify_if": {"worst_case_net_negative": True,
                       "net_negative_at_fee_multiple": 2,
                       "control_lift_ci_includes_zero": True,
                       "min_fills": 30},
    }
    base.update(over)
    return base


def test_hash_is_stable_and_canonical(tmp_path: Path):
    from research.verify.contract_lock import contract_hash

    a, b = _spec_doc(), _spec_doc()
    assert contract_hash(a) == contract_hash(b)
    # key order must not matter (canonical JSON)
    shuffled = dict(reversed(list(_spec_doc().items())))
    assert contract_hash(shuffled) == contract_hash(a)


def test_identity_is_covered(tmp_path: Path):
    """A lock from one strategy cannot launder another strategy's rules."""
    from research.verify.contract_lock import check_lock, contract_hash, write_lock

    doc = _spec_doc()
    lock = write_lock(doc, tmp_path / "l.json")
    ok, _ = check_lock(doc, lock)
    assert ok
    other = _spec_doc(name="different-strategy")
    ok, detail = check_lock(other, lock)
    assert not ok and "name" in detail


def test_rules_are_covered_but_strategy_is_not(tmp_path: Path):
    """The lock protects the judge, not the defendant: exit rules may move,
    falsify_if may not."""
    from research.verify.contract_lock import check_lock, write_lock

    doc = _spec_doc()
    lock = write_lock(doc, tmp_path / "l.json")

    tuned = _spec_doc()
    tuned["exit"] = {"take_profit": 0.05, "stop_loss": -0.12, "timeout_bards": 12}
    ok, _ = check_lock(tuned, lock)
    assert ok, "tuning the strategy under test must stay legal"

    cheated = _spec_doc()
    cheated["falsify_if"]["min_fills"] = 1
    ok, detail = check_lock(cheated, lock)
    assert not ok
    assert "falsify_if" in detail and "min_fills" in detail


def test_cli_lock_and_tamper_exit_codes(tmp_path: Path):
    """End-to-end through the real CLI: lock -> run -> tamper -> exit 99."""
    spec = _spec_doc()
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    lock = tmp_path / "s.verdict_lock.json"

    r = subprocess.run([PY, "-m", "research.verify", "--lock-contract", "--spec", str(sp)],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert lock.is_file()

    # a real audit under a valid lock runs (candles generated locally)
    import random

    rows = []
    px, t0 = 100.0, 1_700_000_000_000
    rng = random.Random(3)
    for i in range(800):
        px *= 1.0 + rng.gauss(0.0, 0.01)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.004))
        rows.append({"t": t0 + i * 3_600_000, "o": o, "h": max(o, c) * 1.003,
                     "l": min(o, c) * 0.997, "c": c, "v": 1000.0})
    import csv

    with (tmp_path / "candles.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["t", "o", "h", "l", "c", "v"])
        w.writeheader()
        w.writerows(rows)
    spec["candles_path"] = "candles.csv"
    sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run([PY, "-m", "research.verify", "--spec", str(sp), "--lock", str(lock),
                        "--out", str(tmp_path / "r.md")],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert "契约校验通过" in r.stderr

    # now relax the declared rule, exactly as a pressured iteration would
    spec["falsify_if"]["min_fills"] = 1
    sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([PY, "-m", "research.verify", "--spec", str(sp), "--lock", str(lock),
                        "--out", str(tmp_path / "r2.md")],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 99
    assert "CONTRACT_TAMPERING_DETECTED" in r.stderr
    assert not (tmp_path / "r2.md").is_file(), "a void run must not produce a report"
