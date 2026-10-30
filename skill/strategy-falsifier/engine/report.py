"""Render a verification result as a markdown falsification report.

The report is the product. Its rules:

  * every number that could be mistaken for a promise is labelled with the
    assumption that produced it (worst/best case, fee multiple, fill model);
  * a "30 秒读懂" block carries the verdict and the three key numbers at the
    very top -- the verdict comes last in reading order but first in the
    summary block, and it quotes the exact criteria that fired;
  * what is NOT modelled is a section, not a footnote -- a reader who does not
    know the limits of the instrument will misuse it.
"""
from __future__ import annotations

import datetime as _dt

ENGINE_VERSION = "verify/0.1"


def _pf(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _pct(v: float | None, digits: int = 2) -> str:
    return f"{v:+.{digits}f}%" if v is not None else "—"


def _n(v) -> str:
    """Counts read with thousands separators so 1,532 成交 never scans as
    three numbers. Missing values stay a dash -- never a fake zero."""
    if v is None:
        return "—"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.2f}"


def _fmt_axis(key: str, v) -> str:
    """Axis values read like the spec declares them: bar counts are counts,
    the rest are signed percentages."""
    if key == "timeout_bars":
        return f"{int(v)} 根"
    return f"{v * 100:+.1f}%"


def _params_txt(params: dict) -> str:
    return " · ".join(f"{k}={_fmt_axis(k, v)}" for k, v in params.items())


def _render_grid_scope(g: dict, spec: dict, verdict: dict, a) -> None:
    """One grid scope: the distribution line, the heat table, and -- only
    when the PRE-DECLARED rule fired -- the overfit flag. No threshold is
    invented here; the flag quotes the spec's own declared minimum."""
    axes, order = g["axes"], g["axis_order"]
    case_label = "最坏优先" if spec["worst_case"] else "最好优先"
    if g["cells_with_fills"] == 0:
        a(f"- {g['n_cells']} 格全部无成交——该范围无法评估参数敏感性（裁决见第 7 节）。")
    else:
        a(f"- 分布：{g['cells_with_fills']} 格有成交 · {g['positive_cells']} 格净收益为正"
          f"（{g['positive_ratio']:.0%}）· {case_label}口径中位 {_pct(g['median_net_pct'])} · "
          f"最好 {_pct(g['best']['net_sum_pct'])}（{_params_txt(g['best']['params'])}）· "
          f"最差 {_pct(g['worst']['net_sum_pct'])}（{_params_txt(g['worst']['params'])}）")
        if g["no_fill_cells"]:
            a(f"- 另有 {g['no_fill_cells']} 格无成交，不计入占比——无证据的格子既不算赢也不算输")
    a("")
    if len(order) == 2:
        r_ax, c_ax = order
        a(f"| {r_ax} ↓ ＼ {c_ax} → | " + " | ".join(_fmt_axis(c_ax, v) for v in axes[c_ax]) + " |")
        a("|---|" + "---:|" * len(axes[c_ax]))
        cell_map = {(c["params"][r_ax], c["params"][c_ax]): c for c in g["cells"]}
        best_key = ((g["best"]["params"][r_ax], g["best"]["params"][c_ax])
                    if g["best"] else None)
        for rv in axes[r_ax]:
            row = [f"`{_fmt_axis(r_ax, rv)}`"]
            for cv in axes[c_ax]:
                c = cell_map[(rv, cv)]
                txt = "无成交" if c["fills"] == 0 else _pct(c["net_sum_pct"])
                row.append(f"**{txt}**" if (rv, cv) == best_key else txt)
            a("| " + " | ".join(row) + " |")
    else:
        a(f"| {order[0]} | 净收益（{case_label}） | 成交 |")
        a("|---|---|---:|")
        cell_map = {c["params"][order[0]]: c for c in g["cells"]}
        for v in axes[order[0]]:
            c = cell_map[v]
            txt = "无成交" if c["fills"] == 0 else _pct(c["net_sum_pct"])
            a(f"| `{_fmt_axis(order[0], v)}` | {txt} | {_n(c['fills'])} |")
    fired = [c for c in verdict["checks"]
             if c.get("rule") == "grid_positive_ratio_min"
             and c.get("triggered") and c.get("scope") == g["scope"]]
    if fired:
        min_ratio = float(spec["falsify_if"]["grid_positive_ratio_min"])
        a("")
        if g["positive_cells"] == 0:
            a(f"> 全部 {g['cells_with_fills']} 格均为负——网格内没有任何参数组合赚钱，"
              f"edge 在这个参数空间里不存在，更谈不上稳健；按事前声明规则 "
              f"`grid_positive_ratio_min`（下限 {min_ratio:.0%}）触发证伪，见第 7 节。")
        else:
            a(f"> 只有 {g['positive_cells']}/{g['cells_with_fills']} 格为正——edge 的存在依赖一小撮参数组合，"
              f"这是过拟合的典型签名；按事前声明规则 `grid_positive_ratio_min`（下限 {min_ratio:.0%}）触发证伪，见第 7 节。")


