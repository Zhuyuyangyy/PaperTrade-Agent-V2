"""策略层（Sprint 2）。

``select(MarketState) -> Selection``：只产生 TradeIntent，不碰仓位与资金。
"""
from .contracts import Candidate, Selection, StrategySpec, TradeIntent, make_intent_id
from .registry import DEFAULT_SPECS, select

__all__ = ["Candidate", "Selection", "StrategySpec", "TradeIntent", "make_intent_id",
           "DEFAULT_SPECS", "select"]