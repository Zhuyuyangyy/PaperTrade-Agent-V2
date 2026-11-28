"""时间对齐对照：把 S20 的通道长度按**小时**固定，再看跨周期是否仍退化。

**要排除的混淆**：`channel=55` 是 55 根 bar。4h 上它是 220 小时（9.2 天），
1h 上只有 55 小时（2.3 天），15m 上只有 13.75 小时。所以「4h 更好」可能
只是因为它的结构窗口更长，而不是因为它周期更长。

做法：固定通道覆盖的**小时数**为 220h（4h×55 的原始覆盖），
  4h → 55 根，1h → 220 根，15m → 880 根。
如果时间对齐后毛期望仍随周期缩短而下降，说明退化是真的；
如果拉平了，说明先前那个「短周期更差」有一部分只是窗口变短。
"""
import csv, statistics, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine as E
import signals_cards as C

DATA = HERE / "data"
BARS = {"4h": 4.0, "1h": 1.0, "15m": 0.25}
TARGET_HOURS = 220.0
SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def load(sym, tf):
    p = DATA / f"{sym}_{tf}.csv"
    if not p.is_file():
        return []
    out = []
    with p.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                        "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"])})
    return out


print("=" * 96)
print(f"时间对齐后的 S20（通道覆盖固定 {TARGET_HOURS:.0f} 小时）")
print("=" * 96)
print(f"{'周期':<6}{'channel':>8}{'币':<10}{'笔数':>7}{'毛期望':>9}{'净期望':>9}"
      f"{'成本':>8}{'持有h':>8}{'笔/年':>8}")
print("-" * 96)

agg = {}
for tf in ("4h", "1h", "15m"):
    ch = int(round(TARGET_HOURS / BARS[tf]))
    for sym in SYMS:
        cs = load(sym, tf)
        if not cs:
            continue
        cache = C.Cache(cs)
        r0 = E.run(cs, card="S20", symbol=sym, timeframe=tf, params={"ch": ch},
                   signal_fn=C.s20_donchian(cache, channel=ch), bar_hours=BARS[tf],
                   max_hold_bars=int(200 * 4 / BARS[tf]), fee_rate=0.0, slip_atr=0.0)
        r1 = E.run(cs, card="S20", symbol=sym, timeframe=tf, params={"ch": ch},
                   signal_fn=C.s20_donchian(cache, channel=ch), bar_hours=BARS[tf],
                   max_hold_bars=int(200 * 4 / BARS[tf]))
        if r1.n_trades == 0:
            continue
        span_y = max((r1.trades[-1].exit_t - r1.trades[0].entry_t) / 3.15e10, 1e-9)
        print(f"{tf:<6}{ch:>8}{sym:<10}{r1.n_trades:>7}{r0.net_expectancy:>+9.3f}"
              f"{r1.net_expectancy:>+9.3f}{r0.net_expectancy - r1.net_expectancy:>8.3f}"
              f"{r1.avg_bars * BARS[tf]:>8.1f}{r1.n_trades / span_y:>8.0f}")
        agg.setdefault(tf, []).append((r0.net_expectancy, r1.net_expectancy))

print()
print(f"{'周期':<6}{'channel':>8}{'毛期望(中位)':>14}{'净期望(中位)':>14}{'成本':>8}")
print("-" * 96)
for tf, v in agg.items():
    g = statistics.median(x[0] for x in v)
    n = statistics.median(x[1] for x in v)
    print(f"{tf:<6}{int(round(TARGET_HOURS / BARS[tf])):>8}{g:>+14.3f}{n:>+14.3f}{g - n:>8.3f}")
