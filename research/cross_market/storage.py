"""事件存储（P0/P1.1）——append-only 原始事件库，Replay 的唯一数据源。

原则：
  - 原始事件只追加，不更新（研究可信度靠不可变历史）；
  - 每条记录带三个时间戳（event/exchange/ingest）；
  - **可用时间视图**：`query_available` 按 max(event_ts, ingest_ts) 排序，
    这是回放/对齐唯一合法的顺序（按 event_ts 排序会让迟到数据触发乱序断言，
    也会让"当时还没到"的数据提前进入特征）；
  - 可选去重：同一 (source, event_type, key, event_ts, payload) 只落一条，
    用于抑制重复轮询同一盘口（S0-10）；
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
CREATE INDEX IF NOT EXISTS idx_raw_events_avail ON raw_events (source, ingest_ts_ms);
"""

# 可用时间 = max(event_ts, ingest_ts)。SQLite 的 max() 是标量函数，可直接用于
# ORDER BY / WHERE；统一在一处定义，避免各模块各写一份。
AVAIL_SQL = "max(event_ts_ms, ingest_ts_ms)"


def _row(r: tuple) -> dict:
    return {
        "source": r[0], "event_type": r[1], "key": r[2],
        "event_ts_ms": r[3], "exchange_ts_ms": r[4], "ingest_ts_ms": r[5],
        "payload": json.loads(r[6]),
    }


_COLS = ("source, event_type, key, event_ts_ms, exchange_ts_ms, ingest_ts_ms, payload")


class EventStore:
    """一个 sqlite 文件装全部原始事件。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SCHEMA)
        self._db.commit()

    # ---------- 写 ----------

    def append(self, source: str, event_type: str, key: str, event_ts_ms: int,
               ingest_ts_ms: int, payload: dict, exchange_ts_ms: int | None = None,
               dedupe: bool = False) -> int | None:
        """追加一条事件。dedupe=True 且完全相同的记录已存在时返回 None（未写入）。"""
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if dedupe:
            hit = self._db.execute(
                "SELECT 1 FROM raw_events WHERE source=? AND event_type=? AND key=? "
                "AND event_ts_ms=? AND payload=? LIMIT 1",
                (source, event_type, key, event_ts_ms, blob),
            ).fetchone()
            if hit is not None:
                return None
        cur = self._db.execute(
            "INSERT INTO raw_events (source, event_type, key, event_ts_ms, exchange_ts_ms, ingest_ts_ms, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, event_type, key, event_ts_ms, exchange_ts_ms, ingest_ts_ms, blob),
        )
        self._db.commit()
        return int(cur.lastrowid)

    # ---------- 读 ----------

    def query(self, source: str | None, start_ms: int, end_ms: int,
              event_type: str | None = None, key: str | None = None) -> list[dict]:
        """按 event_ts 区间查询（事件发生时间视角，旧的诊断/导出接口）。"""
        sql = f"SELECT {_COLS} FROM raw_events WHERE event_ts_ms >= ? AND event_ts_ms < ?"
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
        return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def query_available(self, source: str | None, start_ms: int, end_ms: int,
                        event_type: str | None = None, key: str | None = None,
                        as_of_ms: int | None = None,
                        include_late_before_start: bool = True) -> list[dict]:
        """按可用时间（max(event, ingest)）排序返回窗口内事件。

        - 窗口按 **event_ts** 界定（事件发生在窗口内才算数）；
        - 排序按可用时间：迟到事件晚到，但它仍然属于它发生的那个时刻；
        - as_of_ms：只返回在 as_of 时刻已可用的事件（回放/实时门禁）；
        - include_late_before_start：窗口开始前发生、但窗口内才到达的事件
          （默认包含：否则"晚到 10 分钟"的行情会在窗口内凭空消失，
          看起来像数据缺失，实际是可用性延迟）。
        """
        if include_late_before_start:
            where = "(event_ts_ms >= ? OR (event_ts_ms < ? AND %s >= ?))" % AVAIL_SQL
            args: list = [start_ms, start_ms, start_ms]
        else:
            where = "event_ts_ms >= ?"
            args = [start_ms]
        args.append(end_ms)
        sql = f"SELECT {_COLS} FROM raw_events WHERE {where} AND event_ts_ms < ?"
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        if event_type is not None:
            sql += " AND event_type = ?"
            args.append(event_type)
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        if as_of_ms is not None:
            sql += f" AND {AVAIL_SQL} <= ?"
            args.append(as_of_ms)
        sql += f" ORDER BY {AVAIL_SQL} ASC, event_ts_ms ASC, id ASC"
        return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def last_available(self, source: str, key: str, as_of_ms: int,
                       event_type: str | None = None) -> dict | None:
        """在 as_of 时刻已可用的最后一条事件（point-in-time 取值）。"""
        sql = (f"SELECT {_COLS} FROM raw_events WHERE source = ? AND key = ? "
               f"AND {AVAIL_SQL} <= ?")
        args: list = [source, key, as_of_ms]
        if event_type is not None:
            sql += " AND event_type = ?"
            args.append(event_type)
        sql += f" ORDER BY {AVAIL_SQL} DESC, id DESC LIMIT 1"
        r = self._db.execute(sql, args).fetchone()
        return _row(r) if r else None

    def first_available_after(self, source: str, key: str, from_ms: int,
                              event_type: str | None = None,
                              until_ms: int | None = None,
                              event_min_ms: int | None = None) -> dict | None:
        """可用时间 >= from_ms 的第一条事件（标签窗口用，只会取到未来数据）。

        event_min_ms：额外要求**事件发生时间**也 >= 该值。迟到到达的旧行情
        （event 早、ingest 晚）会满足"可用时间 >= from_ms"，但它发生在过去，
        绝不能当未来价格——R2-01。
        """
        sql = (f"SELECT {_COLS} FROM raw_events WHERE source = ? AND key = ? "
               f"AND {AVAIL_SQL} >= ?")
        args: list = [source, key, from_ms]
        if event_type is not None:
            sql += " AND event_type = ?"
            args.append(event_type)
        if event_min_ms is not None:
            sql += " AND event_ts_ms >= ?"
            args.append(event_min_ms)
        if until_ms is not None:
            sql += f" AND {AVAIL_SQL} <= ?"
            args.append(until_ms)
        sql += f" ORDER BY {AVAIL_SQL} ASC, event_ts_ms ASC, id ASC LIMIT 1"
        r = self._db.execute(sql, args).fetchone()
        return _row(r) if r else None

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])

    def close(self) -> None:
        self._db.close()


def replay_range(store: EventStore, start_ms: int, end_ms: int) -> Iterable[dict]:
    """按可用时间正序吐出窗口内全部事件（回放入口）。"""
    return store.query_available(None, start_ms, end_ms)