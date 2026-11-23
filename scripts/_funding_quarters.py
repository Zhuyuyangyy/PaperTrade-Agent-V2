import time
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import FundingParams, run_funding_variant

root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
bars = bs.bars

for mode, z in (("delta", "2.5"), ("level", "2.5")):
    p = FundingParams(z_threshold=D(z), stop_atr=D("1.5"), trail_atr=D("0.75"),
                      hold_bars=24, mode=mode)
    r = run_funding_variant(bars, fd, p)
    q = defaultdict(lambda: [0, D("0")])
    for t in r["trades"]:
        ts = int(bars[t["signal_bar"]].open_ms) / 1000
        label = time.strftime("%Y-Q", time.gmtime(ts)) + str((time.gmtime(ts).tm_mon-1)//3+1)
        q[label][0] += 1
        q[label][1] += D(t["net_pnl"])
    print(f"\n=== {p.key} 共 {r['all']['n']} 笔，净 {r['all']['net']} ===")
    for k in sorted(q):
        n, net = q[k]
        print(f"  {k}  n={n:2d}  net={net.quantize(D('0.01')):>8}")
