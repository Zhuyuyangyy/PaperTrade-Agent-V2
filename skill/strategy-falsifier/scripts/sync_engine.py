#!/usr/bin/env python3
"""Vendor the falsification engine into this skill package.

The skill must run from a clean machine with no repository around it, so the
engine is COPIED here (not imported from the repo). This script is the single
source of truth for that copy -- run it after any change to research/verify/
or research/audit/pathsim.py, and commit the result.

    python scripts/sync_engine.py            # from the skill package root

What it does:
  1. copies research/verify/*.py -> engine/, rewriting the one cross-package
     import (`..audit.pathsim` -> `.pathsim`); data.py carries its own
     DataError so nothing else leaks in
  2. copies research/audit/pathsim.py -> engine/pathsim.py
  3. copies the demo bundle (samples/) next to the package

Drift control: tests/test_skill_standalone.py asserts the vendored copy
produces byte-identical verdicts to the in-repo engine on the demo spec.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]
ENGINE_SRC = REPO_ROOT / "research" / "verify"
PATHSIM_SRC = REPO_ROOT / "research" / "audit" / "pathsim.py"
ENGINE_DST = SKILL_ROOT / "engine"
SAMPLES_DST = SKILL_ROOT / "samples"

IMPORT_REWRITES = (
    ("from ..audit.pathsim import", "from .pathsim import"),
    ("from ..audit import pathsim", "from . import pathsim"),
)


def main() -> int:
    if not ENGINE_SRC.is_dir():
        print(f"找不到引擎源码 {ENGINE_SRC}（本脚本设计为在仓库内的 skill 包中运行）", file=sys.stderr)
        return 2
    ENGINE_DST.mkdir(exist_ok=True)
    copied = []
    for src in sorted(ENGINE_SRC.glob("*.py")):
        text = src.read_text(encoding="utf-8")
        for old, new in IMPORT_REWRITES:
            text = text.replace(old, new)
        dst = ENGINE_DST / src.name
        dst.write_text(text, encoding="utf-8")
        copied.append(src.name)
    shutil.copy2(PATHSIM_SRC, ENGINE_DST / "pathsim.py")
    copied.append("pathsim.py")

    SAMPLES_DST.mkdir(exist_ok=True)
    for name in ("demo_candles.csv", "demo_spec_csv.json", "demo_spec.json"):
        src = ENGINE_SRC / "samples" / name
        if src.is_file():
            shutil.copy2(src, SAMPLES_DST / name)
            copied.append(f"samples/{name}")

    # Sanity: no cross-package imports may survive in the vendored copy.
    leftovers = []
    for f in ENGINE_DST.glob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "..audit" in line:
                leftovers.append(f"{f.name}:{i}")
    if leftovers:
        print("!! vendored 副本仍存在跨包 import:", leftovers, file=sys.stderr)
        return 1
    print(f"同步完成：{len(copied)} 个文件 -> {ENGINE_DST.relative_to(REPO_ROOT)}/ + samples/")
    print("提示：改完 research/ 后重跑本脚本并提交；test_skill_standalone.py 会校验等价性。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
