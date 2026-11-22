"""实验 E 只读汇总：扣费收益 / 机会成本 / 回撤 / 推理成本 / 样本外。"""
from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

GROUPS = ("A", "B", "C", "D")
LABELS = {"A": "旧版DeepSeek(1h)", "B": "DeepSeek多周期",
          "C": "确定性多周期规则", "D": "规则+AI过滤"}


def _d(v) -> D:
    try:
        return D(str(v or 0))
    except Exception:  # noqa: BLE001
        return D("0")


def _stats(decisions: list[dict], trades: list[dict]) -> dict:
    n_long = sum(1 for d in decisions if d["choice"] == "LONG")
    n_short = sum(1 for d in decisions if d["choice"] == "SHORT")
    n_hold = sum(1 for d in decisions if d["choice"] == "HOLD")
    n_err = sum(1 for d in decisions if d["status"] not in ("OK",))
    pnl = sum((_d(t.get("net_pnl")) for t in trades), D("0"))
    wins = sum(1 for t in trades if _d(t.get("net_pnl")) > 0)
    fees = sum((_d(t.get("entry_fee")) + _d(t.get("exit_fee")) for t in trades), D("0"))
    cost = sum((_d(d.get("cost_usd")) for d in decisions), D("0"))
    # 机会成本：HOLD 的那些 bar 若按"下一根开盘→再下一根收盘"持有，平均可获得的毛变动
    # （仅作机会成本参照，不是策略收益；不能用未来数据下单）
    equity, peak, dd = D("1000"), D("1000"), D("0")
    for t in sorted(trades, key=lambda x: x.get("signal_bar", 0)):
        equity += _d(t.get("net_pnl"))
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak if peak > 0 else D("0"))
    return {
        "label": LABELS.get(decisions[0].get("group", "") if decisions else "", ""),
        "decisions": len(decisions), "long": n_long, "short": n_short, "hold": n_hold,
        "errors": n_err, "trades": len(trades), "win_rate_pct": str(
            (D(wins) / len(trades) * 100).quantize(D("0.01"))) if trades else "0",
        "net_pnl_usdt": str(pnl.quantize(D("0.00000001"))),
        "avg_per_trade": str((pnl / len(trades)).quantize(D("0.00000001"))) if trades else "0",
        "fees_usdt": str(fees.quantize(D("0.00000001"))),
        "inference_cost_usd": str(cost.quantize(D("0.00000001"))),
        "max_drawdown_pct": str((dd * 100).quantize(D("0.0001"))),
        "exit_reasons": _counts(t.get("exit_reason", "?") for t in trades),
    }


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def report(run: Path) -> dict:
    result = json.loads((run / "exp_result.json").read_text(encoding="utf-8"))
    decisions = result["decisions"]
    trades = result["trades"]
    by_group = {}
    by_split = {}
    for g in GROUPS:
        ds = [d for d in decisions if d["group"] == g]
        ts = [t for t in trades if t["group"] == g]
        by_group[g] = _stats(ds, ts)
        for split in ("research", "oos"):
            by_split.setdefault(split, {})[g] = _stats(
                [d for d in ds if d["split"] == split],
                [t for t in ts if t["split"] == split])
    # 覆盖度：三组在同 bar 是否一致 HOLD（对齐入场机会）
    # 只看 A/B/C 三组（D 仅在规则出信号时才存在，不能要求四组齐全）
    core = ("A", "B", "C")
    bar_map: dict[int, dict] = {}
    for d in decisions:
        bar_map.setdefault(d["bar"], {})[d["group"]] = d["choice"]
    complete = {b: m for b, m in bar_map.items() if all(g in m for g in core)}
    aligned = sum(1 for m in complete.values()
                  if all(m[g] == "HOLD" for g in core))
    any_signal = sum(1 for m in complete.values()
                     if any(m[g] != "HOLD" for g in core))
    # P1b：AI 过滤器的逐笔配对效果
    filt = [d for d in decisions if d["group"] == "D"]
    c_trades = {t["bar"]: t for t in trades if t["group"] == "C"}
    d_trades = {t["bar"]: t for t in trades if t["group"] == "D"}
    accepted = sum(1 for d in filt if d["choice"] != "HOLD")
    rejected = sum(1 for d in filt if d["choice"] == "HOLD")
    # 被拒绝的信号若照单全做，会是多少盈亏（机会成本/规避的亏损）
    avoided = sum((_d(c_trades[d["bar"]].get("net_pnl")) for d in filt
                   if d["choice"] == "HOLD" and d["bar"] in c_trades), D("0"))
    kept = sum((_d(d_trades[b].get("net_pnl")) for b in d_trades), D("0"))
    baseline = sum((_d(t.get("net_pnl")) for b, t in c_trades.items()
                    if b in {d["bar"] for d in filt}), D("0"))
    filter_stats = {"calls": len(filt), "accepted": accepted, "rejected": rejected,
                    "accept_rate_pct": str((D(accepted) / len(filt) * 100).quantize(D("0.01")))
                                       if filt else "0",
                    "rejected_signals_net_pnl_if_taken": str(avoided.quantize(D("0.00000001"))),
                    "accepted_net_pnl_usdt": str(kept.quantize(D("0.00000001"))),
                    "filtered_baseline_net_pnl_usdt": str(baseline.quantize(D("0.00000001"))),
                    "filter_cost_usd": str(sum((_d(d.get("cost_usd")) for d in filt), D("0")))}
    return {
        "run": str(run), "meta": result.get("meta", {}), "config": result.get("config", {}),
        "groups": by_group, "by_split": by_split, "entry_filter": filter_stats,
        "signal_coverage": {
            "decision_bars": len(complete), "all_hold_bars": aligned,
            "any_signal_bars": any_signal,
            "pct": str((D(any_signal) / len(complete) * 100).quantize(D("0.01")))
                   if complete else "0"},
        "notes": [
            "收益已扣双边手续费/滑点/资金费；成交按信号下一根开盘价（无前视）。",
            "推理成本按 API 真实 usage 估算；无 usage 时用保守 $/call 估算。",
            "research/oos 严格按时间切分；样本外只评估一次，未调参。",
            "HOLD 机会成本仅为参照，不能当作可实现收益。",
        ],
    }


__all__ = ["report"]
