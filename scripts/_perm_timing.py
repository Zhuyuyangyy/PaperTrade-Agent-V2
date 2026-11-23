import time
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.validate import permutation_test
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
t0 = time.time()
r = permutation_test(bs.bars, n_perm=10)
d = time.time() - t0
print(f"10 次置换耗时 {d:.1f}s → 1000 次约 {d*100:.0f}s")
print(r["real_best"])
print(r["perm_best_net"])
