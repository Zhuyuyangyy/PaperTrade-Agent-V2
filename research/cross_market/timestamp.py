"""三时间戳系统（规格 §8）。

event_ts     市场事件发生时间
exchange_ts  交易所/市场系统提供的时间戳
ingest_ts    我们系统收到数据的时间

所有对齐/回放只用 event_ts 与 exchange_ts；ingest_ts 只用于诊断延迟，
永远不进特征（防 look-ahead：ingest 时间可能晚于事件数秒，拿它当事件时间是错的）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Timestamped:
    event_ts_ms: int
    exchange_ts_ms: int | None
    ingest_ts_ms: int


def align(event_a: Timestamped, event_b: Timestamped, tolerance_ms: int = 2_000) -> bool:
    """两事件是否可对齐（event_ts 差在容差内）。exchange_ts 缺失时退回 event_ts。"""
    ta = event_a.exchange_ts_ms if event_a.exchange_ts_ms is not None else event_a.event_ts_ms
    tb = event_b.exchange_ts_ms if event_b.exchange_ts_ms is not None else event_b.event_ts_ms
    return abs(ta - tb) <= tolerance_ms


def align_gap_ms(event_a: Timestamped, event_b: Timestamped) -> int:
    """两事件时间差（诊断/最近邻配对用；正数表示 a 早于 b）。"""
    ta = event_a.exchange_ts_ms if event_a.exchange_ts_ms is not None else event_a.event_ts_ms
    tb = event_b.exchange_ts_ms if event_b.exchange_ts_ms is not None else event_b.event_ts_ms
    return tb - ta


def is_stale(event: Timestamped, now_ms: int, max_age_ms: int) -> bool:
    """数据是否过期。按**可用时间**判定：迟到到达的老事件不算新鲜数据。"""
    available = max(event.event_ts_ms, event.ingest_ts_ms)
    return now_ms - available > max_age_ms


def freshness_ms(event: Timestamped, now_ms: int) -> int:
    """数据新鲜度 = now − 可用时间（负数表示可用时间在未来，即时钟超前）。"""
    return now_ms - max(event.event_ts_ms, event.ingest_ts_ms)


def lag_ms(event: Timestamped) -> int | None:
    """观测延迟 = ingest - event。诊断用，禁入特征。"""
    return event.ingest_ts_ms - event.event_ts_ms


def clock_offset_ms(exchange_ts_ms: int | None, ingest_ts_ms: int) -> int | None:
    """服务端时钟相对本地时钟的偏移（exchange − ingest）。

    正数 = 服务端超前（本地钟慢）；负数 = 服务端落后（本地钟快，最危险：
    若直接信任服务端时间戳，会把"未来才该发生"的数据提前放进特征）。
    """
    if exchange_ts_ms is None:
        return None
    return exchange_ts_ms - ingest_ts_ms