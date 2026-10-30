"""HTML rendering: the falsification report as a self-contained page.

One file, zero external requests, zero JavaScript. Everything the markdown
says is here, plus three charts markdown cannot carry:
  - realized equity curves (worst vs best case, in trade order)
  - cost-sensitivity bars at each fee multiple
  - the parameter grid as a heatmap (the shape of overfitting at a glance)

The colour language matches the landing page (same CSS variables, light
theme): kill-red for falsified/negative, green for surviving/positive,
amber for "could not tell". Colour never carries meaning alone -- every
coloured cell also states its number in text.
"""
from __future__ import annotations

from .report import _fmt_axis, _n, _params_txt, _pf, _pct, _plain_sentence, _key_numbers, unmodeled_notes

_VERDICT_CLASS = {
    "FALSIFIED": "kill",
    "NOT_FALSIFIED": "ok",
    "INCONCLUSIVE": "warn",
}
_VERDICT_LABEL = {
    "FALSIFIED": "证伪",
    "NOT_FALSIFIED": "未被证伪",
    "INCONCLUSIVE": "无法结论",
}

_CSS = """
:root{--ink:#1a2332;--ink-2:#4a5568;--ink-3:#718096;--line:#e2e8f0;--bg:#ffffff;
--bg-soft:#f7f9fc;--accent:#0b5fff;--accent-ink:#0849c4;--kill:#c0392b;--kill-bg:#fdf0ee;
--ok:#1e7f4f;--ok-bg:#ecf8f1;--warn:#9a6a00;--warn-bg:#fdf6e3;
--mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.page{max-width:880px;margin:0 auto;padding:32px 24px 64px}
header{border-bottom:3px solid var(--ink);padding-bottom:20px;margin-bottom:8px}
h1{font-size:24px;margin:12px 0 6px}
h2{font-size:18px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:20px 0 8px}
.hypo{color:var(--ink-2);margin:4px 0}
.badge{display:inline-block;padding:4px 14px;border-radius:999px;font-weight:700;font-size:14px;letter-spacing:.5px}
.badge.kill{background:var(--kill);color:#fff}
.badge.ok{background:var(--ok);color:#fff}
.badge.warn{background:var(--warn);color:#fff}
.sentence{font-size:16px;background:var(--bg-soft);border-left:4px solid var(--accent);
padding:10px 14px;border-radius:0 8px 8px 0;margin:14px 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 0}
.chip{background:var(--bg-soft);border:1px solid var(--line);border-radius:8px;
padding:6px 10px;font-size:13px;color:var(--ink-2)}
.chip b{color:var(--ink);font-family:var(--mono);font-size:13px}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13.5px}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--bg-soft);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-family:var(--mono);font-size:12.5px;white-space:nowrap}
tr.hit td{background:var(--kill-bg)}
tr.pass td{background:var(--ok-bg)}
tr.weak td{background:var(--warn-bg)}
.note{background:var(--bg-soft);border:1px solid var(--line);border-radius:8px;
padding:10px 14px;margin:10px 0;font-size:13.5px;color:var(--ink-2)}
.warnbox{background:var(--warn-bg);border:1px solid #e8d9ac;border-radius:8px;
padding:10px 14px;margin:10px 0;font-size:13.5px}
ul{margin:8px 0;padding-left:22px}
li{margin:3px 0}
code{font-family:var(--mono);font-size:12.5px;background:var(--bg-soft);
border:1px solid var(--line);border-radius:4px;padding:1px 5px}
pre{background:var(--bg-soft);border:1px solid var(--line);border-radius:8px;
padding:12px 14px;overflow-x:auto;font-family:var(--mono);font-size:12.5px;line-height:1.5}
.chart{margin:14px 0}
.chart figcaption{font-size:12.5px;color:var(--ink-3);margin-top:4px}
.legend{display:flex;gap:18px;font-size:12.5px;color:var(--ink-2);margin:6px 0}
.swatch{display:inline-block;width:14px;height:4px;border-radius:2px;vertical-align:middle;margin-right:6px}
.port{background:var(--warn-bg);border:1px solid #e8d9ac;border-radius:8px;padding:14px 16px;margin:12px 0}
.port pre{background:#fff}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);
font-size:13px;color:var(--ink-3)}
footer a{color:var(--accent)}
.pos{color:var(--ok);font-weight:600}
.neg{color:var(--kill);font-weight:600}
.mono{font-family:var(--mono);font-size:12.5px}
"""


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _signed(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="{cls}">{v:+.{digits}f}%</span>'


def _table(headers: list[str], rows: list[list[str]], num_cols: set[int] | None = None,
           row_class: list[str] | None = None) -> str:
    num_cols = num_cols or set()
    out = ["<table>", "<thead><tr>"]
    for i, h in enumerate(headers):
        cls = ' class="num"' if i in num_cols else ""
        out.append(f"<th{cls}>{_esc(h)}</th>")
    out.append("</tr></thead><tbody>")
    for r, row in enumerate(rows):
        cls = f' class="{row_class[r]}"' if row_class and row_class[r] else ""
        out.append(f"<tr{cls}>")
        for i, cell in enumerate(row):
            c = ' class="num"' if i in num_cols else ""
            out.append(f"<td{c}>{cell}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


# --------------------------------------------------------------------------
# SVG charts
# --------------------------------------------------------------------------

def _resample(series: list[float], max_points: int = 640) -> list[float]:
    if len(series) <= max_points:
        return series
    step = (len(series) - 1) / (max_points - 1)
    return [series[min(len(series) - 1, round(i * step))] for i in range(max_points)]


def _svg_equity(worst_nets: list[float], best_nets: list[float]) -> str:
    """Realized equity curves for both replay orders, in trade order."""
    if not worst_nets and not best_nets:
        return ""

    def curve(nets: list[float]) -> list[float]:
        eq, out = 1.0, [1.0]
        for v in _resample(nets):
            eq *= 1.0 + v
            out.append(eq)
        return out

    w_curve = curve(worst_nets)
    b_curve = curve(best_nets)
    all_v = w_curve + b_curve
    lo, hi = min(all_v), max(all_v)
    pad = max((hi - lo) * 0.08, 0.02)
    lo, hi = lo - pad, hi + pad

    W, H = 680, 240
    ml, mr, mt, mb = 64, 14, 12, 26
    pw, ph = W - ml - mr, H - mt - mb

    def xy(i: int, n: int, v: float) -> tuple[float, float]:
        x = ml + (pw * i / max(1, n - 1))
        y = mt + ph * (hi - v) / (hi - lo)
        return x, y

    def path(c: list[float], color: str) -> str:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (xy(i, len(c), v) for i, v in enumerate(c)))
        end_x, end_y = xy(len(c) - 1, len(c), c[-1])
        return (f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8"/>'
                f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="3" fill="{color}"/>'
                f'<text x="{end_x - 6:.1f}" y="{end_y - 7:.1f}" text-anchor="end" '
                f'font-size="11" fill="{color}">{c[-1]:.2f}×</text>')

    y0 = mt + ph * (hi - 1.0) / (hi - lo)
    grid = "".join(
        f'<line x1="{ml}" y1="{mt + ph * f:.1f}" x2="{W - mr}" y2="{mt + ph * f:.1f}" '
        f'stroke="#e2e8f0" stroke-width="1"/>'
        f'<text x="{ml - 8}" y="{mt + ph * f + 4:.1f}" text-anchor="end" font-size="10.5" '
        f'fill="#718096">{hi - f * (hi - lo):.2f}</text>'
        for f in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    svg = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        f'aria-label="逐笔权益曲线：最坏优先与最好优先">',
        grid,
        f'<line x1="{ml}" y1="{y0:.1f}" x2="{W - mr}" y2="{y0:.1f}" stroke="#718096" '
        f'stroke-width="1" stroke-dasharray="4 4"/>',
        f'<text x="{ml - 8}" y="{y0 + 4:.1f}" text-anchor="end" font-size="10.5" '
        f'fill="#718096">1.00</text>',
        path(w_curve, "#c0392b"),
        path(b_curve, "#1e7f4f"),
        f'<text x="{ml}" y="{H - 8}" font-size="10.5" fill="#718096">第 1 笔</text>',
        f'<text x="{W - mr}" y="{H - 8}" text-anchor="end" font-size="10.5" fill="#718096">'
        f'最后 1 笔（按成交顺序）</text>',
        "</svg>",
    ]
    return "".join(svg)


def _svg_cost_bars(rows: list[dict]) -> str:
    if not rows:
        return ""
    W = 680
    bar_h, gap, mt = 26, 10, 8
    H = mt * 2 + len(rows) * (bar_h + gap)
    zero_x = W / 2
    max_abs = max((abs(r["net_sum_pct"]) for r in rows), default=1.0) or 1.0
    half = zero_x - 130  # leave room for labels on the left
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="成本敏感性">']
    out.append(f'<line x1="{zero_x}" y1="0" x2="{zero_x}" y2="{H}" stroke="#94a3b8" stroke-width="1"/>')
    for i, r in enumerate(rows):
        y = mt + i * (bar_h + gap)
        v = r["net_sum_pct"]
        w = abs(v) / max_abs * half
        color = "#1e7f4f" if v > 0 else "#c0392b"
        x = zero_x if v >= 0 else zero_x - w
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{max(w, 1):.1f}" height="{bar_h}" '
                   f'rx="3" fill="{color}" fill-opacity="0.85"/>')
        label = f'×{r["fee_multiple"]:g}（往返 {r["fee_bps"]:.0f}bps）'
        out.append(f'<text x="8" y="{y + bar_h / 2 + 4:.1f}" font-size="12" fill="#4a5568">{label}</text>')
        tx = (zero_x + w + 8) if v >= 0 else (zero_x - w - 8)
        anchor = "start" if v >= 0 else "end"
        out.append(f'<text x="{tx:.1f}" y="{y + bar_h / 2 + 4:.1f}" text-anchor="{anchor}" '
                   f'font-size="12" font-weight="700" fill="{color}">{v:+.1f}%</text>')
    out.append("</svg>")
    return "".join(out)