def _key_numbers(result: dict) -> list[str]:
    """The three numbers a reader needs before deciding to read on: the
    baseline net, the control-group delta, and the out-of-sample or grid
    measurement -- whichever of the two the spec actually enabled. Only
    presents numbers already in the result; computes nothing new."""
    worst = result["runs"]["worst_case"]["stats"]
    best = result["runs"]["best_case"]["stats"]
    lines = [
        f"基线净收益（最坏口径）：**{_pct(worst['sum_pct'])}**"
        f"（最好口径 {_pct(best['sum_pct'])} · {_n(worst['n'])} 笔成交）"
    ]
    ctrl = result.get("control")
    if ctrl is None:
        lines.append("对照差值：对照组未启用——没有对照组的回放只是叙事")
    elif ctrl.get("diff_pct") is None:
        lines.append(f"对照差值：{ctrl.get('note') or '样本不足，未计算'}")
    else:
        ci = ctrl.get("diff_ci")
        ci_txt = (f"，95% bootstrap 区间 [{ci[0]:+.2f}%, {ci[1]:+.2f}%]"
                  if ci else "（样本不足，区间未计算）")
        lines.append(
            f"对照差值（策略 − 随机入场）：**{_pct(ctrl['diff_pct'])}/笔**{ci_txt}")
    wf = result.get("walk_forward")
    pg = result.get("param_grid")
    if wf and wf.get("out_sample"):
        oos = wf["out_sample"]["runs"]["worst_case"]["stats"]
        isb = wf.get("in_sample")
        is_txt = (f"（样本内 {_pct(isb['runs']['worst_case']['stats']['sum_pct'])}）"
                  if isb else "")
        lines.append(f"样本外净收益（最坏口径）：**{_pct(oos['sum_pct'])}**{is_txt}")
    elif pg:
        g = pg["scopes"][0]
        if g["cells_with_fills"]:
            lines.append(
                f"参数网格：**{g['positive_cells']}/{g['cells_with_fills']} 格为正**"
                f"（{g['positive_ratio']:.0%}，共 {g['n_cells']} 格）")
        else:
            lines.append(f"参数网格：{g['n_cells']} 格全部无成交——无法评估参数敏感性")
    else:
        lines.append("样本外 / 参数网格：均未启用——以上数字全是窗口内结果")
    return lines


def _plain_sentence(result: dict) -> str:
    """One sentence a non-quant reader can act on. It re-voices the verdict
    and the rules that fired; it invents no new judgement."""
    v = result["verdict"]
    trig = [c for c in v["checks"] if c.get("triggered")]
    if v["outcome"] == "FALSIFIED":
        if any(c.get("scope") == "样本外" for c in trig):
            return ("人话：策略在没见过的时段里先撑不住——样本外段触发了事前证伪规则，"
                    "按裁决规则整体判负。")
        rules = {c["rule"].split("=")[0] for c in trig}
        bits = []
        if "worst_case_net_negative" in rules:
            bits.append("按最保守的成交假设这笔账是亏的")
        if "net_negative_at_fee_multiple" in rules:
            bits.append("成本只要翻倍就由赚转亏")
        if "control_lift_ci_includes_zero" in rules:
            bits.append("表现和随机入场区分不开")
        if "grid_positive_ratio_min" in rules:
            bits.append("只有个别参数组合赚钱，换个参数就不成立")
        tail = "、".join(bits) if bits else "事前写好的证伪规则被触发"
        return f"人话：{tail}——规则先于报告存在，这次没能活下来。"
    if v["outcome"] == "INCONCLUSIVE":
        return "人话：样本不够，这份报告既证不实也证不伪——先去攒数据，别拿它做决定。"
    return ("人话：在声明的规则里活下来了，但'未被证伪'不等于'被证实'——"
            "规则没问的账，引擎不替它吹。")


