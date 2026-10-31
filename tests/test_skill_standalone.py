"""The vendored skill engine must be a COPY, not a fork.

Three guards:
  1. no cross-package imports survive the vendoring (the ZIP has no repo)
  2. the vendored engine, run from a bare directory with no repo context,
     produces the same verdict and the same numbers as the in-repo engine
     on identical inputs
  3. the wrapper prefers the vendored copy, so a ZIP recipient never depends
     on the repository

If these fail after an engine change, someone forgot scripts/sync_engine.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "strategy-falsifier"
ENGINE = SKILL / "engine"
PY = sys.executable


def _run(cmd: list[str], cwd: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=300)


def test_vendored_copy_exists_and_is_import_clean():
    assert ENGINE.is_dir(), "engine/ 缺失：先跑 skill/strategy-falsifier/scripts/sync_engine.py"
    for f in ENGINE.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "..audit" not in text, f"{f.name} 仍跨包 import research.audit"
    assert (ENGINE / "pathsim.py").is_file()
    assert (SKILL / "samples" / "demo_candles.csv").is_file()


def test_vendored_engine_matches_inrepo_engine(tmp_path: Path):
    """Identical inputs -> identical verdicts and numbers, from a bare cwd."""
    spec = SKILL / "samples" / "demo_spec_csv.json"
    candles = SKILL / "samples" / "demo_candles.csv"
    assert spec.is_file() and candles.is_file()

    repo_out = tmp_path / "repo.md"
    r1 = _run([PY, "-m", "research.verify", "--spec", str(spec),
               "--candles", str(candles), "--out", str(repo_out), "--json"], cwd=ROOT)
    assert r1.returncode == 0, r1.stderr

    # standalone: a bare temp cwd, only the skill package on the path -- the
    # repository is NOT importable from here
    bare = tmp_path / "bare"
    bare.mkdir()
    standalone_out = bare / "standalone.md"
    r2 = _run([PY, "-m", "engine", "--spec", str(spec),
               "--candles", str(candles), "--out", str(standalone_out), "--json"],
              cwd=bare, env_extra={"PYTHONPATH": str(SKILL)})
    assert r2.returncode == 0, r2.stderr
    assert standalone_out.is_file()

    a = json.loads(repo_out.with_suffix(".json").read_text(encoding="utf-8"))
    b = json.loads(standalone_out.with_suffix(".json").read_text(encoding="utf-8"))
    a.pop("repro", None)
    b.pop("repro", None)
    assert a == b, "vendored 引擎与仓库引擎输出不一致——跑 scripts/sync_engine.py 同步"
    assert b["verdict"]["outcome"] in ("FALSIFIED", "NOT_FALSIFIED", "INCONCLUSIVE")


def test_wrapper_prefers_vendored_engine(tmp_path: Path):
    """A ZIP recipient runs scripts/verify.py with no repo around: it must
    work, and say it used the vendored engine."""
    bare = tmp_path / "zipuser"
    bare.mkdir()
    r = _run([PY, str(SKILL / "scripts" / "verify.py"), "--demo",
              "--out", str(bare / "r.md")], cwd=bare)
    assert r.returncode == 0, r.stderr
    assert "独立引擎" in r.stderr
    assert (bare / "r.md").is_file()
    text = (bare / "r.md").read_text(encoding="utf-8")
    assert "裁决" in text and "python -m engine" in text  # repro line names the module


def test_repo_mode_env_still_works():
    """Development escape hatch: STRATEGY_FALSIFIER_USE_REPO=1 uses the repo."""
    r = _run([PY, str(SKILL / "scripts" / "verify.py"), "--demo"],
             cwd=ROOT, env_extra={"STRATEGY_FALSIFIER_USE_REPO": "1"})
    assert r.returncode == 0, r.stderr
    assert "仓库引擎" in r.stderr


# --------------------------------------------------------------------------
# wrapper argument handling (bugs found by an external reviewer)
# --------------------------------------------------------------------------

def _write_compare_specs(tmp_path: Path) -> tuple[Path, Path]:
    """Two minimal rule specs the wrapper can run through --compare."""
    import csv as _csv

    candles = tmp_path / "klines.csv"
    rng_rows = []
    t0 = 1_700_000_000_000
    px = 100.0
    import random as _random

    rng = _random.Random(7)
    for i in range(1500):
        px *= 1.0 + rng.gauss(0.0004, 0.01)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.004))
        h = max(o, c) * 1.003
        l = min(o, c) * 0.997
        rng_rows.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": 1000.0})
    with candles.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=["t", "o", "h", "l", "c", "v"])
        w.writeheader()
        w.writerows(rng_rows)
    specs = []
    for name, threshold in (("cmp-alpha", 30), ("cmp-beta", 45)):
        d = {
            "name": name, "symbol": "TESTUSDT", "interval": "1h", "venue": "futures",
            "fee_rate": 0.0004, "funding": False,
            "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 12},
            "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": threshold},
            "control": {"enabled": True, "n_random": 100, "seed": 42},
            "fee_multiples": [1, 2, 5],
            "falsify_if": {"min_fills": 10},
        }
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(d), encoding="utf-8")
        specs.append(p)
    return specs[0], specs[1], candles


def test_wrapper_compare_mode(tmp_path: Path):
    """--compare through the wrapper used to get --spec inserted in front of
    the first spec path, colliding with the engine's own guard. Now it runs."""
    a, b, candles = _write_compare_specs(tmp_path)
    # relative paths on purpose: they must resolve against the CALLER's cwd
    # (here tmp_path itself, since every path below is relative to it)
    r = _run([PY, str(SKILL / "scripts" / "verify.py"),
              "--compare", "cmp-alpha.json", "cmp-beta.json",
              "--candles", "klines.csv", "--out", "cmp.md", "--json"],
             cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = tmp_path / "cmp.md"
    text = out.read_text(encoding="utf-8")
    assert "多策略对比" in text and "cmp-alpha" in text and "cmp-beta" in text
    payload = json.loads((tmp_path / "cmp.json").read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 2


def test_wrapper_positional_spec_still_works(tmp_path: Path):
    """The original convenience form must survive the --compare fix."""
    a, _, candles = _write_compare_specs(tmp_path)
    out = tmp_path / "solo.md"
    r = _run([PY, str(SKILL / "scripts" / "verify.py"),
              str(a), "--candles", str(candles), "--out", str(out)], cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "cmp-alpha" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# wrapper argument handling (bugs found by an external reviewer)

