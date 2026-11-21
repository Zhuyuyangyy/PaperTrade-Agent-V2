"""情报层（Sprint 3 + Alpha Discovery V1）：新闻 / Polymarket / 聪明钱，advisory_only。"""
from .ablation import evaluate as evaluate_ablation, signal as ablation_signal
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
from .feeds import FeedItem, fetch_feeds, parse_feed
from .features import IntelFeatures, compute as compute_features
from .news import NewsStore
from .news_llm import ExtractedEvent, LLMConfig, extract as extract_news, rule_extract
from .pipeline import IntelPipelineConfig, collect as collect_intel
from .polymarket import assess_market, screen
from .polymarket_fetch import fetch_books, fetch_gamma, select_btc_markets, to_events
from .text_safety import detect_injection, extract_facts, sanitize_text, text_hash
from .wallets import WalletTrade, analyze as analyze_wallet_trades, scorecard

__all__ = [
    "Fact", "IntelDigest", "NewsEvent", "PredictionMarketEvent", "SourceRef", "WalletSignal",
    "evaluate_ablation", "ablation_signal",
    "attach_to_log", "build_digest",
    "evaluate_incremental", "required_next_step", "verdict_is_actionable",
    "FeedItem", "fetch_feeds", "parse_feed",
    "IntelFeatures", "compute_features",
    "NewsStore",
    "ExtractedEvent", "LLMConfig", "extract_news", "rule_extract",
    "IntelPipelineConfig", "collect_intel",
    "assess_market", "screen",
    "fetch_books", "fetch_gamma", "select_btc_markets", "to_events",
    "detect_injection", "extract_facts", "sanitize_text", "text_hash",
    "WalletTrade", "analyze_wallet_trades", "scorecard",
]