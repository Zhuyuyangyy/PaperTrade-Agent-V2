"""Diagnosis: not just "the strategy died" but HOW, and what to do next.

The falsification battery answers "is this strategy real". A customer with a
real strategy on the line needs the next two answers: "which failure is this"
and "what would I even change". This module derives both from the SAME
measurements the battery already made -- no new runs, no new assumptions.

The classification rules below are the contract; they are declared here, in
code, before any strategy is classified -- the same rules-before-numbers
discipline the falsification rules follow. Priority order is part of the
contract: a strategy can trip several causes at once, and the PRIMARY cause
is the most fundamental one, because fixing a symptom while the root cause
stands is how people waste another six months.

Cause taxonomy (primary, by priority):
  insufficient  verdict is INCONCLUSIVE: not a death, a non-measurement.
                Nothing below it can be assessed honestly.
  randomness    entries cannot beat random entries (CI spans zero) or lose
                to them (CI entirely negative). The most fundamental
                failure: the entry logic contributes nothing (or worse).
  cost          the edge is REAL (control proves selectivity) but the book
                only survives at the declared fee -- doubling it kills the
                strategy. A toll problem, not an edge problem.
  edge_absent   the book loses even at 1x fees AND selectivity is not
                proven: no measurable edge at all.
  overfitting   neither of the above fired (selectivity IS proven), but the
                edge collapses out of sample or only exists in a few grid
                cells. Real in-sample, absent where it matters.
  barrier       the take-profit level is essentially never visited inside
                the hold window: a lottery structure wearing a backtest.
  survived      NOT_FALSIFIED. Reported with the standing caveat that this
                is the absence of disproof, not the presence of proof.

Prescriptions are actionable and numeric: each one quotes the customer's own
numbers from THIS run, because "reduce fees" is noise and "your +0.03%/trade
becomes -0.41%/trade at 8bps round trip" is a decision.
"""
from __future__ import annotations

# The taxonomy, in priority order. Kept as data so the report can print the
# whole ladder (a reader deserves to see what was ruled out and why).
CAUSES = [
    ("insufficient", "样本不足"),
    ("randomness", "随机性杀死"),
    ("cost", "成本杀死"),
    ("edge_absent", "无 edge"),
    ("overfitting", "过拟合杀死"),
    ("barrier", "结构杀死（彩票）"),
    ("survived", "未被证伪"),
]


def _rule_triggered(verdict: dict, rule_prefix: str) -> bool:
    return any(
        c.get("triggered") and str(c.get("rule", "")).startswith(rule_prefix)
        for c in verdict.get("checks") or []
    )


def _pct(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:+.{digits}f}%"


