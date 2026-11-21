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

from research.decision import MarketSnapshot
from research.decision import run as run_providers
from research.decision.contracts import Provider
from research.intel.features import compute as compute_intel_features


class LoopStopped(RuntimeError):
    pass


STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"


DAY_MS = 86_400_000


def utc_day_start(ms: int) -> int:
    """UTC 日界（00:00 UTC）。每日亏损限制必须在每轮按当前日界重新计算。"""
    return ms // DAY_MS * DAY_MS


@dataclass
class LoopConfig:
    interval_ms: int = 3_600_000
    decision_period_ms: int | None = None   # 幂等键的周期（默认 = interval）
    settle_ms: int = 30_000
    max_rounds: int | None = None
    stop_file: str = "STOP"


class RunLoop:
    def __init__(self, root: Path | str, broker: PaperBroker, limits: RiskLimits,
                 source: Source, *,
                 specs: tuple[StrategySpec, ...] = DEFAULT_SPECS,
                 log_path: Path | str | None = None,
                 heartbeat_path: Path | str | None = None,
                 shadow_broker: PaperBroker | None = None,
                 circuit: CircuitBreaker | None = None,
                 circuit_config: CircuitConfig | None = None,
                 config: LoopConfig | None = None,
                 intel_fn=None,
                 providers: list[Provider] | None = None,
                 providers_log_path: Path | str | None = None):
        self.root = Path(root)
        self.broker = broker
        self.shadow_broker = shadow_broker
        self.limits = limits
        self.source = source
        self.specs = specs
        self.log_path = Path(log_path) if log_path else self.root / "data" / "decisions.jsonl"
        self.heartbeat_path = (Path(heartbeat_path) if heartbeat_path
                               else self.root / "state" / "heartbeat.json")
        self.config = config or LoopConfig()
        if circuit is not None:
            self.circuit = circuit
        else:
            # 跨重启恢复：先读心跳里的熔断状态，再用显式配置覆盖阈值
            hb = read_heartbeat(self.heartbeat_path)
            saved = hb.circuit if hb else None
            self.circuit = CircuitBreaker.from_dict(saved)
            if circuit_config is not None:
                self.circuit.config = circuit_config
        self.intel_fn = intel_fn
        self.providers = list(providers or [])
        self.providers_log_path = (Path(providers_log_path) if providers_log_path
                                   else self.root / "data" / "providers.jsonl")
        self.results: list[RoundResult] = []
        self.last_status = STATUS_SKIPPED
        self.last_reason = ""
        self.provider_errors: list[str] = []

    # ---------- 单轮 ----------

    def _compare_providers(self, result: RoundResult, intel, as_of_ms: int) -> None:
        """同一快照交给所有 provider；只写日志，失败不影响交易结果。"""
        try:
            intel_features = compute_intel_features(result.state, intel) if intel is not None else None
            snapshot = MarketSnapshot(as_of_ms, "BTC/USDT", result.state, intel_features)
            run_providers(snapshot, self.providers, log_path=self.providers_log_path)
        except Exception as exc:  # noqa: BLE001 - 研究记录失败不能拖垮交易
            self.provider_errors.append(f"{type(exc).__name__}: {exc}")

    def _heartbeat(self) -> Heartbeat:
        hb = read_heartbeat(self.heartbeat_path)
        if hb is None:
            hb = Heartbeat(pid=os.getpid())
        return hb

    def _round_key(self, as_of_ms: int) -> int:
        """幂等键：绑到已收盘 K 线的周期，而不是运行时毫秒。

        同一根 1h K 线内重复轮询不应该产生新的交易机会；只有进入下一根已收盘
        K 线（新的周期键）才允许再决策一次。
        """
        period = self.config.decision_period_ms or self.config.interval_ms
        return as_of_ms // period * period

    def step(self, as_of_ms: int) -> RoundResult | None:
        key = self._round_key(as_of_ms)
        hb = self._heartbeat()
        self.last_status = STATUS_SKIPPED
        self.last_reason = ""
        if hb.last_round_key_ms >= key:
            self.last_reason = "duplicate_key"
            return None                      # 幂等：同一根 K 线已成功决策过

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
            # 恢复预判：连续成功达到阈值时，本轮就允许开新仓；但只有整轮真的
            # 成功（取数 + run_round）才 record_success，避免"取数成功但决策失败"
            # 被算成成功、把失败计数清零。
            will_recover = (self.circuit.halted
                            and self.circuit.successes + 1 >= self.circuit.config.recover_threshold)
            result = run_round(
                "BTC/USDT", views, as_of_ms, self.broker, self.limits,
                log_path=self.log_path, specs=self.specs,
                day_start_ms=utc_day_start(as_of_ms),
                settle_ms=self.config.settle_ms, intel=intel,
                allow_new_entries=(not self.circuit.halted) or will_recover,
                halt_reason=self.circuit.reason,
                shadow_broker=self.shadow_broker,
            )
            self.circuit.record_success()
            hb.last_ok_ms = as_of_ms
            hb.consecutive_failures = 0
            hb.last_error = ""
            hb.successful_rounds += 1
            hb.last_round_key_ms = key        # 只有成功才推进幂等键
            self.last_status = STATUS_OK
            if self.providers:
                self._compare_providers(result, intel, as_of_ms)
        except LoopStopped:
            raise
        except Exception as exc:             # noqa: BLE001 - 断流/异常都走熔断
            self.circuit.record_failure(str(exc))
            hb.consecutive_failures += 1
            hb.last_error = f"{type(exc).__name__}: {exc}"
            self.last_status = STATUS_FAILED
            self.last_reason = f"{type(exc).__name__}: {exc}"
            # 失败不推进 key：同一根 K 线允许重试

        hb.updated_at_ms = as_of_ms
        hb.last_round_ts_ms = as_of_ms
        hb.rounds += 1
        hb.halted = self.circuit.halted
        hb.halt_reason = self.circuit.reason
        hb.circuit = self.circuit.to_dict()
        hb.pid = os.getpid()
        write_heartbeat(self.heartbeat_path, hb)
        if result is not None:
            self.results.append(result)
        return result

    # ---------- 多轮 ----------

    def run(self, start_ms: int, until_ms: int) -> dict:
        executed = failed = skipped = 0
        stopped = False
        as_of = start_ms
        while as_of <= until_ms:
            if self.config.max_rounds is not None and executed >= self.config.max_rounds:
                break
            try:
                self.step(as_of)
            except LoopStopped:
                stopped = True
                break
            if self.last_status == STATUS_OK:
                executed += 1
            elif self.last_status == STATUS_FAILED:
                failed += 1
            else:
                skipped += 1
            as_of += self.config.interval_ms
        return {
            "executed": executed,          # 成功决策数（= 日志行数）
            "failed": failed,
            "skipped": skipped,
            "stopped": stopped,
            "last_round_ts_ms": as_of - self.config.interval_ms,
            "circuit": self.circuit.to_dict(),
        }