def _grid_heatmap(scope: dict, axes: dict, axis_order: list[str]) -> str:
    """The first two axes as a heatmap; every cell also prints its number."""
    cells = scope.get("cells") or []
    if len(axis_order) < 2 or not cells:
        return ""
    ax0, ax1 = axis_order[0], axis_order[1]
    v0s, v1s = list(axes[ax0]), list(axes[ax1])
    lookup = {}
    for c in cells:
        key = (c["params"].get(ax0), c["params"].get(ax1))
        lookup[key] = c
    max_abs = max((abs(c["net_sum_pct"]) for c in cells), default=1.0) or 1.0

    n_cols = len(v1s)
    cell_w = min(120.0, (680 - 110) / max(1, n_cols))
    cell_h = 52
    W = 110 + cell_w * n_cols + 10
    H = 34 + cell_h * len(v0s) + 8
    out = [f'<svg viewBox="0 0 {W:.0f} {H}" width="100%" role="img" aria-label="参数网格热力图">']
    # column headers (axis1 values)
    for j, v1 in enumerate(v1s):
        x = 110 + j * cell_w + cell_w / 2
        out.append(f'<text x="{x:.1f}" y="16" text-anchor="middle" font-size="11" '
                   f'fill="#4a5568">{_esc(_fmt_axis(ax1, v1))}</text>')
    out.append(f'<text x="{110 + cell_w * n_cols / 2:.1f}" y="30" text-anchor="middle" '
               f'font-size="10.5" fill="#718096">↑ 列：{_esc(ax1)}</text>')
    for i, v0 in enumerate(v0s):
        y = 34 + i * cell_h
        out.append(f'<text x="102" y="{y + cell_h / 2 + 4:.1f}" text-anchor="end" '
                   f'font-size="11" fill="#4a5568">{_esc(_fmt_axis(ax0, v0))}</text>')
        for j, v1 in enumerate(v1s):
            c = lookup.get((v0, v1))
            x = 110 + j * cell_w
            if c is None:
                continue
            if c["fills"] == 0:
                fill, tcol = "#e2e8f0", "#718096"
            else:
                net = c["net_sum_pct"]
                alpha = min(0.92, 0.25 + abs(net) / max_abs * 0.67)
                fill = (f"rgba(30,127,79,{alpha:.2f})" if net > 0 else f"rgba(192,57,43,{alpha:.2f})")
                tcol = "#fff" if alpha > 0.55 else "#1a2332"
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{cell_w - 4:.1f}" height="{cell_h - 4}" '
                       f'rx="4" fill="{fill}"/>')
            txt = f'{c["net_sum_pct"]:+.0f}%' if c["fills"] else "无成交"
            sub = f'{c["fills"]} 笔' if c["fills"] else ""
            out.append(f'<text x="{x + (cell_w - 4) / 2:.1f}" y="{y + cell_h / 2 - 2:.1f}" '
                       f'text-anchor="middle" font-size="12" font-weight="700" fill="{tcol}">{txt}</text>')
            if sub:
                out.append(f'<text x="{x + (cell_w - 4) / 2:.1f}" y="{y + cell_h / 2 + 13:.1f}" '
                           f'text-anchor="middle" font-size="10" fill="{tcol}" fill-opacity="0.85">{sub}</text>')
    out.append("</svg>")
    return "".join(out)


