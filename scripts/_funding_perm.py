import json, time
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import grid, run_funding_variant
from research.experiment.validate import shuffle_bars

root = Path("data/v3_funding")
bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=root/"bars_1h.json")
fd = load_funding(days=730, cache=root/"funding.json")
bars = bs.bars
variants = grid()

def best_research(data):
    best = None
    for p in variants:
        r = run_funding_variant(data, fd, p)
        net = D(r["research"].get("net", "0") or "0")
        if best is None or net > best[0]:
            best = (net, p.key, D(r["oos"].get("net", "0") or "0"))
    return best

t0 = time.time()
real_net, real_key, real_oos = best_research(bars)
print(f"真实最优 {real_key} research={real_net} oos={real_oos}  ({time.time()-t0:.1f}s)", flush=True)

N = 200
seed = 20261124
betters, perms = 0, []
for k in range(N):
    fake = shuffle_bars(bars, seed + k)
    net, key, _ = best_research(fake)
    perms.append(str(net))
    if net >= real_net:
        betters += 1
    if (k+1) % 10 == 0:
        print(f"  {k+1}/{N} betters={betters} elapsed={time.time()-t0:.0f}s", flush=True)

perms_sorted = sorted(perms, key=lambda x: D(x))
p = D(betters) / D(N)
out = {"n_perm": N, "seed": seed, "grid_size": len(variants),
       "real_best": {"key": real_key, "research_net": str(real_net), "oos_net": str(real_oos)},
       "perm_best_net": {"median": perms_sorted[N//2],
                         "p95": perms_sorted[int(N*0.95)],
                         "max": perms_sorted[-1], "min": perms_sorted[0]},
       "n_perm_better_or_equal": betters,
       "p_value": str(p.quantize(D("0.0001"))),
       "verdict": "PASS" if p <= D("0.05") else "FAIL_OVERFIT"}
Path("reports/entryE4_funding_permutation.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(out, ensure_ascii=False, indent=2))
