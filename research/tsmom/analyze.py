"""TSMOM 结果分析：从扫描结果里读出结论，不制造结论。

三条判据，全部事前声明，写成代码是为了让任何人都能重算：

J1 方向性（有没有 edge，与自由度无关）
   看 control.diff_pct 的 95% bootstrap CI。跨 0 = 该策略的入场时点与随机
   入场无显著差异，无论赚了多少都不算发现。这是引擎自己算的，我只读。

J2 beta 剥离（多头的钱是不是只是 BTC 涨了）
   BTC 九年涨 14 倍，任何多头策略都会「赚钱」。真正的问题是：扣除标的自身
   涨幅后还有没有剩余。定义
        beta_null_pct = 窗口内标的涨幅 × 在场比例
   即「同等多头暴露、但完全不择时」能拿到的毛收益。它与复利收益的差才是
   择时贡献。这个基准引擎没有，所以我算——公式一行，可手验。

J3 成本承受力
   双倍成本（16bps 往返）后是否仍为正。edge 付不起 toll 就不是 edge。

输出到 stdout 的同时写一份 md，所有数字都带出处（引擎字段名），
方便回查。
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "_out"


def load() -> list[dict]:
    p = OUT / "scan_results.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    return [r for r in rows if "error" not in r]


def beta_null(symbol: str, start: str, end: str, exposure_pct: float) -> dict:
    """同等多头暴露但不择时的毛收益。

    标的涨幅从日线 CSV 直接算（首收盘→末收盘），在场比例取引擎给的
    buy_and_hold.exposure。两者相乘就是「闭眼持有这么久」的毛收益。
    不减成本：策略那边也还没扣（gross 口径），同口径才可比。
    """
    import csv
    path = HERE / "data" / f"{symbol}.csv"
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh)]
    import datetime as dt

    def ts(r):
        return int(r["t"])

    lo, hi = dt.datetime.fromisoformat(start).timestamp() * 1000, dt.datetime.fromisoformat(end).timestamp() * 1000
    win = [r for r in rows if lo <= ts(r) <= hi]
    if len(win) < 2:
        return {"ok": False}
    first, last = float(win[0]["c"]), float(win[-1]["c"])
    asset_pct = (last / first - 1) * 100
    return {
        "ok": True,
        "asset_pct": asset_pct,
        "exposure_pct": exposure_pct,
        "null_pct": asset_pct * exposure_pct / 100,
        "first": win[0]["t"],
        "last": win[-1]["t"],
        "bars": len(win),
    }


def verdict(r: dict) -> str:
    """把三条判据压成一个标签。顺序有意：方向性优先于收益，
    因为没有方向性的高收益只是 beta 或运气。"""
    ci = r.get("control_ci")
    if not ci:
        return "无对照"
    if ci[0] <= 0 <= ci[1]:
        return "无选择性"           # J1 未过，后面不用看
    if r["net_sum_pct"] <= 0:
        return "负期望"
    cost2 = (r.get("cost") or {}).get("2")
    if cost2 is not None and cost2 <= 0:
        return "成本不过关"
    return "有选择性且正期望"


def main() -> None:
    rows = load()
    print(f"有效结果 {len(rows)} 条\n")

    # ---- 按变体汇总 -----------------------------------------------------
    by_variant: dict[str, list[dict]] = {}
    for r in rows:
        by_variant.setdefault(r["variant"], []).append(r)

    print("=" * 78)
    print("按策略变体汇总（跨币种、跨回看期）")
    print("=" * 78)
    print(f"{'变体':<14}{'n':>4}{'选择性过':>9}{'正期望':>8}{'复利>1':>8}{'中位复利':>11}{'中位算术%':>11}")
    for v, rs in sorted(by_variant.items()):
        sel = sum(1 for r in rs if r.get("control_ci") and not (r["control_ci"][0] <= 0 <= r["control_ci"][1]))
        pos = sum(1 for r in rs if r["net_sum_pct"] > 0)
        cmp1 = sum(1 for r in rs if (r.get("compound_x") or 0) > 1)
        med_cmp = statistics.median([r.get("compound_x") or 0 for r in rs])
        med_sum = statistics.median([r["net_sum_pct"] for r in rs])
        print(f"{v:<14}{len(rs):>4}{sel:>9}{pos:>8}{cmp1:>8}{med_cmp:>11.3f}{med_sum:>11.2f}")

    # ---- 方向性：这是核心 ----------------------------------------------
    print()
    print("=" * 78)
    print("J1 方向性检验：随机对照组 CI 是否跨 0")
    print("=" * 78)
    passed = [r for r in rows if r.get("control_ci") and not (r["control_ci"][0] <= 0 <= r["control_ci"][1])]
    print(f"CI 不跨 0（有选择性）: {len(passed)} / {len(rows)} = {len(passed)/len(rows)*100:.1f}%")
    if passed:
        print(f"\n{'策略':<40}{'fills':>6}{'算术%':>10}{'复利x':>9}{'diff pp':>9}{'CI 下界':>9}")
        for r in sorted(passed, key=lambda x: -(x.get("compound_x") or 0))[:15]:
            print(f"{r['name'][:39]:<40}{r['fills']:>6}{r['net_sum_pct']:>10.2f}"
                  f"{r.get('compound_x') or 0:>9.3f}{r['control_diff_pct']:>9.2f}{r['control_ci'][0]:>9.2f}")

    # ---- beta 剥离 ------------------------------------------------------
    print()
    print("=" * 78)
    print("J2 beta 剥离：多头暴露能解释多少")
    print("=" * 78)
    longs = [r for r in rows if r["variant"] in ("tsmom_long", "tsmom")]
    print(f"{'策略':<36}{'复利x':>8}{'beta零假设%':>12}{'超出pp':>10}")
    shown = 0
    for r in sorted(longs, key=lambda x: -(x.get("compound_x") or 0)):
        b = beta_null(r["symbol"], r["first"], r["last"], r["exposure_pct"])
        if not b.get("ok"):
            continue
        excess = r["net_sum_pct"] - b["null_pct"]
        print(f"{r['name'][:35]:<36}{r.get('compound_x') or 0:>8.3f}{b['null_pct']:>12.1f}{excess:>10.1f}")
        shown += 1
        if shown >= 12:
            break

    # ---- 裁决分布 -------------------------------------------------------
    print()
    print("=" * 78)
    print("综合裁决分布")
    print("=" * 78)
    tag: dict[str, int] = {}
    for r in rows:
        tag[verdict(r)] = tag.get(verdict(r), 0) + 1
    for k, v in sorted(tag.items(), key=lambda x: -x[1]):
        print(f"  {k:<20}{v:>5}  ({v/len(rows)*100:.1f}%)")


if __name__ == "__main__":
    main()