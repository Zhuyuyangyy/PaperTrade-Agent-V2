import json, time
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import grid, run_funding_variant

root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
rows, t0 = [], time.time()
for p in grid():
    r = run_funding_variant(bs.bars, fd, p)
    rows.append({"key": p.key, "mode": p.mode, "z": str(p.z_threshold),
                 "stop": str(p.stop_atr), "trail": str(p.trail_atr),
                 "hold": p.hold_bars, "flip": p.flip,
                 "n": r["all"].get("n", 0),
                 "research": r["research"].get("net", "0"),
                 "oos": r["oos"].get("net", "0"),
                 "win": r["all"].get("win_pct", "0"),
                 "carry": r["all"].get("carry", "0")})
print(f"{len(rows)} 组耗时 {time.time()-t0:.1f}s")
for mode in ("level", "delta"):
    sub = [x for x in rows if x["mode"] == mode]
    pos = sum(1 for x in sub if float(x["research"]) > 0)
    print(f"\n=== {mode} === 正收益 {pos}/{len(sub)}")
    for x in sorted(sub, key=lambda z: float(z["research"]), reverse=True)[:5]:
        print(f"  {x['key']:38s} n={x['n']:3d} research={x['research']:>9s} "
              f"oos={x['oos']:>9s} win={x['win']:>6s}%")
Path("reports/entryE5_grid_2y.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
best = max(rows, key=lambda x: float(x["research"]))
print(f"\n全局最优 {best['key']} research={best['research']} oos={best['oos']} n={best['n']}")
