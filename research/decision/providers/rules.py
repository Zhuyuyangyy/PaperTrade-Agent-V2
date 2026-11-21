"""现有规则引擎作为 DecisionProvider（确定性基线）。"""
from __future__ import annotations

from decimal import Decimal

from research.strategy.contracts import StrategySpec
from research.strategy.registry import DEFAULT_SPECS, select

from ..contracts import Decision, MarketSnapshot


class RulesProvider:
    name = "rules"
    version = "registry-v1"

    def __init__(self, specs: tuple[StrategySpec, ...] = DEFAULT_SPECS):
        self.specs = specs

    def available(self) -> bool:
        return True

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        sel = select(snapshot.state, self.specs)
        if sel.intent is not None:
            choice = "LONG" if sel.intent.side == "LONG" else "SHORT"
            rationale = sel.intent.reason
            evidence = (sel.intent.strategy_id,) + tuple(sel.intent.evidence_codes)
            score = 1.0 if choice == "LONG" else -1.0
            conf = snapshot.state.confidence
        else:
            choice = "HOLD"
            rationale = sel.hold_reason or "无策略适用"
            evidence = tuple(f"{c.strategy_id}={c.reason}" for c in sel.candidates if not c.eligible)[:4]
            score = 0.0
            conf = Decimal("0")
        return Decision(self.name, self.version, "OK", choice, score, conf,
                        rationale, evidence)