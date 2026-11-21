"""OpenAlpha L2（Sprint 1）：市场理解层。

对外只暴露一个入口：``analyze(symbol, candles_by_tf, as_of_ms) -> MarketState``。
策略层消费 MarketState，不再自己算指标；本包不产生交易信号。
"""
from .contracts import (
    SCHEMA_VERSION,
    TIMEFRAMES,
    Candle,
    Contradiction,
    DataQuality,
    Evidence,
    MarketState,
    TimeframeView,
)
from .klines import (
    aggregate,
    closed_only,
    ensure_contiguous,
    gaps,
    load_csv,
    resample,
)
from .registry import MarketAssessment, assess, assess_all, usable
from .state import analyze

__all__ = [
    "SCHEMA_VERSION",
    "TIMEFRAMES",
    "Candle",
    "Contradiction",
    "DataQuality",
    "Evidence",
    "MarketState",
    "TimeframeView",
    "aggregate",
    "closed_only",
    "ensure_contiguous",
    "gaps",
    "load_csv",
    "resample",
    "MarketAssessment",
    "assess",
    "assess_all",
    "usable",
    "analyze",
]