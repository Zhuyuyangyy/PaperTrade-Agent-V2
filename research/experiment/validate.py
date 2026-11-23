"""实验 E3：反过拟合验证——参数高原 + 置换检验。

方法论来自「策略工厂 4 道关」的前两关（与具体模型无关的标准做法）：

1. **参数高原**：真优势在一片参数范围里都成立（热力图是一块平地）；
   靠运气的策略是一根尖刺（只有一组好，旁边挪一格就亏）。
2. **置换检验**：打乱历史涨跌顺序生成假行情——起点终点不变、涨跌幅分布不变，
   但真实存在的规律被打碎。在假行情上**用完全相同的流程重新优化**，
   重复 N 次。若假行情也能优化出同样好的成绩，说明抓到的是优化本身的运气。

铁律：
  - 判定口径统一为「研究集（前 70%）净收益」，样本外只在最后看一次；
  - 置换必须重跑**整条流程**（重新生成信号 + 重新扫参数 + 重新选最优），
    不能只拿真实数据的最优参数去假行情上跑——那样测不出过拟合；
  - 随机种子固定，结果可复算。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal as D

from research.market.contracts import Candle

from .structure import StructVariant, run_variant

BPS = D("10000")

#: 参数网格：与 E2 的扫描范围一致（stop × trail × start）
GRID_STOP = ("1.5", "2.0", "2.5", "3.0")
GRID_TRAIL = ("0.5", "0.75", "1.0", "1.5")
GRID_START = ("0.5", "1.0")
HOLD_TF_BARS = 48


def grid_variants(tf: str = "4h") -> list[StructVariant]:
    out = []
    for stop in GRID_STOP:
        for trail in GRID_TRAIL:
            for start in GRID_START:
                out.append(StructVariant(tf=tf, exit_mode="trail",
                                         stop_atr=D(stop), trail_atr=D(trail),
                                         trail_start_atr=D(start),
                                         hold_tf_bars=HOLD_TF_BARS))
    return out


def shuffle_bars(bars: list[Candle], seed: int) -> list[Candle]:
    """打乱收益序列生成假行情。

    - 用 close 的对数收益做置换；
    - 平移到与原序列**总收益相同**，保证起点与终点价格一致；
    - 每根 K 线的 OHLC 按同一比例缩放，保持自身几何关系与 OHLC 自洽。
    """
    rng = random.Random(seed)
    closes = [float(c.close) for c in bars]
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    total = sum(rets)
    body = rets[1:]
    rng.shuffle(body)
    drift = (total - sum(body)) / len(body) if body else 0.0
    body = [r + drift for r in body]

    new_closes = [closes[0]]
    for r in body:
        new_closes.append(new_closes[-1] * math.exp(r))

    out: list[Candle] = []
    for i, c in enumerate(bars):
        factor = D(str(new_closes[i] / closes[i]))
        out.append(Candle(
            c.open_ms,
            (c.open * factor).quantize(D("0.00000001")),
            (c.high * factor).quantize(D("0.00000001")),
            (c.low * factor).quantize(D("0.00000001")),
            (c.close * factor).quantize(D("0.00000001")),
            c.volume, c.timeframe_ms,
        ))
    return out


def best_on_research(bars: list[Candle], variants: list[StructVariant]) -> dict:
    """在给定数据上跑完整流程：扫全部参数 → 取研究集最优。"""
    best = None
    for v in variants:
        r = run_variant(bars, v)
        net = D(r["research"].get("net", "0") or "0")
        if best is None or net > best["research_net"]:
            best = {"variant": v, "research_net": net,
                    "oos_net": D(r["oos"].get("net", "0") or "0"),
                    "n": r["all"].get("n", 0),
                    "win_pct": r["all"].get("win_pct", "0")}
    return best


def plateau(bars: list[Candle], tf: str = "4h") -> dict:
    """参数高原：stop × trail 的研究集净收益热力图（start 取 1.0）。"""
    cells = []
    for stop in GRID_STOP:
        for trail in GRID_TRAIL:
            v = StructVariant(tf=tf, exit_mode="trail", stop_atr=D(stop),
                              trail_atr=D(trail), trail_start_atr=D("1.0"),
                              hold_tf_bars=HOLD_TF_BARS)
            r = run_variant(bars, v)
            cells.append({"stop": stop, "trail": trail,
                          "research_net": r["research"].get("net", "0"),
                          "oos_net": r["oos"].get("net", "0"),
                          "n": r["all"].get("n", 0),
                          "win_pct": r["all"].get("win_pct", "0")})
    vals = [D(c["research_net"]) for c in cells]
    pos = sum(1 for v in vals if v > 0)
    total = len(vals)
    best = max(cells, key=lambda c: D(c["research_net"]))
    # 高原判据：最优格周围（同行/同列相邻）是否仍为正
    rows = [c for c in cells if c["stop"] == best["stop"]]
    cols = [c for c in cells if c["trail"] == best["trail"]]
    neighbours = [D(c["research_net"]) for c in rows + cols
                  if not (c["stop"] == best["stop"] and c["trail"] == best["trail"])]
    pos_neigh = sum(1 for v in neighbours if v > 0)
    return {
        "cells": cells, "positives": pos, "total": total,
        "positive_share_pct": str((D(pos) / total * 100).quantize(D("0.01"))),
        "best": best,
        "neighbours": len(neighbours), "positive_neighbours": pos_neigh,
        "verdict": ("PLATEAU" if pos >= total * 0.5 and pos_neigh >= len(neighbours) * 0.5
                    else "SPIKE" if pos <= 2 else "MIXED"),
    }


def permutation_test(bars: list[Candle], *, n_perm: int = 1000, seed: int = 20261123,
                     tf: str = "4h") -> dict:
    """完整置换检验：每个假行情都重新扫全部参数并取最优。"""
    variants = grid_variants(tf)
    real = best_on_research(bars, variants)
    real_net = real["research_net"]

    perms: list[D] = []
    for k in range(n_perm):
        fake = shuffle_bars(bars, seed + k)
        b = best_on_research(fake, variants)
        perms.append(b["research_net"])
    better = sum(1 for v in perms if v >= real_net)
    p = D(better) / D(n_perm) if n_perm else D("1")
    perms_sorted = sorted(perms)
    return {
        "n_perm": n_perm, "seed": seed, "tf": tf,
        "grid_size": len(variants),
        "real_best": {"variant": real["variant"].key,
                      "stop": str(real["variant"].stop_atr),
                      "trail": str(real["variant"].trail_atr),
                      "start": str(real["variant"].trail_start_atr),
                      "research_net": str(real_net),
                      "oos_net": str(real["oos_net"]),
                      "n": real["n"], "win_pct": real["win_pct"]},
        "perm_best_net": {
            "median": str(perms_sorted[len(perms_sorted) // 2]),
            "p90": str(perms_sorted[int(len(perms_sorted) * 0.9)]),
            "p95": str(perms_sorted[int(len(perms_sorted) * 0.95)]),
            "max": str(perms_sorted[-1]), "min": str(perms_sorted[0]),
        },
        "n_perm_better_or_equal": better,
        "p_value": str(p.quantize(D("0.0001"))),
        "verdict": ("PASS" if p <= D("0.05") else "FAIL_OVERFIT"),
        "note": "p = 假行情中能优化出 >= 真实成绩的比例；p<=5% 才算过。",
    }


__all__ = ["grid_variants", "shuffle_bars", "best_on_research", "plateau",
           "permutation_test", "GRID_STOP", "GRID_TRAIL", "GRID_START"]


def robustness(trades: list[dict], *, ts_key: str = "signal_bar",
               pnl_key: str = "net_pnl", bars_per_day: int = 24) -> dict:
    """收益稳健性：剔除最大盈利笔 + 按季度拆解。

    置换检验回答「这套流程能否击败随机」；本函数回答
    「真实收益是不是靠极少数几笔撑起来的」——两者缺一不可。
    一个可用策略不应该因为剔掉一笔就归零。
    """
    pnls = sorted((D(str(t.get(pnl_key) or "0")) for t in trades), reverse=True)
    n = len(pnls)
    total = sum(pnls, D("0"))
    if n == 0:
        return {"n": 0, "total": "0", "verdict": "NO_TRADES"}

    def drop(k: int) -> str:
        return str(sum(pnls[k:], D("0")).quantize(D("0.0001")))

    win = sum(1 for x in pnls if x > 0)
    avg_w = sum((x for x in pnls if x > 0), D("0")) / max(1, win)
    avg_l = sum((x for x in pnls if x <= 0), D("0")) / max(1, n - win)

    # 季度拆解：用 signal_bar × bars_per_day 估算日期不精确，故由调用方给 ts_ms 更好；
    # 这里按「时间顺序四分位」拆，避免依赖日历
    quarters: list[dict] = []
    if n >= 4:
        ordered = sorted(trades, key=lambda t: int(t.get(ts_key) or 0))
        size = max(1, n // 4)
        for q in range(4):
            rows = ordered[q * size:(q + 1) * size] if q < 3 else ordered[q * size:]
            net = sum((D(str(r.get(pnl_key) or "0")) for r in rows), D("0"))
            quarters.append({"bucket": f"Q{q+1}", "n": len(rows),
                             "net": str(net.quantize(D("0.0001")))})

    pos_q = sum(1 for q in quarters if D(q["net"]) > 0)
    drop1 = D(drop(1))
    verdict = ("FRAGILE_SINGLE_TRADE" if total > 0 and drop1 <= 0
               else "FRAGILE_FEW_TRADES" if total > 0 and D(drop(2)) <= 0
               else "OK" if total > 0 else "NEGATIVE")
    return {
        "n": n, "total": str(total.quantize(D("0.0001"))),
        "max_trade": str(pnls[0].quantize(D("0.0001"))),
        "second_trade": str(pnls[1].quantize(D("0.0001"))) if n > 1 else "0",
        "after_drop_top1": drop(1), "after_drop_top2": drop(2),
        "after_drop_top3": drop(3),
        "win_rate_pct": str((D(win) / n * 100).quantize(D("0.01"))),
        "avg_win": str(avg_w.quantize(D("0.0001"))),
        "avg_loss": str(avg_l.quantize(D("0.0001"))),
        "payoff_ratio": str((abs(avg_w / avg_l)).quantize(D("0.01"))) if avg_l else "-",
        "quarters": quarters, "positive_quarters": pos_q,
        "verdict": verdict,
        "note": "剔除前 1 大盈利即转负 = 收益依赖单笔，不得视为策略优势。",
    }


__all__ = ["grid_variants", "shuffle_bars", "best_on_research", "plateau",
           "permutation_test", "robustness", "GRID_STOP", "GRID_TRAIL", "GRID_START"]
