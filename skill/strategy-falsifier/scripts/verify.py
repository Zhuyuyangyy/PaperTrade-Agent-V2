#!/usr/bin/env python
"""strategy-falsifier 一层包装：接收 spec 路径，调用引擎 python -m research.verify。

用法（在仓库任意目录下执行均可，脚本自动定位仓库根）：
    verify.py --demo
    verify.py path/to/spec.json [--candles klines.csv] [--out report.md] [--json]
    verify.py --spec path/to/spec.json --signals signals.csv

未识别的参数原样转发给引擎。解释器优先用 $STRATEGY_FALSIFIER_PYTHON，
否则用当前解释器（sys.executable）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 本文件位于 <repo>/skill/strategy-falsifier/scripts/verify.py
REPO_ROOT = Path(__file__).resolve().parents[3]
ENGINE_PKG = "research.verify"


def _interpreter() -> str:
    env = os.environ.get("STRATEGY_FALSIFIER_PYTHON")
    if env:
        return env
    if sys.executable:
        return sys.executable
    return "python"


def main(argv: list[str]) -> int:
    if not (REPO_ROOT / "research" / "verify" / "__main__.py").is_file():
        print(f"[包装错误] 在 {REPO_ROOT} 找不到引擎 research/verify/——"
              f"本脚本必须位于 <repo>/skill/strategy-falsifier/scripts/ 下", file=sys.stderr)
        return 2

    args = list(argv)
    has_spec = any(a == "--spec" or a.startswith("--spec=") for a in args)
    if not has_spec:
        # 第一个不以 "-" 开头的参数视为 spec 路径，补成 --spec
        for i, a in enumerate(args):
            if not a.startswith("-"):
                args.insert(i, "--spec")
                break
        else:
            if not any(a in ("--demo", "--list-market") for a in args):
                print(__doc__.strip(), file=sys.stderr)
                return 2

    cmd = [_interpreter(), "-m", ENGINE_PKG, *args]
    print(f"[包装] {' '.join(cmd)}（cwd={REPO_ROOT}）", file=sys.stderr)
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