def unmodeled_notes(result: dict) -> list[str]:
    """The standing disclosures, computed from what this run actually enabled.
    Shared by the markdown and HTML renderers so the two can never drift."""
    wf = bool(result.get("walk_forward"))
    pg = bool(result.get("param_grid"))
    return [
        "滑点与部分成交：只按费率+成交模型计成本，真实盘口的排队与部分成交不在模型内；",
        ("样本外表现：walk-forward 已启用——最后一段按时间切出作样本外，但引擎无法验证策略参数是否曾用全窗口数据调优；"
         "若调参时已看过最后一段，'样本外'名不副实。" if wf else
         "样本外表现：walk-forward 未启用——所有数字都是窗口内的样本内结果；参数网格已启用，"
         "但它测量的是参数脆弱性而非时间维度上的稳健性，且网格本身也在窗口内测量，不能替代样本外验证。" if pg else
         "样本外表现：所有数字都是窗口内的样本内结果，walk-forward 与参数敏感性不在本引擎射程内；"),
        "资金容量：等权回放，单笔金额对盘口的冲击（除费率外）未建模；",
        "同时持仓相关性：信号逐一独立回放，重叠持仓的相关性风险不计入；",
        "交易所/链路风险：宕机、API 限流、插针、宕机重启后的订单状态，一概不在模型内。",
    ]


