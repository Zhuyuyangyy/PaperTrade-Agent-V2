"""The parameter-sensitivity grid: replay the strategy over a pre-declared
grid of exit parameters and read the DISTRIBUTION, not the best cell.

The industry's number-one cause of backtest->live decay is in-sample overfit
(median -14.3pp). Walk-forward catches the time axis of that failure; this
instrument catches the parameter axis: an edge that exists only at one exact
take-profit/stop-loss combination is a curve fit -- that combination was
chosen on these very bars, and nothing else in the grid survived contact
with them.

The grid values live in the spec, declared before any number is read. Each
cell replays the SAME entries under the SAME headline convention (the spec's
worst/best-case flag, base fee); only the declared axes move.

What the grid deliberately does NOT do:

  * re-price costs per cell -- that is a third dimension, and the
    fee_multiples battery (report section 2) already answers it;
  * re-run the control group per cell -- the control question is about entry
    timing, not exit geometry, and one honest control is better than N
    under-sampled ones;
  * move signal parameters -- changing them changes the entry set itself,
    which is a different experiment, not a sensitivity.
"""
from __future__ import annotations

import itertools
import statistics

from . import metrics
from .pathsim import Exit, simulate


def run(spec_obj, candles: list[dict], entries: dict[int, str], exit_rule: Exit,
        funding: list[dict] | None = None, scope: str = "全窗口") -> dict:
    """Replay every declared parameter combination over ONE entry set.

    Returns a JSON-serialisable dict: the declaration, one row per cell, and
    the distribution summary the verdict and the report read. Cells with no
    fills are kept in `cells` but excluded from the ratio denominator -- a
    cell where nothing traded carries no evidence either way, and counting
    it as a loss would flatter the instrument.
    """
    axes = spec_obj.param_grid.axes
    order = list(axes)
    cells = []
    for combo in itertools.product(*(axes[k] for k in order)):
        params = dict(zip(order, combo))
        sim = simulate(
            candles, entries, _exit_with(exit_rule, params),
            symbol=spec_obj.symbol, fee_rate=spec_obj.fee_rate,
            funding_series=funding if spec_obj.funding else None,
            worst_case=spec_obj.worst_case, fill_model=spec_obj.fill_model,
        )
        st = metrics.equity_stats(sim.net_pct_series())
        cells.append({
            "params": params,
            "fills": st["n"],
            "net_sum_pct": st["sum_pct"],
            "mean_pct": st["mean_pct"],
            "win_pct": st["win_pct"],
            "profit_factor": st["profit_factor"],
        })
    return _summarise(axes, order, cells, scope)


def _exit_with(base: Exit, params: dict) -> Exit:
    """The base exit rule with the declared axes overridden. Everything not
    on an axis keeps its declared value -- a grid moves what it declared and
    nothing else."""
    return Exit(
        take_profit=params.get("take_profit", base.take_profit),
        stop_loss=params.get("stop_loss", base.stop_loss),
        timeout_bars=params.get("timeout_bars", base.timeout_bars),
        trailing=params.get("trailing", base.trailing),
        trailing_offset=params.get("trailing_offset", base.trailing_offset),
        exit_signals=base.exit_signals,
    )


def _summarise(axes: dict, order: list[str], cells: list[dict], scope: str) -> dict:
    traded = [c for c in cells if c["fills"] > 0]
    positive = [c for c in traded if c["net_sum_pct"] > 0]
    nets = sorted(c["net_sum_pct"] for c in traded)
    return {
        "scope": scope,
        "axes": {k: list(v) for k, v in axes.items()},
        "axis_order": order,
        "n_cells": len(cells),
        "cells_with_fills": len(traded),
        "no_fill_cells": len(cells) - len(traded),
        "positive_cells": len(positive),
        "positive_ratio": (len(positive) / len(traded)) if traded else None,
        "median_net_pct": statistics.median(nets) if nets else None,
        "best": max(traded, key=lambda c: c["net_sum_pct"]) if traded else None,
        "worst": min(traded, key=lambda c: c["net_sum_pct"]) if traded else None,
        "cells": cells,
    }
