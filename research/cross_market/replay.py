"""确定性回放引擎（P0，规格 §15）——参考 research/replay/ 的设计，不直接复用。

差异（用户裁定）：原 replay 回放历史决策与价格标记；本引擎回放
Polymarket/Binance 原始事件流，做 point-in-time 特征重建。

铁律（2026-11-18 审查后加固）：
  1. **可用性门禁**：一条事件在 max(event_ts, ingest_ts) 之前不可用——
     event_ts=1000 但 ingest_ts=5000 的事件，在 2000 时刻决策时不可使用
     （审查用例：延迟到达 = 隐蔽前视）。回放按可用时间推进，不用 event_ts。
  2. ingest_ts 只用于可用性判断，**永远不作为特征**。
  3. 同一输入 → 同一输出（确定性，可复现）。
  4. 特征只能从"已可用事件"重建，禁访问未来。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .storage import EventStore


def usable_at(event_ts_ms: int, ingest_ts_ms: int) -> int:
    """事件对决策可见的时刻（可用性门禁，规格 §8/审查 P0）。

    历史重建时用 max(event_ts, ingest_ts)：数据到了才算数。
    实时场景 ingest_ts ≈ now，此式自然退化为"收到了就能用"。
    """
    return max(event_ts_ms, ingest_ts_ms)


@dataclass
class ReplayState:
    now_ms: int = 0                    # 当前可用时间游标
    poly_last: dict = field(default_factory=dict)   # token_id -> 最近 book 状态
    binance_last: dict = field(default_factory=dict)  # symbol -> 最近快照
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
        """
        for ev in self.store.query(None, start_ms, end_ms):
            avail = usable_at(ev["event_ts_ms"], ev["ingest_ts_ms"])
            if decision_now_ms is not None and avail > decision_now_ms:
                self.state.skipped_not_yet_available += 1
                continue
            self._apply(ev, avail)
        return self.state

    def _apply(self, ev: dict, avail_ms: int) -> None:
        # 铁律：可用时间必须 >= 当前游标（调用方必须按可用序喂）
        if avail_ms < self.state.now_ms:
            raise ValueError(f"乱序事件：可用时间 {avail_ms} < 游标 {self.state.now_ms}")
        self.state.now_ms = avail_ms
        self.state.seen_events += 1
        if ev["source"] == "polymarket":
            self.state.poly_last[ev["key"]] = ev["payload"]
        elif ev["source"] == "binance":
            self.state.binance_last[ev["key"]] = ev["payload"]

    def feature_at(self, token_id: str, symbol: str) -> dict:
        """当前游标处的 point-in-time 特征（只用已可用事件；禁含 ingest 时间）。"""
        return {
            "ts_ms": self.state.now_ms,
            "polymarket": self.state.poly_last.get(token_id),
            "binance": self.state.binance_last.get(symbol),
        }
