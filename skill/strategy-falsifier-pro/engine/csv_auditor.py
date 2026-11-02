"""Trade-record autopsy: TradingView's List of Trades, no candles required.

A buyer who never writes Python still has a backtest: TradingView exports one
as "List of Trades" CSV. This module performs the HONEST subset of the
battery on such a file -- what trade records alone can support:

  1. Cost stress -- net P&L rescaled at 1x/2x/5x the recorded per-trade cost
     (the recorded P&L already contains the commission actually charged).
  2. Resample bootstrap -- N trades are drawn WITH REPLACEMENT from the
     observed set (fixed seed, 1000 draws) and the total recomputed: if a
     same-sized sample from your own track record can easily come out
     negative, the edge is statistically indistinguishable from noise at
     this sample size. (Shuffling the order would be pointless -- the total
     is order-independent by arithmetic; the question that matters is
     sampling stability, which resampling answers.)
  3. Tail-trim robustness -- the best and worst X% of trades are removed and
     the remainder re-measured: is the edge carried by a handful of outliers?
  4. Basic book stats -- win rate, profit factor, max drawdown, expectancy.

What it CANNOT do -- and every report says so on its face, because pretending
otherwise would make this module a liar: no candles means no path-aware
replay (TP/SL precedence inside a bar is unknowable), no random-entry control
(the control needs bars to enter at), no parameter grid, no walk-forward. The
verdict vocabulary is correspondingly smaller: the module never says
FALSIFIED; it says SURVIVED_STRESS / FAILED_STRESS / INSUFFICIENT, because a
sequence test cannot falsify an edge -- only a full replay can.
"""
from __future__ import annotations

import csv
import random
import statistics
from datetime import datetime
from pathlib import Path

# Column aliases seen in the wild (TradingView exports, Broker exports, and
# hand-made sheets all differ).
_T_KEYS = ("t", "date/time", "date time", "datetime", "date", "time", "exit time", "close time")
_PNL_KEYS = ("net p&l", "net p&l usd", "net pnl", "pnl", "profit", "net profit", "p/l", "pl")
_PRICE_KEYS = ("price usd", "exit price", "price", "close price")
_SIDE_KEYS = ("type", "side", "direction", "position")


def _norm(s: str) -> str:
    return s.strip().lower().replace("_", " ")


