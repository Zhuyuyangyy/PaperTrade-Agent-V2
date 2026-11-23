"""R0-4h：把随机基线搬到 4h 框架。

要回答的问题与 1h 相同，但换到成本占 ATR 最低的周期：

  1. 4h 下随机入场的期望是多少？（1h 是每笔 −0.14，中位 −4.75）
  2. funding 信号在 4h 下是否优于随机方向？
  3. 4h 的成本门槛是否真的比 1h 低到值得继续？

**不做参数优化**：E3 已证明 4h 扫参数能被置换检验否掉（p=0.522）。
主口径用先验中心参数（stop 2.0 / trail 1.0 / hold 24 根 = 4 天），
另附 18 组网格的**全分布**，看真实方向在每一组的百分位——
这样结论不依赖任何单点选择。

数据：由本地 1h 缓存聚合（factor=4），零缺口，全程不联网。
"""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.experiment.baseline import (COST_SCENARIOS, Prepared,
                                          aggregate_bars, atr_bps_stats,
                                          cost_sensitivity, data_fingerprint,
                                          fourh_params, mc_r0, mc_r1,
                                          multiple_testing, overlap_stats,
                                          reverse_test, risk_adjusted, run_book,
                                          spec_4h)
from research.experiment.data import load_klines
from research.experiment.funding import load_funding

ROOT = Path("data/v3_funding")


def main() -> int:
    n_mc = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    for f in (ROOT / "bars_1h.json", ROOT / "funding.json"):
        if not f.exists():
            raise SystemExit(f"缓存缺失 {f}，拒绝联网")

    bs = load_klines(interval="1h", step_ms=3_600_000, days=730,
                     cache=ROOT / "bars_1h.json")
    fd = load_funding(days=730, cache=ROOT / "funding.json")
    if not bs.quality.cache_hit:
        raise SystemExit("未命中缓存，中止")

    bars4 = aggregate_bars(bs.bars, 4)
    gaps = sum(1 for a, b in zip(bars4, bars4[1:])
               if int(b.open_ms - a.open_ms) != 14_400_000)
    spec = spec_4h(n_mc=n_mc)
    prep = Prepared(bars4, fd, spec)
    base = fourh_params(spec)
    ctxs = prep.contexts(spec.e5_mode, spec.e5_z)
    real = run_book(prep, base, ctxs, [c["default_side"] for c in ctxs])

    print(f"[数据] 4h bars={len(bars4)} 缺口={gaps} ATR={atr_bps_stats(bars4)['median_bps']}bps")
    print(f"[口径] {spec.version} 信号={len(ctxs)} 成交={real['filled']} "
          f"净={real['all'].get('net')} 研究集={real['research'].get('net')} "
          f"样本外={real['oos'].get('net')}")

    exit_grid = [fourh_params(spec, stop_atr=D(s), trail_atr=D(t), hold_bars=h)
                 for s in ("1.5", "2.0", "2.5")
                 for t in ("0.75", "1.0")
                 for h in (12, 24, 48)]

    results = {
        "spec": spec.to_dict(),
        "fingerprint": data_fingerprint(bars4, fd),
        "aggregation": {"source": "1h", "factor": 4, "gaps": gaps,
                        "atr": atr_bps_stats(bars4)},
        "base_params": {"key": base.key, "stop_atr": str(base.stop_atr),
                        "trail_atr": str(base.trail_atr),
                        "trail_start_atr": str(base.trail_start_atr),
                        "hold_bars": base.hold_bars,
                        "note": "先验中心参数，非优化结果"},
        "real_4h": {"research_net": real["research"].get("net"),
                    "oos_net": real["oos"].get("net"),
                    "all": real["all"], "filled": real["filled"],
                    "attempted": real["attempted"]},
        "grid_size": len(exit_grid),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    jobs = [
        ("R0_indep_fixed", lambda: mc_r0(prep, spec, n_mc=n_mc, base_params=base)),
        ("R0_matched_fixed", lambda: mc_r0(prep, spec, n_mc=n_mc, matched=True,
                                           base_params=base)),
        ("R0_indep_select18", lambda: mc_r0(prep, spec, n_mc=n_mc, grid=exit_grid,
                                            base_params=base)),
        ("R1_fixed", lambda: mc_r1(prep, spec, n_mc=n_mc, base_params=base)),
        ("R1_select18", lambda: mc_r1(prep, spec, n_mc=n_mc, grid=exit_grid,
                                      base_params=base)),
    ]
    rec_path = Path("reports/r0_4h_records.jsonl")
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
        s = out["research_net"]
        print(f" {time.time() - t:.1f}s p={s['p_value']:.4f} "
              f"中位={s['median']:+.4f} p95={s['p95']:+.4f}")

    print("[跑] R2_reverse ...", end="", flush=True)
    r2 = reverse_test(prep, spec, grid=exit_grid, base_params=base)
    results["overlap_real"] = overlap_stats(real["trades"])
    results["overlap_r2"] = overlap_stats(r2["trades"])
    r2.pop("trades", None)
    results["R2_reverse"] = r2
    print(f" 净={r2['all']['net']} 研究集={r2['research_net']}")

    print("[跑] 成本敏感性 ...", end="", flush=True)
    results["cost_sensitivity"] = cost_sensitivity(prep, spec, COST_SCENARIOS,
                                                   base_params=base)
    print(" 完成")

    # 18 组网格全分布：不选最优，逐组看真实方向的百分位
    per_cell = []
    for p in exit_grid:
        r = run_book(prep, p, ctxs, [c["default_side"] for c in ctxs])
        per_cell.append({
            "key": p.key,
            "real_research": r["research"].get("net", "0"),
            "real_oos": r["oos"].get("net", "0"),
            "n": r["filled"],
        })
    nets = sorted(float(c["real_research"]) for c in per_cell)
    results["grid_cells"] = per_cell
    results["grid_positive"] = sum(1 for v in nets if v > 0)
    results["grid_total"] = len(nets)
    results["grid_median_research"] = round(nets[len(nets) // 2], 4)

    results["risk_adjusted_real"] = risk_adjusted(real["trades"])
    results["multiple_testing"] = multiple_testing(
        {"E2/E3 均线结构(4h)": 0.5220, "E4 费率水平": 0.1000,
         "E5 费率变化率": 0.0833, "R0-4h 先验参数": results["R0_indep_fixed"]["research_net"]["p_value"]})

    Path("reports/r0_4h_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[落盘] reports/r0_4h_results.json")
    print(f"[网格] 18 组中正收益 {results['grid_positive']}/18，"
          f"研究集中位 {results['grid_median_research']:+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
