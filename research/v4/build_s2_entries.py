"""S2 信号生成（可复算）。

口径全部来自 research/v4/specs/s2_squeeze_breakout.json::signal_definition，
在锁契约时一起写死。本脚本只实现口径，不做任何参数搜索。

--mode full : 正式五道过滤（契约口径）→ 2 笔
--mode no_body : 诊断探针，去掉第 3 道防追高 → 16 笔（非契约，不计入验收）
"""
from __future__ import annotations

import argparse
import bisect
from decimal import Decimal as D
from pathlib import Path

from research.experiment.data import load_klines
from research.experiment.funding import load_funding

ROOT = Path("data/v3_funding")
OUT = Path("research/v4/entries")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("full", "no_body"), default="full")
    args = ap.parse_args()

    bs = load_klines(interval="1h", step_ms=3_600_000, days=730,
                     cache=ROOT / "bars_1h.json")
    fd = load_funding(days=730, cache=ROOT / "funding.json")
    bars = bs.bars

    B = []
    for i in range(0, len(bars) - 3, 4):
        c = bars[i:i + 4]
        B.append({"t": int(c[0].open_ms), "o": D(c[0].open),
                  "h": max(x.high for x in c), "l": min(x.low for x in c),
                  "c": D(c[-1].close), "v": sum(x.volume for x in c)})

    def atr(i: int, n: int) -> D | None:
        if i < n:
            return None
        trs = []
        for j in range(i - n + 1, i + 1):
            trs.append(max(B[j]["h"] - B[j]["l"],
                           abs(B[j]["h"] - B[j - 1]["c"]),
                           abs(B[j]["l"] - B[j - 1]["c"])))
        return sum(trs) / n

    def volume_z(i: int, n: int = 20) -> D | None:
        if i < n:
            return None
        vs = [B[j]["v"] for j in range(i - n, i)]
        m = sum(vs) / n
        var = sum((v - m) ** 2 for v in vs) / n
        sd = D(float(var) ** 0.5)
        return (B[i]["v"] - m) / sd if sd > 0 else None

    def ref(i: int, n: int = 20) -> tuple[D, D]:
        w = B[i - n:i]
        return max(x["h"] for x in w), min(x["l"] for x in w)

    rates = [D(x["rate"]) for x in fd]
    ftimes = [int(x["funding_ms"]) for x in fd]

    def crowded(ts: int, side: str) -> bool:
        """同向 funding 已在极端分位 → 拒绝（防接最后一棒）。"""
        k = bisect.bisect_right(ftimes, ts) - 1
        if k < 90:
            return False
        w = sorted(rates[k - 90:k])
        return rates[k] >= w[89] if side == "LONG" else rates[k] <= w[9]

    counts = {"squeezed": 0, "breakout": 0, "volume": 0,
              "body_ok": 0, "not_crowded": 0, "confirmed": 0}
    rows: list[tuple[int, str]] = []
    for i in range(65, len(B) - 2):
        a14, a60 = atr(i, 14), atr(i, 60)
        if a14 is None or a60 is None or a14 <= 0:
            continue
        if a14 / a60 > D("0.75"):
            continue
        counts["squeezed"] += 1
        rh, rl = ref(i)
        c, o = B[i]["c"], B[i]["o"]
        if c > rh * D("1.0005"):
            side = "LONG"
        elif c < rl * D("0.9995"):
            side = "SHORT"
        else:
            continue
        counts["breakout"] += 1
        z = volume_z(i)
        if z is None or z < D("1.5"):
            continue
        counts["volume"] += 1
        if args.mode == "full":
            if abs(c - o) > D("1.0") * a14:
                continue
            counts["body_ok"] += 1
        if crowded(B[i]["t"], side):
            continue
        counts["not_crowded"] += 1
        nxt = B[i + 1]
        if (side == "LONG" and nxt["c"] <= rh) or (side == "SHORT" and nxt["c"] >= rl):
            continue
        counts["confirmed"] += 1
        rows.append((B[i]["t"], side))

    name = "s2_btc_4h_entries.csv" if args.mode == "full" else "s2_btc_4h_entries_NOBODY.csv"
    p = OUT / name
    with p.open("w", encoding="utf-8") as f:
        f.write("t,side\n")
        for t, s in rows:
            f.write(f"{t},{s}\n")
    print("mode:", args.mode, "| funnel:", counts, "| signals:", len(rows), "| out:", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
