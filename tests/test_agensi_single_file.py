"""The Agensi single-file engine must behave EXACTLY like the in-repo engine.

The Agensi runtime writes each script unchanged to a temporary file and runs
it there -- no sibling files, no package context. These tests reproduce that
constraint literally: the amalgamated verify.py is copied ALONE into a bare
directory and run, then its output is diffed against the in-repo engine's on
identical inputs.

Guards:
  1. the single file runs from a bare directory with zero siblings (demo path)
  2. identical inputs -> identical JSON (verdict + every number), single and
     compare modes, md and html outputs
  3. the demo is deterministic (same seed -> same numbers, twice)
  4. the amalgamation build is reproducible (rebuild -> byte-identical)
  5. SKILL.md frontmatter parses and carries the required keys

If the engine changes, rebuild with:
  python skill/strategy-falsifier/scripts/build_agensi.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENSI = ROOT / "skill" / "strategy-falsifier" / "agensi"
SINGLE = AGENSI / "scripts" / "verify.py"
BUILDER = ROOT / "skill" / "strategy-falsifier" / "scripts" / "build_agensi.py"
GRID_CASE = ROOT / "docs" / "cases" / "external-grid-freqbot"
PY = sys.executable


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600)


@pytest.fixture(scope="module")
def bare_dir(tmp_path_factory) -> Path:
    """A directory containing ONLY the single file -- the Agensi constraint."""
    d = tmp_path_factory.mktemp("agensi_bare")
    assert SINGLE.is_file(), "single-file engine missing: run scripts/build_agensi.py"
    (d / "verify.py").write_text(SINGLE.read_text(encoding="utf-8"), encoding="utf-8")
    return d


def test_demo_runs_from_a_bare_directory(bare_dir: Path):
    """No siblings, no repo, no cache: the built-in demo must still produce a
    full verdict on its embedded synthetic series."""
    out = bare_dir / "demo.md"
    r = _run([PY, "verify.py", "--demo", "--out", str(out), "--json"], cwd=bare_dir)
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert "裁决" in text
    assert "FALSIFIED" in text or "NOT_FALSIFIED" in text or "INCONCLUSIVE" in text
    # the demo must not claim data it does not have
    assert "_synthetic_demo_candles()" in text
    payload = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["verdict"]["outcome"] == "FALSIFIED"
    assert payload["runs"]["worst_case"]["stats"]["n"] >= 30  # demo passes its own min_fills


def test_demo_is_deterministic(bare_dir: Path):
    """Same seed -> same numbers. The only permitted difference between two
    runs is the generation timestamp in the header."""
    out1, out2 = bare_dir / "d1.md", bare_dir / "d2.md"
    assert _run([PY, "verify.py", "--demo", "--out", str(out1)], cwd=bare_dir).returncode == 0
    assert _run([PY, "verify.py", "--demo", "--out", str(out2)], cwd=bare_dir).returncode == 0

    def strip_volatile(text: str) -> str:
        # generation timestamp + the repro command echoes this run's own
        # output filename (d1.md vs d2.md) -- both are expected to differ
        return "\n".join(l for l in text.splitlines()
                         if "生成时间" not in l and "python -m engine" not in l)

    assert strip_volatile(out1.read_text(encoding="utf-8")) == strip_volatile(out2.read_text(encoding="utf-8"))


def _localised_case(dir_path: Path) -> tuple[Path, Path]:
    """The real grid case, copied with its own data next to the spec."""
    dir_path.mkdir(parents=True, exist_ok=True)
    spec = json.loads((GRID_CASE / "spec.json").read_text(encoding="utf-8"))
    spec["candles_path"] = "candles.csv"
    spec["signals"]["path"] = "signals.csv"
    spec["exit"]["exit_signals_path"] = "exit_signals.csv"
    sp = dir_path / "spec.json"
    sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    for f in ("candles.csv", "signals.csv", "exit_signals.csv"):
        (dir_path / f).write_bytes((GRID_CASE / f).read_bytes())
    return sp, dir_path


def test_single_file_matches_inrepo_engine(bare_dir: Path, tmp_path: Path):
    """Identical inputs -> identical JSON, verdict and every number."""
    spec, data_dir = _localised_case(tmp_path / "case")

    repo_out = tmp_path / "repo.md"
    r1 = _run([PY, "-m", "research.verify", "--spec", str(spec),
               "--out", str(repo_out), "--json"], cwd=ROOT)
    assert r1.returncode == 0, r1.stderr

    single_out = bare_dir / "single.md"
    r2 = _run([PY, "verify.py", "--spec", str(spec),
               "--out", str(single_out), "--json"], cwd=bare_dir)
    assert r2.returncode == 0, r2.stderr

    a = json.loads(repo_out.with_suffix(".json").read_text(encoding="utf-8"))
    b = json.loads(single_out.with_suffix(".json").read_text(encoding="utf-8"))
    a.pop("repro", None)
    b.pop("repro", None)
    assert a == b, "single-file engine diverged from the in-repo engine"
    # and the case's published numbers are what both engines produce
    assert b["verdict"]["outcome"] == "FALSIFIED"
    assert b["runs"]["worst_case"]["stats"]["sum_pct"] == pytest.approx(61.06, abs=0.01)


def test_single_file_compare_and_html(bare_dir: Path, tmp_path: Path):
    spec, _ = _localised_case(tmp_path / "cmp")
    spec_b = json.loads(spec.read_text(encoding="utf-8"))
    spec_b["name"] = "variant-b"
    spec_b["exit"]["timeout_bars"] = 72
    b_path = spec.parent / "spec_b.json"
    b_path.write_text(json.dumps(spec_b, ensure_ascii=False), encoding="utf-8")

    cmp_out = bare_dir / "cmp.md"
    r = _run([PY, "verify.py", "--compare", str(spec), str(b_path),
              "--out", str(cmp_out), "--json"], cwd=bare_dir)
    assert r.returncode == 0, r.stderr
    payload = json.loads(cmp_out.with_suffix(".json").read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 2
    assert "多策略对比" in cmp_out.read_text(encoding="utf-8")

    html_out = bare_dir / "r.html"
    r = _run([PY, "verify.py", "--spec", str(spec), "--out", str(html_out)], cwd=bare_dir)
    assert r.returncode == 0, r.stderr
    html = html_out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>") and "<svg" in html


def test_build_is_reproducible(tmp_path: Path):
    """Rebuilding the amalgamation from the same sources is byte-identical."""
    import hashlib

    before = hashlib.sha256(SINGLE.read_bytes()).hexdigest()
    r = _run([PY, str(BUILDER)], cwd=ROOT)
    assert r.returncode == 0, r.stderr
    after = hashlib.sha256(SINGLE.read_bytes()).hexdigest()
    assert before == after, "build_agensi.py is not deterministic"


def test_skill_md_frontmatter():
    text = (AGENSI / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    fm = text[4:end]
    for key in ("name:", "description:", "summary:", "version:"):
        assert key in fm, f"SKILL.md frontmatter missing {key}"
    # the description is what a buyer's agent reads first: it must state the
    # value proposition, not the mechanics
    desc = [l for l in fm.splitlines() if l.startswith("description:")][0]
    assert "falsif" in desc.lower() or "kill" in desc.lower()
    # no leftover placeholders
    for bad in ("your-", "TODO", "FIXME", "占位"):
        assert bad not in text, f"SKILL.md still contains {bad}"