def render(result: dict) -> str:
    spec = result["spec"]
    v = result["verdict"]
    L: list[str] = []
    a = L.append

    a(f"# 证伪报告 · {spec['name']}")
    a("")
    a("> **30 秒读懂**")
    a(">")
    a(f"> - 裁决：**{v['outcome']}**")
    for ln in _key_numbers(result):
        a(f"> - {ln}")
    a(f"> - {_plain_sentence(result)}")
    a("")
    a(f"> **裁决：{v['outcome']}** —— {v['reason']}")
    a("")
    a(f"- 生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 引擎 {ENGINE_VERSION}")
    a(f"- 标的：{spec['symbol']} {spec['interval']}（{spec['venue']}）· 窗口 {result['window']['from']} → {result['window']['to']}（{_n(result['window']['bars'])} 根 K 线）")
    if spec.get("hypothesis"):
        a(f"- 待检验假设：{spec['hypothesis']}")
    if spec.get("source"):
        a(f"- 策略来源：{spec['source']}")
    ex = spec["exit"]
    exit_txt = " · ".join(
        t for t in [
            f"止盈 {ex['take_profit']*100:+.1f}%" if ex.get("take_profit") is not None else "",
            f"止损 {ex['stop_loss']*100:+.1f}%" if ex.get("stop_loss") is not None else "",
            f"超时 {ex['timeout_bars']} 根" if ex.get("timeout_bars") is not None else "",
            f"移动止损回撤 {ex['trailing']*100:.1f}%" if ex.get("trailing") is not None else "",
            "信号退出（收盘价）" if ex.get("exit_signals_path") else "",
        ] if t
    )
    a(f"- 退出规则：{exit_txt}")
    a(f"- 执行假设：{spec['fill_model']} 成交模型 · 单边费率 {spec['fee_rate']*10000:.0f}bps"
      + (f" · 含资金费" if result.get("funding_applied") else (" · spec 声明含资金费但数据缺失，按 0 处理" if spec.get("funding") else " · 不含资金费"))
      + f" · bar 内先到止损判止损（最坏优先）")
    a(f"- 信号数：{_n(result['n_signals'])}")
    # --- section 0: port notes (strategies ported from somewhere else) -----
    if spec.get("port_notes"):
        a("")
        a("## 0. 移植说明")
        a("")
        for line in spec["port_notes"].splitlines():
            a(line.rstrip())
        a("")
    wf = result.get("walk_forward")
    if wf:
        oos_v = (wf.get("out_sample") or {}).get("verdict", {}).get("outcome", "无信号")
        a(f"- Walk-forward：{wf['folds']} 折（样本外界界 {wf['boundary_ts']}，第 {wf['boundary_bar']} 根）· 样本外段裁决 {oos_v}")
    pg = result.get("param_grid")
    if pg:
        dims = " × ".join(f"{k}({len(pg['axes'][k])})" for k in pg["axis_order"])
        parts = []
        for g in pg["scopes"]:
            if g["cells_with_fills"]:
                parts.append(f"{g['scope']} {g['positive_cells']}/{g['cells_with_fills']} 格为正")
            else:
                parts.append(f"{g['scope']} 无成交")
        a(f"- 参数网格：{dims} 共 {_n(pg['scopes'][0]['n_cells'])} 格 · " + " · ".join(parts))
    a("")

    # -- 1. replay ----------------------------------------------------------
    a("## 1. Path-aware 回放")
    a("")
    a("| 口径 | 成交 | 未成交 | 未决 | 净收益 | 均值/笔 | 胜率 | PF | 最大回撤(时序) |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        r = result["runs"][case]
        st = r["stats"]
        a(f"| {label} | {_n(st['n'])} | {_n(r['unfilled'])} | {_n(r['open_signals'])} "
          f"| {_pct(st['sum_pct'])} | {_pct(st['mean_pct'])} | {st['win_pct']:.0f}% "
          f"| {_pf(st['profit_factor'])} | {_pct(st['max_dd_pct'])} |")
    a("")
    worst, best = result["runs"]["worst_case"]["stats"], result["runs"]["best_case"]["stats"]
    if worst["n"] and best["n"] and worst["sum_pct"] > 0 > best["sum_pct"]:
        a("> 同一策略只在乐观口径下赚钱——它赚的是假设，不是价差。")
        a("")
    # The realized drawdown is the chronological curve. The losses-first
    # replay is a BOUND, not a history: both are printed so neither can be
    # mistaken for the other (the sorted-series mistake is exactly how a
    # "worst case" number ends up quoted as a lived experience).
    dd_worst_order = worst.get("max_dd_worst_order_pct")
    if dd_worst_order is not None and worst["n"]:
        a(f"> 回撤口径：表中为**时序**最大回撤（真实经历）；同账面按“先亏后赚”重放的界为 {dd_worst_order:.2f}%——是界，不是历史。")
        a("")
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        by = result["runs"][case]["by_reason"]
        if not by:
            continue
        parts = [f"{k} {_n(b['n'])} 笔({b['net_sum_pct']:+.2f}%)" for k, b in sorted(by.items(), key=lambda kv: -kv[1]["net_sum_pct"])]
        a(f"- {label}退出结构：{' · '.join(parts)}")
    a("")

    # -- 2. cost sensitivity ------------------------------------------------
    a("## 2. 成本敏感性")
    a("")
    a("| 成本倍数 | 往返费率 | 净收益 | PF | 胜率 |")
    a("|---|---:|---:|---:|---:|")
    for row in result["cost_sensitivity"]:
        a(f"| ×{row['fee_multiple']:g} | {row['fee_bps']:.0f}bps | {_pct(row['net_sum_pct'])} "
          f"| {_pf(row['profit_factor'])} | {row['win_pct']:.0f}% |")
    a("")

    # -- 3. barrier reachability -------------------------------------------
    a("## 3. 障碍可达性")
    a("")
    br = result["barrier_reach"]
    if br.get("n"):
        a(f"- 样本 {_n(br['n'])} 笔 · 摸到止盈位的 {br['reached_tp_pct']:.1f}% · 摸到止损位的 {br['reached_sl_pct']:.1f}% · 两者都没摸到的 {br['reached_neither_pct']:.1f}%")
        a(f"- 最大有利偏离（MFE）中位 {_pct(br['mfe']['median'], 1)} / p90 {_pct(br['mfe']['p90'], 1)} · 最大不利偏离（MAE）中位 {_pct(br['mae']['median'], 1)} / p10 {_pct(br['mae']['p10'], 1)}")
        if br.get("tp_level_pct") is not None and br["reached_tp_pct"] < 10:
            a("")
            a(f"> 止盈位 {br['tp_level_pct']:+.1f}% 在持仓窗口内几乎从未被触及——这个策略的大部分盈利期望是彩票结构。")
        if br.get("sl_level_pct") is not None and br["reached_sl_pct"] < 10:
            a("")
            a(f"> 止损位 {br['sl_level_pct']:+.1f}% 在持仓窗口内几乎从未被触及——回测里的'止损'大部分时间没有在工作。")
    else:
        a(f"- {br.get('note', '无样本')}")
    a("")

    # -- 4. control ---------------------------------------------------------
    a("## 4. 随机入场对照组")
    a("")
    ctrl = result.get("control")
    if ctrl:
        cs = ctrl["stats"]
        rng = ctrl.get("bar_range")
        if ctrl.get("note"):
            a(f"- {ctrl['note']}")
        else:
            rng_txt = f"，随机入场范围 第 {rng[0]} → 第 {rng[1]} 根" if rng and rng[0] != 30 else ""
            a(f"- 对照组：{_n(ctrl['n_random'])} 个随机入场（seed={ctrl['seed']}，允许重复，镜像策略主方向 {ctrl.get('side', 'long')}{rng_txt}），同一窗口、同一退出规则、同一成本假设")
            a(f"- 策略均值/笔 {_pct(ctrl['strategy_mean_pct'])} vs 对照组 {_pct(ctrl['control_mean_pct'])}（成交 {_n(cs['n'])} 笔，胜率 {cs['win_pct']:.0f}%）")
            if ctrl.get("diff_pct") is not None:
                ci = ctrl.get("diff_ci")
                ci_txt = f"[{ci[0]:+.2f}%, {ci[1]:+.2f}%]" if ci else "（样本不足，未计算）"
                a(f"- 差值 {_pct(ctrl['diff_pct'])}，95% bootstrap 区间 {ci_txt}")
            sel = ctrl.get("selectivity")
            msg = {
                "proven": "策略显著优于随机入场——选择性成立（在本窗口内）。",
                "refuted": "策略显著差于随机入场——信号方向可能是反的，或成本吃掉了一切。",
                "unproven": "区间跨 0：未能证明策略优于抛硬币。绝对收益数字在这种情况下没有意义。",
                "unknown": "样本不足，无法比较。",
            }[sel]
            a(f"- 判定：**{sel}** —— {msg}")
    else:
        a("- 未启用对照组（spec.control.enabled=false）——没有对照组的回放只是叙事。")
    a("")

    # -- 5. walk-forward ------------------------------------------------------
    if wf:
        a("## 5. Walk-forward（样本内 / 样本外）")
        a("")
        a(f"- 切分：全窗口 {_n(result['window']['bars'])} 根 K 线切 {wf['folds']} 折（每折 {_n(wf['fold_bars'])} 根）；样本外界界 {wf['boundary_ts']}（第 {_n(wf['boundary_bar'])} 根）")
        a(f"- 样本内 = 前 {wf['folds'] - 1} 折的入场 · 样本外 = 最后 1 折的入场；两段各跑完整电池"
          f"（回放 + 成本敏感性 + 障碍可达性 + 随机对照组；样本外对照组的随机入场只在该段内抽取，"
          f"两段的成交都在全窗口上推演——入场之后的 K 线对入场而言仍是未来）")
        a("")
        a("| 段 | 信号 | 成交 | 净收益(最坏) | 均值/笔 | 胜率 | PF | 段内裁决 |")
        a("|---|---:|---:|---:|---:|---:|---:|---|")
        for seg, label in (("in_sample", f"样本内（前 {wf['folds'] - 1} 折）"),
                           ("out_sample", "样本外（最后 1 折）")):
            b = wf.get(seg)
            if b is None:
                a(f"| {label} | 0 | — | — | — | — | — | 无信号 |")
                continue
            st = b["runs"]["worst_case"]["stats"]
            a(f"| {label} | {_n(b['n_signals'])} | {_n(st['n'])} | {_pct(st['sum_pct'])} | {_pct(st['mean_pct'])} "
              f"| {st['win_pct']:.0f}% | {_pf(st['profit_factor'])} | {b['verdict']['outcome']} |")
        a("")
        isb, oosb = wf.get("in_sample"), wf.get("out_sample")
        if isb and oosb:
            is_sum = isb["runs"]["worst_case"]["stats"]["sum_pct"]
            oos_sum = oosb["runs"]["worst_case"]["stats"]["sum_pct"]
            a(f"- 样本内 → 样本外（最坏口径净收益）：{_pct(is_sum)} → {_pct(oos_sum)}（变化 {oos_sum - is_sum:+.2f}pp）")
            if oos_sum <= 0 < is_sum:
                a("")
                a("> 样本外转负——在未见过的时段里，edge 没有活下来。")
            elif 0 < oos_sum < is_sum:
                a("")
                a("> 样本外仍为正但低于样本内——衰减模式与过拟合一致，不能排除。")
            elif is_sum <= 0 and oos_sum <= 0:
                a("")
                a("> 两段均为负——这不是衰减，是稳定地亏；样本内外一致地证伪。")
            a("")
        if v["outcome"] == "FALSIFIED" and any(
            c.get("triggered") and c.get("scope") == "样本外" for c in v["checks"]
        ):
            a("> 样本外段触发了事前证伪规则——按裁决规则，整体判 FALSIFIED：只在看得见的地方成立的 edge 不是 edge。")
            a("")

    # -- 6. parameter grid ---------------------------------------------------
    if pg:
        a("## 6. 参数敏感性网格")
        a("")
        decl = " × ".join(
            f"`{k}` [{', '.join(_fmt_axis(k, v) for v in pg['axes'][k])}]"
            for k in pg["axis_order"]
        )
        a(f"- 事前声明：{decl}（共 {_n(pg['scopes'][0]['n_cells'])} 格；网格值写于 spec，先于本报告存在）")
        if wf:
            a("- walk-forward 已启用：样本内 / 样本外两段各跑一遍全网格（两段入场之和 = 全体入场）；"
              "每格按 spec 声明的成交口径、基准费率 ×1、同一入场集回放——成本敏感性见第 2 节，"
              "网格只回答「edge 是否依赖精确的参数组合」")
        else:
            a("- 测量口径：每格按 spec 声明的成交口径、基准费率 ×1、同一入场集回放——"
              "成本敏感性见第 2 节，网格只回答「edge 是否依赖精确的参数组合」；"
              "网格在全体入场上测量，本身是窗口内（样本内）结果")
        for g in pg["scopes"]:
            if len(pg["scopes"]) > 1:
                seg_label = ("样本内（前 %d 折）" % (wf["folds"] - 1) if g["scope"] == "样本内"
                             else "样本外（最后 1 折）" if g["scope"] == "样本外" else g["scope"])
                a("")
                a(f"**{seg_label}**")
            _render_grid_scope(g, spec, v, a)
        a("")

    # -- 7. criteria --------------------------------------------------------
    a("## 7. 事前证伪规则判定")
    a("")
    if v["checks"]:
        a("| 规则 | 范围 | 结果 | 明细 |")
        a("|---|---|---|---|")
        for c in v["checks"]:
            mark = "触发" if c.get("triggered") else ("样本不足" if c.get("insufficient") else "通过")
            a(f"| `{c['rule']}` | {c.get('scope', '全窗口')} | {mark} | {c['detail']} |")
    else:
        a("- spec 未声明任何 falsify_if 规则——本报告只呈现测量，不下裁决。")
    a("")

    # -- disclosure ---------------------------------------------------------
    a("## 8. 未建模事项（阅读前必读）")
    a("")
    for t in unmodeled_notes(result):
        a(f"- {t}")
    a("")

    # -- reproduce ----------------------------------------------------------
    repro = result.get("repro")
    if repro:
        a("## 9. 复现命令")
        a("")
        a("本次报告由以下命令产生；在同一目录重跑，可复现同样数字：")
        a("")
        a("```bash")
        a(repro["command"])
        a("```")
        a("")
        a("| 输入 | 路径 / 来源 |")
        a("|---|---|")
        a(f"| spec | {repro['spec']} |")
        a(f"| candles | {repro['candles']} |")
        a(f"| signals | {repro['signals']} |")
        if repro.get("funding"):
            a(f"| funding | {repro['funding']} |")
        a("")

    a("---")
    a(f"*{ENGINE_VERSION} · 本报告由事前证伪规则驱动，规则先于数字写入 spec；未被证伪不等于被证实。*")
    return "\n".join(L)


def render_json(result: dict) -> str:
    import json

    return json.dumps(result, ensure_ascii=False, indent=2, default=str)