def _grid_table(scope: dict, axes: dict, axis_order: list[str]) -> str:
    rows = []
    for c in scope.get("cells") or []:
        rows.append([
            _esc(_params_txt(c["params"])),
            _n(c["fills"]),
            _signed(c["net_sum_pct"]),
            _signed(c["mean_pct"]),
            f'{c["win_pct"]:.0f}%',
            _pf(c.get("profit_factor")),
        ])
    return _table(["参数组合", "成交", "净收益", "均值/笔", "胜率", "PF"],
                  rows, num_cols={1, 2, 3, 4, 5})


# --------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------

def render_html(result: dict) -> str:
    spec = result.get("spec") or {}
    verdict = result.get("verdict") or {}
    outcome = verdict.get("outcome", "?")
    cls = _VERDICT_CLASS.get(outcome, "warn")
    vlabel = _VERDICT_LABEL.get(outcome, outcome)
    name = spec.get("name", "strategy")
    worst = result["runs"]["worst_case"]
    best = result["runs"]["best_case"]
    wst, bst = worst["stats"], best["stats"]
    wf = result.get("walk_forward")
    pg = result.get("param_grid")
    ctrl = result.get("control")

    h: list[str] = []
    a = h.append
    a("<!DOCTYPE html>")
    a('<html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{_esc(name)} · 策略证伪报告</title>")
    a(f"<style>{_CSS}</style></head><body><div class='page'>")

    # ---- header -----------------------------------------------------------
    a("<header>")
    a(f'<span class="badge {cls}">{_esc(outcome)} · {_esc(vlabel)}</span>')
    a(f"<h1>{_esc(name)}</h1>")
    if spec.get("hypothesis"):
        a(f'<p class="hypo">{_esc(spec["hypothesis"])}</p>')
    a(f'<p class="sentence">{_esc(_plain_sentence(result))}</p>')
    a('<div class="chips">')
    for k in _key_numbers(result):
        a(f'<span class="chip">{_esc(k)}</span>')
    a("</div>")
    a(f'<p class="hypo" style="margin-top:10px">标的 <b>{_esc(spec.get("symbol", "?"))}</b> · '
      f'周期 {_esc(spec.get("interval", "?"))} · {_esc(spec.get("venue", "?"))} · '
      f'单边费率 {float(spec.get("fee_rate", 0)) * 10000:.0f}bps · '
      f'{_esc(spec.get("fill_model", "?"))} 成交 · '
      f'{"含资金费" if spec.get("funding") else "不含资金费"}</p>')
    a("</header>")

    # ---- 0. port notes ----------------------------------------------------
    if spec.get("port_notes"):
        a('<div class="port">')
        a("<h3 style='margin-top:0'>0. 移植说明（外部策略保真度）</h3>")
        for para in spec["port_notes"].split("\n\n"):
            para = para.strip()
            if para:
                a(f"<p>{_esc(para).replace(chr(10), '<br>')}</p>")
        a("</div>")

    # ---- 1. replay --------------------------------------------------------
    a("<h2>1. Path-aware 回放</h2>")
    a(_table(
        ["口径", "成交", "未成交", "未决", "净收益", "均值/笔", "胜率", "PF", "最大回撤(时序)"],
        [[f"{label}", _n(r["stats"]["n"]), _n(r["unfilled"]), _n(r["open_signals"]),
          _signed(r["stats"]["sum_pct"]), _signed(r["stats"]["mean_pct"]),
          f'{r["stats"]["win_pct"]:.0f}%', _pf(r["stats"]["profit_factor"]),
          _signed(r["stats"]["max_dd_pct"])]
         for label, r in (("最坏优先", worst), ("最好优先", best))],
        num_cols={1, 2, 3, 4, 5, 6, 7, 8},
    ))
    if wst["n"] and bst["n"] and wst["sum_pct"] > 0 > bst["sum_pct"]:
        a('<div class="warnbox">同一策略只在乐观口径下赚钱——它赚的是假设，不是价差。</div>')
    dd = wst.get("max_dd_worst_order_pct")
    if dd is not None and wst["n"]:
        a(f'<p class="note">回撤口径：表中为<b>时序</b>最大回撤（真实经历）；'
          f'同账面按“先亏后赚”重放的界为 {dd:.2f}%——是界，不是历史。</p>')
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        by = result["runs"][case].get("by_reason") or {}
        if by:
            parts = " · ".join(
                f"{k} {_n(b['n'])} 笔({b['net_sum_pct']:+.2f}%)"
                for k, b in sorted(by.items(), key=lambda kv: -kv[1]["net_sum_pct"]))
            a(f"<p>{label}退出结构：{_esc(parts)}</p>")
    eq = _svg_equity(wst.get("nets") or [], bst.get("nets") or [])
    if eq:
        a('<figure class="chart">')
        a(eq)
        a('<div class="legend">'
          '<span><span class="swatch" style="background:#c0392b"></span>最坏优先（同根 K 线平局判止损）</span>'
          '<span><span class="swatch" style="background:#1e7f4f"></span>最好优先（同根 K 线平局判止盈）</span>'
          "</div>")
        a("<figcaption>逐笔权益曲线（按成交顺序，起点 1.00×）：两条线的分离程度就是"
          "“平局假设”对结论的影响幅度；重合则说明结论不依赖该假设。</figcaption>")
        a("</figure>")

    # ---- 2. cost ----------------------------------------------------------
    a("<h2>2. 成本敏感性</h2>")
    a(_table(["成本倍数", "往返费率", "净收益", "PF", "胜率"],
             [[f'×{r["fee_multiple"]:g}', f'{r["fee_bps"]:.0f}bps', _signed(r["net_sum_pct"]),
               _pf(r.get("profit_factor")), f'{r["win_pct"]:.0f}%']
              for r in result["cost_sensitivity"]],
             num_cols={0, 1, 2, 3, 4}))
    bars = _svg_cost_bars(result["cost_sensitivity"])
    if bars:
        a('<figure class="chart">')
        a(bars)
        a("<figcaption>你的 edge 付得起多贵的过路费。条越过零线向左，"
          "即该成本下策略亏损。</figcaption>")
        a("</figure>")

    # ---- 3. barriers ------------------------------------------------------
    a("<h2>3. 障碍可达性</h2>")
    br = result.get("barrier_reach") or {}
    if br.get("n"):
        a("<ul>")
        a(f"<li>样本 {_n(br['n'])} 笔 · 摸到止盈位的 {br['reached_tp_pct']:.1f}% · "
          f"摸到止损位的 {br['reached_sl_pct']:.1f}% · 两者都没摸到的 {br['reached_neither_pct']:.1f}%</li>")
        a(f"<li>最大有利偏离（MFE）中位 {_pct(br['mfe']['median'], 1)} / p90 {_pct(br['mfe']['p90'], 1)} · "
          f"最大不利偏离（MAE）中位 {_pct(br['mae']['median'], 1)} / p10 {_pct(br['mae']['p10'], 1)}</li>")
        a("</ul>")
        if br.get("tp_level_pct") is not None and br["reached_tp_pct"] < 10:
            a(f'<div class="warnbox">止盈位 {br["tp_level_pct"]:+.1f}% 在持仓窗口内几乎从未被触及'
              "——这个策略的大部分盈利期望是彩票结构。</div>")
        if br.get("sl_level_pct") is not None and br["reached_sl_pct"] < 10:
            a(f'<div class="warnbox">止损位 {br["sl_level_pct"]:+.1f}% 在持仓窗口内几乎从未被触及'
              "——回测里的“止损”大部分时间没有在工作。</div>")
    else:
        a(f'<p class="note">{_esc(br.get("note", "无样本"))}</p>')

    # ---- 4. control -------------------------------------------------------
    a("<h2>4. 随机入场对照组</h2>")
    if ctrl:
        cs = ctrl["stats"]
        a("<ul>")
        if ctrl.get("note"):
            a(f"<li>{_esc(ctrl['note'])}</li>")
        else:
            rng = ctrl.get("bar_range")
            rng_txt = f"，随机入场范围 第 {rng[0]} → 第 {rng[1]} 根" if rng and rng[0] != 30 else ""
            a(f"<li>对照组：{_n(ctrl['n_random'])} 个随机入场（seed={ctrl['seed']}，允许重复，"
              f"镜像策略主方向 {ctrl.get('side', 'long')}{rng_txt}），同一窗口、同一退出规则、同一成本假设</li>")
            a(f"<li>策略均值/笔 {_pct(ctrl['strategy_mean_pct'])} vs 对照组 {_pct(ctrl['control_mean_pct'])}"
              f"（成交 {_n(cs['n'])} 笔，胜率 {cs['win_pct']:.0f}%）</li>")
            if ctrl.get("diff_pct") is not None:
                ci = ctrl.get("diff_ci")
                ci_txt = f"[{ci[0]:+.2f}%, {ci[1]:+.2f}%]" if ci else "（样本不足，未计算）"
                a(f"<li>差值 {_pct(ctrl['diff_pct'])}，95% bootstrap 区间 {ci_txt} · "
                  f"选择性判定：<b>{_esc(ctrl.get('selectivity', 'n/a'))}</b></li>")
        a("</ul>")
    else:
        a('<p class="note">本 run 未启用对照组。</p>')

    # ---- 5. walk-forward --------------------------------------------------
    a("<h2>5. Walk-forward（样本内 / 样本外）</h2>")
    if wf and wf.get("out_sample"):
        isb = wf.get("in_sample") or {}
        oos = wf["out_sample"]
        rows, rc = [], []
        for label, seg in (("样本内", isb), ("样本外", oos)):
            if not seg:
                continue
            st = seg["runs"]["worst_case"]["stats"]
            seg_ctrl = seg.get("control") or {}
            ci = seg_ctrl.get("diff_ci")
            ci_txt = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a"
            seg_verdict = (seg.get("verdict") or {}).get("outcome", "?")
            rows.append([label, _n(st["n"]), _signed(st["sum_pct"]),
                         _signed(st.get("mean_pct")), ci_txt,
                         _esc(seg_ctrl.get("selectivity", "n/a")),
                         f'<b>{_esc(seg_verdict)}</b>'])
            rc.append("hit" if seg_verdict == "FALSIFIED" else "")
        a(_table(["段", "成交", "最坏净", "均值/笔", "对照组 CI", "选择性", "裁决"],
                 rows, num_cols={1, 2, 3}, row_class=rc))
        for label, key in (("样本内", "in_sample_grid"), ("样本外", "out_sample_grid")):
            g = wf.get(key) or {}
            if g.get("cells"):
                a(f"<h3>{label} · 参数网格（{g.get('positive_cells', 0)}/{g.get('n_cells', 0)} 格为正）</h3>")
                hm = _grid_heatmap(g, g.get("axes") or {}, g.get("axis_order") or [])
                if hm:
                    a('<figure class="chart">')
                    a(hm)
                    a("<figcaption>颜色 = 该参数组合的净收益（绿正红负），"
                      "数字同时印在格内——不依赖颜色传达信息。</figcaption>")
                    a("</figure>")
    else:
        a('<p class="note">本 run 未启用 walk-forward——所有数字都是窗口内的样本内结果。</p>')

    # ---- 6. param grid ----------------------------------------------------
    a("<h2>6. 参数敏感性网格</h2>")
    if pg and pg.get("scopes"):
        axes = pg.get("axes") or {}
        axis_order = pg.get("axis_order") or []
        for scope in pg["scopes"]:
            if not scope.get("cells"):
                continue
            a(f"<h3>{_esc(scope.get('scope', '全窗口'))} · "
              f"{scope.get('positive_cells', 0)}/{scope.get('n_cells', 0)} 格为正"
              f"（{scope.get('cells_with_fills', 0)} 格有成交）</h3>")
            hm = _grid_heatmap(scope, axes, axis_order)
            if hm:
                a('<figure class="chart">')
                a(hm)
                a("<figcaption>换一组参数还活着的格子比例，就是策略对参数的依赖度；"
                  "只有个别格子为正 = 曲线拟合，不是 edge。</figcaption>")
                a("</figure>")
            a(_grid_table(scope, axes, axis_order))
    else:
        a('<p class="note">本 run 未启用参数网格。</p>')

    # ---- 7. verdict rules -------------------------------------------------
    a("<h2>7. 事前证伪规则判定</h2>")
    checks = verdict.get("checks") or []
    if checks:
        rows, rc = [], []
        for c in checks:
            if c.get("triggered"):
                mark, klass = "触发", "hit"
            elif c.get("insufficient"):
                mark, klass = "样本不足", "weak"
            else:
                mark, klass = "通过", "pass"
            rows.append([f'<code>{_esc(c["rule"])}</code>', _esc(c.get("scope", "全窗口")),
                         mark, _esc(c["detail"])])
            rc.append(klass)
        a(_table(["规则", "范围", "结果", "明细"], rows, row_class=rc))
    else:
        a('<p class="note">spec 未声明任何 falsify_if 规则——本报告只呈现测量，不下裁决。</p>')

    # ---- 8. diagnosis ------------------------------------------------------
    a("<h2>8. 诊断：死因与处方</h2>")
    dx = result.get("diagnosis") or {}
    if dx:
        a(f'<p class="sentence" style="border-left-color:var(--kill)">'
          f'主死因：<b>{_esc(dx.get("primary_label", dx.get("primary", "?")))}</b>'
          + (f'　·　附带问题：{_esc(" · ".join(dx.get("tag_labels") or []))}'
             if dx.get("tag_labels") else "")
          + "</p>")
        if dx.get("evidence"):
            a("<p><b>证据</b>（全部来自本次测量，无新增假设）：</p><ul>")
            for e in dx["evidence"]:
                a(f"<li>{_esc(e)}</li>")
            a("</ul>")
        if dx.get("prescriptions"):
            a("<p><b>处方</b>（按优先级；每条都引用本次运行的真实数字）：</p><ol>")
            for p in dx["prescriptions"]:
                a(f"<li>{_esc(p)}</li>")
            a("</ol>")
        a('<p class="note">死因归类规则事前声明于 <code>research/verify/diagnosis.py</code>，'
          "先于任何策略被分类——与证伪规则同一条纪律。处方是工程建议，不是投资建议。</p>")
    else:
        a('<p class="note">（本报告无诊断信息）</p>')

    # ---- 9. disclosures ---------------------------------------------------
    a("<h2>9. 未建模事项（阅读前必读）</h2>")
    a("<ul>")
    for t in unmodeled_notes(result):
        a(f"<li>{_esc(t)}</li>")
    a("</ul>")

    # ---- 9. repro ---------------------------------------------------------
    repro = result.get("repro")
    if repro:
        a("<h2>10. 复现命令</h2>")
        a("<p>本次报告由以下命令产生；在同一目录重跑，可复现同样数字：</p>")
        a(f"<pre>{_esc(repro['command'])}</pre>")
        a(_table(["输入", "路径 / 来源"],
                 [["spec", _esc(repro["spec"])],
                  ["candles", _esc(repro["candles"])],
                  ["signals", _esc(repro["signals"])]]
                 + ([["funding", _esc(repro["funding"])]] if repro.get("funding") else [])))

    # ---- footer -----------------------------------------------------------
    a("<footer>")
    a('<p>由策略证伪引擎生成（六件仪器：path-aware 回放 · 成本敏感性 · 障碍可达性 · '
      '随机对照组 · walk-forward · 参数网格）。规则先于数字，失败同样发布。</p>')
    a('<p><a href="https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/">落地页</a> · '
      '<a href="https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2">开源仓库</a> · '
      '服务：带策略来，我出证伪报告 —— 首 10 单免费（换匿名公开案例授权），付费档 $99 / $299。</p>')
    a("</footer>")
    a("</div></body></html>")
    return "\n".join(h)


def render_to(result: dict, path) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_html(result), encoding="utf-8")


