from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding

root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730,
                 cache=root / "bars_1h.json")
print("1h bars:", len(bs.bars), "gaps:", bs.quality.gaps,
      "requests:", bs.quality.requests, "cache:", bs.quality.cache_hit)
if bs.bars:
    import time
    print("  范围:", time.strftime('%Y-%m-%d', time.localtime(bs.bars[0].open_ms/1000)),
          "→", time.strftime('%Y-%m-%d', time.localtime(bs.bars[-1].open_ms/1000)))
f = load_funding(days=730, cache=root / "funding.json")
print("funding records:", len(f))
if f:
    import time
    print("  范围:", time.strftime('%Y-%m-%d %H:%M', time.localtime(f[0]['funding_ms']/1000)),
          "→", time.strftime('%Y-%m-%d %H:%M', time.localtime(f[-1]['funding_ms']/1000)))
