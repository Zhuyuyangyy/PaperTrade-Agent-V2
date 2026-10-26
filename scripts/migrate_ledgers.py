"""Safely migrate old V2 Dry-run SQLite ledgers into isolated runtime directories.

Run only after stopping the old Compose stack. The SQLite backup API includes committed
WAL state consistently; target ledgers are never overwritten by this script.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
# Running this file directly (the documented invocation) does not put the project
# root on sys.path, so the lazy `from scripts.export_experiment import ...` below
# would raise ModuleNotFoundError. Add it explicitly before any scripts.* import.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def ledger_counts(path: Path) -> tuple[int, int]:
    uri = f"file:{quote(str(path.resolve()), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "trades" not in tables:
            raise ValueError(f"No trades table in {path}")
        fields = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        if not {"is_open", "close_profit_abs"}.issubset(fields):
            raise ValueError(f"Unrecognized Freqtrade trade schema in {path}")
        rows = conn.execute("SELECT is_open FROM trades").fetchall()
        open_count = sum(bool(row[0]) for row in rows)
        return open_count, len(rows) - open_count
    finally:
        conn.close()


def legacy_source_for(legacy_root: Path, kind: str) -> Path:
    """Prefer the current isolated runtime ledger, then fall back to the pre-isolation path."""
    candidates = (
        legacy_root / "runtime" / kind / f"{kind}.dryrun.sqlite",
        legacy_root / "user_data" / f"{kind}.dryrun.sqlite",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    return candidates[0]


def migrate_one(source: Path, target: Path) -> str:
    if source.is_symlink() or not source.is_file():
        return "source-missing"
    if target.is_symlink():
        raise ValueError(f"Refusing symlinked target: {target}")
    if target.exists():
        return "target-exists-not-overwritten"
    open_before, closed_before = ledger_counts(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    from scripts.export_experiment import copy_sqlite_snapshot
    if not copy_sqlite_snapshot(source, target):
        target.unlink(missing_ok=True)
        raise RuntimeError(f"SQLite backup failed for {source}")
    open_after, closed_after = ledger_counts(target)
    if (open_before, closed_before) != (open_after, closed_after):
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Ledger count mismatch after backup for {source}")
    return f"migrated open={open_after} closed={closed_after}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate old V2 paper-trading ledgers without overwriting targets")
    parser.add_argument("--legacy-root", type=Path, default=ROOT,
                        help="Old project root; old DBs are expected under user_data/")
    parser.add_argument("--target-root", type=Path, default=ROOT,
                        help="Updated project root; target DBs go under runtime/{baseline,hybrid,fast}/")
    args = parser.parse_args()
    for kind in ("baseline", "hybrid", "fast"):
        source = legacy_source_for(args.legacy_root, kind)
        target = args.target_root / "runtime" / kind / f"{kind}.dryrun.sqlite"
        try:
            status = migrate_one(source, target)
        except Exception as exc:
            print(f"{kind}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"{kind}: {status}")
    print("Migration finished. Check baseline and hybrid counts before starting Docker; fast is a new exploratory lane and may have no legacy DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
