"""R0-full：完整流程对等对照 + 逐事件配对。

为什么需要这个：E5 的 +7.8727 是「96 组网格里挑出来的」。要判断它是否只是
挑选的运气，随机对照必须走完同样的挑选流程。但有个陷阱——

  锁定信号时间后，``mode``/``z``/``flip`` 不再是有效自由度：
  它们只改参数名，不改任何一笔模拟结果。96 组里有 8 份是重复的。

所以真正对等的对照必须让**信号集也参与选择**：随机方向 + 4 个信号集
× 12 组退出参数 = 48 个有效网格点，每次抽样重新取研究集最优。

本脚本同时输出逐事件配对（每个信号在 2000 次随机方向下的收益分布），
回答「优势是均匀分布还是集中在少数事件上」。
"""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.experiment.baseline import (Prepared, R0Spec, data_fingerprint,
                                          mc_r0_signal_family, run_book,
                                          e5_params, overlap_stats)
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

    spec = R0Spec(n_mc=n_mc)
    prep = Prepared(bs.bars, fd, spec)

    t = time.time()
    print(f"[跑] R0_full_process n_mc={n_mc} ...", flush=True)
    r = mc_r0_signal_family(prep, spec, n_mc=n_mc, collect_per_signal=True)
    recs = r.pop("records", [])
    print(f"     完成 {time.time() - t:.1f}s  grid={r['grid_effective']}  "
          f"p={r['research_net']['p_value']:.4f}  中位={r['research_net']['median']:+.4f}")

    # 逐事件配对：把真实每笔与随机分布并排
    real_ctxs = prep.contexts(spec.e5_mode, spec.e5_z)
    real = run_book(prep, e5_params(spec), real_ctxs,
                    [c["default_side"] for c in real_ctxs])
    real_by_idx = {int(t_["signal_bar"]): t_ for t_ in real["trades"]}
    paired = []
    for row in r["per_signal"]:
        idx = row["signal_bar"]
        rt = real_by_idx.get(idx)
        paired.append({
            **row,
            "in_real_book": rt is not None,
            "real_exit": rt["exit_reason"] if rt else None,
            "real_holding_h": round(int(rt["holding_ms"]) / 3_600_000, 2) if rt else None,
            "advantage_vs_random_median": round(row["real_net"] - row["rand_median"], 4)
            if row["real_net"] is not None else None,
        })
    # 只在真实账本里出现的事件才有"优势"可言
    in_book = [p for p in paired if p["in_real_book"]]
    adv = sorted((p["advantage_vs_random_median"] for p in in_book), reverse=True)
    total_adv = sum(adv)
    top3 = sum(adv[:3])
    r["paired_events"] = paired
    r["paired_summary"] = {
        "events_in_real_book": len(in_book),
        "events_covered_by_random": len(paired),
        "advantage_total": round(total_adv, 4),
        "advantage_top1": round(adv[0], 4) if adv else None,
        "advantage_top3": round(top3, 4),
        "advantage_top3_share_pct": round(100 * top3 / total_adv, 2) if total_adv else None,
        "events_with_positive_advantage": sum(1 for a in adv if a > 0),
        "note": "优势 = 真实该笔净收益 − 同一信号在随机方向下的中位收益；"
                "集中度越高说明优势越依赖少数事件。",
    }
    r["overlap_real"] = overlap_stats(real["trades"])
    r["fingerprint"] = data_fingerprint(bs.bars, fd)
    r["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    out = Path("reports/r0_full_process.json")
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[落盘] {out}")

    s = r["research_net"]
    print(f"\n完整流程对照 p={s['p_value']:.4f} "
          f"CI={s['p_wilson_ci95']} 中位={s['median']:+.3f} "
          f"p95={s['p95']:+.3f} 真实={s['real']:+.3f} "
          f"百分位={s['real_percentile']:.1f}")
    ps = r["paired_summary"]
    print(f"逐事件：真实账本 {ps['events_in_real_book']} 笔，"
          f"优势合计 {ps['advantage_total']:+.3f}，"
          f"前 3 笔占 {ps['advantage_top3_share_pct']}%")
    print(f"最佳参数分布 top3: {list(r['best_key_freq'].items())[:3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
