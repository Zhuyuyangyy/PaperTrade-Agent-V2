"""TSMOM 2×2 消融实验：把「方向信号」与「波动率配仓」的贡献分开。

这是用户审阅后重设的主实验。四个格子共用同一批数据、同一套成本、同一个
目标波动率，只切换两个因子：

                固定敞口              波动率调整敞口
  买入持有   A  B&H, sizing=1      B  B&H, sizing=target/vol
  TSMOM      C  mom,  sizing=1      D  mom,  sizing=target/vol

三个待估的量：
  C − A  动量方向信号的贡献（在固定敞口下择时 vs 不择时）
  B − A  波动率调整本身的贡献（必须扣除，否则会把 sizing 的收益算到动量头上）
  D − B  ★ 风险调整后动量择时的额外贡献 —— 这才是原假设真正要检验的东西

关键口径：
  · 所有格子用 daily_replay 的 open-to-open 收益，信号 t 收盘 → 仓位 t+1 开盘；
  · 事前波动率只用 <= t 的收益，无前视；
  · 换手按 |Δw| × 4bps 收费，资金费按 8h 边界、多头付空头收；
  · 不做强平（与引擎一致），因此结果仍带这一限制，写进报告；
  · 不搜索参数：lookback 固定 30/60 两档，vol_window 固定 20，target 20%。
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import replay as rp
import signals as sg

START, END = "2020-09-01", "2026-11-26"
TARGET_VOL = 0.20
VOL_WINDOW = 20
MAX_LEV = 3.0
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "LTCUSDT", "LINKUSDT", "BCHUSDT", "ADAUSDT"]
LOOKBACKS = (30, 60)


def bootstrap_ci(series: list[float], n_boot: int = 2000, seed: int = 7) -> tuple[float, float]:
    """配对差的 bootstrap CI。用固定 seed，保证每次跑出同样的数。

    向量化：纯 Python 版要 2000×n 次循环，20 个配置会跑几分钟。
    numpy 版一次索引完成 2000 行抽样，结果等价。
    """
    import numpy as np
    n = len(series)
    if n == 0:
        return (float("nan"), float("nan"))
    a = np.asarray(series, dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    means = a[idx].mean(axis=1)
    return (float(np.quantile(means, 0.025) * 252 * 100),
            float(np.quantile(means, 0.975) * 252 * 100))


def one_cell(candles: list[dict], positions: list[int], *, sizing: str,
             funding: list[dict]) -> dict:
    return rp.daily_replay(candles, positions, sizing=sizing,
                           target_vol=TARGET_VOL, vol_window=VOL_WINDOW,
                           max_lev=MAX_LEV, funding=funding)


def run_one(symbol: str, lookback: int) -> dict:
    candles = sg.read_candles(HERE / "data" / f"{symbol}.csv")
    candles = [c for c in candles
               if sg._fmt(c["t"]) >= START and sg._fmt(c["t"]) <= END]
    closes = [c["c"] for c in candles]
    mom = sg.positions_tsmom(closes, lookback)
    hold = [1] * len(closes)
    funding = rp.read_funding(symbol)

    a = one_cell(candles, hold, sizing="fixed", funding=funding)
    b = one_cell(candles, hold, sizing="vol", funding=funding)
    c = one_cell(candles, mom, sizing="fixed", funding=funding)
    d = one_cell(candles, mom, sizing="vol", funding=funding)

    out = {"symbol": symbol, "lookback": lookback,
           "cells": {k: {kk: vv for kk, vv in v.items() if kk != "returns"}
                     for k, v in (("A", a), ("B", b), ("C", c), ("D", d))}}
    for name, x, y in (("C_minus_A", c, a), ("B_minus_A", b, a), ("D_minus_B", d, b)):
        diff = rp.paired_diff(x, y)
        ci = bootstrap_ci(diff["series"])
        out[name] = {"n": diff["n"],
                     "ann_pct": round(diff["ann_pct"], 2),
                     "t_stat": round(diff["t_stat"], 3) if diff["t_stat"] else None,
                     "ci95_ann_pct": [round(ci[0], 2), round(ci[1], 2)]}
    return out


def main() -> None:
    rows = []
    for sym in SYMBOLS:
        for lb in LOOKBACKS:
            r = run_one(sym, lb)
            rows.append(r)
            print(f"{sym:<9} lb={lb:>2}  C−A={r['C_minus_A']['ann_pct']:>8.2f}%  "
                  f"B−A={r['B_minus_A']['ann_pct']:>8.2f}%  "
                  f"D−B={r['D_minus_B']['ann_pct']:>8.2f}%  "
                  f"t={r['D_minus_B']['t_stat']}", flush=True)

    out = HERE / "_out" / "ablation_2x2.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print("\n=== 全样本汇总（跨币中位）===")
    for key in ("C_minus_A", "B_minus_A", "D_minus_B"):
        vals = [r[key]["ann_pct"] for r in rows]
        ci_excl0 = sum(1 for r in rows
                       if r[key]["ci95_ann_pct"][0] > 0 or r[key]["ci95_ann_pct"][1] < 0)
        print(f"  {key:<12} 中位 {statistics.median(vals):>8.2f}%  "
              f">0 {sum(1 for v in vals if v>0)}/{len(vals)}  "
              f"CI不含0 {ci_excl0}/{len(vals)}")


if __name__ == "__main__":
    main()
