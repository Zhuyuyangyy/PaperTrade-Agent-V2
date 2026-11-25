"""S2b 信号生成（BTC 1h，F/N 两变体，可复算）。

口径全部来自 research/v4/S2b_draft.md，阈值事前写死：
  ATR14/ATR60 <= 0.75（压缩）
  突破前20根 high/low + 5bps 缓冲
  volume_z(20) >= 1.5
  防透支：突破前3根累计涨幅 <= 2.0×ATR14（不再卡突破当根实体）
  防拥挤（仅F）：同向 funding 90条滚动分位 >= p90 / <= p10 则拒绝
  持效：下一根收盘仍站稳 ref
  cooldown 12 根

--variant F|N|both（F=防拥挤开，N=关；N 用于消融验证 funding 逻辑）
"""
from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

DATA = Path("data/v3_market")
OUT = Path("research/v4/entries")


def _load(sym: str):
    b = json.loads((DATA / sym / "bars_1h.json").read_text(encoding="utf-8"))
    f = json.loads((DATA / sym / "funding.json").read_text(encoding="utf-8"))
    B = [{"t": int(r["t"]), "o": D(r["o"]), "h": D(r["h"]),
          "l": D(r["l"]), "c": D(r["c"]), "v": D(r["v"])} for r in b["rows"]]
    fd = [{"t": int(r["t"]), "rate": D(r["rate"])} for r in f["rows"]]
    return B, fd, b["meta"]["sha256"], f["meta"]["sha256"]


def generate(B: list[dict], fd: list[dict], *, funding_filter: bool) -> tuple[list[tuple[int, str]], dict]:
    rates = [r["rate"] for r in fd]
    ftimes = [r["t"] for r in fd]

    def atr(i: int, n: int) -> D | None:
        if i < n:
            return None
        return sum(max(B[j]["h"] - B[j]["l"],
                       abs(B[j]["h"] - B[j - 1]["c"]),
                       abs(B[j]["l"] - B[j - 1]["c"]))
                   for j in range(i - n + 1, i + 1)) / n

    def ref(i: int, n: int = 20) -> tuple[D, D]:
        w = B[i - n:i]
        return max(x["h"] for x in w), min(x["l"] for x in w)

    def volume_z(i: int, n: int = 20) -> D | None:
        if i < n:
            return None
        vs = [B[j]["v"] for j in range(i - n, i)]
        m = sum(vs) / n
        var = sum((v - m) ** 2 for v in vs) / n
        sd = D(float(var) ** 0.5)
        return (B[i]["v"] - m) / sd if sd > 0 else None

    def crowded(ts: int, side: str) -> bool:
        k = bisect.bisect_right(ftimes, ts) - 1
        if k < 90:
            return False
        w = sorted(rates[k - 90:k])
        return rates[k] >= w[89] if side == "LONG" else rates[k] <= w[9]

    cnt: Counter = Counter()
    rows: list[tuple[int, str]] = []
    busy = -1
    for i in range(70, len(B) - 2):
        a14, a60 = atr(i, 14), atr(i, 60)
        if a14 is None or a60 is None or a14 <= 0:
            continue
        cnt["bars"] += 1
        if a14 / a60 > D("0.75"):
            continue
        cnt["squeezed"] += 1
        rh, rl = ref(i)
        c = B[i]["c"]
        if c > rh * D("1.0005"):
            side = "LONG"
        elif c < rl * D("0.9995"):
            side = "SHORT"
        else:
            continue
        cnt["breakout"] += 1
        z = volume_z(i)
        if z is None or z < D("1.5"):
            continue
        cnt["volume"] += 1
        # 防透支：突破前3根累计涨幅（相对突破当根ATR14）
        if side == "LONG":
            run = (B[i - 1]["c"] - B[i - 4]["o"]) / a14
        else:
            run = (B[i - 4]["o"] - B[i - 1]["c"]) / a14
        if run > D("2.0"):
            continue
        cnt["not_runup"] += 1
        if funding_filter and crowded(B[i]["t"], side):
            cnt["crowded_reject"] += 1
            continue
        nxt = B[i + 1]
        if (side == "LONG" and nxt["c"] <= rh) or (side == "SHORT" and nxt["c"] >= rh):
            continue
        cnt["confirmed"] += 1
        if B[i]["t"] <= busy:
            cnt["cooldown_skip"] += 1
            continue
        rows.append((B[i]["t"], side))
        busy = B[i]["t"] + 12 * 3_600_000  # 1h 的 cooldown 12 根
    return rows, dict(cnt)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=("F", "N", "both"), default="both")
    args = ap.parse_args()

    B, fd, bars_sha, fund_sha = _load("BTCUSDT")
    print(f"数据 BTCUSDT 1h bars={len(B)} funding={len(fd)} "
          f"sha bars={bars_sha[:12]} funding={fund_sha[:12]}", flush=True)

    variants = ("F", "N") if args.variant == "both" else (args.variant,)
    for v in variants:
        rows, cnt = generate(B, fd, funding_filter=(v == "F"))
        p = OUT / f"s2b_btc_1h_{v}.csv"
        with p.open("w", encoding="utf-8") as f:
            f.write("t,side\n")
            for t, s in rows:
                f.write(f"{t},{s}\n")
        sides = Counter(s for _, s in rows)
        print(f"[{v}] funnel={cnt} | 信号={len(rows)} "
              f"LONG={sides['LONG']} SHORT={sides['SHORT']} | {p}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
