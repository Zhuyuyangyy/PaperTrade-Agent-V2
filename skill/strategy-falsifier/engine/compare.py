"""Multi-strategy comparison: the same battery, several strategies, one table.

The most common real question is not "is my strategy good" but "which of my
variants is the least overfitted". This module answers exactly that: it runs
the declared ranking rule over the per-strategy results and prints the table,
with the rule itself stated BEFORE the table -- the same
rules-before-numbers discipline the single-strategy reports follow.

Ranking rule (declared before any number is shown):
  1. Verdict tier: NOT_FALSIFIED > INCONCLUSIVE > FALSIFIED. The tier IS the
     honesty ranking -- a strategy that survived every declared rule outranks
     one that merely could not be measured, which outranks one that broke.
  2. Within a tier: control-group difference CI lower bound, descending.
     A positive lower bound is PROOF of selectivity; zero-crossing is merely
     unrefuted; negative is worse than coin flips.
  3. Then out-of-sample net return, descending.
  4. Then out-of-sample parameter-grid positive ratio, descending.
  5. Then worst-case fill count, descending (more evidence, more say).
"""
from __future__ import annotations

from .spec import SpecError  # noqa: F401  (re-exported for callers' convenience)

# Verdict tier -> rank. Higher is better. The mapping is the ranking's value
# judgement and lives here, in one place, in the open.
_TIER = {"NOT_FALSIFIED": 3, "INCONCLUSIVE": 2, "FALSIFIED": 1}


def _tier(outcome: str) -> int:
    return _TIER.get(outcome, 0)


def _oos_net(result: dict) -> float | None:
    wf = result.get("walk_forward") or {}
    seg = wf.get("out_sample") or {}
    st = (seg.get("runs") or {}).get("worst_case", {}).get("stats")
    return None if not st else st.get("sum_pct")


def _oos_grid_ratio(result: dict) -> float | None:
    wf = result.get("walk_forward") or {}
    grid_scope = wf.get("out_sample_grid") or {}
    return grid_scope.get("positive_ratio")


def _grid_ratio(result: dict) -> float | None:
    """Best available grid positive ratio: out-of-sample if present, else the
    whole-window grid (top-level param_grid scopes), else None."""
    r = _oos_grid_ratio(result)
    if r is not None:
        return r
    pg = result.get("param_grid") or {}
    for sc in (pg.get("scopes") or []):
        if isinstance(sc, dict) and sc.get("positive_ratio") is not None:
            return sc["positive_ratio"]
    return None


def _ci_lower(result: dict) -> float | None:
    ctrl = result.get("control") or {}
    ci = ctrl.get("diff_ci")
    return ci[0] if ci else None


def _worst_net(result: dict) -> float:
    return (result.get("runs") or {}).get("worst_case", {}).get("stats", {}).get("sum_pct", 0.0)


def _best_net(result: dict) -> float:
    return (result.get("runs") or {}).get("best_case", {}).get("stats", {}).get("sum_pct", 0.0)


def _fee2_net(result: dict) -> float | None:
    for row in result.get("cost_sensitivity") or []:
        if row.get("fee_multiple") == 2:
            return row.get("net_sum_pct")
    return None


def _fills(result: dict) -> int:
    return (result.get("runs") or {}).get("worst_case", {}).get("stats", {}).get("n", 0)


def _selectivity(result: dict) -> str:
    return (result.get("control") or {}).get("selectivity") or "n/a"


def _pct(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:+.{digits}f}%"


def rank_key(result: dict) -> tuple:
    """Sort key implementing the declared rule. Tuple order IS the rule."""
    ci_lo = _ci_lower(result)
    oos = _oos_net(result)
    grid = _grid_ratio(result)
    return (
        _tier((result.get("verdict") or {}).get("outcome", "")),
        ci_lo if ci_lo is not None else -99.0,
        oos if oos is not None else -9999.0,
        grid if grid is not None else -1.0,
        _fills(result),
    )


def _triggered_rules(result: dict) -> list[str]:
    """Rules that fired, as short 'scope: rule' tags, de-duplicated."""
    seen: list[str] = []
    for c in (result.get("verdict") or {}).get("checks") or []:
        if c.get("triggered"):
            tag = f"{c.get('rule')}({c.get('scope')})"
            if tag not in seen:
                seen.append(tag)
    return seen


