import json
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.structure import StructVariant, run_variant

bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
rows = []
# 固定 trail=0.75/start=1.0，只扫 stop 与 hold，检验"宽止损"是否是主因
for tf in ("1h", "4h"):
    for stop in ("1.5", "2.0", "2.5", "3.0"):
        for hold in (24, 48):
            for entry in ("ema20",):
                v = StructVariant(tf=tf, exit_mode="trail", stop_atr=D(stop),
                                  trail_atr=D("0.75"), trail_start_atr=D("1.0"),
                                  hold_tf_bars=hold)
                r = run_variant(bs.bars, v)
                rows.append({"tf": tf, "stop": stop, "hold": hold,
                             "n": r["all"].get("n", 0),
                             "research": r["research"].get("net", "0"),
                             "oos": r["oos"].get("net", "0"),
                             "win": r["all"].get("win_pct", "0"),
                             "dd": r["all"].get("max_dd_pct", "0"),
                             "fee": r["all"].get("fees", "0"),
                             "hold_h": r["all"].get("avg_hold_h", "0")})
print(f"{'tf':4s} {'stop':5s} {'hold':5s} {'n':>4s} {'research':>10s} {'oos':>10s} {'win%':>6s} {'dd%':>6s} {'fee':>7s} {'hold_h':>7s}")
for x in rows:
    print(f"{x['tf']:4s} {x['stop']:5s} {x['hold']:5d} {x['n']:4d} {x['research']:>10s} "
          f"{x['oos']:>10s} {x['win']:>6s} {x['dd']:>6s} {x['fee']:>7s} {x['hold_h']:>7s}")
Path('/tmp/sweep2.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
