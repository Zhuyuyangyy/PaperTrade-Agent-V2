"""SQLite 快照必须包含未 checkpoint 的 WAL 内容（Sprint 0，S0-C10）。

`shutil.copy` 只拷主文件：WAL 模式下最近提交的事务留在 `-wal` 里，副本读不到。
审计窗口的末端恰好是最新结论依赖的部分，复制法会静默少数据。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from research.ledger import snapshot


def test_snapshot_includes_uncheckpointed_wal(tmp_path: Path) -> None:
    src = tmp_path / "live.sqlite"
    conn = sqlite3.connect(src)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (7)")
        conn.commit()
        # 不 close、不 checkpoint：内容仍在 -wal 中
        dst = tmp_path / "copy.sqlite"
        snapshot(src, dst)
        out = sqlite3.connect(dst)
        try:
            assert out.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
            assert out.execute("SELECT x FROM t").fetchone()[0] == 7
        finally:
            out.close()
    finally:
        conn.close()


def test_snapshot_does_not_write_the_live_ledger(tmp_path: Path) -> None:
    src = tmp_path / "live.sqlite"
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    before = src.stat().st_mtime_ns
    snapshot(src, tmp_path / "copy.sqlite")
    assert src.stat().st_mtime_ns == before
    conn.close()


def test_snapshot_of_missing_ledger_is_loud(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        snapshot(tmp_path / "nope.sqlite", tmp_path / "copy.sqlite")