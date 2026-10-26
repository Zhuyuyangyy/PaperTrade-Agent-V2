"""Replay Engine V1: offline, deterministic, paper-only.

Answers one question with recorded data instead of opinion: did the rule-based
strategy keep a positive edge after costs, on a time window it never saw?
"""
from .engine import DEFAULT_PARAMS, ReplayEngine, ReplayResult, ReplayTrade  # noqa: F401
from .labels import (  # noqa: F401
    candidate_returns,
    collect_once,
    ensure_schema,
    label_coverage,
    quote_mints,
)
from .report import build_report, cost_sensitivity  # noqa: F401
