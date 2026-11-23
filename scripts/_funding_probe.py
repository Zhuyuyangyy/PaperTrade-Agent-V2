import json
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import funding_signals, load_funding
root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
rates = sorted(float(D(x["rate"])) for x in fd)
print(f"费率条数 {len(rates)}  中位 {rates[len(rates)//2]*100:.4f}%  "
      f"p95 {rates[int(len(rates)*0.95)]*100:.4f}%  max {rates[-1]*100:.4f}%")
for z in ("1.5","2.0","2.5","3.0"):
    for lb in (90, 180):
        sg = funding_signals(fd, bs.bars, lookback=lb, z_threshold=D(z))
        longs = sum(1 for s in sg if s.side=="LONG"); shorts = len(sg)-longs
        print(f"z>={z} lookback={lb}: 信号 {len(sg)}（多 {longs} / 空 {shorts}）")
