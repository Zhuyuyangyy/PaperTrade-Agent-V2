"""OPTIONAL daily TradingAgents adapter. Research only; never touches order APIs.

Usage requires independently installed upstream TradingAgents and its LLM credentials.
The main research worker can use this as an additional entry veto (require_buy).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from research.core import PAIR_MAP, atomic_write_json

ROOT = Path(__file__).resolve().parents[1]


def normalize_decision(value: Any) -> str | None:
    """Do NOT infer action from free-form prose ('not BUY' is not BUY)."""
    if isinstance(value, dict):
        for field in ("action", "signal", "decision", "recommendation"):
            if field in value:
                return normalize_decision(value[field])
        return None
    if isinstance(value, str) and value.strip().upper() in ("BUY", "HOLD", "SELL"):
        return value.strip().upper()
    return None


def run_bridge(pair: str, outdir: Path, day: str) -> dict:
    if pair not in PAIR_MAP:
        raise ValueError("Unsupported pair")
    if day != datetime.now(timezone.utc).date().isoformat():
        raise ValueError("Daily TA bias must be generated for TODAY only")
    provider = os.getenv("TA_LLM_PROVIDER", "").strip()
    quick = os.getenv("TA_QUICK_MODEL", "").strip()
    deep = os.getenv("TA_DEEP_MODEL", "").strip()
    if not all((provider, quick, deep)):
        raise RuntimeError("Set TA_LLM_PROVIDER, TA_QUICK_MODEL, TA_DEEP_MODEL explicitly (API costs may apply)")
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
    except ImportError as ex:
        raise RuntimeError("Install upstream TradingAgents separately; it is not a default dependency") from ex
    cfg = DEFAULT_CONFIG.copy()
    cfg.update({"llm_provider": provider, "quick_think_llm": quick,
                "deep_think_llm": deep, "max_debate_rounds": 1})
    graph = TradingAgentsGraph(debug=False, config=cfg)
    _, raw = graph.propagate(PAIR_MAP[pair], day)
    action = normalize_decision(raw)
    if action is None:
        raise ValueError("Upstream TradingAgents returned an ambiguous decision; bias was NOT written")
    observed = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload = {"schema_version": 1, "source": "TradingAgents", "pair": pair,
               "ticker": PAIR_MAP[pair], "analysis_date": day,
               "observed_at_ms": observed, "action": action,
               "notes": "Daily cross-vendor research bias; NEVER authorizes entry alone."}
    atomic_write_json(outdir / (pair.replace("/", "_") + ".json"), payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Optional daily TradingAgents vote producer")
    parser.add_argument("--pair", choices=list(PAIR_MAP), required=True)
    parser.add_argument("--date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "ta_bias")
    args = parser.parse_args()
    print(run_bridge(args.pair, args.out, args.date))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