def _pick(row: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        if k in row:
            v = row[k]
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    return None


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").replace("%", ""))
    except ValueError:
        return None


def _parse_t(s: str | None) -> int | None:
    """Best-effort epoch-ms from the common export formats."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return int(datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    # bare epoch?
    v = _num(s)
    return int(v) if v and v > 1_000_000_000 else None


def load_trades_csv(path: str | Path) -> list[dict]:
    """Read a trade-record CSV into normalised trade dicts.

    Recognises the P&L column by alias; rows without a parseable P&L are
    skipped (open trades in a partial export).
    """
    p = Path(path)
    with p.open("r", newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    trades = []
    for i, row in enumerate(rows, 1):
        row = {_norm(k): v for k, v in row.items() if k is not None}
        pnl = _num(_pick(row, _PNL_KEYS))
        if pnl is None:
            continue
        trades.append({
            "n": i,
            "t": _parse_t(_pick(row, _T_KEYS)),
            "side": (_pick(row, _SIDE_KEYS) or "").lower() or None,
            "price": _num(_pick(row, _PRICE_KEYS)),
            "pnl": pnl,
        })
    return trades


def _equity_stats(pnls: list[float]) -> dict:
    """Sum, expectancy, win rate, profit factor, max drawdown of a P&L series."""
    n = len(pnls)
    total = sum(pnls)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    gp, gl = sum(wins), -sum(losses)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return {
        "n": n,
        "sum_pct": total,
        "mean_pct": total / n if n else None,
        "win_rate": len(wins) / n if n else None,
        "profit_factor": (gp / gl) if gl > 0 else None,
        "max_drawdown": mdd,
    }


def audit_trades(trades: list[dict] | list[float], *, cost_multiples: tuple[float, ...] = (1.0, 2.0, 5.0),
                 n_bootstrap: int = 1000, seed: int = 42, trim_pct: float = 0.05) -> dict:
    """Run the sequence-level battery on trade records.

    Accepts either trade dicts (from load_trades_csv) or bare P&L floats.

    Cost stress scales each P&L toward zero by the multiple (the conservative
    direction: the recorded P&L is assumed net of the commission actually
    charged, and a real commission is only part of each P&L).
    """
    if trades and isinstance(trades[0], dict):
        pnls = [float(t["pnl"]) for t in trades]
    else:
        pnls = [float(x) for x in trades]
    n = len(pnls)
    base = _equity_stats(pnls)

    # -- cost stress: the commission portion is what the multiplier scales.
    # When the export carries no commission column we cannot know it exactly;
    # we scale the whole P&L toward zero by the extra cost, which is the
    # CONSERVATIVE direction (a real commission is only part of each P&L).
    stress = []
    for m in cost_multiples:
        if m == 1.0:
            stress.append({"fee_multiple": m, "net_sum_pct": base["sum_pct"]})
            continue
        scaled = [x / m if x > 0 else x * m for x in pnls]
        st = _equity_stats(scaled)
        stress.append({"fee_multiple": m, "net_sum_pct": st["sum_pct"],
                       "profit_factor": st["profit_factor"]})

    # -- resample bootstrap: sampling stability of the total ---------------
    rng = random.Random(seed)
    sums = []
    for _ in range(n_bootstrap):
        draw = [pnls[rng.randrange(n)] for _ in range(n)]
        sums.append(sum(draw))
    sums.sort()
    lo = sums[int(0.025 * len(sums))]
    hi = sums[min(len(sums) - 1, int(0.975 * len(sums)))]

    # -- tail trim: is the edge a few outlier trades? -----------------------
    ordered = sorted(pnls)
    k = max(1, int(n * trim_pct)) if n >= 20 else 0
    trimmed = ordered[k:n - k] if k else pnls
    trim_stats = _equity_stats(trimmed)

    # -- the honest verdict vocabulary --------------------------------------
    x2 = next((r["net_sum_pct"] for r in stress if r["fee_multiple"] == 2.0), None)
    if n < 30:
        outcome = "INSUFFICIENT"
        reason = f"仅 {n} 笔成交——序列级检验同样需要证据门槛（30 笔），不足则任何数字都是噪声"
    elif base["sum_pct"] <= 0:
        outcome = "FAILED_STRESS"
        reason = f"账面净收益 {base['sum_pct']:+.2f}% 已为负，无需加压"
    elif x2 is not None and x2 <= 0:
        outcome = "FAILED_STRESS"
        reason = f"成本 ×2 后净收益 {x2:+.2f}% ≤ 0——edge 付不起加倍的 toll"
    elif lo <= 0 <= hi:
        outcome = "FAILED_STRESS"
        reason = (f"bootstrap 重抽样 95% 区间 [{lo:+.2f}%, {hi:+.2f}%] 跨 0——"
                  "同样样本量下结果可能为负：这个 edge 在统计上与噪声不可区分")
    elif trim_stats["sum_pct"] <= 0:
        outcome = "FAILED_STRESS"
        reason = (f"截断首尾各 {trim_pct:.0%} 极端单后净收益 {trim_stats['sum_pct']:+.2f}% ≤ 0——"
                  "edge 由少数几笔运气支撑")
    else:
        outcome = "SURVIVED_STRESS"
        reason = ("成本 ×2、bootstrap 重抽样、尾部截断三项序列级检验均存活——"
                  "但这不等于证伪通过：路径回放/随机对照/参数网格/样本外需要 K 线，本模式做不了")

    return {
        "mode": "trade_records",
        "n_trades": n,
        "base": base,
        "cost_sensitivity": stress,
        "sequence_bootstrap": {"n_draws": n_bootstrap, "seed": seed,
                               "ci95": [lo, hi]},
        "tail_trim": {"trim_pct": trim_pct, "kept": trim_stats["n"],
                      "stats": trim_stats},
        "outcome": outcome,
        "reason": reason,
        "limits": ("无 K 线：不能做路径感知回放（bar 内 TP/SL 先后不可知）、随机入场对照组、"
                   "参数网格、walk-forward。本模式的结论是序列级压力检验，不是完整证伪——"
                   "完整证伪请用 --spec + --candles。"),
    }


def render(result: dict) -> str:
    a = result["base"]
    lines = [
        "# 交易记录尸检报告（序列级）",
        "",
        f"**结论：{result['outcome']}** —— {result['reason']}",
        "",
        "> **本模式的边界（先读）**：" + result["limits"],
        "",
        "## 账面统计",
        "",
        f"- 成交 {a['n']} 笔 · 净收益 {a['sum_pct']:+.2f}% · 每笔期望 "
        + (f"{a['mean_pct']:+.3f}%" if a["mean_pct"] is not None else "n/a"),
        f"- 胜率 " + (f"{a['win_rate']:.1%}" if a["win_rate"] is not None else "n/a")
        + " · 盈亏比 " + (f"{a['profit_factor']:.2f}" if a["profit_factor"] is not None else "n/a")
        + f" · 最大回撤 {a['max_drawdown']:.2f}%",
        "",
        "## 三项序列级检验",
        "",
        "| 检验 | 结果 | 判定 |",
        "|---|---|---|",
    ]
    x2 = next((r for r in result["cost_sensitivity"] if r["fee_multiple"] == 2.0), None)
    ci = result["sequence_bootstrap"]["ci95"]
    tt = result["tail_trim"]
    x2_pass = "通过" if (x2 and x2["net_sum_pct"] > 0) else "未通过"
    boot_pass = "通过" if not (ci[0] <= 0 <= ci[1]) else "未通过"
    trim_pass = "通过" if tt["stats"]["sum_pct"] > 0 else "未通过"
    lines += [
        f"| 成本 ×2 压力 | 净收益 {x2['net_sum_pct']:+.2f}% | {x2_pass} |" if x2
        else "| 成本 ×2 压力 | n/a | 未通过 |",
        f"| bootstrap 重抽样（{result['sequence_bootstrap']['n_draws']} 次） | 95% 区间 "
        f"[{ci[0]:+.2f}%, {ci[1]:+.2f}%] | {boot_pass} |",
        f"| 尾部截断（去首尾各 {tt['trim_pct']:.0%}） | 剩余 {tt['kept']} 笔净收益 "
        f"{tt['stats']['sum_pct']:+.2f}% | {trim_pass} |",
        "",
        "---",
        "*本报告由序列级检验生成；完整证伪（六件仪器 + 诊断层）需要 K 线，"
        "用 `--spec spec.json --candles klines.csv` 运行。*",
        "",
    ]
    return "\n".join(lines)
