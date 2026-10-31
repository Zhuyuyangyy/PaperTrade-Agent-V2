"""Diagnosis: not just "did it survive" but WHAT the evidence says, and what
to test next.

The falsification battery answers "is this strategy real". A customer with a
real strategy on the line needs the next answers: "which failure mode is
this", "what do I test next", and "what result would count as an answer".
This module derives all three from the SAME measurements the battery already
made -- no new runs, no new assumptions.

Two vocabulary decisions, both deliberate:

1. These are DIAGNOSTIC OUTCOMES, not "causes of death". A strategy that is
   NOT_FALSIFIED did not "survive a kill" -- it simply was not falsified
   under the declared rules. An INCONCLUSIVE result is not a death either:
   it is a non-measurement. Calling every outcome a death teaches customers
   to distrust the instrument when it says anything other than "guilty".

2. Every prescription is an EXPERIMENT HYPOTHESIS with an acceptance
   criterion, not advice. "Tighten your entries" is an opinion; "add one
   orthogonal filter, expect ~1/5 the signals, accept if the control CI's
   lower bound clears zero" is a falsifiable next step. The customer -- or
   the customer's critic -- can check whether the experiment was run and
   whether its acceptance criterion was met. Numbers get opinions nowhere.

The classification rules below are the contract; they are declared here, in
code, before any strategy is classified -- the same rules-before-numbers
discipline the falsification rules follow. Priority order is part of the
contract: a strategy can trip several outcomes at once, and the PRIMARY one
is the most fundamental reading of the evidence, because treating a symptom
while the root cause stands is how people waste another six months.

Outcome taxonomy (primary, by priority):
  insufficient  verdict is INCONCLUSIVE: not a death, a non-measurement.
                Nothing below it can be assessed honestly.
  randomness    entries cannot beat random entries (CI spans zero) or lose
                to them (CI entirely negative). The most fundamental
                reading: the entry logic contributes nothing (or worse).
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
"""
from __future__ import annotations

# The taxonomy, in priority order. Kept as data so the report can print the
# whole ladder (a reader deserves to see what was ruled out and why).
OUTCOMES = [
    ("insufficient", "证据不足"),
    ("randomness", "随机对照未通过"),
    ("cost", "成本敏感"),
    ("edge_absent", "正期望未证实"),
    ("overfitting", "过拟合迹象"),
    ("barrier", "结构受限"),
    ("survived", "未被证伪"),
]

# Every prescription is a hypothesis + acceptance criterion. The caveat is
# printed with all of them: a next experiment is a bet, not a verdict.
CAVEAT = "以上为下一步实验假设，不是已证最优方案——按验收标准判定，不按感觉判定。"


def _rule_triggered(verdict: dict, rule_prefix: str) -> bool:
    return any(
        c.get("triggered") and str(c.get("rule", "")).startswith(rule_prefix)
        for c in verdict.get("checks") or []
    )


def _pct(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:+.{digits}f}%"


