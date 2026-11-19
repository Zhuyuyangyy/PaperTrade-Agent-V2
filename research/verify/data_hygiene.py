"""Data hygiene: disclose what the candle timestamps say BEFORE replaying.

Why this exists (a real, published bug): CME glues holiday half-sessions into
the next trade date, so ES after Labor Day prints ONE 47-hour daily bar from
Sunday 18:00 to Tuesday 17:00 ET (345-strategies/thestrat-suite, FIX
GLUE-1/LINEEND-CLOSE-1). Upstream Pine logic that measures staleness from the
bar's open stamp misfired mid-session on exactly such bars. The engine cannot
reconstruct which period a bar belongs to -- but it CAN see the anomalous
interval in the timestamps, and it must not swallow it silently.

Honest boundary: this module DISCLOSES facts (gaps, duplicate/unordered
stamps, interval mismatch vs the spec). It never changes the verdict: a gap
is not evidence against the strategy, it is evidence about the DATA. The
report renders it next to the window so a reader knows the replay walked over
a holiday-glued bar, a trading halt, or a hole in the export.
"""
from __future__ import annotations

import re
import statistics

# Multipliers of the median bar spacing that mark a gap as "anomalous".
# 1.5x tolerates ordinary jitter (DST shifts, exchange micro-pauses); a
# holiday-glued bar is >= 2x, a weekend on an intraday series is much more.
_GAP_FACTOR = 1.5
_MAX_GAPS_REPORTED = 5

_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def interval_ms(interval: str) -> int | None:
    """'1h' / '15m' / '4h' / '1d' / '1w' -> milliseconds; None if unparseable.

    Unparseable is not an error: the hygiene check then falls back to the
    observed median spacing and simply skips the spec-vs-data comparison.
    """
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", str(interval).lower())
    if not m:
        return None
    return int(m.group(1)) * _UNIT_MS[m.group(2)]


def check(candles: list[dict], interval: str | None = None) -> dict:
    """Timestamp facts for one ascending candle series.

    Returns {"n_bars", "median_gap_ms", "expected_gap_ms", "interval_match",
             "gaps": [{"after_t", "before_t", "gap_ms", "multiple"}],
             "n_gaps", "duplicates", "unordered", "notes": [str, ...]}.
    Never raises on data facts -- only the replay itself may reject data.
    """
    n = len(candles)
    out: dict = {
        "n_bars": n, "median_gap_ms": None, "expected_gap_ms": interval_ms(interval or ""),
        "interval_match": None, "gaps": [], "n_gaps": 0,
        "duplicates": 0, "unordered": 0, "notes": [],
    }
    if n < 3:
        out["notes"].append("K 线不足 3 根，间隔检查跳过")
        return out

    ts = [c["t"] for c in candles]
    diffs = [b - a for a, b in zip(ts, ts[1:])]
    out["duplicates"] = sum(1 for d in diffs if d == 0)
    out["unordered"] = sum(1 for d in diffs if d < 0)
    positive = [d for d in diffs if d > 0]
    if not positive:
        out["notes"].append("时间戳全部重复，无法计算间隔")
        return out
    med = statistics.median(positive)
    out["median_gap_ms"] = med

    exp = out["expected_gap_ms"]
    if exp:
        # The spec's interval should describe the data. A 2x mismatch either
        # way means the replay's bar-count-based rules (timeout_bars, folds)
        # measure something different from what the spec author intended.
        ratio = med / exp
        out["interval_match"] = 0.5 <= ratio <= 2.0
        if not out["interval_match"]:
            out["notes"].append(
                f"spec 周期 {interval}（{exp}ms）与数据中位间隔 {int(med)}ms 不符（×{ratio:.2f}）"
                "——timeout_bars/折数等按 bar 计数的规则含义已改变")

    threshold = med * _GAP_FACTOR
    gaps = []
    for i, d in enumerate(diffs):
        if d > threshold:
            gaps.append({"after_t": ts[i], "before_t": ts[i + 1],
                         "gap_ms": d, "multiple": round(d / med, 2)})
    out["n_gaps"] = len(gaps)
    gaps.sort(key=lambda g: -g["gap_ms"])
    out["gaps"] = gaps[:_MAX_GAPS_REPORTED]
    if gaps:
        out["notes"].append(
            f"{len(gaps)} 处异常间隔（>中位×{_GAP_FACTOR}）——可能是假日粘连 bar、停牌或导出缺口；"
            "上游按周期归属的逻辑（Pine 审计清单第 7 项）需人工核对")
    if out["duplicates"]:
        out["notes"].append(f"{out['duplicates']} 处重复时间戳")
    if out["unordered"]:
        out["notes"].append(f"{out['unordered']} 处时间戳倒序（CSV 加载会排序，直传数据不会）")
    return out
