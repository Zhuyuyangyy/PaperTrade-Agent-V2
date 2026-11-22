"""只读汇总配对回放：净收益、利润捕获、回撤、成本、AI 调用与样本相关性。"""
from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

GROUPS = ("A", "B", "C", "D")
REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL")


def _d(v) -> D:
    try:
        return D(str(v or 0))
    except Exception:  # noqa: BLE001
        return D("0")


def _rows(run: Path) -> list[dict]:
    p = run / "paired_results.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    pnls = [_d(r["result"].get("net_pnl")) for r in rows]
    realized = [_d(r["result"].get("realized_pnl")) for r in rows]
    unreal = [_d(r["result"].get("unrealized_pnl")) for r in rows]
    mfe = [_d(r["result"].get("mfe")) for r in rows]
    hold = [int(r["result"].get("holding_ms") or 0) for r in rows]
    fees = [_d(r["result"].get("fees_paid")) for r in rows]
    peak, dd = D("0"), D("0")
    equity = D("1000")
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak if peak > 0 else D("0"))
    capture = (sum(pnls) / sum(mfe) * D("100")) if sum(mfe) > 0 else D("0")
    return {
        "n": len(rows),
        "net_pnl_sum": str(sum(pnls)),
        "net_pnl_avg": str(sum(pnls) / len(rows)),
        "median_pnl": str(sorted(pnls)[len(pnls) // 2]),
        "win_rate_pct": str(D(sum(1 for p in pnls if p > 0)) / len(rows) * D("100")),
        "realized_sum": str(sum(realized)),
        "unrealized_sum": str(sum(unreal)),
        "profit_capture_pct_of_mfe": str(capture),  # 可为负；仅作相对指标
        "max_drawdown_pct": str(dd * D("100")),
        "avg_holding_ms": str(sum(hold) / len(rows)),
        "fees_sum": str(sum(fees)),
        "open_at_end": sum(1 for r in rows if r["result"].get("exit_reason") == "OPEN_AT_WINDOW_END"),
    }


def _ai_stats(rows: list[dict]) -> dict:
    calls = sum(int(r["result"].get("ai_calls") or 0) for r in rows)
    ok = sum(int(r["result"].get("ai_ok") or 0) for r in rows)
    hold = sum(int(r["result"].get("ai_hold") or 0) for r in rows)
    fail = sum(int(r["result"].get("ai_fail") or 0) for r in rows)
    mgmt = sum(1 for r in rows for a in r["result"].get("actions", [])
               if a.get("action") in ("MOVE_STOP", "UPDATE_TP", "CLOSE_PARTIAL", "CLOSE_ALL"))
    no_ai = sum(1 for r in rows if any(a.get("action") == "NO_AI"
                                       for a in r["result"].get("actions", [])))
    return {"calls": calls, "ok": ok, "hold": hold, "fail": fail,
            "management_actions": mgmt, "no_ai_events": no_ai,
            "success_rate_pct": str(D(ok) / calls * D("100")) if calls else "0"}


def _overlap(rows: list[dict], horizon_bars: int) -> dict:
    times = sorted({int(r["event"]["as_of_ms"]) for r in rows})
    span = horizon_bars * 300_000
    pairs = 0
    for i, t in enumerate(times):
        pairs += sum(1 for u in times[i + 1:] if u - t < span)
    n = len(times)
    return {"events": n, "overlapping_pairs": pairs,
            "overlap_ratio": str(D(pairs) / D(n * (n - 1) // 2)) if n > 1 else "0",
            "note": "重叠窗口的交易不是独立样本，置信区间需按有效样本折减"}


def analyze_run(run: Path, *, top_n: int = 12) -> dict:
    rows = _rows(run)
    cfg = json.loads((run / "config.json").read_text(encoding="utf-8")) \
        if (run / "config.json").exists() else {}
    horizon = int(cfg.get("horizon_bars", 72))
    by_group = {g: _stats([r for r in rows if r["result"]["group"] == g]) for g in GROUPS}
    by_regime = {
        regime: {g: _stats([r for r in rows if r["result"]["group"] == g
                            and r["event"].get("regime") == regime]) for g in GROUPS}
        for regime in REGIMES
    }
    ordered = sorted(rows, key=lambda r: r["event"]["as_of_ms"])
    cut = int(len(ordered) * 0.7)
    split = {
        "research_70pct": {g: _stats([r for r in ordered[:cut]
                                      if r["result"]["group"] == g]) for g in GROUPS},
        "oos_last_30pct": {g: _stats([r for r in ordered[cut:]
                                      if r["result"]["group"] == g]) for g in GROUPS},
    }
    d_rows = [r for r in rows if r["result"]["group"] == "D"]
    ai = _ai_stats(d_rows)
    if ai["ok"] == 0:
        ai["verdict"] = "NO_AI_CALL: D 组不得标注为 AI 持仓管理"
    elif ai["management_actions"] == 0:
        ai["verdict"] = "AI_HOLD_ONLY: 与固定持有几乎等价，不得宣称动态管理优势"
    else:
        ai["verdict"] = "AI_MANAGEMENT_CALLED"
    return {
        "run": str(run), "config": cfg, "groups": by_group, "by_regime": by_regime,
        "time_split": split, "ai_position_manager": ai,
        "sample_overlap": _overlap(rows, horizon),
        "notes": [
            "净收益含窗口末盯市与预计平仓成本；未平仓不再记 0。",
            "新闻为 NEWS_UNAVAILABLE，D 组没有外部情报输入。",
            "AI 推理费用尚未计入净收益；样本外区间仅按时间顺序划分。",
        ],
    }