def diagnose(result: dict) -> dict:
    """Classify what the evidence says about one verified strategy and what
    to test next.

    Returns {"primary": str, "primary_label": str, "tags": [...],
             "tag_labels": [...], "evidence": [str, ...],
             "experiments": [{"diagnosis", "next_experiment", "acceptance"}],
             "caveat": str}.
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
    # Grid data lives in walk_forward.out_sample_grid when walk-forward ran;
    # a param-grid-only spec keeps its scopes in result["param_grid"]. Read
    # the out-of-sample grid first, fall back to the whole-window grid, so a
    # grid-only strategy can still be classified as overfitting.
    grid = wf.get("out_sample_grid") or {}
    if not grid:
        for scope in ((result.get("param_grid") or {}).get("scopes") or []):
            if isinstance(scope, dict) and scope.get("positive_ratio") is not None:
                grid = scope
                break
    br = result.get("barrier_reach") or {}
    # The brittleness threshold is the one the SPEC declared (the same value
    # the falsification rule uses) -- never a hardcoded second opinion. A
    # spec declaring 70% with an actual 60% must classify as overfitting.
    min_ratio = float(
        ((result.get("spec") or {}).get("falsify_if") or {}).get(
            "grid_positive_ratio_min", 0.5))

    worst_net = worst.get("sum_pct")
    x2_net = row_x2.get("net_sum_pct") if row_x2 else None
    evidence: list[str] = []
    tags: list[str] = []

    def ev(s: str) -> None:
        if s not in evidence:
            evidence.append(s)

    def add_tag(t: str) -> None:
        if t not in tags:
            tags.append(t)

    # ---- the classification tree, in priority order -----------------------
    if outcome == "INCONCLUSIVE":
        primary = "insufficient"
        min_fills = next(
            (c for c in verdict.get("checks") or [] if c.get("insufficient")), None)
        ev(f"成交 {worst.get('n', 0)} 笔，低于 spec 声明的证据门槛——"
           + (min_fills["detail"] if min_fills else "样本不足"))
    elif outcome == "NOT_FALSIFIED":
        # Nothing was falsified: no cause below applies, however the numbers
        # look. Absence of disproof, not presence of proof.
        primary = "survived"
    elif selectivity == "refuted":
        primary = "randomness"
        ev(f"对照组：策略 − 随机 = {_pct(diff)}/笔，95% 区间 [{_pct(ci[0])}, {_pct(ci[1])}] "
           f"整体为负——入场逻辑显著差于随机（这是随机对照未通过的强形式：不是分不清，是更差）")
    elif selectivity == "unproven":
        primary = "randomness"
        ev(f"对照组：策略 − 随机 = {_pct(diff)}/笔，95% 区间 [{_pct(ci[0])}, {_pct(ci[1])}] "
           f"跨 0——无法证明入场逻辑优于随机（随机对照未通过的弱形式：分不清）")
        if worst_net is not None and worst_net > 0:
            ev(f"账面净收益 {_pct(worst_net)} 是真实的，但收益尚未证明来自策略选择性——"
               "它同样可能来自 beta 或运气，对照组无法区分")
    elif selectivity == "proven" and x2_net is not None and x2_net <= 0 < (worst_net or 0):
        primary = "cost"
        ev(f"对照组已证明选择性（差值 CI 下界为正），且 ×1 费率净 {_pct(worst_net)}、"
           f"×2 费率净 {_pct(x2_net)}——edge 真实存在，但付不起加倍的 toll")
    elif x2_net is not None and x2_net <= 0 < (worst_net or 0):
        # The book profits at 1x and dies at 2x, but selectivity was NOT
        # proven (control disabled or unproven). A profitable book is not a
        # proven edge: the main reading is "source of return unverified",
        # with cost fragility as a secondary fact -- never the reverse.
        primary = "edge_absent"
        ev(f"账面净 {_pct(worst_net)} 但 ×2 费率净 {_pct(x2_net)}，且选择性未获证明"
           + ("（随机对照组未启用）" if selectivity is None else "")
           + "——收益来源未经验证，成本脆弱是附带事实而非主诊断")
        add_tag("cost")
    elif worst_net is not None and worst_net <= 0:
        primary = "edge_absent"
        ev(f"×1 费率下净收益 {_pct(worst_net)}，且对照组未能证明选择性——"
           "没有可测量的正期望")
    elif oos.get("sum_pct") is not None and isb.get("sum_pct") is not None:
        primary = "overfitting"
        ev(f"样本内净 {_pct(isb['sum_pct'])} vs 样本外净 {_pct(oos['sum_pct'])}——"
           "edge 在没见过的数据上撑不住")
    elif grid.get("positive_ratio") is not None and grid["positive_ratio"] < min_ratio:
        # grid-only spec (no walk-forward): a brittle grid is still an
        # overfitting reading -- edge existing only in a few cells.
        primary = "overfitting"
        ev(f"参数网格正格率 {grid['positive_ratio'] * 100:.0f}%，低于 spec 声明的 "
           f"{min_ratio * 100:.0f}%"
           f"（{grid.get('positive_cells', 0)}/{grid.get('n_cells', 0)} 格为正）——"
           "edge 只在一小撮参数组合下存在，是曲线拟合不是规律")
    elif br.get("reached_tp_pct") is not None and br["reached_tp_pct"] < 10:
        primary = "barrier"
        ev(f"止盈位在持仓窗口内仅被摸到 {br['reached_tp_pct']:.1f}%——盈利期望是彩票结构")
    else:
        primary = "insufficient"  # FALSIFIED but none of the above: say so honestly
        ev("裁决为 FALSIFIED 但结论无法归入上述类别——请人工看第 7 节规则判定表")

    # ---- secondary tags (a strategy can fail several ways at once) --------
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
    if grid.get("positive_ratio") is not None and grid["positive_ratio"] < min_ratio \
            and primary != "overfitting":
        add_tag("overfitting")
        ev(f"参数网格正格率 {grid['positive_ratio'] * 100:.0f}% 低于 spec 声明的 "
           f"{min_ratio * 100:.0f}%（{grid.get('positive_cells', 0)}/{grid.get('n_cells', 0)} 格为正）——"
           "edge 只在一小撮参数组合下存在")
    if br.get("reached_tp_pct") is not None and br["reached_tp_pct"] < 10 \
            and primary not in ("barrier", "insufficient"):
        add_tag("barrier")

    # ---- next experiments: hypothesis + acceptance criterion --------------
    # Each entry: diagnosis (what the evidence says), next_experiment (the
    # falsifiable bet), acceptance (what result counts as an answer).
    experiments: list[dict] = []
    n = worst.get("n", 0)
    mean = worst.get("mean_pct")

    if primary == "insufficient":
        experiments.append({
            "diagnosis": f"成交 {n} 笔低于自声明门槛，当前数据无法支持任何结论",
            "next_experiment": "扩窗口、加标的、放宽阈值三者选一；若放宽阈值，"
                               "必须同步下调 min_fills 并在同一 spec 内重跑全部规则",
            "acceptance": f"成交 ≥ 新的 min_fills，且结论在 min_fills 的合理取值范围内不变",
        })
    elif primary == "randomness":
        experiments.append({
            "diagnosis": "入场信号未展示出独立于随机入场的优势（统计上无法区分或显著更差）",
            "next_experiment": "给入场加一个正交过滤器（趋势方向 / 时段 / 量能任选其一），"
                               "信号数预计降到约 1/5——这是实验假设，不是目标",
            "acceptance": "对照组差值 95% CI 下界 > 0（严格为正；下界为 0 时裁决引擎仍判未通过），"
                          "且成交数仍 ≥ min_fills；"
                          "若 CI 仍跨 0，换过滤器而不是降阈值",
        })
        if worst_net is not None and worst_net > 0:
            experiments.append({
                "diagnosis": f"账面 {_pct(worst_net)} 与随机入场无统计差异——"
                             "收益尚未证明来自策略选择性（可能来自 beta 或运气）",
                "next_experiment": "若目标就是赚 beta：改用买入持有/定投做基准对比，"
                                   "本引擎的回答是“不优于随机”，不是“不能赚”",
                "acceptance": "策略对买入持有的超额收益 CI 下界 > 0（那是另一个实验，"
                              "本引擎不测这个——先想清楚要回答哪个问题）",
            })
        else:
            experiments.append({
                "diagnosis": f"账面 {_pct(worst_net)} 且打不过随机：没有可测量的正期望",
                "next_experiment": "停止调参。写下 edge 的市场学基础（谁在犯错、错误定价"
                                   "为什么持续）；写不出来就换假设，不要换参数",
                "acceptance": "新假设的对照组差值 CI 下界 > 0（严格为正，不是 ≥ 0）",
            })
    elif primary == "cost":
        experiments.append({
            "diagnosis": f"edge 统计上成立，但 ×2 费率净收益 {_pct(x2_net)}——toll 预算不足",
            "next_experiment": "三条互斥实验各跑一次：① 降频（加最小信号间隔）"
                               "② 改 maker 限价成交（单边费率通常减半）③ 拉长持仓周期",
            "acceptance": f"×2 费率下净收益 > 0（当前 {_pct(x2_net)}），"
                          "或 maker 改造后 ×2 净收益 > 0",
        })
        experiments.append({
            "diagnosis": f"当前每笔净 {_pct(mean)}，频率与单笔利润的乘积就是 toll 预算",
            "next_experiment": "算出“频率 × 单笔毛利润”的盈亏平衡点，反推最低可行频率",
            "acceptance": "在目标频率下，×2 成本敏感性仍为正",
        })
    elif primary == "edge_absent":
        experiments.append({
            "diagnosis": "1x 费率下即亏损且无选择性证据——没有可测量的正期望",
            "next_experiment": "停止调参。写下 edge 的市场学基础（谁在犯错、错误定价"
                               "为什么持续）；写不出来就换假设",
            "acceptance": "新假设的对照组差值 CI 下界 > 0（严格为正，不是 ≥ 0）",
        })
    elif primary == "overfitting":
        if isb.get("sum_pct") is not None and oos.get("sum_pct") is not None:
            acc = (f"样本外净收益 ≥ 样本内的一半（当前样本内 {_pct(isb['sum_pct'])} "
                   f"vs 样本外 {_pct(oos['sum_pct'])}），且网格正格率 ≥ 声明值")
        else:
            acc = (f"网格正格率 ≥ spec 声明的 {min_ratio * 100:.0f}%"
                   f"（当前 {grid.get('positive_ratio', 0) * 100:.0f}%）；"
                   "若同时启用 walk-forward，样本外净收益还应 ≥ 样本内的一半")
        experiments.append({
            "diagnosis": "样本内成立但样本外崩溃/参数网格脆弱——拟合了历史而非规律",
            "next_experiment": "砍自由度：参数空间砍到 1-2 个维度再测；必须多参数时改用"
                               "滚动前向验证（每折内重新选参，而不是单次划分）",
            "acceptance": acc,
        })
        if grid.get("n_cells"):
            experiments.append({
                "diagnosis": f"样本外网格 {grid.get('positive_cells', 0)}/{grid.get('n_cells', 0)} "
                             "格为正——edge 只在一小撮参数组合下存在",
                "next_experiment": "在表现最好的格子附近加密采样：edge 若真实，邻域应该活着",
                "acceptance": "邻域格子多数为正；邻域全死说明拟合了一个尖峰，回到上一条",
            })
    elif primary == "barrier":
        experiments.append({
            "diagnosis": f"止盈位在持仓窗口内仅被摸到 {br.get('reached_tp_pct', 0):.1f}%——"
                         "盈利期望是彩票结构",
            "next_experiment": "止盈改到市场真实波动距离（用 ATR 或已实现波动校准），"
                               "不要用整数百分比的心理价位",
            "acceptance": f"止盈可达率 ≥ 30%（当前 {br.get('reached_tp_pct', 0):.1f}%），"
                          "或显式声明更长的持仓窗口并重跑",
        })
    elif primary == "survived":
        experiments.append({
            "diagnosis": "当前检验条件下未被证伪——这是“没有反证”，不是“已被证实”",
            "next_experiment": "模拟盘跑满一个完整的样本外周期，重点记录滑点与成交率"
                               "（引擎不建模的两项）",
            "acceptance": "模拟成交率 ≥ 回测假设，且净收益不低于样本外口径的 50%",
        })

    # secondary-tag follow-ups: the primary experiment does not retire these
    if "cost" in tags:
        experiments.append({
            "diagnosis": f"附带问题：成本翻倍即亏损（×2 净 {_pct(x2_net)}）",
            "next_experiment": "主实验之外，单独跑一次 ×5 成本压力，确认 toll 预算的上限",
            "acceptance": "×5 下的亏损幅度在可接受范围内（由你定义，写进 spec）",
        })
    if "overfitting" in tags:
        experiments.append({
            "diagnosis": "附带问题：样本外/网格显示参数脆弱",
            "next_experiment": "主实验收敛后，用更少的参数重跑一次作为确认",
            "acceptance": "砍参数后的结论与砍之前一致",
        })

    label = dict(OUTCOMES).get(primary, primary)
    return {
        "primary": primary,
        "primary_label": label,
        "tags": tags,
        "tag_labels": [dict(OUTCOMES).get(t, t) for t in tags],
        "evidence": evidence,
        "experiments": experiments,
        "caveat": CAVEAT,
    }
