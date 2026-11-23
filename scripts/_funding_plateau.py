import json, time
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import grid, run_funding_variant
root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
rows = []
t0 = time.time()
for p in grid():
    r = run_funding_variant(bs.bars, fd, p)
    rows.append({"key": p.key, "z": str(p.z_threshold), "stop": str(p.stop_atr),
                 "trail": str(p.trail_atr), "hold": p.hold_bars, "flip": p.flip,
                 "n": r["all"].get("n", 0),
                 "research": r["research"].get("net", "0"),
                 "oos": r["oos"].get("net", "0"),
                 "win": r["all"].get("win_pct", "0"),
                 "carry": r["all"].get("carry", "0"),
                 "dd": r["all"].get("max_dd_pct", "0")})
el = time.time()-t0
rows.sort(key=lambda x: float(x["research"]), reverse=True)
print(f"72 组耗时 {el:.1f}s")
print(f"{'key':30s} {'n':>4s} {'research':>10s} {'oos':>10s} {'win%':>6s} {'dd%':>6s}")
for x in rows[:12]:
    print(f"{x['key']:30s} {x['n']:4d} {x['research']:>10s} {x['oos']:>10s} {x['win']:>6s} {x['dd']:>6s}")
pos = sum(1 for x in rows if float(x["research"]) > 0)
print(f"\n研究集为正: {pos}/{len(rows)} = {pos/len(rows)*100:.1f}%")
print("最差 3 组:")
for x in rows[-3:]:
    print(f"{x['key']:30s} {x['n']:4d} {x['research']:>10s} {x['oos']:>10s}")
Path("reports/entryE4_funding_grid.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
