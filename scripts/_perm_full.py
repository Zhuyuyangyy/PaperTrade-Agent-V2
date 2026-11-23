import json
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.validate import permutation_test
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
r = permutation_test(bs.bars, n_perm=1000, seed=20261123, tf="4h")
Path("reports/entryE3_permutation.json").write_text(
    json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(r, ensure_ascii=False, indent=2))
