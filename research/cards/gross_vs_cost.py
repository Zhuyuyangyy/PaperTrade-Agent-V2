"""拆解「短周期为什么全灭」：毛优势退化 vs 交易摩擦。

**为什么必须拆开**：S20 的净期望从 4h 的 +0.53% 掉到 1h 的 −0.19%，
差 0.72pp。但成本敏感性实测的单笔衰减只有 0.15pp——如果只是成本变高，
1h 该留在 +0.38% 附近。差出来的 0.57pp 必须有第二个原因。

两个候选解释，性质完全不同：
  A. **摩擦放大**：同样 2 ATR 止损，在 1h 上是更小的绝对波动，所以
     `0.02 × ATR × 2` 的滑点占单笔收益的比例变大。成本敏感性已经
     证明这一项确实存在且线性。
  B. **毛优势退化**：信号在短周期本来就弱（同样「突破 55 根高点」，
     4h 是 220 小时＝9 天的结构，15m 只有 13.75 小时）。这一项如果
     存在，加多少算力、多好的模型都救不回来——因为输入信息变少了。

判断方法：**看零成本下的毛期望**。成本被归零后如果毛期望仍随周期
缩短而单调下降，就是 B；如果毛期望基本持平，那就是纯 A。

这个脚本只输出观察，不做裁决。裁决写在 REPORT.md 里，且必须同时
引用本脚本与 cost_sensitivity.py 两个来源。
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

TFS = ("4h", "1h", "15m")
BARS_PER_YEAR = {"4h": 365 * 6, "1h": 365 * 24, "15m": 365 * 96}

# 与 cost_sensitivity.py 同一组探针，便于两表对照
PROBES = [
    ("S20", "Donchian 55", lambda c: C.s20_donchian(c, channel=55), 200),
    ("S08", "OTE", lambda c: C.s08_ote(c), 60),
    ("S03", "FVG edge", lambda c: C.s03_fvg_retest(c, entry_at="edge"), 60),
    ("S01", "BOS 回抽", lambda c: C.s01_bos_continuation(c), 60),
    ("S17", "Pin Bar", lambda c: M.s17_pin_bar(c), 60),
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


BAR_HOURS = {"4h": 4.0, "1h": 1.0, "15m": 0.25}


def main() -> int:
    print("=" * 108)
    print("拆解：毛优势退化 vs 交易摩擦")
    print("=" * 108)
    print()
    print(f"{'卡':<6}{'周期':<6}{'币':<10}{'笔数':>7}{'毛期望':>9}{'净期望':>9}"
          f"{'成本':>8}{'ATR%':>8}{'持有h':>8}{'笔/年':>8}")
    print("-" * 108)

    rows: dict[tuple[str, str], list[dict]] = {}

    for card, label, factory, mh in PROBES:
        for tf in TFS:
            for sym in SYMBOLS:
                cs = load(sym, tf)
                if not cs:
                    continue
                cache = C.Cache(cs)
                # 零成本：只看毛期望，把摩擦彻底拿掉
                fn0 = factory(cache)
                r0 = E.run(cs, card=card, symbol=sym, timeframe=tf, params={},
                           signal_fn=fn0, bar_hours=BAR_HOURS[tf],
                           max_hold_bars=mh, fee_rate=0.0, slip_atr=0.0)
                # 真实成本
                fn1 = factory(cache)
                r1 = E.run(cs, card=card, symbol=sym, timeframe=tf, params={},
                           signal_fn=fn1, bar_hours=BAR_HOURS[tf],
                           max_hold_bars=mh)
                if r0.n_trades == 0 or r1.n_trades == 0:
                    continue

                atr_pct = statistics.median(t.risk_pct for t in r1.trades) \
                    if r1.trades else 0.0
                hold_h = r1.avg_bars * BAR_HOURS[tf]
                py = r1.n_trades / max(
                    (r1.trades[-1].exit_t - r1.trades[0].entry_t) / 3.15e10, 1e-9)

                cost = r0.net_expectancy - r1.net_expectancy
                print(f"{card:<6}{tf:<6}{sym:<10}{r1.n_trades:>7}"
                      f"{r0.net_expectancy:>+9.3f}{r1.net_expectancy:>+9.3f}"
                      f"{cost:>8.3f}{atr_pct:>8.2f}{hold_h:>8.1f}{py:>8.0f}")

                rows.setdefault((card, tf), []).append({
                    "sym": sym, "n": r1.n_trades,
                    "gross": r0.net_expectancy,
                    "net": r1.net_expectancy,
                    "cost": cost, "atr_pct": atr_pct,
                    "hold_h": hold_h, "per_year": py,
                })

    print()
    print("=" * 108)
    print("按 (卡, 周期) 取三币中位数：成本占比 = 成本 / 毛期望")
    print("=" * 108)
    print()
    print(f"{'卡':<6}{'周期':<6}{'笔数':>7}{'毛期望':>9}{'净期望':>9}"
          f"{'成本':>8}{'成本/毛':>9}{'ATR%':>8}{'持有h':>8}")
    print("-" * 108)

    for (card, tf), rs in rows.items():
        g = statistics.median(x["gross"] for x in rs)
        n = statistics.median(x["net"] for x in rs)
        c = statistics.median(x["cost"] for x in rs)
        a = statistics.median(x["atr_pct"] for x in rs)
        h = statistics.median(x["hold_h"] for x in rs)
        nn = int(statistics.median(x["n"] for x in rs))
        ratio = (c / g * 100) if g > 0 else float("nan")
        print(f"{card:<6}{tf:<6}{nn:>7}{g:>+9.3f}{n:>+9.3f}{c:>8.3f}"
              f"{ratio:>8.0f}%{a:>8.2f}{h:>8.1f}")

    print()
    print("读法：")
    print("  · 毛期望随周期缩短**单调下降** → 信号本身在短周期变弱（解释 B）")
    print("  · 毛期望基本持平、成本上升      → 纯摩擦（解释 A）")
    print("  · 成本/毛 超过 100%             → 毛优势还不够付摩擦，"
          "再加过滤器也救不回来")
    print()
    print("注意 ATR% 那列：同样是 2 ATR 止损，1h 的绝对止损距离更小，")
    print("所以固定比例的滑点在单笔收益里占比更大——这一项是机械放大的，")
    print("与策略好坏无关。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())