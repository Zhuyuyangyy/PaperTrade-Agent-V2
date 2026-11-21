"""策略层契约（Sprint 2）。

一个策略只做三件事：声明自己**在什么市场状态下**适用、给出方向、给出失效条件。
它不碰仓位、不碰资金、不碰风控——那些是独立确定性程序的事。

``verification`` 是硬门：``UNTESTED`` / ``SHADOW`` 只能生成影子意图（不进正式模拟账本），
只有 ``VERIFIED`` 才允许占账本额度；``FALSIFIED`` 直接排除。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

VERIFICATION_STATES = ("UNTESTED", "SHADOW", "VERIFIED", "FALSIFIED")
SIDES = ("LONG", "SHORT")


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    version: str
    name: str
    applicable_trends: tuple[str, ...]
    applicable_vol: tuple[str, ...]
    sides: tuple[str, ...]
    verification: str = "UNTESTED"
    priority: int = 100
    max_holding_ms: int = 4 * 3_600_000

    def __post_init__(self) -> None:
        if self.verification not in VERIFICATION_STATES:
            raise ValueError(f"verification 非法: {self.verification}")
        for s in self.sides:
            if s not in SIDES:
                raise ValueError(f"side 非法: {s}")

    def applicable(self, trend: str, vol: str) -> bool:
        if trend not in self.applicable_trends:
            return False
        if self.applicable_vol and vol not in self.applicable_vol:
            return False
        return True


def make_intent_id(symbol: str, strategy_id: str, version: str, decision_ts_ms: int, side: str) -> str:
    """确定性意图 ID：同一决策时刻 + 同一策略版本 + 同一方向 → 同一 ID（幂等）。"""
    raw = f"{symbol}|{strategy_id}|{version}|{decision_ts_ms}|{side}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    decision_ts_ms: int
    symbol: str
    side: str
    strategy_id: str
    strategy_version: str
    entry_ref_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    expected_holding_ms: int
    confidence: Decimal
    reason: str
    evidence_codes: tuple[str, ...] = ()
    invalidations: tuple[str, ...] = ()
    shadow: bool = True
    market_state_trend: str = ""
    market_state_vol: str = ""
    market_state_risk: str = ""

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError(f"side 非法: {self.side}")
        if self.entry_ref_price <= 0:
            raise ValueError("entry_ref_price 必须 > 0")
        if self.side == "LONG" and not (self.stop_price < self.entry_ref_price < self.take_profit_price):
            raise ValueError("LONG 必须 stop < entry < tp")
        if self.side == "SHORT" and not (self.take_profit_price < self.entry_ref_price < self.stop_price):
            raise ValueError("SHORT 必须 tp < entry < stop")

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "decision_ts_ms": self.decision_ts_ms,
            "symbol": self.symbol,
            "side": self.side,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "entry_ref_price": str(self.entry_ref_price),
            "stop_price": str(self.stop_price),
            "take_profit_price": str(self.take_profit_price),
            "expected_holding_ms": self.expected_holding_ms,
            "confidence": str(self.confidence),
            "reason": self.reason,
            "evidence_codes": list(self.evidence_codes),
            "invalidations": list(self.invalidations),
            "shadow": self.shadow,
            "market_state_trend": self.market_state_trend,
            "market_state_vol": self.market_state_vol,
            "market_state_risk": self.market_state_risk,
        }


@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    version: str
    eligible: bool
    reason: str
    intent: TradeIntent | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "eligible": self.eligible,
            "reason": self.reason,
            "intent": self.intent.to_dict() if self.intent else None,
        }


@dataclass(frozen=True)
class Selection:
    decision: str                      # LONG / SHORT / HOLD
    intent: TradeIntent | None
    candidates: tuple[Candidate, ...] = ()
    hold_reason: str = ""
    shadow: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "intent": self.intent.to_dict() if self.intent else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "hold_reason": self.hold_reason,
            "shadow": self.shadow,
        }