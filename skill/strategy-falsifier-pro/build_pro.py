"""Build the Pro skill package (the $29 commercial bundle).

The MIT engine is the repository's research/verify package. The Pro package
ships the SAME engine (open-core: the algorithm stays public, the package
adds the workflow) under the import path `engine`, plus the Pro-only assets:
SKILL.md iron rules, outcome playbooks, contract presets, the installer.

The engine copy is verified equivalent to the in-repo engine after copying:
tests/test_pro_package.py runs both on identical inputs and diffs the JSON.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "research" / "verify"
PRO = ROOT / "skill" / "strategy-falsifier-pro"
ENGINE = PRO / "engine"
OUT_ZIP = PRO / "papertrade-falsifier-pro-v1.0.0.zip"
SUMS = PRO / "SHA256SUMS.txt"

CLI_SHIM = '''"""Command-line entry for the Pro package.

The engine's real CLI lives in __main__.py; this shim exists so both
`python -m engine` and `python -m engine.cli` work from an installed skill.
"""
from __future__ import annotations

from .__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
'''

# The engine's modules import two things from the parent package
# (research.audit.pathsim / research.audit.binance). Copied flat into
# engine/, those become local modules -- the same rewrite the Agensi
# amalgamation does, kept here as data so it stays reviewable.
COMPAT = '''"""Compat shims for the flat engine package.

DataError is lifted verbatim from research/audit/binance.py (that module's
network code does not ship -- the engine is offline by construction).
"""
from __future__ import annotations


class DataError(RuntimeError):
    """Raised when Binance could not be read after retrying."""
'''

REWRITES = [
    ("from ..audit.pathsim import", "from ._pathsim import"),
    ("from ..audit.binance import DataError", "from ._compat import DataError"),
]


def build() -> Path:
    if ENGINE.exists():
        shutil.rmtree(ENGINE)
    shutil.copytree(SRC, ENGINE,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # the demo bundle must travel with the package: --demo is the first
    # command a buyer runs, and it must work with no repo and no cache
    (ENGINE / "samples").mkdir(exist_ok=True)
    for f in ("demo_candles.csv", "demo_spec_csv.json"):
        src = SRC / "samples" / f
        if src.is_file():
            shutil.copy2(src, ENGINE / "samples" / f)
    (ENGINE / "cli.py").write_text(CLI_SHIM, encoding="utf-8")
    (ENGINE / "_compat.py").write_text(COMPAT, encoding="utf-8")
    # pathsim travels with the engine (it is the simulator, not network code)
    shutil.copy2(ROOT / "research" / "audit" / "pathsim.py", ENGINE / "_pathsim.py")
    for py in ENGINE.glob("*.py"):
        s = py.read_text(encoding="utf-8")
        for old, new in REWRITES:
            s = s.replace(old, new)
        py.write_text(s, encoding="utf-8")

    # the engine must still compile and run as the `engine` package
    r = subprocess.run([sys.executable, "-c",
                        "import engine.cli, engine.contract_lock, engine.csv_auditor, "
                        "engine.diagnosis, engine.html_report; print('engine imports OK')"],
                       cwd=str(PRO), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"Pro engine import failed:\n{r.stderr}")

    files = [PRO / "SKILL.md", PRO / "install_skill.py", PRO / "README.md",
             PRO / "engine", PRO / "playbooks", PRO / "presets", PRO / "references"]
    for f in files:
        if not f.exists():
            raise SystemExit(f"missing package asset: {f}")

    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if f.is_dir():
                for p in sorted(f.rglob("*")):
                    if p.is_file() and "__pycache__" not in p.parts:
                        z.write(p, p.relative_to(PRO).as_posix())
            else:
                z.write(f, f.name)
    lines = []
    for f in [OUT_ZIP] + files:
        if f.is_file():
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            lines.append(f"{h}  {f.relative_to(PRO).as_posix()}")
    SUMS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return OUT_ZIP


if __name__ == "__main__":
    p = build()
    print(f"built {p} ({p.stat().st_size} bytes)")
    with zipfile.ZipFile(p) as z:
        print("zip OK:", z.testzip() is None, "| entries:", len(z.namelist()))
