"""Render a verification result as a markdown falsification report.

The report is the product. Its rules:

  * every number that could be mistaken for a promise is labelled with the
    assumption that produced it (worst/best case, fee multiple, fill model);
  * the verdict comes last in reading order but first in the summary block,
    and it quotes the exact criteria that fired;
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


def render(result: dict) -> str:
    spec = result["spec"]
    v = result["verdict"]
    L: list[str] = []
    a = L.append

    a(f"# 证伪报告 · {spec['name']}")
    a("")
    a(f"> **裁决：{v['outcome']}** —— {v['reason']}")
    a("")
    a(f"- 生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 引擎 {ENGINE_VERSION}")
    a(f"- 标的：{spec['symbol']} {spec['interval']}（{spec['venue']}）· 窗口 {result['window']['from']} → {result['window']['to']}（{result['window']['bars']} 根 K 线）")
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
      + (f" · 含资金费" if spec.get("funding") else " · 不含资金费")
      + f" · bar 内先到止损判止损（最坏优先）")
    a(f"- 信号数：{result['n_signals']}")
    a("")

    # -- 1. replay ----------------------------------------------------------
    a("## 1. Path-aware 回放")
    a("")
    a("| 口径 | 成交 | 未成交 | 未决 | 净收益 | 均值/笔 | 胜率 | PF | 最大回撤 |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        r = result["runs"][case]
        st = r["stats"]
        a(f"| {label} | {st['n']} | {r['unfilled']} | {r['open_signals']} "
          f"| {_pct(st['sum_pct'])} | {_pct(st['mean_pct'])} | {st['win_pct']:.0f}% "
          f"| {_pf(st['profit_factor'])} | {_pct(st['max_dd_pct'])} |")
    a("")
    worst, best = result["runs"]["worst_case"]["stats"], result["runs"]["best_case"]["stats"]
    if worst["n"] and best["n"] and worst["sum_pct"] > 0 > best["sum_pct"]:
        a("> 同一策略只在乐观口径下赚钱——它赚的是假设，不是价差。")
        a("")
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        by = result["runs"][case]["by_reason"]
        if not by:
            continue
        parts = [f"{k} {b['n']} 笔({b['net_sum_pct']:+.2f}%)" for k, b in sorted(by.items(), key=lambda kv: -kv[1]["net_sum_pct"])]
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
        a(f"- 样本 {br['n']} 笔 · 摸到止盈位的 {br['reached_tp_pct']:.1f}% · 摸到止损位的 {br['reached_sl_pct']:.1f}% · 两者都没摸到的 {br['reached_neither_pct']:.1f}%")
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
        a(f"- 对照组：{ctrl['n_random']} 个随机入场（seed={ctrl['seed']}，允许重复，镜像策略主方向 {ctrl.get('side', 'long')}），同一窗口、同一退出规则、同一成本假设")
        a(f"- 策略均值/笔 {_pct(ctrl['strategy_mean_pct'])} vs 对照组 {_pct(ctrl['control_mean_pct'])}（成交 {cs['n']} 笔，胜率 {cs['win_pct']:.0f}%）")
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

    # -- 5. criteria --------------------------------------------------------
    a("## 5. 事前证伪规则判定")
    a("")
    if v["checks"]:
        a("| 规则 | 结果 | 明细 |")
        a("|---|---|---|")
        for c in v["checks"]:
            mark = "触发" if c.get("triggered") else ("样本不足" if c.get("insufficient") else "通过")
            a(f"| `{c['rule']}` | {mark} | {c['detail']} |")
    else:
        a("- spec 未声明任何 falsify_if 规则——本报告只呈现测量，不下裁决。")
    a("")

    # -- disclosure ---------------------------------------------------------
    a("## 6. 未建模事项（阅读前必读）")
    a("")
    for t in [
        "滑点与部分成交：只按费率+成交模型计成本，真实盘口的排队与部分成交不在模型内；",
        "样本外表现：所有数字都是窗口内的样本内结果，walk-forward 与参数敏感性不在本引擎射程内；",
        "资金容量：等权回放，单笔金额对盘口的冲击（除费率外）未建模；",
        "同时持仓相关性：信号逐一独立回放，重叠持仓的相关性风险不计入；",
        "交易所/链路风险：宕机、API 限流、插针、宕机重启后的订单状态，一概不在模型内。",
    ]:
        a(f"- {t}")
    a("")
    a("---")
    a(f"*{ENGINE_VERSION} · 本报告由事前证伪规则驱动，规则先于数字写入 spec；未被证伪不等于被证实。*")
    return "\n".join(L)


def render_json(result: dict) -> str:
    import json

    return json.dumps(result, ensure_ascii=False, indent=2, default=str)
