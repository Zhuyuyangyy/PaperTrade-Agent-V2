"""Append auditable account-equity snapshots from read-only Dry-run ledgers.

The sampler has read-only access to Freqtrade SQLite files and public mark files. Its
only writable target is the append-only snapshot journal. It cannot place orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.report import summarize


def snapshot_record(kind: str, summary: dict, sampled_at_ms: int) -> dict:
    return {
        "schema_version": 1,
        "kind": kind,
        "sampled_at_ms": sampled_at_ms,
        "sampled_at": datetime.fromtimestamp(sampled_at_ms / 1000, tz=timezone.utc).isoformat(),
        "complete": summary["equity_complete"],
        "equity_usdt": summary["equity_usdt"],
        "starting_cash_usdt": summary["starting_cash_usdt"],
        "realized_usdt": summary["realized_usdt"],
        "unrealized_usdt": summary["unrealized_usdt"],
        "equity_change_usdt": summary["equity_change_usdt"],
        "equity_change_pct": summary["equity_change_pct"],
        "closed_count": summary["closed_count"],
        "open_count": summary["open_count"],
        "unpriced_open_count": summary["unpriced_open_count"],
        "mark_age_seconds": summary["mark_age_seconds"],
        "starting_cash_source": summary["starting_cash_source"],
    }


def append_jsonl(path: Path, records: list[dict]) -> None:
    if path.is_symlink():
        raise RuntimeError("Refusing to append through a symlinked snapshot journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def sample_once(db_paths: dict[str, Path], config_paths: dict[str, Path], marks_dir: Path,
                output: Path, now_ms: int | None = None) -> list[dict]:
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    records = []
    for kind in ("baseline", "hybrid", "fast"):
        if kind not in db_paths or kind not in config_paths:
            continue
        summary = summarize(db_paths[kind], config_paths[kind], marks_dir, now_ms)
        if summary is None:
            print(f"equity sampler: {kind}: ledger missing, locked, or schema not recognized", flush=True)
            continue
        record = snapshot_record(kind, summary, now_ms)
        records.append(record)
        equity = f"{summary['equity_usdt']:.2f}" if summary["equity_usdt"] is not None else "unavailable"
        print(
            f"equity sampler: {kind}: equity={equity} complete={summary['equity_complete']} "
            f"realized={summary['realized_usdt']:.2f} unrealized={summary['unrealized_usdt']}",
            flush=True,
        )
    if records:
        append_jsonl(output, records)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-only equity snapshot sampler")
    parser.add_argument("command", choices=("once", "loop"))
    parser.add_argument("--baseline-db", type=Path, default=ROOT / "runtime/baseline/baseline.dryrun.sqlite")
    parser.add_argument("--hybrid-db", type=Path, default=ROOT / "runtime/hybrid/hybrid.dryrun.sqlite")
    parser.add_argument("--baseline-config", type=Path, default=ROOT / "user_data/config.baseline.json")
    parser.add_argument("--hybrid-config", type=Path, default=ROOT / "user_data/config.hybrid.json")
    parser.add_argument("--fast-db", type=Path, default=ROOT / "runtime/fast/fast.dryrun.sqlite")
    parser.add_argument("--fast-config", type=Path, default=ROOT / "user_data/config.fast.json")
    parser.add_argument("--marks", type=Path, default=ROOT / "user_data/marks")
    parser.add_argument("--out", type=Path, default=ROOT / "data/equity_snapshots.jsonl")
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.interval_seconds < 15:
        parser.error("interval-seconds must be >= 15")
    dbs = {"baseline": args.baseline_db, "hybrid": args.hybrid_db, "fast": args.fast_db}
    configs = {"baseline": args.baseline_config, "hybrid": args.hybrid_config, "fast": args.fast_config}
    if args.command == "once":
        sample_once(dbs, configs, args.marks, args.out)
        return 0
    print("Equity sampler running: read-only SQLite + public marks; append-only snapshots.", flush=True)
    try:
        while True:
            try:
                sample_once(dbs, configs, args.marks, args.out)
            except Exception as exc:
                print(f"equity sampler failed closed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        print("Equity sampler stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
