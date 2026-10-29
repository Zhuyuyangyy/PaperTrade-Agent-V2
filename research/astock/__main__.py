"""CLI: audit the convertible-bond discount.

    python -m research.astock audit [--limit N] [--top N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .discount import run, summarize

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "runtime" / "astock" / "bonds.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convertible-bond discount audit")
    parser.add_argument("command", choices=["audit"], nargs="?", default="audit")
    parser.add_argument("--limit", type=int, default=60,
                        help="how many bonds to pull; 0 means the whole universe"
                             " (sina rate-limits, so a full run takes ~20 minutes)")
    args = parser.parse_args(argv)
    if args.command == "audit":
        print(f"缓存: {CACHE}")
        report = run(CACHE, limit=args.limit)
        print()
        print(summarize(report))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
