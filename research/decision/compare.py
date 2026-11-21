"""多 provider 对照（研究工具）：同一快照，各模型各投一票，全部只记录。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .contracts import Decision, MarketSnapshot, Provider


def run(snapshot: MarketSnapshot, providers: list[Provider],
        log_path: Path | str | None = None) -> list[Decision]:
    decisions: list[Decision] = []
    for p in providers:
        try:
            decisions.append(p.decide(snapshot))
        except Exception as exc:  # noqa: BLE001 - 单个 provider 失败不影响其它
            decisions.append(Decision(p.name, getattr(p, "version", ""), "ERROR", "HOLD",
                                      rationale=f"provider_error: {type(exc).__name__}"))
    if log_path is not None:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "as_of_ms": snapshot.as_of_ms,
            "symbol": snapshot.symbol,
            "completed_at_ms": int(time.time() * 1000),
            "snapshot": snapshot.summary(),
            "state": {"trend": snapshot.state.trend,
                      "volatility": snapshot.state.volatility,
                      "momentum": snapshot.state.momentum,
                      "risk": snapshot.state.risk},
            "decisions": [d.to_dict() for d in decisions],
            "note": "research only; not applied to Selection/Risk",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return decisions


def consensus(decisions: list[Decision]) -> dict:
    """各 provider 的方向票数（只用于观察，不参与交易）。"""
    votes = {"LONG": 0, "SHORT": 0, "HOLD": 0}
    for d in decisions:
        if d.status == "OK":
            votes[d.choice] += 1
    actionable = votes["LONG"] + votes["SHORT"]
    return {
        "votes": votes,
        "actionable": actionable,
        "agreement": (max(votes.values()) / max(1, sum(votes.values()))),
    }