from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import FundingParams, run_funding_variant
root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
p = FundingParams(z_threshold=D("2.0"), stop_atr=D("2.0"), trail_atr=D("1.0"), hold_bars=72)
r = run_funding_variant(bs.bars, fd, p)
print("signals:", r["signals"])
for k in ("all","research","oos"):
    s = r[k]
    print(f"{k:9s} n={s.get('n',0):3d} net={s.get('net','-'):>10s} carry={s.get('carry','-'):>9s} "
          f"win={s.get('win_pct','-'):>6s}% hold={s.get('avg_hold_h','-'):>6s}h dd={s.get('max_dd_pct','-'):>6s}%")
print("exits:", r["all"].get("exits"))
print("前3笔:")
for t in r["trades"][:3]:
    print(f"  {t['side']:5s} z={t['z'][:6]} net={t['net_pnl'][:9]} carry={t['funding'][:9]} "
          f"{t['exit_reason']:10s} hold={int(t['holding_ms'])//3600000}h atr={t['atr_bps']}bps")
