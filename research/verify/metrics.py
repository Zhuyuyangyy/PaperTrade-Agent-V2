"""Equity statistics over a net-return series.

One assumption, stated everywhere it matters: every fill is equal weight and
the series is the fills in chronological order. That is the right frame for a
first look -- compounding, position sizing, and correlation across concurrent
positions are the strategy's job, not the measuring stick's.
"""
from __future__ import annotations

import statistics


def equity_stats(nets: list[float]) -> dict:
    """nets: per-trade net return as a fraction (0.03 = +3%)."""
    if not nets:
        return {"n": 0, "sum_pct": 0.0, "mean_pct": 0.0, "median_pct": 0.0,
                "win_pct": 0.0, "profit_factor": None, "max_dd_pct": 0.0,
                "expectancy_pct": 0.0, "best_pct": 0.0, "worst_pct": 0.0}
    s = sorted(nets)
    n = len(s)
    gains = sum(v for v in s if v > 0)
    losses = -sum(v for v in s if v < 0)
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for v in s:  # worst-case ordering is fine for a drawdown bound; the
        equity *= (1.0 + v)  # chronological curve is in report.equity_curve
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    return {
        "n": n,
        "sum_pct": sum(s) * 100,
        "mean_pct": statistics.fmean(s) * 100,
        "median_pct": (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2) * 100,
        "win_pct": sum(1 for v in s if v > 0) / n * 100,
        "profit_factor": (gains / losses) if losses > 0 else None,
        "max_dd_pct": max_dd * 100,
        "expectancy_pct": statistics.fmean(s) * 100,
        "best_pct": s[-1] * 100,
        "worst_pct": s[0] * 100,
        # The raw series travels with the stats so the control-group bootstrap
        # can resample it later without re-running the replay.
        "nets": s,
    }


def equity_curve(nets: list[float]) -> list[float]:
    """Cumulative equity (start = 1.0), chronological order."""
    out, eq = [], 1.0
    for v in nets:
        eq *= 1.0 + v
        out.append(eq)
    return out
