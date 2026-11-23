import time
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.structure import StructVariant, run_variant
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
t0 = time.time()
v = StructVariant(tf="4h", exit_mode="trail", stop_atr=D("2.0"),
                  trail_atr=D("0.75"), trail_start_atr=D("1.0"), hold_tf_bars=48)
r = run_variant(bs.bars, v)
d = time.time() - t0
print(f"单次 4h 变体: {d:.2f}s  n={r['all']['n']}")
print(f"16 组 × 200 次置换 ≈ {d*16*200/60:.0f} 分钟")