def render_comparison(results: list[dict], title: str = "多策略对比") -> str:
    """results: one verify() output per strategy. Order in == order listed;
    the ranking is computed here and shown separately."""
    if not results:
        raise SpecError("对比模式至少需要一个策略的验证结果")
    if len(results) == 1:
        raise SpecError("只有一个策略：请直接用单策略报告模式（--spec）")

    ranked = sorted(results, key=rank_key, reverse=True)
    lines: list[str] = []
    a = lines.append

    a(f"# {title}")
    a("")
    a(f"本表由 **{len(results)} 个策略**过同一套证伪电池产出。每个策略的回放口径、成本敏感性、"
      "随机对照组、walk-forward 样本外折叠、参数网格、事前证伪规则均按其各自 spec 声明执行。")
    a("")
    a("## 排名规则（事前声明，先于任何数字）")
    a("")
    a("1. **裁决档位优先**：`NOT_FALSIFIED` > `INCONCLUSIVE` > `FALSIFIED`——档位本身就是诚实度排序："
      "扛住全部事前规则的策略，排在「测不出结论」的之上，排在「被规则抓住」的之上。")
    a("2. **同档内按对照组差值 CI 下界降序**：下界为正 = 已证明选择性；跨 0 = 未被证伪但也未证明；整体为负 = 显著差于随机。")
    a("3. **再按样本外净收益降序**（只在没见过的数据段上成立过的钱才算数）。")
    a("4. **再按样本外参数网格正格率降序**（换参数还活着的格子的比例）。")
    a("5. **最后按最坏口径成交数降序**（样本量即话语权）。")
    a("")

    a("## 对比矩阵")
    a("")
    a("| 排名 | 策略 | 裁决 | 死因 | 成交 | 最坏净 | 最好净 | ×2 成本净 | 对照组 CI | 选择性 | 样本外净 | 样本外网格 | 触发规则 |")
    a("|---:|---|---|---|---:|---:|---:|---:|---|---|---:|---|---|")
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        name = spec.get("name") or f"strategy-{i}"
        verdict = (r.get("verdict") or {}).get("outcome", "?")
        dx = r.get("diagnosis") or {}
        cause = dx.get("primary_label") or "—"
        if dx.get("tag_labels"):
            cause += f"（+{'、'.join(dx['tag_labels'])}）"
        ci = _ci_lower(r)
        ci_txt = "n/a" if ci is None else f"[{_pct(ci)}, {_pct((r.get('control') or {}).get('diff_ci')[1])}]"
        oos = _oos_net(r)
        grid = _grid_ratio(r)
        grid_txt = "n/a" if grid is None else f"{grid * 100:.0f}%"
        fired = _triggered_rules(r)
        fired_txt = "、".join(fired) if fired else "无"
        a(f"| {i} | {name} | {verdict} | {cause} | {_fills(r)} | {_pct(_worst_net(r))} | {_pct(_best_net(r))} "
          f"| {_pct(_fee2_net(r))} | {ci_txt} | {_selectivity(r)} | {_pct(oos)} | {grid_txt} | {fired_txt} |")
    a("")

    a("## 稳健性排名（一句话版）")
    a("")
    best = ranked[0]
    best_name = (best.get("spec") or {}).get("name") or "第 1 名"
    best_ci = _ci_lower(best)
    if best_ci is not None and best_ci > 0:
        why = "对照组差值 CI 下界为正——它是这一组里唯一（或最能）证明自己胜过随机入场的"
    elif best_ci is not None:
        why = "它的对照组 CI 下界最接近 0——未被证伪，但也远未被证明"
    else:
        why = "无对照组数据，按样本外与网格表现排序"
    a(f"**{best_name}**：{why}。")
    worst = ranked[-1]
    worst_name = (worst.get("spec") or {}).get("name") or "末位"
    a(f"**{worst_name}**：排名最低——详见其触发规则。")
    a("")
    a("> 注意：这个排名只回答\"哪一个最不过拟合\"，不回答\"该不该实盘\"。"
      "第一名同样可能不该实盘——看它自己的报告第 8 节未建模事项。")
    a("")

    a("## 每个策略的裁决理由（原样摘录）")
    a("")
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        name = spec.get("name") or f"strategy-{i}"
        v = r.get("verdict") or {}
        dx = r.get("diagnosis") or {}
        a(f"### {i}. {name} —— {v.get('outcome', '?')}")
        a("")
        a(f"{v.get('reason', '（无理由）')}")
        a("")
        if dx:
            cause = f"**死因：{dx.get('primary_label', dx.get('primary', '?'))}**"
            if dx.get("tag_labels"):
                cause += f"（附带：{'、'.join(dx['tag_labels'])}）"
            a(cause)
            if dx.get("prescriptions"):
                a("")
                a("首要处方：" + dx["prescriptions"][0])
            a("")
        # Per-strategy provenance: what data, what assumptions. A comparison
        # across different symbols/windows must say so on its face.
        a(f"- 标的/周期/场所：{spec.get('symbol', '?')} · {spec.get('interval', '?')} · {spec.get('venue', '?')}"
          f" · 单边费率 {float(spec.get('fee_rate', 0)) * 10000:.0f}bps · {spec.get('fill_model', '?')} 成交"
          f" · {'含资金费' if spec.get('funding') else '不含资金费'}")
        window = r.get("window") or {}
        if window:
            a(f"- 数据窗口：{window.get('bars', '?')} 根（{window.get('from', '?')} → {window.get('to', '?')}）")
        a("")

    return "\n".join(lines)
