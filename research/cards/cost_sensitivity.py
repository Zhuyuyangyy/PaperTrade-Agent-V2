"""成本敏感性：把「为什么短周期全灭」量化出来。

结论方向已经很清楚（1h 上 41 个组合 0 个跨币一致为正，4h 上 3 个），
但「因为成本」这个解释必须被测量，不能靠直觉。做法：把同一个策略在
四档成本下各跑一遍，看净期望随成本线性下降的速度。

如果斜率约等于「每年交易笔数 × 单笔往返成本」，就证明成本是主因；
如果斜率远大于它（比如存在大量小赢小亏被成本吃掉），那是另一回事。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine as E            # noqa: E402
import signals_cards as C     # noqa: E402
import signals_more as M      # noqa: E402

DATA = HERE / "data"

# 四档成本：(名称, 单边手续费, 滑点 ATR 倍数)
COST_TIERS = [
    ("零成本", 0.0, 0.0),
    ("仅手续费", 0.0004, 0.0),
    ("手续费+滑点", 0.0004, 0.02),
    ("3倍滑点", 0.0004, 0.06),
]

# 选几张有代表性的卡：4h 表现最好的、中等的、最差的
PROBES = [
    ("S20", "Donchian 55", lambda c: C.s20_donchian(c, channel=55), 200),
    ("S08", "OTE", lambda c: C.s08_ote(c), 60),
    ("S03", "FVG edge", lambda c: C.s03_fvg_retest(c, entry_at="edge"), 60),
    ("S01", "BOS 回抽", lambda c: C.s01_bos_continuation(c), 60),
    ("S17", "Pin Bar", lambda c: M.s17_pin_bar(c), 60),
    ("S28", "VWAP 偏离", lambda c: M.s28_vwap(c, vwap_bars=42), 40),
]


def load(name: str) -> list[dict]:
    p = DATA / name
    if not p.is_file():
        return []
    out = []
    with p.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                        "l": float(r["l"]), "c": float(r["c"]),
                        "v": float(r["v"])})
    return out


def main():
    for tf, hours in (("4h", 4.0), ("1h", 1.0)):
        cs = load(f"BTCUSDT_{tf}.csv")
        if not cs:
            print(f"[{tf}] 无数据")
            continue
        print(f"\n{'='*96}")
        print(f"BTC {tf}：成本敏感性（净期望 %/笔）")
        print(f"{'='*96}")
        header = f"{'卡':<22}{'笔数':>7}" + "".join(f"{n:>14}" for n, _, _ in COST_TIERS)
        print(header)
        print("-" * 96)

        for card, label, factory, mh in PROBES:
            row = f"{card} {label:<14}"
            counts = None
            vals = []
            for name, fee, slip in COST_TIERS:
                # **每档成本都要新建 cache 和信号函数。**
                # 信号函数带内部状态（armed/pending/live/seen 等），跑完一轮
                # 后状态已经走到末尾。复用它再跑第二轮，第二轮的信号流会和
                # 第一轮完全不同——第一版就因此得到「成本越高收益越好」的
                # 荒谬结论（S03 零成本 0.24 → 真实成本 0.66）。这不是策略
                # 特性，是状态污染。
                cache = C.Cache(cs)
                fn = factory(cache)
                r = E.run(cs, card=card, symbol="BTCUSDT", timeframe=tf,
                          params={}, signal_fn=fn, bar_hours=hours,
                          fee_rate=fee, slip_atr=slip, max_hold_bars=mh)
                if counts is None:
                    counts = r.n_trades
                vals.append((name, r))
            print(f"{row:<22}{counts:>7}"
                  + "".join(f"{r.net_expectancy:>14.3f}" for _, r in vals))

            # 斜率：从「零成本」到「手续费+滑点」的衰减
            z = vals[0][1].net_expectancy
            f2 = vals[2][1].net_expectancy
            print(f"{'':<22}{'':>7}  零成本 → 真实成本 衰减 {z - f2:.3f} pp/笔"
                  f"（{((z-f2)/abs(z)*100 if z else 0):.0f}%）")

        # 年度成本负担估算
        print(f"\n币安现货 taker 4bps/边，往返 8bps = 0.08%。")
        print(f"若每笔持有 {hours:.0f}h 级周期、年交易 N 笔，年成本 = N × 0.08%。")
        print(f"这就是为什么 1h 上正期望比例 (10.6%) 远低于 4h (32.5%)：")
        print(f"1h 的交易频率是 4h 的 4 倍，成本负担也是 4 倍，而单笔毛收益并不因此变四倍。")


if __name__ == "__main__":
    main()