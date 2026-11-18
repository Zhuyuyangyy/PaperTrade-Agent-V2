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


def is_stale(event: Timestamped, now_ms: int, max_age_ms: int) -> bool:
    """数据是否过期（event_ts 距 now 超过阈值）。过期数据禁入特征。"""
    return now_ms - event.event_ts_ms > max_age_ms


def lag_ms(event: Timestamped) -> int | None:
    """观测延迟 = ingest - event。诊断用，禁入特征。"""
    return event.ingest_ts_ms - event.event_ts_ms
