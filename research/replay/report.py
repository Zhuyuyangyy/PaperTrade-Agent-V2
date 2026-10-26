"""Replay reporting: the numbers, plus the caveats that keep them honest.

Every report states how much of the candidate history had a forward price at
all. A replay over 30 labelled candidates and a replay over 30,000 are not the
same claim, and the report says so on its first line.
"""
from __future__ import annotations

from .engine import ReplayResult


def _split_by_time(trades, ratio: float = 0.7) -> tuple[list, list]:
    """Split on the time axis, never randomly: shuffling a time series leaks."""
    ordered = sorted(trades, key=lambda t: t.opened_ms)
    cut = int(len(ordered) * ratio)
    return ordered[:cut], ordered[cut:]


def _summarise(result: ReplayResult) -> dict:
    return {
        "label": result.label,
        "trades": len(result.trades),
        "closed": len(result.closed),
        "open_at_end": len(result.trades) - len(result.closed),
        "net_usd": result.net_usd,
        "expectancy_usd": result.expectancy_usd,
        "win_rate_pct": result.win_rate,
        "profit_factor": result.profit_factor,
        "timeout_share_pct": result.timeout_share,
        "avg_hold_minutes": result.avg_hold_minutes,
        "max_drawdown_pct": result.max_drawdown_pct(),
        "exit_reasons": _exit_reasons(result),
    }


def _exit_reasons(result: ReplayResult) -> dict:
    out: dict[str, int] = {}
    for trade in result.closed:
        out[trade.exit_reason or "?"] = out.get(trade.exit_reason or "?", 0) + 1
    return out


def build_report(result: ReplayResult, coverage: dict | None = None) -> str:
    """Human-readable report. Coverage first, so the rest is read in context."""
    lines: list[str] = []
    if coverage:
        lines.append(
            f"标注覆盖：{coverage['with_marks']}/{coverage['candidates']} 个候选有后续价格"
            f"（{coverage['coverage_pct']}%）；eligible {coverage['eligible_with_marks']}/{coverage['eligible']}"
        )
        if coverage["eligible"] and coverage["eligible_with_marks"] < 20:
            lines.append(
                "⚠️ 有后续价格的 eligible 候选不足 20 个，结论只能是方向性的，不是统计显著的"
            )
    lines.append("")
    summary = _summarise(result)
    lines.append(f"回放 [{summary['label']}]")
    lines.append(f"  开仓 {summary['trades']} 笔 · 平仓 {summary['closed']} 笔 · 数据末尾仍持仓 {summary['open_at_end']} 笔")
    lines.append(f"  净收益        {summary['net_usd']:+.2f} USD")
    if summary["expectancy_usd"] is not None:
        lines.append(f"  单笔期望      {summary['expectancy_usd']:+.4f} USD")
    if summary["win_rate_pct"] is not None:
        lines.append(f"  胜率          {summary['win_rate_pct']:.1f}%")
    if summary["profit_factor"] is not None:
        lines.append(f"  盈亏比        {summary['profit_factor']:.2f}")
    if summary["timeout_share_pct"] is not None:
        lines.append(f"  TIMEOUT 占比  {summary['timeout_share_pct']:.1f}%")
    if summary["avg_hold_minutes"] is not None:
        lines.append(f"  平均持仓      {summary['avg_hold_minutes']:.0f} 分钟")
    if summary["max_drawdown_pct"] is not None:
        lines.append(f"  最大回撤      {summary['max_drawdown_pct']:.2f}%")
    if summary["exit_reasons"]:
        reasons = " · ".join(f"{k} {v}" for k, v in sorted(summary["exit_reasons"].items()))
        lines.append(f"  退出原因      {reasons}")

    dev, test = _split_by_time(result.trades)
    if dev and test:
        lines.append("")
        lines.append(f"时间划分：开发集 {len(dev)} 笔（前 70%）· 独立测试集 {len(test)} 笔（后 30%，未参与任何调参）")
        for name, part in (("开发集", dev), ("测试集", test)):
            sub = ReplayResult(label=name, params=result.params, trades=part)
            if sub.closed:
                lines.append(
                    f"  {name}: 净 {sub.net_usd:+.2f} · 期望 {sub.expectancy_usd:+.4f}"
                    f" · 胜率 {sub.win_rate_pct}% · TIMEOUT {sub.timeout_share_pct}%"
                )
            else:
                lines.append(f"  {name}: 无平仓，无法评估")
    for note in result.notes:
        lines.append(f"注：{note}")
    return "\n".join(lines)


def cost_sensitivity(root, base_params: dict, bps_levels=(0, 60, 120, 240)) -> str:
    """Same decisions, different cost assumptions. A rule that only works at
    0 bps is not an edge; it is a fee subsidy."""
    from .engine import ReplayEngine

    lines = ["成本敏感性（同一批决策，只改往返成本假设）", "  bps    净收益    单笔期望   胜率"]
    for bps in bps_levels:
        params = dict(base_params, round_trip_cost_bps=float(bps))
        engine = ReplayEngine(root, params)
        try:
            result = engine.run(label=f"cost-{bps}")
        finally:
            engine.close()
        expectancy = result.expectancy_usd
        win = result.win_rate
        lines.append(
            f"  {bps:<6} {result.net_usd:+8.2f}  "
            f"{(f'{expectancy:+.4f}' if expectancy is not None else '--'):>9}  "
            f"{(f'{win:.1f}%' if win is not None else '--'):>6}"
        )
    return "\n".join(lines)
