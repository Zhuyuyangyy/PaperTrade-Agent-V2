from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.structure import StructVariant, run_variant
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
v = StructVariant(tf="4h", exit_mode="trail", stop_atr=D("2.0"),
                  trail_atr=D("0.75"), trail_start_atr=D("1.0"), hold_tf_bars=48)
r = run_variant(bs.bars, v)
cut = int(len(bs.bars) * 0.7)
print("all n=", r["all"]["n"], "research n=", r["research"]["n"], "oos n=", r["oos"]["n"])
print("research=", r["research"].get("net"), "oos=", r["oos"].get("net"))
for t in r["trades"]:
    tag = "RESEARCH" if t["signal_bar"] < cut else "OOS"
    print(f"  {tag:8s} bar{t['signal_bar']:5d} {t['side']:5s} net={t['net_pnl'][:10]:>10s} "
          f"{t['exit_reason']:11s} hold={int(t['holding_ms'])//3600000:2d}h atr_bps={t['atr_bps']}")
