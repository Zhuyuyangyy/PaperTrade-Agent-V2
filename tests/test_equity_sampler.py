from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from research.equity_sampler import sample_once

NOW = int(datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def make_db(path, pnl=0.0):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (is_open INTEGER, close_profit_abs REAL, close_date TEXT, pair TEXT)")
    if pnl:
        conn.execute("INSERT INTO trades VALUES (0, ?, '2026-10-23 22:00:00', 'BTC/USDT')", (pnl,))
    conn.commit()
    conn.close()


def test_sampler_writes_separate_auditable_account_snapshots(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "baseline").mkdir(parents=True)
    (runtime / "hybrid").mkdir(parents=True)
    (runtime / "fast").mkdir(parents=True)
    baseline_db = runtime / "baseline" / "baseline.dryrun.sqlite"
    hybrid_db = runtime / "hybrid" / "hybrid.dryrun.sqlite"
    fast_db = runtime / "fast" / "fast.dryrun.sqlite"
    make_db(baseline_db, 4.0)
    make_db(hybrid_db, -2.0)
    make_db(fast_db, 1.0)
    configs = {}
    for kind in ("baseline", "hybrid", "fast"):
        configs[kind] = tmp_path / f"config.{kind}.json"
        configs[kind].write_text(json.dumps({"dry_run_wallet": 1000}), encoding="utf-8")

    out = tmp_path / "data" / "equity_snapshots.jsonl"
    records = sample_once(
        {"baseline": baseline_db, "hybrid": hybrid_db, "fast": fast_db},
        configs,
        tmp_path / "marks",
        out,
        NOW,
    )
    assert len(records) == 3
    assert all(record["complete"] is True for record in records)
    assert {record["kind"] for record in records} == {"baseline", "hybrid", "fast"}
    assert {record["equity_usdt"] for record in records} == {998.0, 1001.0, 1004.0}
    persisted = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert persisted == records


def test_sampler_records_incomplete_equity_instead_of_faking_marks(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "baseline").mkdir(parents=True)
    (runtime / "hybrid").mkdir(parents=True)
    (runtime / "fast").mkdir(parents=True)
    # An open trade without mark-to-market columns must make equity incomplete.
    for kind in ("baseline", "hybrid", "fast"):
        db = runtime / kind / f"{kind}.dryrun.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trades (is_open INTEGER, close_profit_abs REAL, pair TEXT)")
        conn.execute("INSERT INTO trades VALUES (1, NULL, 'BTC/USDT')")
        conn.commit()
        conn.close()
    configs = {}
    for kind in ("baseline", "hybrid", "fast"):
        configs[kind] = tmp_path / f"config.{kind}.json"
        configs[kind].write_text('{"dry_run_wallet": 1000}', encoding="utf-8")
    out = tmp_path / "equity.jsonl"
    records = sample_once(
        {kind: runtime / kind / f"{kind}.dryrun.sqlite" for kind in ("baseline", "hybrid", "fast")},
        configs,
        tmp_path / "marks",
        out,
        NOW,
    )
    assert len(records) == 3
    assert all(record["complete"] is False for record in records)
    assert all(record["equity_usdt"] is None for record in records)
