"""熔断（Sprint 4）：连续失败 → 停止开新仓；连续恢复 → 才解除。

规则：
  - 连续 `fail_threshold` 次失败（数据源异常、状态计算异常）→ halted；
  - halted 期间照常 mark/expire 与写日志（不能"假装没运行"），但**不允许开新仓**；
  - 连续 `recover_threshold` 次成功才解除；解除后仍从 0 开始计数。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CircuitConfig:
    fail_threshold: int = 3
    recover_threshold: int = 2


class CircuitBreaker:
    def __init__(self, config: CircuitConfig | None = None):
        self.config = config or CircuitConfig()
        self.failures = 0
        self.successes = 0
        self.halted = False
        self.reason = ""
        self.trips = 0

    def record_failure(self, reason: str) -> None:
        self.failures += 1
        self.successes = 0
        if not self.halted and self.failures >= self.config.fail_threshold:
            self.halted = True
            self.trips += 1
            self.reason = f"连续失败 {self.failures} 次：{reason}"

    def record_success(self) -> None:
        self.failures = 0
        if not self.halted:
            return
        self.successes += 1
        if self.successes >= self.config.recover_threshold:
            self.halted = False
            self.reason = ""
            self.successes = 0

    def reset(self) -> None:
        self.failures = 0
        self.successes = 0
        self.halted = False
        self.reason = ""

    @classmethod
    def from_dict(cls, data: dict | None) -> "CircuitBreaker":
        """从心跳恢复熔断状态：重启不能把"已经熔断"变成"从未发生"。"""
        cfg = CircuitConfig(
            fail_threshold=int((data or {}).get("fail_threshold", 3)),
            recover_threshold=int((data or {}).get("recover_threshold", 2)),
        )
        cb = cls(cfg)
        if not data:
            return cb
        cb.failures = int(data.get("failures", 0))
        cb.successes = int(data.get("successes", 0))
        cb.halted = bool(data.get("halted", False))
        cb.reason = str(data.get("reason", ""))
        cb.trips = int(data.get("trips", 0))
        return cb

    def to_dict(self) -> dict:
        return {
            "halted": self.halted,
            "reason": self.reason,
            "failures": self.failures,
            "successes": self.successes,
            "trips": self.trips,
            "fail_threshold": self.config.fail_threshold,
            "recover_threshold": self.config.recover_threshold,
        }