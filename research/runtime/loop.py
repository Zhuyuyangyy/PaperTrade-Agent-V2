"""运行循环（Sprint 4）：调度、心跳、断流熔断、幂等与停止。

一轮 = 一个 ``as_of_ms``：
  1. 幂等检查（心跳里的 last_round_ts >= as_of → 跳过，不重复下单）；
  2. STOP 文件 → 退出；
  3. 熔断打开 → 只 mark/expire，不开新仓（日志写 HALTED 原因）；
  4. 取数失败 → 计一次失败，心跳记录 error，不产生决策；
  5. 正常 → run_round → 写决策日志 → 更新心跳。

时间由调用方推进（``run(start, until)``），因此 72h 验收可以离线加速。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from research.market.contracts import Candle
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.strategy.contracts import StrategySpec
from research.strategy.registry import DEFAULT_SPECS

from .circuit import CircuitBreaker, CircuitConfig
from .heartbeat import Heartbeat, read as read_heartbeat, write as write_heartbeat
from .round import RoundResult, run_round
from .sources import Source, SourceError


class LoopStopped(RuntimeError):
    pass


@dataclass
class LoopConfig:
    interval_ms: int = 3_600_000
    settle_ms: int = 30_000
    max_rounds: int | None = None
    stop_file: str = "STOP"


class RunLoop:
    def __init__(self, root: Path | str, broker: PaperBroker, limits: RiskLimits,
                 source: Source, *,
                 specs: tuple[StrategySpec, ...] = DEFAULT_SPECS,
                 log_path: Path | str | None = None,
                 heartbeat_path: Path | str | None = None,
                 circuit: CircuitBreaker | None = None,
                 circuit_config: CircuitConfig | None = None,
                 config: LoopConfig | None = None,
                 intel_fn=None,
                 day_start_ms: int = 0):
        self.root = Path(root)
        self.broker = broker
        self.limits = limits
        self.source = source
        self.specs = specs
        self.log_path = Path(log_path) if log_path else self.root / "data" / "decisions.jsonl"
        self.heartbeat_path = (Path(heartbeat_path) if heartbeat_path
                               else self.root / "state" / "heartbeat.json")
        self.circuit = circuit or CircuitBreaker(circuit_config)
        self.config = config or LoopConfig()
        self.intel_fn = intel_fn
        self.day_start_ms = day_start_ms
        self.results: list[RoundResult] = []

    # ---------- 单轮 ----------

    def _heartbeat(self) -> Heartbeat:
        hb = read_heartbeat(self.heartbeat_path)
        if hb is None:
            hb = Heartbeat(pid=os.getpid())
        return hb

    def step(self, as_of_ms: int) -> RoundResult | None:
        hb = self._heartbeat()
        if hb.last_round_ts_ms >= as_of_ms:
            return None                      # 幂等：同一时刻不重复执行

        stop = self.root / self.config.stop_file
        if stop.exists():
            raise LoopStopped(f"STOP 文件存在: {stop}")

        intel = None
        if self.intel_fn is not None:
            try:
                intel = self.intel_fn(as_of_ms)
            except Exception as exc:         # noqa: BLE001 - 情报失败不阻塞交易
                intel = None
                hb.last_error = f"intel_error: {exc}"

        result: RoundResult | None = None
        try:
            views = self.source.candles(as_of_ms)
            if not views or "1h" not in views:
                raise SourceError("缺少 1h 数据")
            # 数据可用即计入恢复：连续成功达到阈值时，本轮就允许开新仓
            self.circuit.record_success()
            result = run_round(
                "BTC/USDT", views, as_of_ms, self.broker, self.limits,
                log_path=self.log_path, specs=self.specs, day_start_ms=self.day_start_ms,
                settle_ms=self.config.settle_ms, intel=intel,
                allow_new_entries=not self.circuit.halted,
                halt_reason=self.circuit.reason,
            )
            hb.last_ok_ms = as_of_ms
            hb.consecutive_failures = 0
            hb.last_error = ""
        except LoopStopped:
            raise
        except Exception as exc:             # noqa: BLE001 - 断流/异常都走熔断
            self.circuit.record_failure(str(exc))
            hb.consecutive_failures += 1
            hb.last_error = f"{type(exc).__name__}: {exc}"

        hb.updated_at_ms = as_of_ms
        hb.last_round_ts_ms = as_of_ms
        hb.rounds += 1
        hb.halted = self.circuit.halted
        hb.halt_reason = self.circuit.reason
        hb.pid = os.getpid()
        write_heartbeat(self.heartbeat_path, hb)
        if result is not None:
            self.results.append(result)
        return result

    # ---------- 多轮 ----------

    def run(self, start_ms: int, until_ms: int) -> dict:
        executed = 0
        stopped = False
        skipped = 0
        as_of = start_ms
        while as_of <= until_ms:
            if self.config.max_rounds is not None and executed >= self.config.max_rounds:
                break
            try:
                r = self.step(as_of)
            except LoopStopped as exc:
                stopped = True
                last_error = str(exc)
                break
            if r is None:
                skipped += 1
            else:
                executed += 1
            as_of += self.config.interval_ms
        return {
            "executed": executed,
            "skipped": skipped,
            "stopped": stopped,
            "last_round_ts_ms": as_of - self.config.interval_ms,
            "circuit": self.circuit.to_dict(),
        }