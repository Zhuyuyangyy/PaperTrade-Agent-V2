"""CLI for the replay engine: collect shadow labels, replay, report.

    python -m research.replay labels   # one round of shadow price collection
    python -m research.replay replay   # replay recorded decisions, print report
    python -m research.replay cost     # cost sensitivity across bps levels
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .engine import DEFAULT_PARAMS, ReplayEngine
from .labels import collect_once, ensure_schema, label_coverage
from .report import build_report, cost_sensitivity


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _connect(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(root / "runtime" / "memehunter" / "memehunter.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay Engine V1 (paper only, offline)")
    parser.add_argument("command", choices=["labels", "replay", "cost", "calibration", "signals", "dmarks", "timeout", "moneyflow", "mfmarks", "selectivity", "fees"], nargs="?", default="replay")
    parser.add_argument("--params", default="", help="JSON file with replay params")
    args = parser.parse_args(argv)

    root = _root()
    params = dict(DEFAULT_PARAMS)
    if args.params:
        params.update(json.loads(Path(args.params).read_text(encoding="utf-8")))

    if args.command == "labels":
        conn = _connect(root)
        try:
            from datetime import datetime, timezone

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            summary = collect_once(conn, now_ms)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print(json.dumps(label_coverage(conn), ensure_ascii=False, indent=2))
        finally:
            conn.close()
        return 0

    if args.command == "replay":
        engine = ReplayEngine(root, params)
        try:
            result = engine.run(label="rules-v1")
        finally:
            engine.close()
        conn = _connect(root)
        try:
            coverage = label_coverage(conn)
        finally:
            conn.close()
        print(build_report(result, coverage))
        return 0

    if args.command == "cost":
        print(cost_sensitivity(root, params))
        return 0

    if args.command == "moneyflow":
        from .moneyflow import collect_signals, mark_signals, compare_sources, format_compare
        print(json.dumps(collect_signals(root), ensure_ascii=False))
        print(json.dumps(mark_signals(root), ensure_ascii=False))
        print(format_compare(compare_sources(root)))
        return 0

    if args.command == "mfmarks":
        from .moneyflow import mark_signals
        print(json.dumps(mark_signals(root), ensure_ascii=False))
        return 0

    if args.command == "timeout":
        from .timeout_replay import counterfactual, format_report
        print(format_report(counterfactual(root)))
        return 0

    if args.command == "fees":
        from .costs import calibrate, format_calibration
        print(format_calibration(calibrate(root)))
        return 0

    if args.command == "selectivity":
        from .selectivity import (
            audit,
            decomposition,
            excursions,
            format_audit,
            format_decomposition,
            format_excursions,
        )
        print(format_decomposition(decomposition(root)))
        print()
        print(format_audit(audit(root)))
        print()
        print(format_excursions(excursions(root, horizon_min=60)))
        return 0

    if args.command == "signals":
        from .signals import compare
        r = compare(root)
        print(json.dumps({k: v for k, v in r.items() if k != "scored"}, ensure_ascii=False, default=str))
        return 0

    if args.command == "dmarks":
        from .signals import collect_d_marks
        print(json.dumps(collect_d_marks(root), ensure_ascii=False))
        return 0

    if args.command == "calibration":
        from .calibration import format_calibration, judge_calibration
        conn = _connect(root)
        try:
            horizon = int(params.get("calibration_horizon_minutes", 60))
            print(format_calibration(judge_calibration(conn, horizon)))
        finally:
            conn.close()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
