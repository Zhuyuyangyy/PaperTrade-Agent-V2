"""The random-entry control group and the selectivity comparison.

Same window, same exit rule, same fill model, same costs -- only the entry
timing differs. The comparison is a bootstrap CI on (strategy mean - control
mean); an interval spanning zero means the strategy has not demonstrated it can
beat coin flips, however good its absolute numbers look.

This is the instrument that killed the desk's own conviction (E≈C across six
agents), generalised so any strategy can be held to the same standard.
"""
from __future__ import annotations

import random
import statistics
from collections import Counter

from . import metrics
from .signals import bar_bounds, random_signals
from ..audit.pathsim import Exit, simulate

BOOTSTRAP = 2000


def random_control(candles: list[dict], rule: Exit, spec_obj, entries: dict[int, str], *,
                   n_random: int, seed: int, funding: list[dict] | None = None,
                   bar_range: tuple[int, int] | None = None) -> dict:
    """Replay one random-entry book with the strategy's exact parameters.

    The control mirrors the strategy's DOMINANT side: a long-only book gets a
    long-only control. When a strategy trades both directions, mirroring the
    dominant side is disclosed in the report -- the control is about entry
    timing, not about direction, and pretending otherwise would flatter it.

    bar_range restricts where the random entries may land ([lo, hi] bar
    indices, inclusive). The out-of-sample battery uses it so its control is
    drawn from the same segment as the strategy's out-of-sample entries; the
    effective range travels with the result so the report can disclose it.
    """
    sides = list(entries.values())
    # The dominant side, ties broken by FIRST APPEARANCE -- never by set
    # iteration order: string hashes are randomised per process, and a control
    # whose mirrored side flips between runs of the same spec is not a control.
    side = Counter(sides).most_common(1)[0][0] if sides else "long"
    rand_entries = random_signals(candles, n_random, seed, side=side, bar_range=bar_range)
    sim = simulate(
        candles, rand_entries, rule,
        symbol=spec_obj.symbol, fee_rate=spec_obj.fee_rate,
        funding_series=funding, worst_case=spec_obj.worst_case,
        fill_model=spec_obj.fill_model,
    )
    nets = sim.net_pct_series()
    lo, hi = bar_bounds(candles, bar_range=bar_range)
    return {
        "n_random": len(rand_entries),
        "seed": seed,
        "side": side,
        "bar_range": [lo, hi],
        "fills": sim.closed,
        "unfilled": sim.unfilled_entries,
        # Same accounting identity as the strategy books: fills + unfilled +
        # open == n_random. A random entry the replay never resolved is a fact
        # about the window, and dropping it here would quietly inflate the
        # control's apparent fill rate.
        "open_signals": sim.open_signals,
        "stats": metrics.equity_stats(nets),
    }


def _bootstrap_diff_ci(a: list[float], b: list[float], seed: int = 42) -> tuple[float, float] | None:
    """95% CI for mean(a) - mean(b) by paired-independent resampling."""
    if len(a) < 5 or len(b) < 5:
        return None
    rng = random.Random(seed)
    fmean = statistics.fmean
    diffs = sorted(
        fmean(rng.choices(a, k=len(a))) - fmean(rng.choices(b, k=len(b)))
        for _ in range(BOOTSTRAP)
    )
    return diffs[int(len(diffs) * 0.025)] * 100, diffs[int(len(diffs) * 0.975)] * 100


def compare(strategy_stats: dict, control: dict, seed: int = 42) -> dict:
    """Attach the selectivity verdict to a control run.

    strategy_stats carries the raw fill returns under 'nets' when produced by
    equity_stats; when it does not, only the summary comparison is possible and
    the CI is reported as unavailable rather than invented.
    """
    out = dict(control)
    cs = control["stats"]
    out["strategy_mean_pct"] = strategy_stats.get("mean_pct")
    out["control_mean_pct"] = cs.get("mean_pct")
    if strategy_stats.get("mean_pct") is not None and cs.get("mean_pct") is not None:
        out["diff_pct"] = strategy_stats["mean_pct"] - cs["mean_pct"]
    else:
        out["diff_pct"] = None
    a = strategy_stats.get("nets")
    b = cs.get("nets")
    ci = _bootstrap_diff_ci(a, b, seed=seed) if (a and b) else None
    out["diff_ci"] = list(ci) if ci else None
    out["selectivity"] = (
        "proven" if ci and ci[0] > 0 else
        "refuted" if ci and ci[1] < 0 else
        "unproven" if ci else "unknown"
    )
    return out
