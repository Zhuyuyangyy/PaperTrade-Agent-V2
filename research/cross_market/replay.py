"""确定性回放引擎（P0，规格 §15；P1.1 修复可用性排序）。

铁律：
  1. **可用性门禁**：一条事件在 max(event_ts, ingest_ts) 之前不可用；
  2. **按可用时间回放**：查询必须走 `query_available`——按 event_ts 排序会在
     迟到数据上触发"乱序事件"异常（S0-11：真实现场会出现），也会让回放顺序
     与真实到达顺序不一致；
  3. ingest_ts 只用于可用性判断，**永远不作为特征**；
  4. 同一输入 → 同一输出（确定性）；
  5. 特征只能从"已可用事件"重建，禁访问未来。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .storage import EventStore


def usable_at(event_ts_ms: int, ingest_ts_ms: int) -> int:
    """事件对决策可见的时刻（可用性门禁）。

    历史重建时用 max(event_ts, ingest_ts)：数据到了才算数。
    实时场景 ingest_ts ≈ now，此式自然退化为"收到了就能用"。
    """
    return max(event_ts_ms, ingest_ts_ms)


def availability_ms(ev: dict) -> int:
    """从事件字典取可用时间（与 usable_at 同义，供存储层之外的调用者使用）。"""
    return usable_at(ev["event_ts_ms"], ev["ingest_ts_ms"])


@dataclass
class ReplayState:
    now_ms: int = 0                    # 当前可用时间游标
    poly_last: dict = field(default_factory=dict)     # token_id -> 最近 book 状态
    binance_last: dict = field(default_factory=dict)  # symbol -> 最近快照
    poly_event_ts: dict = field(default_factory=dict)     # token_id -> 状态的事件时间
    binance_event_ts: dict = field(default_factory=dict)  # symbol -> 状态的事件时间
    seen_events: int = 0
    skipped_not_yet_available: int = 0  # 因可用性门禁被挡的事件数（诊断）


class ReplayEngine:
    """按可用时间正序重放原始事件，维护 point-in-time 状态。"""

    def __init__(self, store: EventStore):
        self.store = store
        self.state = ReplayState()

    def run(self, start_ms: int, end_ms: int, decision_now_ms: int | None = None) -> ReplayState:
        """回放 [start_ms, end_ms)。

        decision_now_ms：若给出，则只允许"在 decision_now_ms 时刻已可用"的事件
        （可用性门禁的决策时刻形态）；ingest 晚于决策时刻的事件被挡下并计数。

        未给出时，回放的截止时刻是 **end_ms**：窗口结束之后才到达的事件不得
        出现在窗口内的状态里（R2-02）。窗口按 event_ts 界定，所以"窗口内发生、
        窗口后才到"的事件会进入查询、被门禁计数并挡下——这样它至少是可见的，
        而不是从窗口里凭空消失。
        """
        as_of = decision_now_ms if decision_now_ms is not None else end_ms
        for ev in self.store.query_available(None, start_ms, end_ms):
            avail = availability_ms(ev)
            if avail > as_of:
                self.state.skipped_not_yet_available += 1
                continue
            self._apply(ev, avail)
        return self.state

    def _apply(self, ev: dict, avail_ms: int) -> None:
        # 同一可用时刻允许多条（同一批到达）；严格递减才是乱序
        if avail_ms < self.state.now_ms:
            raise ValueError(f"乱序事件：可用时间 {avail_ms} < 游标 {self.state.now_ms}")
        self.state.now_ms = avail_ms
        self.state.seen_events += 1
        if ev["source"] == "polymarket":
            key, ts = ev["key"], ev["event_ts_ms"]
            # 迟到旧行情不得覆盖更新行情（R2-03）：状态按事件时间取最新。
            if ts >= self.state.poly_event_ts.get(key, -(1 << 62)):
                self.state.poly_last[key] = ev["payload"]
                self.state.poly_event_ts[key] = ts
        elif ev["source"] == "binance":
            key, ts = ev["key"], ev["event_ts_ms"]
            if ts >= self.state.binance_event_ts.get(key, -(1 << 62)):
                self.state.binance_last[key] = ev["payload"]
                self.state.binance_event_ts[key] = ts

    def feature_at(self, token_id: str, symbol: str) -> dict:
        """当前游标处的 point-in-time 特征（只用已可用事件；禁含 ingest 时间）。"""
        return {
            "ts_ms": self.state.now_ms,
            "polymarket": self.state.poly_last.get(token_id),
            "binance": self.state.binance_last.get(symbol),
        }