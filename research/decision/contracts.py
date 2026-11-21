"""决策提供者契约（DecisionProvider）。

同一条决策时刻、同一份行情快照，分别交给：
  - rules     现有三策略 + 注册表（确定性基线）
  - deepseek  新闻/事件理解后的结构化判断
  - jev       TypeSafe 风格的结构化判断 API
  - linear    从历史特征训练的轻量模型（可导出 JSON 权重）

所有 provider 的输出都只是 ``Decision``（研究记录），**不进入** `Selection`、
`RiskDecision`，也不能改 `VERIFIED`。谁更有效，由未见样本的净收益裁决。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from research.market.contracts import MarketState

CHOICES = ("LONG", "SHORT", "HOLD")
STATUSES = ("OK", "UNAVAILABLE", "NO_DATA", "ERROR")


@dataclass(frozen=True)
class MarketSnapshot:
    """一次决策时刻的完整输入（state + 五类情报特征）。"""

    as_of_ms: int
    symbol: str
    state: MarketState
    intel: Any = None                     # IntelFeatures 或 None

    def summary(self) -> dict:
        s = self.state
        f = s.features
        intel = self.intel
        return {
            "as_of_ms": self.as_of_ms,
            "symbol": self.symbol,
            "trend": s.trend,
            "volatility": s.volatility,
            "momentum": s.momentum,
            "risk": s.risk,
            "confidence": str(s.confidence),
            "adx": _num(f.get("adx")),
            "rsi": _num(f.get("rsi")),
            "atr_pct": _num(f.get("atr_pct")),
            "range_high": _num(f.get("range_high")),
            "range_low": _num(f.get("range_low")),
            "breakout_ref_high": _num(f.get("breakout_ref_high")),
            "breakout_ref_low": _num(f.get("breakout_ref_low")),
            "intel": intel.to_dict() if intel is not None else None,
            "critical_stale": bool(s.data_quality.critical_stale) if s.data_quality else True,
        }


def _num(v) -> str | None:
    if v is None:
        return None
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class Decision:
    provider: str
    version: str
    status: str
    choice: str
    score: float = 0.0
    confidence: Decimal = Decimal("0")
    rationale: str = ""
    evidence: tuple[str, ...] = ()
    latency_ms: int = 0
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status 非法: {self.status}")
        if self.choice not in CHOICES:
            raise ValueError(f"choice 非法: {self.choice}")

    @property
    def actionable(self) -> bool:
        return self.status == "OK" and self.choice != "HOLD"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "version": self.version,
            "status": self.status,
            "choice": self.choice,
            "score": round(float(self.score), 4),
            "confidence": str(self.confidence),
            "rationale": self.rationale,
            "evidence": list(self.evidence),
            "latency_ms": self.latency_ms,
            "extra": self.extra,
        }


class Provider(Protocol):
    name: str
    version: str

    def available(self) -> bool: ...

    def decide(self, snapshot: MarketSnapshot) -> Decision: ...


def unavailable(name: str, version: str, reason: str) -> Decision:
    return Decision(name, version, "UNAVAILABLE", "HOLD",
                    rationale=reason, extra={"reason": reason})