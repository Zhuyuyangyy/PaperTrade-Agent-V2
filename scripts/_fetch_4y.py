import time
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding

root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=1460,
                 cache=root / "bars_1h_4y.json")
print("1h bars:", len(bs.bars), "gaps:", bs.quality.gaps,
      "max_gap_h:", bs.quality.max_gap_ms // 3_600_000,
      "requests:", bs.quality.requests, "cache:", bs.quality.cache_hit)
if bs.bars:
    print("  范围:", time.strftime('%Y-%m-%d', time.localtime(bs.bars[0].open_ms/1000)),
          "→", time.strftime('%Y-%m-%d', time.localtime(bs.bars[-1].open_ms/1000)))
f = load_funding(days=1460, cache=root / "funding_4y.json")
print("funding:", len(f))
if f:
    print("  范围:", time.strftime('%Y-%m-%d', time.localtime(f[0]['funding_ms']/1000)),
          "→", time.strftime('%Y-%m-%d', time.localtime(f[-1]['funding_ms']/1000)))