def diagnose(result: dict) -> dict:
    """Classify the failure mode(s) of one verified strategy and prescribe.

    Returns {"primary": str, "primary_label": str, "tags": [...],
             "evidence": [str, ...], "prescriptions": [str, ...]}.
    """
    verdict = result.get("verdict") or {}
    outcome = verdict.get("outcome", "?")
    runs = result.get("runs") or {}
    worst = (runs.get("worst_case") or {}).get("stats") or {}
    ctrl = result.get("control") or {}
    selectivity = ctrl.get("selectivity")
    diff = ctrl.get("diff_pct")
    ci = ctrl.get("diff_ci") or [None, None]
    cost_rows = result.get("cost_sensitivity") or []
    row_x2 = next((r for r in cost_rows if r.get("fee_multiple") == 2), None)
    wf = result.get("walk_forward") or {}
    oos = (wf.get("out_sample") or {}).get("runs", {}).get("worst_case", {}).get("stats") or {}
    isb = (wf.get("in_sample") or {}).get("runs", {}).get("worst_case", {}).get("stats") or {}
    grid = wf.get("out_sample_grid") or {}
    br = result.get("barrier_reach") or {}

    worst_net = worst.get("sum_pct")
    x2_net = row_x2.get("net_sum_pct") if row_x2 else None
    evidence: list[str] = []
    tags: list[str] = []

    def ev(s: str) -> None:
        if s not in evidence:
            evidence.append(s)

    # ---- the classification tree, in priority order -----------------------
    if outcome == "INCONCLUSIVE":
        primary = "insufficient"
        min_fills = next(
            (c for c in verdict.get("checks") or [] if c.get("insufficient")), None)
        ev(f"成交 {worst.get('n', 0)} 笔，低于 spec 声明的证据门槛——"
           + (min_fills["detail"] if min_fills else "样本不足"))
    elif outcome == "NOT_FALSIFIED":
        # Nothing died: no cause below applies, however the numbers look.
        primary = "survived"
    elif selectivity == "refuted":
        primary = "randomness"
        ev(f"对照组：策略 − 随机 = {_pct(diff)}/笔，95% 区间 [{_pct(ci[0])}, {_pct(ci[1])}] "
           f"整体为负——入场逻辑显著差于随机")
    elif selectivity == "unproven":
        primary = "randomness"
        ev(f"对照组：策略 − 随机 = {_pct(diff)}/笔，95% 区间 [{_pct(ci[0])}, {_pct(ci[1])}] "
           f"跨 0——无法证明入场逻辑优于随机")
        if worst_net is not None and worst_net > 0:
            ev(f"账面净收益 {_pct(worst_net)} 是真实的，但它与随机入场的差异在统计上不存在："
               "赚的是 beta，包装成 alpha")
    elif x2_net is not None and x2_net <= 0 < (worst_net or 0):
        primary = "cost"
        ev(f"×1 费率净 {_pct(worst_net)}，×2 费率净 {_pct(x2_net)}——edge 真实存在，"
           "但付不起加倍的 toll")
    elif worst_net is not None and worst_net <= 0:
        primary = "edge_absent"
        ev(f"×1 费率下净收益 {_pct(worst_net)}，且对照组未能证明选择性——"
           "没有可测量的正期望")
    elif oos.get("sum_pct") is not None and isb.get("sum_pct") is not None:
        primary = "overfitting"
        ev(f"样本内净 {_pct(isb['sum_pct'])} vs 样本外净 {_pct(oos['sum_pct'])}——"
           "edge 在没见过的数据上撑不住")
    elif br.get("reached_tp_pct") is not None and br["reached_tp_pct"] < 10:
        primary = "barrier"
        ev(f"止盈位在持仓窗口内仅被摸到 {br['reached_tp_pct']:.1f}%——盈利期望是彩票结构")
    else:
        primary = "insufficient"  # FALSIFIED but none of the above: say so honestly
        ev("裁决为 FALSIFIED 但死因无法归入上述类别——请人工看第 7 节规则判定表")

    # ---- secondary tags (a strategy can fail several ways at once) --------
    def add_tag(t: str) -> None:
        if t not in tags:
            tags.append(t)

    if primary != "insufficient" and outcome == "INCONCLUSIVE":
        add_tag("insufficient")
    if selectivity in ("unproven", "refuted") and primary != "randomness":
        add_tag("randomness")
    if x2_net is not None and x2_net <= 0 < (worst_net or 0) and primary != "cost":
        add_tag("cost")
    if oos.get("sum_pct") is not None and isb.get("sum_pct") is not None \
            and oos["sum_pct"] < isb["sum_pct"] * 0.5 and primary != "overfitting":
        add_tag("overfitting")
        ev(f"样本外净 {_pct(oos['sum_pct'])} 不足样本内 {_pct(isb['sum_pct'])} 的一半")
    if grid.get("positive_ratio") is not None and grid["positive_ratio"] < 0.5 \
            and primary != "overfitting":
        add_tag("overfitting")
        ev(f"样本外参数网格仅 {grid.get('positive_cells', 0)}/{grid.get('n_cells', 0)} 格为正——"
           "edge 只在一小撮参数组合下存在")
    if br.get("reached_tp_pct") is not None and br["reached_tp_pct"] < 10 \
            and primary not in ("barrier", "insufficient"):
        add_tag("barrier")

    # ---- prescriptions: numeric, actionable, tied to THIS run --------------
    rx: list[str] = []
    if primary == "insufficient":
        rx.append(f"先解决样本量：成交 {worst.get('n', 0)} 笔不足以支撑任何结论。"
                  "扩窗口、加标的，或放宽入场阈值——但放宽后要在报告里重新声明 min_fills，"
                  "不允许用同一个宽松阈值既扩样本又保裁决")
    elif primary == "randomness":
        rx.append("把入场条件加严一个量级：趋势过滤（如只在 EMA 多头侧做多）+ 时段过滤 + "
                  "量能确认。信号数降到现在的 1/5 再测——宁少勿滥，30 笔真信号胜过 300 笔噪声")
        if worst_net is not None and worst_net > 0:
            rx.append(f"承认定位：这份账面（{_pct(worst_net)}）大概率是 beta。若目标就是赚 beta，"
                      "该做的是定投/资产配置工具，不是策略引擎——先把目的想清楚，再决定要不要继续")
        else:
            rx.append(f"账面 {_pct(worst_net)} 且打不过随机：回到假设阶段——这个 edge 的"
                      "市场学基础是什么（谁在犯错、错误定价为什么持续）？写不出来就不要回测了")
    elif primary == "cost":
        rx.append("三条路选一条：① 降频——当前每笔净 "
                  f"{_pct((worst.get('mean_pct')))}，频率砍半则 toll 占比同步砍半；"
                  "② 改 maker 限价成交，单边费率通常减半；③ 拉长持仓周期，"
                  "让每笔毛利润覆盖 toll（当前 ×2 成本净 "
                  f"{_pct(x2_net)}，这就是 toll 预算的上限）")
    elif primary == "edge_absent":
        rx.append("1x 费率下就亏损且无选择性证据：不要调参，调参是把噪声拟合得更漂亮。"
                  "回到策略假设本身，或换标的/周期重测")
    elif primary == "overfitting":
        rx.append("砍自由度：参数空间砍到 1-2 个自由度再测；若必须多参数，"
                  "用滚动前向验证（每折内重新选参）而不是单次划分")
        if grid.get("n_cells"):
            rx.append(f"当前网格 {grid.get('positive_cells', 0)}/{grid.get('n_cells', 0)} 格为正："
                      "把表现最好的格子附近再加密采样——如果 edge 真实，邻域应该活着；"
                      "邻域全死说明你拟合了一个尖峰")
    elif primary == "barrier":
        rx.append(f"止盈位只被摸到 {br.get('reached_tp_pct', 0):.1f}%：把止盈放到市场真实波动"
                  "距离上（用 ATR 或近期已实现波动校准），别用整数百分比的心理价位")
    elif primary == "survived":
        rx.append("未被证伪 ≠ 被证实。下一步是模拟盘跑满一个完整的样本外周期——"
                  "本引擎回答统计问题，不回答执行问题（滑点、成交率、心理）")

    for t in tags:
        if t == "cost" and primary != "cost":
            rx.append(f"附带问题：成本翻倍即亏损（×2 净 {_pct(x2_net)}）——即使主死因修好了，"
                      "toll 预算仍是硬约束")
        if t == "overfitting" and primary != "overfitting":
            rx.append("附带问题：样本外/网格显示参数脆弱——主死因修好后，用更少的参数重测")

    label = dict(CAUSES).get(primary, primary)
    return {
        "primary": primary,
        "primary_label": label,
        "tags": tags,
        "tag_labels": [dict(CAUSES).get(t, t) for t in tags],
        "evidence": evidence,
        "prescriptions": rx,
    }
