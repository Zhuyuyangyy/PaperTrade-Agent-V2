"""The random-entry control group and the selectivity comparison.

Same window, same exit rule, same fill model, same costs -- only the entry
timing differs. The comparison is a bootstrap CI on (strategy mean - control
mean); an interval spanning zero means the strategy has not demonstrated it can
beat coin flips, however good its absolute numbers look.

This is the instrument that killed the desk's own conviction (E≈C across six
agents), generalised so any strategy can be held to the same standard.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter

from . import metrics
from .signals import bar_bounds, random_signals
from .pathsim import Exit, simulate

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


def buy_and_hold(candles: list[dict], fills: list, spec_obj, entries: dict[int, str], *,
                 strategy_net_pct: float, bar_range: tuple[int, int] | None = None) -> dict:
    """The drift baseline: what holding the coin over the same segment paid.

    A dip-buy book in a rising market can show a positive net that is nothing
    but the coin's own drift. The random-entry control asks "is the entry
    timing better than chance?"; this baseline asks the cruder question "did
    the strategy beat just sitting in the market for as long as it was in?".

    Definitions (all from data already in the run, nothing new assumed):
      * segment      = candles[lo..hi] inclusive (the whole window, or one
                       walk-forward segment's bar range)
      * B&H return   = last close / first open - 1, minus ONE round trip of
                       fees (spec.fee_rate * 2). Mirrored to short-and-hold
                       (1 - last/first) when the book's DOMINANT side is short,
                       the same side rule the random control uses.
      * bars in market = sum over the worst-case fills of (exit bar - entry
                       bar + 1): entry and exit bars both count, matching the
                       B&H span of open-of-first to close-of-last. Overlapping
                       positions add up, the same way the strategy's net sum
                       adds up overlapping trades -- so exposure can exceed 1.
      * exposure     = bars in market / bars in segment
      * matched B&H  = per-bar net drift * bars in market, where the drift is
                       LOGARITHMIC: (ln(last/first) - round-trip fee) / bars
                       in segment, sign-flipped for short-and-hold. Equal to
                       (log B&H net) * exposure. The log form is deliberate:
                       the strategy's net is a SUM of per-trade simple returns
                       (no compounding), and scaling the compounded B&H
                       return by exposure would overstate the hurdle in an
                       uptrend and understate it in a downtrend. Per trade,
                       a short hold's simple return is ~ its log return, so
                       the log drift is the like-for-like per-bar number, and
                       it mirrors exactly for the short side.
      * beats        = strategy net sum (worst case) > matched B&H; None when
                       the strategy has no fills (no evidence either way).

    Funding is not charged to the baseline; the report says so when the
    strategy's own net includes funding. Informational unless the spec opts
    in via falsify_if.net_below_buy_and_hold.
    """
    n = len(candles)
    lo, hi = (0, n - 1) if bar_range is None else (int(bar_range[0]), int(bar_range[1]))
    lo, hi = max(lo, 0), min(hi, n - 1)
    sides = list(entries.values())
    counts = Counter(sides)
    side = counts.most_common(1)[0][0] if sides else "long"
    out = {
        "side": side,
        "mixed_sides": len(counts) > 1,
        "bar_range": [lo, hi],
        "bars_in_segment": max(hi - lo + 1, 0),
        "fee_round_trip_pct": spec_obj.fee_rate * 2 * 100,
        "bh_gross_pct": None,
        "bh_net_pct": None,
        "drift_per_bar_pct": None,
        "fills": len(fills),
        "bars_in_market": sum(f.exit_i - f.entry_i + 1 for f in fills),
        "exposure": None,
        "matched_bh_pct": None,
        "strategy_net_pct": strategy_net_pct,
        "diff_pct": None,
        "beats_buy_and_hold": None,
        "note": None,
    }
    if hi < lo:
        out["note"] = "该段没有 K 线，无法计算买入持有基线"
        return out
    first_open, last_close = candles[lo]["o"], candles[hi]["c"]
    if not first_open or not last_close or first_open < 0 or last_close < 0:
        out["note"] = "该段首开或末收价格非正，无法计算买入持有基线"
        return out
    fee_rt = spec_obj.fee_rate * 2
    ratio = last_close / first_open
    gross = (ratio - 1.0) if side == "long" else (1.0 - ratio)
    log_gross = math.log(ratio) if side == "long" else -math.log(ratio)
    exposure = out["bars_in_market"] / out["bars_in_segment"]
    out["bh_gross_pct"] = gross * 100
    out["bh_net_pct"] = (gross - fee_rt) * 100
    out["drift_per_bar_pct"] = (log_gross - fee_rt) / out["bars_in_segment"] * 100
    out["exposure"] = exposure
    out["matched_bh_pct"] = (log_gross - fee_rt) * exposure * 100
    if fills:
        out["diff_pct"] = strategy_net_pct - out["matched_bh_pct"]
        out["beats_buy_and_hold"] = out["diff_pct"] > 0
    else:
        out["note"] = "策略在该段无成交，无从比较（无证据不算输，也不算赢）"
    return out
