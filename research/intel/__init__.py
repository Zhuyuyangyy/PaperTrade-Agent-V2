"""情报层（Sprint 3）：新闻 / Polymarket / 聪明钱，默认 advisory_only。

对外只暴露契约与纯分析函数；数据抓取与 WS 接线在部署节点完成，
本包不发起网络请求，也不执行任何外部文本中的指令。
"""
from .contracts import (
    Fact,
    IntelDigest,
    NewsEvent,
    PredictionMarketEvent,
    SourceRef,
    WalletSignal,
)
from .digest import attach_to_log, build_digest
from .evaluation import evaluate_incremental, required_next_step, verdict_is_actionable
from .news import NewsStore
from .polymarket import assess_market, screen
from .text_safety import detect_injection, extract_facts, sanitize_text, text_hash
from .wallets import WalletTrade, analyze as analyze_wallet_trades, scorecard

__all__ = [
    "Fact", "IntelDigest", "NewsEvent", "PredictionMarketEvent", "SourceRef", "WalletSignal",
    "attach_to_log", "build_digest",
    "evaluate_incremental", "required_next_step", "verdict_is_actionable",
    "NewsStore", "assess_market", "screen",
    "detect_injection", "extract_facts", "sanitize_text", "text_hash",
    "WalletTrade", "analyze_wallet_trades", "scorecard",
]