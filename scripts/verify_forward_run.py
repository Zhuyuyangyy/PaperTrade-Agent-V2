#!/usr/bin/env python
"""Forward Test 验收：校验真实运行是否达标，而不是只看 rounds。

用法：
    python scripts/verify_forward_run.py --root data/openalpha_forward_v1 \
        --rounds 73 --hours 72

退出码：0 = PASS，1 = FAIL（JSON 里会列出每一项失败原因）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.runtime.verify_run import verify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--rounds", type=int, default=73)
    parser.add_argument("--hours", type=float, default=72.0)
    args = parser.parse_args()
    out = verify(Path(args.root), expected_successful=args.rounds,
                 expected_hours=args.hours)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())