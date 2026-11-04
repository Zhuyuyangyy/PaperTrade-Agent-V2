#!/usr/bin/env python
"""strategy-falsifier 一层包装：接收 spec 路径，调用证伪引擎。

两种运行模式，自动选择：
  1. 独立模式（默认）：调用本包内 vendored 引擎（engine/）。ZIP 解压即用，
     不需要主仓库，不需要安装任何第三方依赖（引擎纯标准库）。
  2. 仓库模式：环境变量 STRATEGY_FALSIFIER_USE_REPO=1 时调用主仓库的
     research.verify（用于开发中对齐两边行为）。

用法（在包任意目录下执行均可）：
    verify.py --demo
    verify.py path/to/spec.json [--candles klines.csv] [--out report.md] [--json]
    verify.py --spec path/to/spec.json --signals signals.csv
    verify.py --compare a.json b.json [c.json ...] --out compare.md [--json]

未识别的参数原样转发给引擎。解释器优先用 $STRATEGY_FALSIFIER_PYTHON，
否则用当前解释器（sys.executable）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 本文件位于 <pkg>/skill/strategy-falsifier/scripts/verify.py
PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_ROOT.parents[1]


def _engine_target() -> tuple[str, Path]:
    """(python -m 的目标, 工作目录)。独立模式优先。"""
    if os.environ.get("STRATEGY_FALSIFIER_USE_REPO") == "1" \
            and (REPO_ROOT / "research" / "verify" / "__main__.py").is_file():
        return "research.verify", REPO_ROOT
    if (PKG_ROOT / "engine" / "__main__.py").is_file():
        return "engine", PKG_ROOT
    if (REPO_ROOT / "research" / "verify" / "__main__.py").is_file():
        return "research.verify", REPO_ROOT
    return "", PKG_ROOT


def _interpreter() -> str:
    env = os.environ.get("STRATEGY_FALSIFIER_PYTHON")
    if env:
        return env
    if sys.executable:
        return sys.executable
    return "python"


# Options that consume the next argv element. A positional spec path must
# never be confused with one of their values (--demo --out r.md used to break
# exactly there: r.md got --spec inserted in front of it).
VALUE_OPTS = ("--spec", "--candles", "--signals", "--market", "--out")
# Options that consume EVERY following non-option argv element. --compare is
# nargs="+" in the engine: its values are spec paths, and inserting --spec in
# front of the first one used to collide with the engine's own
# "--compare 与 --spec 不能同用" guard.
MULTI_VALUE_OPTS = ("--compare",)
# The ones whose values are filesystem paths the USER typed: they are relative
# to the caller's cwd, but the engine runs with cwd=PKG_ROOT, so the wrapper
# absolutises them before handing them over.
PATH_OPTS = ("--spec", "--candles", "--signals", "--market", "--out")
PATH_MULTI_OPTS = ("--compare",)


def _absolutise_paths(args: list[str]) -> list[str]:
    """Resolve path-valued options against the CALLER's cwd."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            key, _, val = a.partition("=")
            if key in PATH_OPTS and val:
                a = f"{key}={str(Path(val).expanduser().resolve())}"
            out.append(a)
            i += 1
            continue
        if a in PATH_OPTS and i + 1 < len(args):
            val = args[i + 1]
            out.append(a)
            out.append(str(Path(val).expanduser().resolve()))
            i += 2
            continue
        if a in PATH_MULTI_OPTS:
            out.append(a)
            i += 1
            while i < len(args) and not args[i].startswith("-"):
                out.append(str(Path(args[i]).expanduser().resolve()))
                i += 1
            continue
        out.append(a)
        i += 1
    return out


def _munge_args(args: list[str]) -> list[str]:
    """Insert --spec before the first POSITIONAL argument, if any.

    A positional is an argv element that is neither an option, nor the value
    of a value-taking option, nor one of --compare's spec paths. Returns args
    unchanged when a --spec (either form) is already present.
    """
    if any(a == "--spec" or a.startswith("--spec=") for a in args):
        return list(args)
    if any(a == "--compare" or a.startswith("--compare=") for a in args):
        return list(args)  # compare carries its own specs; nothing to insert
    out: list[str] = []
    inserted = False
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            out.append(a)
            i += 1
            continue
        if a in VALUE_OPTS:
            out.append(a)
            if i + 1 < len(args):
                out.append(args[i + 1])
                i += 2
            else:
                i += 1
            continue
        if a in MULTI_VALUE_OPTS:
            out.append(a)
            i += 1
            while i < len(args) and not args[i].startswith("-"):
                out.append(args[i])
                i += 1
            continue
        if not a.startswith("-") and not inserted:
            out.append("--spec")
            out.append(a)
            inserted = True
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def main(argv: list[str]) -> int:
    pkg, cwd = _engine_target()
    if not pkg:
        print(f"[包装错误] 在 {PKG_ROOT} 找不到引擎（engine/ 缺失，且不在仓库内）。"
              f"如果是仓库内使用，请从 skill/strategy-falsifier/ 目录运行，"
              f"或先执行 python scripts/sync_engine.py 生成本地引擎副本。",
              file=sys.stderr)
        return 2

    args = _absolutise_paths(_munge_args(list(argv)))
    has_entry = any(
        a in ("--demo", "--list-market", "--compare") or a.startswith("--compare=")
        or a == "--spec" or a.startswith("--spec=")
        for a in args
    )
    if not has_entry:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    cmd = [_interpreter(), "-m", pkg, *args]
    print(f"[包装] {' '.join(cmd)}（cwd={cwd}，{'独立引擎' if pkg == 'engine' else '仓库引擎'}）",
          file=sys.stderr)
    return subprocess.run(cmd, cwd=cwd).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