# --------------------------------------------------------------------------
# comparison page
# --------------------------------------------------------------------------

def render_comparison_html(results: list[dict], title: str = "多策略对比") -> str:
    """The comparison as a page: the declared rule, the matrix, and one
    collapsible detail block per strategy (native <details>, no JS)."""
    from .compare import (_best_net, _ci_lower, _fee2_net, _fills, _grid_ratio,
                          _oos_net, _selectivity, _triggered_rules, _worst_net, rank_key)

    ranked = sorted(results, key=rank_key, reverse=True)
    h: list[str] = []
    a = h.append
    a("<!DOCTYPE html>")
    a('<html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{_esc(title)} · 策略证伪</title>")
    a(f"<style>{_CSS}</style></head><body><div class='page'>")
    a("<header>")
    a("<h1 style='margin-top:0'>多策略对比</h1>")
    a(f'<p class="hypo">{len(ranked)} 个策略，同一套证伪电池，同一套事前声明的排名规则。</p>')
    a("</header>")

    a("<h2>排名规则（事前声明，先于任何数字）</h2>")
    a("<ol>")
    a("<li><b>裁决档位优先</b>：<code>NOT_FALSIFIED</code> &gt; <code>INCONCLUSIVE</code> &gt; "
      "<code>FALSIFIED</code>——档位本身就是诚实度排序。</li>")
    a("<li><b>同档内按对照组差值 CI 下界降序</b>：下界为正 = 已证明选择性；跨 0 = 未证明；整体为负 = 差于随机。</li>")
    a("<li><b>再按样本外净收益降序</b>（只在没见过的数据段上成立过的钱才算数）。</li>")
    a("<li><b>再按样本外参数网格正格率降序</b>（换参数还活着的格子比例）。</li>")
    a("<li><b>最后按成交数降序</b>（样本量即话语权）。</li>")
    a("</ol>")

    a("<h2>对比矩阵</h2>")
    rows, rc = [], []
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        outcome = (r.get("verdict") or {}).get("outcome", "?")
        ci = _ci_lower(r)
        ctrl = r.get("control") or {}
        ci_txt = "n/a" if ci is None else f"[{ci:+.2f}%, {ctrl.get('diff_ci', [None, None])[1]:+.2f}%]"
        grid = _grid_ratio(r)
        dx = r.get("diagnosis") or {}
        cause = dx.get("primary_label", "—")
        if dx.get("tag_labels"):
            cause += "（+" + "、".join(dx["tag_labels"]) + "）"
        fired = _triggered_rules(r)
        rows.append([
            str(i),
            f'<b>{_esc(spec.get("name", "?"))}</b>',
            f'<span class="badge {_VERDICT_CLASS.get(outcome, "warn")}" '
            f'style="font-size:12px;padding:2px 10px">{_esc(outcome)}</span>',
            _esc(cause),
            _n(_fills(r)),
            _signed(_worst_net(r)),
            _signed(_best_net(r)),
            _signed(_fee2_net(r)),
            ci_txt,
            _esc(_selectivity(r)),
            _signed(_oos_net(r)),
            "n/a" if grid is None else f"{grid * 100:.0f}%",
            _esc("、".join(fired) if fired else "无"),
        ])
        rc.append("hit" if outcome == "FALSIFIED" else
                  ("pass" if outcome == "NOT_FALSIFIED" else "weak"))
    a(_table(["#", "策略", "裁决", "死因", "成交", "最坏净", "最好净", "×2 成本净",
              "对照组 CI", "选择性", "样本外净", "样本外网格", "触发规则"],
             rows, num_cols={0, 4, 5, 6, 7, 10, 11}, row_class=rc))

    best = ranked[0]
    best_name = (best.get("spec") or {}).get("name", "第 1 名")
    best_ci = _ci_lower(best)
    if best_ci is not None and best_ci > 0:
        why = "对照组差值 CI 下界为正——它是这一组里唯一（或最能）证明自己胜过随机入场的。"
    elif best_ci is not None:
        why = "它的对照组 CI 下界最接近 0——未被证伪，但也远未被证明。"
    else:
        why = "无对照组数据，按样本外与网格表现排序。"
    a(f'<p class="sentence">稳健性第一：<b>{_esc(best_name)}</b>。{why}'
      f'<br>注意：这个排名只回答「哪一个最不过拟合」，不回答「该不该实盘」。</p>')

    a("<h2>每个策略的裁决理由</h2>")
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        outcome = (r.get("verdict") or {}).get("outcome", "?")
        a("<details>" + (" open" if i <= 2 else "") + ">")
        a(f"<summary><b>{i}. {_esc(spec.get('name', '?'))}</b> —— "
          f'<span class="badge {_VERDICT_CLASS.get(outcome, "warn")}" '
          f'style="font-size:12px;padding:2px 10px">{_esc(outcome)}</span></summary>')
        a(f'<p>{_esc((r.get("verdict") or {}).get("reason", "（无理由）"))}</p>')
        dx = r.get("diagnosis") or {}
        if dx:
            cause = f'死因：<b>{_esc(dx.get("primary_label", dx.get("primary", "?")))}</b>'
            if dx.get("tag_labels"):
                cause += "（附带：" + _esc("、".join(dx["tag_labels"])) + "）"
            a(f"<p>{cause}</p>")
            if dx.get("prescriptions"):
                a(f'<p class="hypo">首要处方：{_esc(dx["prescriptions"][0])}</p>')
        chips = _key_numbers(r)
        if chips:
            a('<div class="chips">')
            for k in chips:
                a(f'<span class="chip">{_esc(k)}</span>')
            a("</div>")
        a(f'<p class="hypo">标的 {_esc(spec.get("symbol", "?"))} · {_esc(spec.get("interval", "?"))} · '
          f'{_esc(spec.get("venue", "?"))} · 单边费率 {float(spec.get("fee_rate", 0)) * 10000:.0f}bps · '
          f'{_esc(spec.get("fill_model", "?"))} 成交 · '
          f'{"含资金费" if spec.get("funding") else "不含资金费"}</p>')
        window = r.get("window") or {}
        if window:
            a(f'<p class="hypo">数据窗口：{window.get("bars", "?")} 根'
              f'（{_esc(window.get("from", "?"))} → {_esc(window.get("to", "?"))}）</p>')
        a("</details>")

    a("<footer>")
    a('<p>由策略证伪引擎生成。规则先于数字，失败同样发布。</p>')
    a('<p><a href="https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/">落地页</a> · '
      '<a href="https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2">开源仓库</a></p>')
    a("</footer>")
    a("</div></body></html>")
    return "\n".join(h)


def render_comparison_html_to(results: list[dict], path, title: str = "多策略对比") -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_comparison_html(results, title=title), encoding="utf-8")
