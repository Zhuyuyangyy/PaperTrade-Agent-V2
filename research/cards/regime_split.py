"""按市场状态分层：策略到底在哪种行情里赚钱。

**这是分年数据的直接后续。** 分年结果显示 S08 和 S03 的盈利集中在 2022
（熊市），2021（牛市）反而亏。如果这个观察成立，它们就不是"通用形态
策略"，而是"下跌市做空工具"——价值完全不同，用法也完全不同。

分层判据（用 4h K 线，全部因果）：
  趋势方向：过去 `trend_lookback` 根的收益率符号
  波动水平：过去 `vol_lookback` 根的 ATR/价格 分位（用全样本分位做切分线，
            但**只用历史数据计算分位**，分位线本身来自训练段）

四个状态：上涨低波 / 上涨高波 / 下跌低波 / 下跌高波。

**必须注意的陷阱**：状态标签如果用全样本的分位线，会引入轻微前视。
本脚本用「每个 bar 只跟它之前的 ATR 分布比较」的滚动分位，虽然慢一点，
但结论不会因为标签泄漏而虚高。
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine as E            # noqa: E402
import signals_cards as C     # noqa: E402
import signals_more as M      # noqa: E402

DATA = HERE / "data"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

CANDS = [
    ("S20", {"channel": 55, "atr_mult": 2.0}, "Donchian 55",
     lambda c, p: C.s20_donchian(c, **p), 200),
    ("S08", {"fib_lo": 0.62, "fib_hi": 0.79}, "OTE",
     lambda c, p: C.s08_ote(c, **p), 60),
    ("S03", {"entry_at": "edge", "retest_max_bars": 40}, "FVG edge",
     lambda c, p: C.s03_fvg_retest(c, **p), 60),
]

STATES = ("涨·低波", "涨·高波", "跌·低波", "跌·高波")


def load(sym: str, tf: str = "4h") -> list[dict]:
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


def build_regime(cs: list[dict], trend_lb: int = 60, vol_lb: int = 40,
                 vol_window: int = 200) -> list[str | None]:
    """为每根 bar 打上状态标签。严格因果：只用 i 及之前的数据。"""
    n = len(cs)
    out: list[str | None] = [None] * n
    atr_hist: list[float] = []

    for i in range(n):
        a = C._atr(cs, i, 14)
        if a:
            atr_hist.append(a / cs[i]["c"])
        if i < max(trend_lb, vol_lb) or len(atr_hist) < 50:
            continue

        # 趋势：过去 trend_lb 根的收益
        ret = cs[i]["c"] / cs[i - trend_lb]["c"] - 1
        up = ret > 0

        # 波动分位：当前 vol 在**历史**分布中的位置（不含未来）
        cur = atr_hist[-1]
        hist = atr_hist[-vol_window:] if len(atr_hist) > vol_window else atr_hist
        rank = sum(1 for x in hist if x <= cur) / len(hist)
        high_vol = rank > 0.5

        out[i] = ("涨" if up else "跌") + ("·高波" if high_vol else "·低波")
    return out


def main():
    print("=" * 104)
    print("市场状态分层 · 4h · 3 候选 × 3 币")
    print("=" * 104)
    print("状态定义（全部因果）：")
    print("  趋势 = 过去 60 根（4h × 60 = 10 天）收益率符号")
    print("  波动 = 当前 ATR/价格 在滚动 200 根历史分布中的分位 > 50% 记为高波")

    for card, prm, label, factory, mh in CANDS:
        print(f"\n{'─'*104}")
        print(f"{card} {label}")
        print(f"{'─'*104}")
        buckets: dict[str, list[float]] = {s: [] for s in STATES}
        total_long = total_short = 0
        long_nets: list[float] = []
        short_nets: list[float] = []

        for sym in SYMBOLS:
            cs = load(sym)
            if not cs:
                continue
            regimes = build_regime(cs)
            cache = C.Cache(cs)
            fn = factory(cache, prm)
            r = E.run(cs, card=card, symbol=sym, timeframe="4h",
                      params=prm, signal_fn=fn, bar_hours=4.0,
                      max_hold_bars=mh)
            for t in r.trades:
                # 用**入场时**的状态，不是出场时的——入场决策时才用得到它
                st = regimes[t.entry_i] if t.entry_i < len(regimes) else None
                if st in buckets:
                    buckets[st].append(t.net_pct)
                if t.side == "long":
                    total_long += 1
                    long_nets.append(t.net_pct)
                else:
                    total_short += 1
                    short_nets.append(t.net_pct)

        print(f"{'状态':<10}{'笔数':>7}{'净期望':>10}{'胜率':>8}{'结论':>16}")
        print("-" * 104)
        for s in STATES:
            v = buckets[s]
            if not v:
                print(f"{s:<10}{0:>7}{'—':>10}{'—':>8}{'无样本':>16}")
                continue
            m = statistics.mean(v)
            wr = sum(1 for x in v if x > 0) / len(v) * 100
            verdict = "有效" if m > 0.15 else ("微弱" if m > 0 else "无效")
            print(f"{s:<10}{len(v):>7}{m:>10.3f}{wr:>8.1f}{verdict:>16}")

        print(f"\n  多空分解：")
        if long_nets:
            print(f"    做多 {total_long:>4} 笔  净期望 {statistics.mean(long_nets):+.3f}%")
        if short_nets:
            print(f"    做空 {total_short:>4} 笔  净期望 {statistics.mean(short_nets):+.3f}%")
        if long_nets and short_nets:
            lm, sm = statistics.mean(long_nets), statistics.mean(short_nets)
            if sm > lm * 2 and sm > 0:
                print(f"    → **收益主要来自做空**，这与分年数据（2022 熊市最好）一致")
            elif lm > sm * 2 and lm > 0:
                print(f"    → 收益主要来自做多")

    print(f"\n{'='*104}")
    print("读法提示：")
    print("  · 如果某策略只在「跌」状态下为正，它是 regime-dependent 的，需要状态分类器。")
    print("  · 如果多空分解显示空头贡献远大于多头，那它在牛市里注定无效——这不是 bug。")
    print("  · 分层后每格样本更少，别对单个格子的数字太当真；看的是**方向模式**。")


if __name__ == "__main__":
    main()