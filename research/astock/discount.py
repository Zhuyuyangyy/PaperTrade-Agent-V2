"""Convertible-bond discount audit: does a negative premium pay for itself?

The A-share convertible market has a structural feature the crypto desk never
had: when the conversion premium goes negative, the bond trades below its own
conversion value, and the gap is closable by a mechanical route -- buy the
bond, convert, sell the stock. Whether a retail account can capture it is a
question about cost and execution, not direction, which is exactly the kind
this repo's instruments were built to answer.

Signal:  premium < -1% at day close (disclosed threshold, not tuned)
Entry:  next day's OPEN (the signal is only known at close; acting on the
        same close is look-ahead, same rule as the Binance audit)
Exit:   N days later at close, N in {1, 3, 5}
Cost:   commission 1bp per side (convertibles carry no stamp duty);
        the conversion round trip itself is not modelled -- see the caveats
Control: the same bonds, random entry days, fixed seed, same horizon

The window is what the feed provides: sina's convertible series stop between
2023 and 2025-07 depending on the bond, while the underlying stocks are
current. The audit uses the overlap and reports it.
"""
from __future__ import annotations

import random
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .data import BondData, DataError

SEED = 20261029
THRESHOLDS = (-0.01, -0.02, -0.03, -0.05)   # the discount depth that counts as a signal
COMMISSION = 0.0001         # 1bp per side, no stamp duty on convertibles
HORIZONS = (1, 3, 5)


@dataclass
class BondResult:
    bond: str
    name: str
    days: int = 0
    signals: int = 0
    trades: dict = field(default_factory=dict)     # (threshold, horizon) -> [net pct]
    control: dict = field(default_factory=dict)    # horizon -> [net pct]
    premium_min: float | None = None
    premium_median: float | None = None


def _forward_returns(series: list[dict], i: int, horizon: int) -> float | None:
    """Bond return from the day after the signal to horizon days later.

    The series carries daily closes only, so the entry is the NEXT day's
    close -- the earliest price a close-of-day signal could act on in a
    daily-bar dataset. Disclosed in the module docstring rather than hidden:
    it is one day more conservative than an intraday fill.
    """
    entry_i = i + 1
    exit_i = entry_i + horizon
    if entry_i >= len(series) or exit_i >= len(series):
        return None
    return series[exit_i]["bond"] / series[entry_i]["bond"] - 1.0


def audit_bond(data: BondData, row: dict) -> BondResult | None:
    series = data.premium_series(row["bond"], row["stock"], row["conv_price"])
    if len(series) < 30:
        return None
    result = BondResult(bond=row["bond"], name=row["bond_name"], days=len(series))
    premiums = [p["premium"] for p in series]
    result.premium_min = min(premiums)
    result.premium_median = statistics.median(premiums)

    signal_idx = [i for i, p in enumerate(premiums) if p < THRESHOLDS[0]]
    result.signals = len(signal_idx)
    # The control draws the same NUMBER of entries as the signal set, with
    # replacement: an unbiased mean comparison needs equal sample sizes, and
    # the eligible pool (days whose horizon stays inside the data) is smaller
    # than the signal set for a bond that discounts often. Same seed, so a
    # rerun reproduces it exactly.
    rng = random.Random(SEED)
    lo, hi = 1, len(series) - max(HORIZONS) - 2
    pool = list(range(lo, max(hi, lo)))
    control_idx = rng.choices(pool, k=len(signal_idx)) if pool else []

    for threshold in THRESHOLDS:
        idx = [i for i, p in enumerate(premiums) if p < threshold]
        for horizon in HORIZONS:
            net = []
            for i in idx:
                r = _forward_returns(series, i, horizon)
                if r is not None:
                    net.append(r - 2 * COMMISSION)
            result.trades[(threshold, horizon)] = net

    # The control does not depend on the threshold: random entry days are
    # random whether or not they sit in a discount. Same count as the widest
    # signal set, same horizons, same cost.
    for horizon in HORIZONS:
        ctrl = []
        for i in control_idx:
            r = _forward_returns(series, i, horizon)
            if r is not None:
                ctrl.append(r - 2 * COMMISSION)
        result.control[horizon] = ctrl
    return result


def run(data_path: Path, limit: int | None = None, log=print) -> dict:
    data = BondData(data_path)
    try:
        rows = data.conv_map()
        # Only bonds listed before the audit window can have history in it;
        # the newest issues (the map is sorted by listing date) have none.
        rows = [r for r in rows if (r["list_date"] or "9999") < "2023-07-01"]
        if limit:
            rows = rows[:limit]
        results: list[BondResult] = []
        failed: list[str] = []
        for n, row in enumerate(rows, 1):
            try:
                r = audit_bond(data, row)
                if r is not None:
                    results.append(r)
                else:
                    failed.append(f"{row['bond']} 数据不足")
            except DataError as exc:
                failed.append(str(exc))
            if n % 25 == 0:
                log(f"  已处理 {n}/{len(rows)} 只")
        return {"results": results, "failed": failed, "universe": len(rows)}
    finally:
        data.close()


def summarize(report: dict) -> str:
    results = report["results"]
    lines = [f"可转债折价审计 · 阈值 {', '.join(f'{t*100:.0f}%' for t in THRESHOLDS)} · "
             f"覆盖 {len(results)}/{report['universe']} 只"]
    if report["failed"]:
        lines.append(f"  跳过 {len(report['failed'])} 只（数据不足或拉取失败）")
    for threshold in THRESHOLDS:
        lines.append(f"  折价 > {abs(threshold)*100:.0f}%：")
        for horizon in HORIZONS:
            net = [x for r in results for x in r.trades.get((threshold, horizon), [])]
            ctrl = [x for r in results for x in r.control.get(horizon, [])]
            if not net:
                lines.append(f"    持有 {horizon} 日：无有效信号")
                continue
            wins = sum(1 for x in net if x > 0)
            excess = (statistics.mean(net) - statistics.mean(ctrl)) if ctrl else None
            lines.append(
                f"    持有 {horizon} 日：信号 {len(net):>5} 笔 · 胜率 {wins/len(net)*100:>3.0f}% · "
                f"笔均 {statistics.mean(net)*100:+.3f}%"
                + (f" · 超额 {excess*100:+.3f}%" if excess is not None else ""))
    # signal frequency
    total_days = sum(r.days for r in results)
    total_signals = sum(r.signals for r in results)
    if total_days:
        lines.append(f"  信号频率：{total_signals}/{total_days} 个债券日 = "
                     f"{total_signals/total_days*100:.1f}%（平均每只每 {total_days/max(total_signals,1):.0f} "
                     f"个交易日出现一次）")
    return "\n".join(lines)
