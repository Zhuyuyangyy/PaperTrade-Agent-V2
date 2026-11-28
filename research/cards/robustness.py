"""稳健性检验：Bootstrap 置信区间 + 分年稳定性 + Walk-forward。

**这是把「候选」变成「可继续研究」或「取消资格」的那道门。**

三个检验回答三个不同的问题：

1. **Bootstrap CI**：+0.53% 这个数字的 95% 区间跨不跨 0？
   跨 0 就意味着「944 笔也不够分辨它和随机」。

2. **分年表现**：是否只在某一年赚钱？
   只在 2021 牛市赚钱的趋势策略，2022 之后就没有价值了。

3. **Walk-forward**：用前半段选参数、后半段验证，还活着吗？
   这是唯一能对抗「在同一样本上挑最好参数」的检验。

不通过这三项的组合，不应该进入 Phase 4 的 LLM 实验——在噪声上叠加
LLM 只会拟合出更精美的噪声。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine as E            # noqa: E402
import signals_cards as C     # noqa: E402
import signals_more as M      # noqa: E402

DATA = HERE / "data"
OUT = HERE / "_out"

# 要检验的候选：卡、参数、标签、工厂
CANDS = [
    ("S20", {"channel": 55, "atr_mult": 2.0}, "Donchian 55",
     lambda c, p: C.s20_donchian(c, **p), 200),
    ("S08", {"fib_lo": 0.62, "fib_hi": 0.79}, "OTE",
     lambda c, p: C.s08_ote(c, **p), 60),
    ("S03", {"entry_at": "edge", "retest_max_bars": 40}, "FVG edge",
     lambda c, p: C.s03_fvg_retest(c, **p), 60),
]

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def load(sym: str, tf: str) -> list[dict]:
    p = DATA / f"{sym}_{tf}.csv"
    if not p.is_file():
        return []
    out = []
    with p.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"t": int(r["t"]), "o": float(r["o"]),
                        "h": float(r["h"]), "l": float(r["l"]),
                        "c": float(r["c"]), "v": float(r["v"])})
    return out


def bootstrap_ci(vals: list[float], n_boot: int = 5000, seed: int = 11) -> tuple:
    """对每笔净收益做有放回重采样，返回均值的 95% 区间。"""
    a = np.asarray(vals, dtype=float)
    if a.size == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, a.size, size=(n_boot, a.size))
    means = a[idx].mean(axis=1)
    return (float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975)))


def yearly(trades) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for t in trades:
        y = dt.datetime.utcfromtimestamp(t.exit_t / 1000).year
        out.setdefault(y, []).append(t.net_pct)
    return out


def run_cand(card, prm, label, factory, mh, cs, sym, tf, hours):
    cache = C.Cache(cs)
    fn = factory(cache, prm)
    return E.run(cs, card=card, symbol=sym, timeframe=tf, params=prm,
                 signal_fn=fn, bar_hours=hours, max_hold_bars=mh)


def main():
    tf, hours = "4h", 4.0
    print("=" * 100)
    print(f"稳健性检验 · {tf} · 3 个候选 × 3 币")
    print("=" * 100)

    for card, prm, label, factory, mh in CANDS:
        print(f"\n{'─'*100}")
        print(f"{card} {label}  {prm}")
        print(f"{'─'*100}")
        print(f"{'币':<9}{'笔数':>6}{'净期望':>9}{'95% CI':>22}{'跨0':>6}"
              f"{'胜率':>7}{'PF':>6}")

        all_nets: list[float] = []
        per_sym: dict[str, dict] = {}
        for sym in SYMBOLS:
            cs = load(sym, tf)
            if not cs:
                continue
            r = run_cand(card, prm, label, factory, mh, cs, sym, tf, hours)
            if r.n_trades == 0:
                continue
            nets = [t.net_pct for t in r.trades]
            all_nets.extend(nets)
            lo, hi = bootstrap_ci(nets)
            crosses = "是" if lo <= 0 <= hi else "**否**"
            per_sym[sym] = {"n": r.n_trades, "mean": r.net_expectancy,
                            "ci": (lo, hi), "crosses0": lo <= 0 <= hi,
                            "pf": r.profit_factor, "trades": r.trades}
            print(f"{sym:<9}{r.n_trades:>6}{r.net_expectancy:>9.3f}"
                  f"{f'[{lo:+.3f}, {hi:+.3f}]':>22}{crosses:>6}"
                  f"{r.win_rate:>7.1f}{r.profit_factor:>6.2f}")

        # 合并三币
        lo, hi = bootstrap_ci(all_nets)
        crosses = lo <= 0 <= hi
        print(f"{'合并':<9}{len(all_nets):>6}{statistics.mean(all_nets):>9.3f}"
              f"{f'[{lo:+.3f}, {hi:+.3f}]':>22}"
              f"{'是' if crosses else '**否**':>6}")
        if crosses:
            print("  → **置信区间跨 0，候选资格取消**")
        else:
            print("  → CI 不含 0，进入分年稳定性检验")

        # ---- 分年 ----
        print(f"\n  分年净期望（%/笔，括号内为笔数）：")
        years: dict[int, list[float]] = {}
        for sym, d in per_sym.items():
            for y, v in yearly(d["trades"]).items():
                years.setdefault(y, []).extend(v)
        pos_years = 0
        total_years = 0
        row = []
        for y in sorted(years):
            v = years[y]
            if len(v) < 20:
                continue
            total_years += 1
            m = statistics.mean(v)
            if m > 0:
                pos_years += 1
            row.append(f"{y}: {m:+.2f}%({len(v)})")
        print("    " + "  ".join(row))
        print(f"    正收益年份 {pos_years}/{total_years}"
              f"  {'→ 稳定' if pos_years >= total_years * 0.6 else '→ **不稳定，只在个别年份赚钱**'}")

        # ---- Walk-forward：前半段选参数，后半段验证 ----
        print(f"\n  Walk-forward（2020-09~2023-12 训练 / 2024-01~ 测试）：")
        split_t = int(dt.datetime(2024, 1, 1,
                                  tzinfo=dt.timezone.utc).timestamp() * 1000)
        is_nets, oos_nets = [], []
        for sym in SYMBOLS:
            cs = load(sym, tf)
            if not cs:
                continue
            r = run_cand(card, prm, label, factory, mh, cs, sym, tf, hours)
            for t in r.trades:
                (oos_nets if t.exit_t >= split_t else is_nets).append(t.net_pct)
        if is_nets and oos_nets:
            is_m = statistics.mean(is_nets)
            oos_m = statistics.mean(oos_nets)
            lo2, hi2 = bootstrap_ci(oos_nets)
            ok = "**通过**" if oos_m > 0 and lo2 > 0 else \
                ("方向一致但 CI 跨 0" if oos_m > 0 else "**样本外转负，不通过**")
            print(f"    样本内 {is_m:+.3f}% ({len(is_nets)} 笔) → "
                  f"样本外 {oos_m:+.3f}% ({len(oos_nets)} 笔) "
                  f"CI[{lo2:+.3f}, {hi2:+.3f}]  {ok}")
        else:
            print("    样本不足，无法切分")

    print(f"\n{'='*100}")
    print("判据说明：")
    print("  · Bootstrap CI 基于每笔净收益重采样 5000 次（有放回），不假设正态。")
    print("  · 分年稳定性要求至少 60% 的年份为正。趋势策略通常过不了这条。")
    print("  · Walk-forward 用固定切分点，不重新优化参数——"
          "因为参数量已在上一步固定，重优化就等于又做了一次多重检验。")


if __name__ == "__main__":
    main()