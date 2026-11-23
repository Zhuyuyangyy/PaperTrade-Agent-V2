import json
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.structure import StructVariant, run_variant

bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
bars = bs.bars
rows = []
combos = []
for tf in ("1h", "4h"):
    for trail in ("0.5", "0.75", "1.0", "1.5"):
        for start in ("0.5", "1.0"):
            for stop in ("1.0", "1.5", "2.0"):
                combos.append((tf, trail, start, stop))
for tf, trail, start, stop in combos:
    v = StructVariant(tf=tf, exit_mode="trail", stop_atr=D(stop),
                      trail_atr=D(trail), trail_start_atr=D(start),
                      hold_tf_bars=48 if tf == "1h" else 24)
    r = run_variant(bars, v)
    rows.append({"tf": tf, "trail": trail, "start": start, "stop": stop,
                 "n": r["all"].get("n", 0),
                 "research": r["research"].get("net", "0"),
                 "oos": r["oos"].get("net", "0"),
                 "win": r["all"].get("win_pct", "0"),
                 "dd": r["all"].get("max_dd_pct", "0"),
                 "fee": r["all"].get("fees", "0")})
rows.sort(key=lambda x: float(x["research"]), reverse=True)
print(f"{'tf':4s} {'trail':6s} {'start':6s} {'stop':5s} {'n':>4s} {'research':>10s} {'oos':>10s} {'win%':>6s} {'dd%':>6s}")
for x in rows[:14]:
    print(f"{x['tf']:4s} {x['trail']:6s} {x['start']:6s} {x['stop']:5s} {x['n']:4d} "
          f"{x['research']:>10s} {x['oos']:>10s} {x['win']:>6s} {x['dd']:>6s}")
Path('/tmp/sweep.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
