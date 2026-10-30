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
