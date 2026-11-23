from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import FundingParams, run_funding_variant

root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
bars = bs.bars

for mode in ("delta", "level"):
    p = FundingParams(z_threshold=D("2.5"), stop_atr=D("1.5"), trail_atr=D("0.75"),
                      hold_bars=24, mode=mode)
    r = run_funding_variant(bars, fd, p)
    pnls = sorted((D(t["net_pnl"]) for t in r["trades"]), reverse=True)
    total = sum(pnls, D("0"))
    print(f"\n=== {p.key}  n={len(pnls)}  总净={total.quantize(D('0.01'))} ===")
    print(f"  最大单笔 {pnls[0].quantize(D('0.01'))}  次大 {pnls[1].quantize(D('0.01'))}")
    for k in (1, 2, 3, 5):
        if k < len(pnls):
            rest = sum(pnls[k:], D("0"))
            print(f"  剔除前 {k} 大盈利后: {rest.quantize(D('0.01'))}")
    win = sum(1 for x in pnls if x > 0)
    avg_w = sum((x for x in pnls if x > 0), D("0")) / max(1, win)
    avg_l = sum((x for x in pnls if x <= 0), D("0")) / max(1, len(pnls) - win)
    print(f"  胜率 {win}/{len(pnls)}  均盈 {avg_w.quantize(D('0.01'))}  均亏 {avg_l.quantize(D('0.01'))}"
          f"  盈亏比 {abs(avg_w/avg_l).quantize(D('0.01')) if avg_l else '-'}")
