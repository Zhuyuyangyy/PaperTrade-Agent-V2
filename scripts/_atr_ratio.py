import statistics
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.structure import _aggregate, _atr, TF_MINUTES
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
rt = D("18")  # fee 4*2 + slip 5*2
print(f"{'tf':5s} {'bars':>6s} {'ATR bps 中位':>12s} {'ATR bps 25%':>12s} {'ATR bps 75%':>12s} {'成本/ATR':>10s}")
for tf, m in TF_MINUTES.items():
    agg = _aggregate(bs.bars, m)
    atrs = _atr(agg)
    vals = sorted(float(a / c.close * D("10000")) for a, c in zip(atrs, agg) if a > 0)
    med = statistics.median(vals); p25 = vals[len(vals)//4]; p75 = vals[3*len(vals)//4]
    print(f"{tf:5s} {len(agg):6d} {med:12.1f} {p25:12.1f} {p75:12.1f} {float(rt)/med*100:9.0f}%")
