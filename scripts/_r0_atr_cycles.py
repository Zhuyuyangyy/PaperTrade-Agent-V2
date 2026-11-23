"""各周期的成本占 ATR 比例（R0 报告用）。

只读本地缓存：
  - 1h / 4h 用 `data/v3_funding/bars_1h.json`（2 年）
  - 5m / 15m 用 `data/v3_entryE/bars_5m_cache.json`（约 60 天）

两个区间不同（1h 是 2 年、5m 是近 60 天），所以只用于**量级比较**，
不用于跨周期收益比较。撮合始终在 1h 上跑，不受影响。
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.experiment.baseline import (aggregate_bars, atr_bps_stats,
                                          cost_pressure, data_fingerprint)
from research.experiment.data import load_klines

ROUND_TRIP = 18.0   # bps，冻结口径实测值

TFS = [
    # (目标周期, 源周期 step_ms, 缓存路径, 聚合倍数)
    ("5m", 300_000, "data/v3_entryE/bars_5m_cache.json", 1),
    ("15m", 300_000, "data/v3_entryE/bars_5m_cache.json", 3),
    ("1h", 3_600_000, "data/v3_funding/bars_1h.json", 1),
    ("4h", 3_600_000, "data/v3_funding/bars_1h.json", 4),
]


def main() -> int:
    cache: dict[str, object] = {}
    rows = []
    for tf, src_step, path, factor in TFS:
        p = Path(path)
        if not p.exists():
            print(f"[跳过] {tf}: 缓存不存在 {p}")
            continue
        if path not in cache:
            interval = "5m" if src_step == 300_000 else "1h"
            bs = load_klines(interval=interval, step_ms=src_step,
                             days=60 if interval == "5m" else 730, cache=p)
            if not bs.quality.cache_hit:
                print(f"[中止] {path} 未命中缓存，拒绝联网")
                return 1
            cache[path] = bs.bars
        bars = cache[path]  # type: ignore[assignment]
        big = aggregate_bars(bars, factor)
        st = atr_bps_stats(big)
        if st.get("n", 0) == 0:
            continue
        cp = cost_pressure(st["median_bps"], ROUND_TRIP)
        rows.append({"tf": tf, "bars": len(big), "source_step_ms": src_step,
                     "source": path, "factor": factor,
                     "target_step_ms": src_step * factor,
                     "atr": st, "pressure": cp})
        print(f"{tf:4s} bars={len(big):6d}  ATR中位={st['median_bps']:7.1f}bps  "
              f"往返18bps占比={cp['cost_over_atr_pct']:6.1f}%  "
              f"需 {cp['atr_needed_to_break_even']:.2f}×ATR")

    out = Path("reports/r0_cost_pressure.json")
    out.write_text(json.dumps({
        "round_trip_bps": ROUND_TRIP, "rows": rows,
        "note": "5m/15m 来自近 60 天缓存，1h/4h 来自 2 年缓存；只做量级比较。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[落盘] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
