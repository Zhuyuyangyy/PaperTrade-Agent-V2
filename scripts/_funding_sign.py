from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import FundingParams, run_funding_variant
root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
print(f"{'config':34s} {'n':>4s} {'research':>10s} {'oos':>10s} {'win%':>6s} {'carry':>9s}")
for z in ("1.5","2.0","2.5"):
    for flip in (False, True):
        p = FundingParams(z_threshold=D(z), stop_atr=D("2.0"), trail_atr=D("1.0"),
                          hold_bars=72, flip=flip)
        r = run_funding_variant(bs.bars, fd, p)
        a, rs, o = r["all"], r["research"], r["oos"]
        tag = "顺势" if flip else "反向"
        print(f"z>={z} {tag} stop2.0 trail1.0 h72     {a.get('n',0):4d} "
              f"{rs.get('net','-'):>10s} {o.get('net','-'):>10s} "
              f"{a.get('win_pct','-'):>6s} {a.get('carry','-'):>9s}")
