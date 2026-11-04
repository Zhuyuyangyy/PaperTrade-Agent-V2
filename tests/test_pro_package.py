"""The Pro package's engine must be the same engine.

Open-core only works if the copy is faithful: the Pro package's engine/ is
verified equivalent to research/verify on identical inputs, the ZIP
round-trips, and the installer lands a working skill (demo runs, contract
lock works) in a bare project directory.
"""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRO = ROOT / "skill" / "strategy-falsifier-pro"
PY = sys.executable


def test_pro_engine_matches_inrepo_engine(tmp_path: Path):
    spec = ROOT / "research" / "verify" / "samples" / "demo_spec_csv.json"
    candles = ROOT / "research" / "verify" / "samples" / "demo_candles.csv"
    repo_out = tmp_path / "repo.json"
    r = subprocess.run([PY, "-m", "research.verify", "--spec", str(spec),
                        "--candles", str(candles), "--out", str(tmp_path / "repo.md"),
                        "--json"], capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr

    pro_out = tmp_path / "pro.json"
    r = subprocess.run([PY, "-m", "engine", "--spec", str(spec),
                        "--candles", str(candles), "--out", str(tmp_path / "pro.md"),
                        "--json"], capture_output=True, text=True, cwd=str(PRO))
    assert r.returncode == 0, r.stderr

    a = json.loads((tmp_path / "repo.json").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "pro.json").read_text(encoding="utf-8"))
    a.pop("repro", None)
    b.pop("repro", None)
    assert a == b, "the Pro engine diverged from the in-repo engine"


def test_pro_zip_round_trips_and_installs(tmp_path: Path):
    zpath = PRO / "papertrade-falsifier-pro-v1.0.0.zip"
    assert zpath.is_file(), "run build_pro.py first"
    with zipfile.ZipFile(zpath) as z:
        assert z.testzip() is None
        z.extractall(tmp_path / "pkg")
    project = tmp_path / "project"
    project.mkdir()
    r = subprocess.run([PY, "install_skill.py", str(project)],
                       capture_output=True, text=True, cwd=str(tmp_path / "pkg"))
    assert r.returncode == 0, r.stderr
    installed = project / ".claude" / "skills" / "papertrade-falsifier-pro"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "engine" / "cli.py").is_file()

    # the installed skill's first promised command must work, bare
    r = subprocess.run([PY, "-m", "engine", "--demo", "--out", "demo.md"],
                       capture_output=True, text=True, cwd=str(installed))
    assert r.returncode == 0, r.stderr
    assert "裁决" in (installed / "demo.md").read_text(encoding="utf-8")


def test_pro_contract_lock_end_to_end(tmp_path: Path):
    """The Pro headline feature, inside the installed package."""
    zpath = PRO / "papertrade-falsifier-pro-v1.0.0.zip"
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp_path / "pkg")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run([PY, "install_skill.py", str(project)],
                   capture_output=True, text=True, cwd=str(tmp_path / "pkg"))
    skill = project / ".claude" / "skills" / "papertrade-falsifier-pro"
    spec = skill / "engine" / "samples" / "demo_spec_csv.json"

    r = subprocess.run([PY, "-m", "engine", "--lock-contract", "--spec", str(spec)],
                       capture_output=True, text=True, cwd=str(skill))
    assert r.returncode == 0, r.stderr
    lock = spec.with_suffix(".verdict_lock.json")
    assert lock.is_file()

    # tamper: relax the declared rule, as a pressured iteration would
    doc = json.loads(spec.read_text(encoding="utf-8"))
    doc["falsify_if"]["min_fills"] = 1
    spec.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([PY, "-m", "engine", "--spec", str(spec), "--lock", str(lock),
                        "--candles", str(skill / "engine" / "samples" / "demo_candles.csv")],
                       capture_output=True, text=True, cwd=str(skill))
    assert r.returncode == 99
    assert "CONTRACT_TAMPERING_DETECTED" in r.stderr
