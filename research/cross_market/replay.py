"""确定性回放引擎（P0，规格 §15）——参考 research/replay/ 的设计，不直接复用。

差异（用户裁定）：原 replay 回放历史决策与价格标记；本引擎回放
Polymarket/Binance 原始事件流，做 point-in-time 特征重建。

铁律：
  - 只喂 event_ts <= 当前时刻的事件（ingest_ts 只用于诊断）；
  - 同一输入 → 同一输出（确定性，可复现）；
  - 特征永远从"已见事件"重建，禁访问未来。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .storage import EventStore


@dataclass
class ReplayState:
    now_ms: int = 0
    poly_last: dict = field(default_factory=dict)   # token_id -> 最近 book 状态
    binance_last: dict = field(default_factory=dict)  # symbol -> 最近快照
    seen_events: int = 0


class ReplayEngine:
    """按时间正序重放原始事件，维护 point-in-time 状态。"""

    def __init__(self, store: EventStore):
        self.store = store
        self.state = ReplayState()

    def run(self, start_ms: int, end_ms: int) -> ReplayState:
        for ev in self.store.query(None, start_ms, end_ms):
            self._apply(ev)
        return self.state

    def _apply(self, ev: dict) -> None:
        # 铁律 1：事件时间必须 <= 当前游标（乱序/未来事件拒绝）
        if ev["event_ts_ms"] < self.state.now_ms:
            raise ValueError(f"乱序事件：{ev['event_ts_ms']} < 游标 {self.state.now_ms}")
        self.state.now_ms = ev["event_ts_ms"]
        self.state.seen_events += 1
        if ev["source"] == "polymarket":
            self.state.poly_last[ev["key"]] = ev["payload"]
        elif ev["source"] == "binance":
            self.state.binance_last[ev["key"]] = ev["payload"]

    def feature_at(self, token_id: str, symbol: str) -> dict:
        """当前游标处的 point-in-time 特征（只用已见事件）。"""
        return {
            "ts_ms": self.state.now_ms,
            "polymarket": self.state.poly_last.get(token_id),
            "binance": self.state.binance_last.get(symbol),
        }
