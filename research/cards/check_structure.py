"""结构层自检：数量是否合理、因果性是否成立。

因果性检验方法：**截断数据后，前半段结果必须逐字节相同**。
这是唯一能证明「没有未来函数」的机械方法——只要有任何函数读了 bar t
之后的数据，截断就会改变 t 之前的结果。
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import structure as S  # noqa: E402

DATA = HERE / "data"


def load(path: Path) -> list[dict]:
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                        "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"])})
    return out


def ms(t: int) -> str:
    import datetime as dt
    return dt.datetime.utcfromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M")


def summarize(path: Path, limit: int = 0) -> dict:
    cs = load(path)
    if limit:
        cs = cs[:limit]
    ps = S.pivots(cs, 3, 3)
    fv = S.find_fvgs(cs, min_gap_atr=0.5)
    ob = S.find_order_blocks(cs)
    sw = S.find_sweeps(cs, ps)
    rg = S.find_ranges(cs)
    ev = S.structure_events(cs, ps)

    kinds = Counter(e.kind for e in ev)
    dirs = Counter(e.direction for e in ev)
    return {
        "path": path.name, "bars": len(cs), "span": f"{ms(cs[0]['t'])} → {ms(cs[-1]['t'])}",
        "pivot": len(ps), "pivot_high": sum(1 for p in ps if p.kind == "high"),
        "fvg": len(fv), "fvg_filled": sum(1 for f in fv if f.filled_at),
        "ob": len(ob), "sweep": len(sw), "range": len(rg),
        "events": len(ev), "BOS": kinds.get("BOS", 0), "CHoCH": kinds.get("CHoCH", 0),
        "bull": dirs.get("bull", 0), "bear": dirs.get("bear", 0),
    }


def causality_ok(path: Path, cut: int) -> dict:
    """截断到 cut 根，比较 prefix 结果与全量结果在上半段是否一致。"""
    full = load(path)
    half = full[:cut]

    res = {}
    # pivot：只比较 confirmed_at <= cut-1 的
    pf = S.pivots(full, 3, 3)
    ph = S.pivots(half, 3, 3)
    res["pivot"] = [(p.i, p.kind, p.confirmed_at) for p in pf if p.confirmed_at <= cut - 1] \
        == [(p.i, p.kind, p.confirmed_at) for p in ph]

    # FVG：只比较 t 落在 half 内的（成立时间早于截断）
    ff = S.find_fvgs(full, min_gap_atr=0.5)
    fh = S.find_fvgs(half, min_gap_atr=0.5)
    res["fvg"] = [(f.i, f.direction, round(f.lo, 8), round(f.hi, 8))
                  for f in ff if f.t <= half[-1]["t"]] \
        == [(f.i, f.direction, round(f.lo, 8), round(f.hi, 8)) for f in fh]

    # 结构事件：只比较 t <= cut-1 的
    ef = S.structure_events(full, pf)
    eh = S.structure_events(half, ph)
    res["events"] = [(e.t, e.kind, e.direction, round(e.level, 8))
                     for e in ef if e.t <= cut - 1] \
        == [(e.t, e.kind, e.direction, round(e.level, 8)) for e in eh]

    # OB：只比较 confirmed_at <= cut-1 的
    of = S.find_order_blocks(full)
    oh = S.find_order_blocks(half)
    res["ob"] = [(o.i, o.direction, round(o.lo, 8), round(o.hi, 8))
                 for o in of if o.confirmed_at <= cut - 1] \
        == [(o.i, o.direction, round(o.lo, 8), round(o.hi, 8)) for o in oh]

    # range：只比较 end_i <= cut-1 的
    rf = S.find_ranges(full)
    rh = S.find_ranges(half)
    res["range"] = [(r.start_i, r.end_i, round(r.hi, 8), round(r.lo, 8))
                    for r in rf if r.end_i <= cut - 1] \
        == [(r.start_i, r.end_i, round(r.hi, 8), round(r.lo, 8)) for r in rh]

    # sweep：只比较 t (字符串时间) 落在 half 内的
    sf = S.find_sweeps(full, pf)
    sh = S.find_sweeps(half, ph)
    res["sweep"] = [(s.t, s.direction, round(s.level, 8)) for s in sf if s.t <= half[-1]["t"]] \
        == [(s.t, s.direction, round(s.level, 8)) for s in sh]
    return res


def main():
    files = sorted(DATA.glob("*.csv"))
    if not files:
        print("没有数据")
        return 1
    print(f"{'file':22} {'bars':>7} {'pivot':>6} {'FVG':>5} {'fill':>5} "
          f"{'OB':>6} {'sweep':>6} {'range':>5} {'BOS':>5} {'CHoCH':>5}")
    print("-" * 88)
    for p in files:
        r = summarize(p)
        print(f"{r['path']:22} {r['bars']:>7} {r['pivot']:>6} {r['fvg']:>5} "
              f"{r['fvg_filled']:>5} {r['ob']:>6} {r['sweep']:>6} {r['range']:>5} "
              f"{r['BOS']:>5} {r['CHoCH']:>5}")
    print()
    print("区间：" + summarize(files[0])["span"])

    print("\n因果性（截断后半段，前半段结果必须不变）：")
    bad = 0
    for p in files:
        cs = load(p)
        cut = len(cs) * 2 // 3
        r = causality_ok(p, cut)
        flags = " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in r.items())
        if not all(r.values()):
            bad += 1
        print(f"  {p.name:22} cut={cut:>6}  {flags}")
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 个文件有 FAIL'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())