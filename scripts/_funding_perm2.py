import gc, json, time
from decimal import Decimal as D
from pathlib import Path
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import grid, run_funding_variant
from research.experiment.validate import shuffle_bars

root = Path("data/v3_funding")
progress = Path("reports/entryE4_perm_progress.jsonl")
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
            best = (net, p.key)
    return best

t0 = time.time()
real_net, real_key = best_research(bars)
print(f"真实最优 {real_key} research={real_net} ({time.time()-t0:.1f}s)", flush=True)
progress.write_text(json.dumps({"real_net": str(real_net), "real_key": real_key}) + "\n",
                    encoding="utf-8")

N, seed = 120, 20261124
betters = 0
done = 0
with progress.open("a", encoding="utf-8") as fh:
    for k in range(N):
        ts = time.time()
        try:
            fake = shuffle_bars(bars, seed + k)
            net, key = best_research(fake)
        except Exception as exc:                       # noqa: BLE001 - 单次失败不中断
            print(f"  {k+1}/{N} ERROR {type(exc).__name__}: {exc}", flush=True)
            continue
        dt = time.time() - ts
        del fake
        gc.collect()
        done += 1
        if net >= real_net:
            betters += 1
        fh.write(json.dumps({"k": k, "perm_net": str(net), "key": key,
                             "secs": round(dt, 1)}) + "\n")
        fh.flush()
        if (k + 1) % 5 == 0 or dt > 30:
            print(f"  {k+1}/{N} betters={betters} last={net} {dt:.1f}s "
                  f"elapsed={time.time()-t0:.0f}s", flush=True)

p = D(betters) / D(max(1, done))
out = {"n_perm": done, "seed": seed, "grid_size": len(variants),
       "real_best": {"key": real_key, "research_net": str(real_net)},
       "n_perm_better_or_equal": betters,
       "p_value": str(p.quantize(D("0.0001"))),
       "verdict": "PASS" if p <= D("0.05") else "FAIL_OVERFIT",
       "note": "p = 假行情中能优化出 >= 真实成绩的比例；p<=5% 才算过。"}
Path("reports/entryE4_funding_permutation.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(out, ensure_ascii=False, indent=2))
