"""事件存储（P0）——append-only 原始事件库，Replay 的唯一数据源。

原则：
  - 原始事件只追加，不更新（研究可信度靠不可变历史）；
  - 每条记录带三个时间戳（event/exchange/ingest）；
  - 查询按 event_ts 区间，支持按源过滤；
  - SQLite 单文件（与 research/audit/binance.py 的缓存哲学一致：离线可回放）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- 'polymarket' | 'binance'
    event_type TEXT NOT NULL,        -- book/trade/snapshot/force_order...
    key TEXT NOT NULL,               -- market_id 或 symbol
    event_ts_ms INTEGER NOT NULL,
    exchange_ts_ms INTEGER,
    ingest_ts_ms INTEGER NOT NULL,
    payload TEXT NOT NULL            -- JSON 原文
);
CREATE INDEX IF NOT EXISTS idx_raw_events_ts ON raw_events (source, event_ts_ms);
"""


class EventStore:
    """一个 sqlite 文件装全部原始事件。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SCHEMA)
        self._db.commit()

    def append(self, source: str, event_type: str, key: str, event_ts_ms: int,
               ingest_ts_ms: int, payload: dict, exchange_ts_ms: int | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO raw_events (source, event_type, key, event_ts_ms, exchange_ts_ms, ingest_ts_ms, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, event_type, key, event_ts_ms, exchange_ts_ms, ingest_ts_ms,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def query(self, source: str | None, start_ms: int, end_ms: int,
              event_type: str | None = None, key: str | None = None) -> list[dict]:
        sql = "SELECT source, event_type, key, event_ts_ms, exchange_ts_ms, ingest_ts_ms, payload FROM raw_events WHERE event_ts_ms >= ? AND event_ts_ms < ?"
        args: list = [start_ms, end_ms]
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        if event_type is not None:
            sql += " AND event_type = ?"
            args.append(event_type)
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        sql += " ORDER BY event_ts_ms ASC, id ASC"
        rows = self._db.execute(sql, args).fetchall()
        return [
            {
                "source": r[0], "event_type": r[1], "key": r[2],
                "event_ts_ms": r[3], "exchange_ts_ms": r[4], "ingest_ts_ms": r[5],
                "payload": json.loads(r[6]),
            }
            for r in rows
        ]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])

    def close(self) -> None:
        self._db.close()


def replay_range(store: EventStore, start_ms: int, end_ms: int) -> Iterable[dict]:
    """按时间正序吐出全部事件（回放入口）。"""
    return store.query(None, start_ms, end_ms)
