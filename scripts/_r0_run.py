"""R0 随机基线：一次性跑完全部对照并落盘。

用法：
    python scripts/_r0_run.py                 # 全量（2000 次）
    python scripts/_r0_run.py --n-mc 100      # 快速复核

约束：
  - **只读本地缓存**。缓存不存在就退出，绝不联网（fapi 418 封禁不可重试）。
  - 口径冻结在 R0Spec，脚本不改任何参数。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.experiment.baseline import (COST_SCENARIOS, Prepared, R0Spec,
                                          cost_sensitivity, data_fingerprint,
                                          e5_params, mc_r0, mc_r1,
                                          multiple_testing, overlap_stats,
                                          reverse_test, risk_adjusted, run_book)
from research.experiment.data import load_klines
from research.experiment.funding import load_funding
from research.experiment.funding_exp import grid

ROOT = Path("data/v3_funding")
OUT = Path("reports")


def _require_cache(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"缓存不存在：{path}\n拒绝联网重拉（418 封禁不可重试）。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-mc", type=int, default=2000)
    ap.add_argument("--out", default="reports/r0_results.json")
    args = ap.parse_args()

    bars_cache, fund_cache = ROOT / "bars_1h.json", ROOT / "funding.json"
    _require_cache(bars_cache)
    _require_cache(fund_cache)

    t_all = time.time()
    bs = load_klines(interval="1h", step_ms=3_600_000, days=730, cache=bars_cache)
    fd = load_funding(days=730, cache=fund_cache)
    if not bs.quality.cache_hit:
        raise SystemExit("未命中缓存，已中止（不允许联网）。")

    spec = R0Spec(n_mc=args.n_mc)
    prep = Prepared(bs.bars, fd, spec)
    fp = data_fingerprint(bs.bars, fd)

    print(f"[数据] bars={fp['bars']} funding={fp['funding']} "
          f"bars_sha={fp['bars_sha256'][:12]} fund_sha={fp['funding_sha256'][:12]}")
    print(f"[口径] {spec.version} 研究集切点={prep.cut} 可交易池={len(prep.tradable)}")
    print(f"[对照] n_mc={spec.n_mc} seed={spec.seed}\n")

    base = e5_params(spec)
    ctxs = prep.contexts(spec.e5_mode, spec.e5_z)
    real = run_book(prep, base, ctxs, [c["default_side"] for c in ctxs])

    full_grid = grid()
    base_list = [r for r in full_grid
                 if r.mode == spec.e5_mode and r.z_threshold == spec.e5_z]
    exit_grid = [r for r in base_list if not r.flip]   # 锁定方向后的 12 组退出参数

    results: dict = {
        "spec": spec.to_dict(), "fingerprint": fp,
        "real_e5": {"params": base.key, "research_net": real["research"].get("net"),
                    "oos_net": real["oos"].get("net"), "all": real["all"],
                    "filled": real["filled"], "attempted": real["attempted"],
                    "n_signals": len(ctxs)},
        "grid_sizes": {"full_96": len(full_grid), "exit_12": len(exit_grid)},
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    def prog(tag):
        def _p(k, n):
            print(f"  {tag} {k}/{n}", end="\r", flush=True)
        return _p

    jobs = [
        ("R0_indep_fixed", lambda: mc_r0(prep, spec, n_mc=spec.n_mc)),
        ("R0_matched_fixed", lambda: mc_r0(prep, spec, n_mc=spec.n_mc, matched=True)),
        ("R0_indep_select12", lambda: mc_r0(prep, spec, n_mc=spec.n_mc, grid=exit_grid)),
        ("R0_matched_select12", lambda: mc_r0(prep, spec, n_mc=spec.n_mc, grid=exit_grid,
                                              matched=True)),
        ("R0_indep_select96", lambda: mc_r0(prep, spec, n_mc=spec.n_mc, grid=full_grid)),
        ("R1_fixed", lambda: mc_r1(prep, spec, n_mc=spec.n_mc)),
        ("R1_select12", lambda: mc_r1(prep, spec, n_mc=spec.n_mc, grid=exit_grid)),
    ]
    rec_path = Path(args.out).with_name("r0_mc_records.jsonl")
    rec_path.write_text("", encoding="utf-8")
    for name, fn in jobs:
        t = time.time()
        print(f"[跑] {name} ...", end="", flush=True)
        out = fn()
        recs = out.pop("records", [])
        with rec_path.open("a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps({"job": name, **r}, ensure_ascii=False) + "\n")
        results[name] = out
        print(f" 完成 {time.time() - t:.1f}s  "
              f"p={out['research_net']['p_value']:.4f} "
              f"中位={out['research_net']['median']:+.4f}")

    print("[跑] R2_reverse ...", end="", flush=True)
    r2 = reverse_test(prep, spec, grid=exit_grid)
    results["overlap"] = {"real_e5": overlap_stats(real["trades"]),
                          "r2_reverse": overlap_stats(r2["trades"])}
    r2.pop("trades", None)
    results["R2_reverse"] = r2
    print(f" 完成 net={r2['all']['net']} 研究集={r2['research_net']}")

    print("[跑] 成本敏感性 ...", end="", flush=True)
    cs = cost_sensitivity(prep, spec, COST_SCENARIOS)
    results["cost_sensitivity"] = cs
    print(" 完成")

    results["risk_adjusted_real"] = risk_adjusted(real["trades"])
    results["multiple_testing"] = multiple_testing(
        {"E2/E3 均线结构(4h)": 0.5220, "E4 费率水平": 0.1000, "E5 费率变化率": 0.0833},
        {"E2/E3 均线结构(4h)": 32, "E4 费率水平": 72, "E5 费率变化率": 96})

    OUT.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n[落盘] {args.out}")
    print(f"[总耗时] {time.time() - t_all:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
