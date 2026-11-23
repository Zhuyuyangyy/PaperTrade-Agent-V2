import statistics
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.validate import shuffle_bars
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
bars = bs.bars
fake = shuffle_bars(bars, 12345)
print("原始 起/终:", bars[0].close, bars[-1].close)
print("置换 起/终:", fake[0].close, fake[-1].close)
print("根数一致:", len(fake) == len(bars))
# 收益分布对比
def rets(xs):
    return [float(xs[i].close/xs[i-1].close)-1 for i in range(1, len(xs))]
r1, r2 = rets(bars), rets(fake)
print(f"真实 收益 mean={statistics.mean(r1):.3e} sd={statistics.pstdev(r1):.3e}")
print(f"置换 收益 mean={statistics.mean(r2):.3e} sd={statistics.pstdev(r2):.3e}")
# 序列相关性（规律是否被打碎）
def acf1(xs):
    m = statistics.mean(xs)
    num = sum((xs[i]-m)*(xs[i-1]-m) for i in range(1, len(xs)))
    den = sum((v-m)**2 for v in xs)
    return num/den if den else 0
print(f"真实 lag1 自相关={acf1(r1):+.5f}  置换 lag1 自相关={acf1(r2):+.5f}")
# OHLC 自洽
bad = sum(1 for c in fake if not (c.low <= min(c.open,c.close) and c.high >= max(c.open,c.close)))
print("OHLC 不自洽根数:", bad)
