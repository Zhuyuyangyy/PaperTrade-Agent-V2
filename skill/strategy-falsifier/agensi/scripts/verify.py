"""strategy-falsifier: the whole falsification engine in one file.

 Self-contained, stdlib-only, no sibling files, no network. Runs from
 any directory -- including a temporary file, which is exactly where
 Agensi puts it. Built by skill/strategy-falsifier/scripts/
build_agensi.py from the in-repo engine; do not edit by hand.

 Usage:
   python verify.py --demo
   python verify.py --spec spec.json --candles klines.csv --out report.md --json
   python verify.py --compare a.json b.json --out compare.md
   python verify.py --spec spec.json --out report.html
"""
from __future__ import annotations
from dataclasses import dataclass, field
import statistics
import random
import json
from pathlib import Path
from collections import Counter
import itertools
import csv
import sqlite3
import datetime as _dt
import argparse
import sys


# ---------------------------------------------------------------------------
# Synthetic demo series (Agensi single-file build)
#
# The Agensi runtime runs this script from a temporary file with no sibling
# files readable, so the demo candles are GENERATED, not shipped. Same seed,
# same bars, every run. Real audits pass --candles with a real CSV; the
# report's repro section always names the actual data source.
# ---------------------------------------------------------------------------
def _synthetic_demo_candles(n: int = 2000, seed: int = 42) -> list[dict]:
    """Deterministic synthetic 1h OHLCV: mild upward drift, fat tails.

    Parameters chosen so the built-in demo rule (RSI(14)<30) fires ~90 times
    over the window -- comfortably past the demo spec's min_fills=30 bar, so
    the demo shows a real verdict instead of an INCONCLUSIVE shrug.
    """
    import random as _random

    rng = _random.Random(seed)
    out, px = [], 100.0
    t0 = 1_700_000_000_000
    for i in range(n):
        px *= 1.0 + rng.gauss(0.0001, 0.012)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.004))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.003)))
        low = min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.003)))
        out.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": low, "c": c, "v": 1000.0})
    return out


# ==== from pathsim.py ==================================================
"""Path-aware trade simulation: walk the price, do not sample it.

The single most expensive lesson in this repo's lineage: an early analysis of
CascadeFader showed +469% / 91% win rate by measuring whether price ever
*touched* a take-profit within a horizon. Re-simulated bar by bar with proper
SL/TP/timeout precedence -- where a bar that contains both a stop and a target
resolves to the stop -- the same signals returned +132% / PF 1.62. Nothing
about the strategy changed; only the honesty of the measurement did.

So this simulator resolves every bar in a fixed, disclosed order:

    worst case (default): if a bar contains both the stop and the target,
        the stop fires first. Conservative: it can only understate a strategy.
    best case: the target fires first. Reported alongside, never alone.

Both are printed for every run. A strategy that only works under the
optimistic assumption is telling you something.
"""

from dataclasses import dataclass, field


@dataclass
class Exit:
    """When a position gives up."""

    take_profit: float | None = None      # fraction, e.g. 0.03
    stop_loss: float | None = None        # fraction, negative, e.g. -0.08
    timeout_bars: int | None = None       # hard exit after N bars, any price
    trailing: float | None = None         # giveback from peak, e.g. 0.03
    trailing_offset: float | None = None  # activate trail only after this gain
    exit_signals: dict[int, str] | None = None  # bar index -> reason, fills at close


@dataclass
class Fill:
    """One completed round trip, with the toll itemised."""

    symbol: str
    side: str                    # 'long' | 'short'
    entry_i: int
    entry_t: int
    entry_price: float
    exit_i: int
    exit_t: int
    exit_price: float
    reason: str                  # take_profit | stop_loss | timeout | trailing | end_of_data
    gross_pct: float             # price move, before any cost
    fee_pct: float               # round-trip fee as a fraction of notional
    funding_pct: float           # funding paid (positive = received)
    net_pct: float               # gross - fee + funding
    bars_held: int


@dataclass
class SimResult:
    fills: list[Fill] = field(default_factory=list)
    open_signals: int = 0        # entries that never resolved
    unfilled_entries: int = 0    # maker limit orders the market never crossed
    bars: int = 0

    @property
    def closed(self) -> int:
        return len(self.fills)

    def net_pct_series(self) -> list[float]:
        return [f.net_pct for f in self.fills]


def simulate(
    candles: list[dict],
    entries: dict[int, str],        # bar index -> 'long' | 'short'
    exit_rule: Exit,
    *,
    symbol: str = "",
    fee_rate: float = 0.0004,       # 0.04% per side (Binance futures taker)
    funding_series: list[dict] | None = None,   # [{'t': ms, 'rate': per 8h}]
    worst_case: bool = True,
    max_entries: int | None = None,
    fill_model: str = "taker",      # 'taker' (market) or 'maker' (limit, crossed)
) -> SimResult:
    """Replay entries over candles, resolving each bar in a disclosed order.

    fill_model='maker' prices entries and take-profits as LIMIT orders: they
    fill only when the market trades THROUGH the limit price (not merely
    touches it), they fill AT the limit price (a price improvement over the
    market order), and an entry that never gets crossed never becomes a trade
    at all -- counted as unfilled, not silently dropped. Stops stay market
    orders: a resting stop is exactly the order that does not fill when it
    matters, so modelling it as one would flatter the strategy.

    funding: a position pays/receives the funding rate in force at each 8h
    boundary it crosses, prorated by how much of the interval it was open.
    """
    result = SimResult(bars=len(candles))
    funding = funding_series or []
    taken = 0
    stats = {"unfilled": 0}

    for i, side in sorted(entries.items()):
        if i >= len(candles) - 1:
            # the window ends before the fill bar exists: unresolved, and it
            # must be counted rather than dropped
            result.open_signals += 1
            continue
        if max_entries is not None and taken >= max_entries:
            break
        taken += 1
        before = stats["unfilled"]
        fill = _one_trade(
            candles, i, side, exit_rule, symbol=symbol,
            fee_rate=fee_rate, funding=funding, worst_case=worst_case,
            fill_model=fill_model, stats=stats,
        )
        if fill is not None:
            result.fills.append(fill)
        elif stats["unfilled"] > before:
            # A maker limit the market never crossed: already counted as
            # unfilled. Counting it as open too would double-book the same
            # signal and quietly shrink the fill rate.
            pass
        else:
            result.open_signals += 1
    result.unfilled_entries = stats["unfilled"]
    return result


def _crossed(bar: dict, price: float, side: str) -> bool:
    """Did the market trade THROUGH the limit price inside this bar?

    A touch is not enough: a resting order is filled by price crossing it.
    """
    return bar["l"] < price if side == "long" else bar["h"] > price


def _worse_exit(tp_price: float, sl_price: float, side: str, sl_from_trail: bool):
    """Of the two exits that fired inside this bar, the one that pays LESS.

    A long pays less at a lower price; a short pays less at a higher price.
    The stop slot is not automatically the worse one -- an armed trailing
    stop is a profit-taking level that can beat the take-profit.
    """
    if side == "long":
        if sl_price <= tp_price:
            return ("trailing" if sl_from_trail else "stop_loss"), sl_price
        return "take_profit", tp_price
    if sl_price >= tp_price:
        return ("trailing" if sl_from_trail else "stop_loss"), sl_price
    return "take_profit", tp_price


def _better_exit(tp_price: float, sl_price: float, side: str, sl_from_trail: bool):
    """Of the two exits that fired inside this bar, the one that pays MORE."""
    if side == "long":
        if sl_price > tp_price:
            return ("trailing" if sl_from_trail else "stop_loss"), sl_price
        return "take_profit", tp_price
    if sl_price < tp_price:
        return ("trailing" if sl_from_trail else "stop_loss"), sl_price
    return "take_profit", tp_price


def _one_trade(candles, i, side, rule: Exit, *, symbol, fee_rate, funding, worst_case,
               fill_model="taker", stats=None):
    signal_bar = candles[i]
    # Entry fills at the next bar's open: the signal is only known once the
    # bar closes, and acting inside the bar that produced it is look-ahead.
    j = i + 1
    if j >= len(candles):
        return None

    if fill_model == "maker":
        # The limit order rests at the signal bar's close. If the next bar
        # never trades through it, the signal simply did not become a trade.
        limit = signal_bar["c"]
        if not limit:
            return None
        if not _crossed(candles[j], limit, side):
            if stats is not None:
                stats["unfilled"] += 1
            return None
        entry_price = limit      # filled at OUR price: a price improvement
    else:
        entry_price = candles[j]["o"]
    entry_t = candles[j]["t"]
    if not entry_price:
        return None

    peak = entry_price
    trail_armed = False
    funding_paid = 0.0
    last_funding_t = entry_t

    for k in range(j, len(candles)):
        bar = candles[k]
        high, low = bar["h"], bar["l"]
        held_bars = k - j

        # --- funding at every 8h boundary crossed ---
        if funding:
            for f in funding:
                ft = f["t"]
                if last_funding_t < ft <= bar["t"]:
                    covered = min(ft, bar["t"]) - last_funding_t
                    span = max(bar["t"] - last_funding_t, 1)
                    # Positive funding: longs PAY, shorts COLLECT. funding_pct
                    # is signed as net received, so a long flips it negative.
                    funding_paid += f["rate"] * (covered / span) * (
                        -1 if side == "long" else 1
                    )
                    last_funding_t = ft
        last_funding_t = max(last_funding_t, entry_t)

        # --- where would each exit fire inside this bar? ---
        tp_price = sl_price = None
        if rule.take_profit is not None:
            tp_price = entry_price * (1 + rule.take_profit) if side == "long" \
                else entry_price * (1 - rule.take_profit)
        if rule.stop_loss is not None:
            sl_price = entry_price * (1 + rule.stop_loss) if side == "long" \
                else entry_price * (1 - rule.stop_loss)
        # Once the trail is armed it REPLACES the stop as the binding level,
        # and it is a profit-taking mechanism, not a loss: label its exit
        # 'trailing', not 'stop_loss', or the by-reason bookkeeping reads a
        # winning giveback as a losing stop. Reset per bar because sl_price is
        # recomputed per bar.
        sl_from_trail = False
        if rule.trailing is not None:
            peak = max(peak, high) if side == "long" else min(peak, low)
            gain = (peak / entry_price - 1) if side == "long" else (1 - peak / entry_price)
            if rule.trailing_offset is not None and gain >= rule.trailing_offset:
                trail_armed = True
            if trail_armed:
                giveback = rule.trailing
                trail_price = peak * (1 - giveback) if side == "long" else peak * (1 + giveback)
                if sl_price is None or (side == "long" and trail_price > sl_price) \
                        or (side == "short" and trail_price < sl_price):
                    sl_price = trail_price
                    sl_from_trail = True

        if fill_model == "maker":
            # A take-profit is the OPPOSITE order from the entry: a long's TP
            # rests ABOVE the market as a sell, so it needs the high to cross
            # it -- reusing the entry's cross direction here silently inverted
            # the whole maker model.
            tp_hit = tp_price is not None and (
                (high > tp_price) if side == "long" else (low < tp_price)
            )
        else:
            tp_hit = tp_price is not None and (high >= tp_price if side == "long" else low <= tp_price)
        sl_hit = sl_price is not None and (low <= sl_price if side == "long" else high >= sl_price)

        if tp_hit and sl_hit:
            # The whole point of this module: disclose which one you assume.
            # BUT "the stop" is not always the worse exit: once a trailing
            # stop arms it becomes a profit-taking level that can beat the
            # take-profit (the FreqBot audit showed worst-case +89.86% vs
            # best-case -27.22% -- an inversion that made the two labels
            # meaningless). So pick by what the exit is actually WORTH for
            # this side, not by which slot the price came from.
            if worst_case:
                reason, price = _worse_exit(tp_price, sl_price, side, sl_from_trail)
            else:
                reason, price = _better_exit(tp_price, sl_price, side, sl_from_trail)
        elif tp_hit:
            reason, price = "take_profit", tp_price
        elif sl_hit:
            reason, price = ("trailing" if sl_from_trail else "stop_loss"), sl_price
        elif rule.timeout_bars is not None and held_bars >= rule.timeout_bars:
            reason, price = "timeout", bar["c"]
        elif rule.exit_signals and k in rule.exit_signals:
            # A signal exit is confirmed on the close, so anything that already
            # fired inside the bar wins; this is the last check for a reason.
            reason, price = rule.exit_signals[k], bar["c"]
        else:
            continue

        gross = (price / entry_price - 1) if side == "long" else (1 - price / entry_price)
        return Fill(
            symbol=symbol, side=side, entry_i=j, entry_t=entry_t, entry_price=entry_price,
            exit_i=k, exit_t=bar["t"], exit_price=price, reason=reason,
            gross_pct=round(gross, 6), fee_pct=round(fee_rate * 2, 6),
            funding_pct=round(funding_paid, 6),
            net_pct=round(gross - fee_rate * 2 + funding_paid, 6),
            bars_held=k - j,
        )

    # Ran out of candles: the signal never resolved. That is a fact about the
    # data window, not a trade, and it is counted rather than dropped.
    return None

# ==== from metrics.py ==================================================
"""Equity statistics over a net-return series.

One assumption, stated everywhere it matters: every fill is equal weight and
the series is the fills in chronological order. That is the right frame for a
first look -- compounding, position sizing, and correlation across concurrent
positions are the strategy's job, not the measuring stick's.
"""

import statistics


def _max_dd(series: list[float]) -> float:
    """Max peak-to-trough drawdown of the compounded equity, as a negative
    fraction. Order matters: this is the realized curve of the given order."""
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for v in series:
        equity *= 1.0 + v
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    return max_dd


def equity_stats(nets: list[float]) -> dict:
    """nets: per-trade net return as a fraction (0.03 = +3%), chronological.

    max_dd_pct is the REALIZED drawdown of the chronological order -- the
    number a trader would have lived through. max_dd_worst_order_pct is the
    same book replayed losses-first: a bound, not a history, reported
    separately so neither can be mistaken for the other.
    """
    if not nets:
        return {"n": 0, "sum_pct": 0.0, "mean_pct": 0.0, "median_pct": 0.0,
                "win_pct": 0.0, "profit_factor": None, "max_dd_pct": 0.0,
                "max_dd_worst_order_pct": 0.0,
                "expectancy_pct": 0.0, "best_pct": 0.0, "worst_pct": 0.0}
    s = sorted(nets)
    n = len(s)
    gains = sum(v for v in s if v > 0)
    losses = -sum(v for v in s if v < 0)
    return {
        "n": n,
        "sum_pct": sum(s) * 100,
        "mean_pct": statistics.fmean(s) * 100,
        "median_pct": (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2) * 100,
        "win_pct": sum(1 for v in s if v > 0) / n * 100,
        "profit_factor": (gains / losses) if losses > 0 else None,
        "max_dd_pct": _max_dd(nets) * 100,
        "max_dd_worst_order_pct": _max_dd(s) * 100,
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

# ==== from signals.py ==================================================
"""Signal sources: rule-based, random (control), and CSV.

The random generator is not a toy -- it IS the control group. It draws entry
bars uniformly from the same window with the same count as the strategy under
test, so the only difference between the two books is where the entries came
from. Anything the strategy shows beyond that difference is selectivity;
anything it does not show is noise with extra steps.
"""

import random


# Bars reserved for indicator warmup at the start of any window. Random
# entries never draw from this zone: an entry there could not have been
# signalled by any indicator-based strategy, which would make the control
# easier than the strategy by construction.
CONTROL_WARMUP = 30


def rsi(closes: list[float], period: int) -> list[float | None]:
    """Wilder RSI, pure python, aligned to the input (None while warming up).

    Deliberately not pandas: the engine's data path is plain dicts and the
    indicator is small enough to eyeball against a reference.
    """
    if period < 2:
        raise ValueError("RSI period 至少为 2")
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period, len(closes)):
        if i > period:
            d = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def rule_signals(candles: list[dict], *, rule: str, period: int, threshold: float,
                 side: str = "long") -> dict[int, str]:
    """Entries from a single configurable indicator rule.

    The signal fires on bar i (computed from data up to and including bar i);
    pathsim fills at bar i+1, so there is no look-ahead. Deliberately a tiny
    rule set: the engine is for testing hypotheses, not for searching them.
    """
    closes = [c["c"] for c in candles]
    values = rsi(closes, period)
    entries: dict[int, str] = {}
    for i, v in enumerate(values):
        if v is None:
            continue
        if rule == "rsi_below" and v < threshold:
            entries[i] = side
        elif rule == "rsi_above" and v > threshold:
            entries[i] = side
    return entries


def bar_bounds(candles: list[dict], warmup: int = CONTROL_WARMUP,
               bar_range: tuple[int, int] | None = None) -> tuple[int, int]:
    """Effective inclusive [lo, hi] bar range for random entries.

    Default (bar_range=None): the historical convention [warmup, len-2].
    An explicit bar_range lets a segment battery draw its control from the
    same segment as the strategy's entries -- a control drawn from the whole
    window would not be a control for the segment, it would be a different
    experiment wearing the same label.
    """
    hi = len(candles) - 2
    if bar_range is None:
        lo = warmup
        if hi <= lo:
            raise ValueError(f"数据窗口太短（{len(candles)} 根），无法生成随机信号")
        return lo, hi
    lo, hi_req = int(bar_range[0]), int(bar_range[1])
    lo = max(lo, 0)  # a negative index would silently wrap to the last candle
    hi = min(hi, hi_req)
    if hi < lo:
        raise ValueError(f"随机入场范围 [{lo}, {hi_req}] 在窗口内为空或倒退")
    return lo, hi


def random_signals(candles: list[dict], n: int, seed: int, side: str = "long",
                   warmup: int = CONTROL_WARMUP, bar_range: tuple[int, int] | None = None) -> dict[int, str]:
    """Uniform random entry bars in [warmup, len-2], with replacement allowed.

    With replacement: the control is about entry *timing* distribution, not
    about cardinality. Two random signals may share a bar; that is fine and it
    is disclosed in the report.
    """
    lo, hi = bar_bounds(candles, warmup=warmup, bar_range=bar_range)
    rng = random.Random(seed)
    return {rng.randint(lo, hi): side for _ in range(n)}


def signal_stats(entries: dict[int, str], candles: list[dict]) -> dict:
    """Basic provenance of an entry set, for the report header."""
    if not entries:
        return {"n": 0}
    idx = sorted(entries)
    return {
        "n": len(entries),
        "long": sum(1 for s in entries.values() if s == "long"),
        "short": sum(1 for s in entries.values() if s == "short"),
        "first_bar": idx[0],
        "last_bar": idx[-1],
    }

# ==== from spec.py =====================================================
"""The strategy spec: everything the engine needs, declared up front.

A spec is a small JSON document. It exists so that the falsification criteria
are written BEFORE anyone sees a single number -- the same discipline the
research arms used (`_arm` fields pinned in config before the run). A spec
without `falsify_if` is accepted and measured in full, but the verdict comes
back NOT_FALSIFIED with the criteria table saying "no rules were declared" --
an audit without pre-declared kill criteria is a measurement, not a test.

Example:

    {
      "name": "rsi-dip-btc-1h",
      "hypothesis": "BTC 1h RSI<30 后的均值回复",
      "symbol": "BTCUSDT", "interval": "1h", "venue": "futures",
      "fee_rate": 0.0004, "fill_model": "taker", "funding": true,
      "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
      "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30},
      "control": {"enabled": true, "n_random": 200, "seed": 42},
      "walk_forward": {"enabled": true, "folds": 3},
      "param_grid": {"axes": {"take_profit": [0.02, 0.03, 0.04],
                              "stop_loss": [-0.06, -0.08, -0.10]}},
      "fee_multiples": [1, 2, 5],
      "falsify_if": {
        "control_lift_ci_includes_zero": true,
        "net_negative_at_fee_multiple": 2,
        "worst_case_net_negative": false,
        "grid_positive_ratio_min": 0.5,
        "min_fills": 30
      }
    }
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


class SpecError(ValueError):
    """Raised when a spec is malformed. The message says which field and why."""


VALID_FILL_MODELS = ("taker", "maker")
VALID_RULES = ("rsi_below", "rsi_above")
VALID_SIGNAL_KINDS = ("csv", "random", "rule")
# Exit parameters the sensitivity grid may move. Signal parameters are NOT
# gridable here: changing them changes the entry set itself, which is a
# different experiment (and a different control group), not a sensitivity.
GRIDABLE_PARAMS = ("take_profit", "stop_loss", "timeout_bars", "trailing")


@dataclass
class ExitSpec:
    take_profit: float | None = None
    stop_loss: float | None = None
    timeout_bars: int | None = None
    trailing: float | None = None
    trailing_offset: float | None = None
    exit_signals_path: str | None = None   # CSV(t[,reason]): bar-close exit signals

    def validate(self) -> None:
        if self.take_profit is not None and self.take_profit <= 0:
            raise SpecError("exit.take_profit 必须是正数（小数，如 0.03 表示 +3%）")
        if self.stop_loss is not None and self.stop_loss >= 0:
            raise SpecError("exit.stop_loss 必须是负数（如 -0.08 表示 -8%）")
        if self.timeout_bars is not None and self.timeout_bars < 1:
            raise SpecError("exit.timeout_bars 至少为 1")
        if all(v is None for v in (self.take_profit, self.stop_loss, self.timeout_bars,
                                   self.trailing, self.exit_signals_path)):
            raise SpecError("exit 至少要声明一个退出条件（take_profit / stop_loss / timeout_bars / trailing / exit_signals_path）")


@dataclass
class SignalSpec:
    kind: str = "rule"
    path: str | None = None
    side: str = "long"
    n_random: int = 200
    seed: int = 42
    rule: str | None = None
    period: int = 14
    threshold: float = 30.0

    def validate(self) -> None:
        if self.kind not in VALID_SIGNAL_KINDS:
            raise SpecError(f"signals.kind 必须是 {VALID_SIGNAL_KINDS} 之一，得到 {self.kind!r}")
        if self.kind == "csv" and not self.path:
            raise SpecError("signals.kind='csv' 时必须提供 signals.path")
        if self.kind == "rule":
            if self.rule not in VALID_RULES:
                raise SpecError(f"signals.rule 必须是 {VALID_RULES} 之一，得到 {self.rule!r}")
            if self.period < 2:
                raise SpecError("signals.period 至少为 2")
        if self.side not in ("long", "short"):
            raise SpecError("signals.side 必须是 'long' 或 'short'")


@dataclass
class ControlSpec:
    enabled: bool = True
    n_random: int = 200
    seed: int = 42

    def validate(self) -> None:
        if self.n_random < 10:
            raise SpecError("control.n_random 至少为 10，否则 bootstrap 没有意义")


@dataclass
class WalkForwardSpec:
    """Time-split the window into N folds; the last fold is out-of-sample.

    Declaring the block is the intent (same convention as `control`). The
    industry's number-one cause of backtest->live decay is in-sample overfit
    (median -14.3pp); a strategy that only works on the bars its parameters
    were chosen on is a story, not an edge. The engine cannot know how the
    parameters were chosen -- that provenance is disclosed as a limit, not
    assumed away.
    """

    enabled: bool = False
    folds: int = 3

    def validate(self) -> None:
        if self.folds < 2:
            raise SpecError("walk_forward.folds 至少为 2（1 折没有样本外段，walk-forward 失去意义）")


@dataclass
class ParamGridSpec:
    """Grid the exit parameters and replay EVERY cell: an edge that exists
    only at one exact take-profit/stop-loss combination is a curve fit, not
    an edge.

    The grid values are declared in the spec BEFORE anyone sees a number --
    the same discipline as `control` and `walk_forward`; a grid chosen after
    reading the report is marketing. Each cell replays the SAME entries
    under the SAME headline convention (the spec's worst/best-case flag,
    base fee); only the declared axes move. At most two axes: the report
    renders the grid as a two-dimensional heat table, and a three-axis
    product is a reading problem, not a measurement problem.
    """

    enabled: bool = False
    axes: dict[str, list] = field(default_factory=dict)
    max_cells: int = 64

    def validate(self) -> None:
        if not self.axes:
            raise SpecError(
                "param_grid 至少要声明一个参数轴（如 \"take_profit\": [0.02, 0.03]）"
            )
        if len(self.axes) > 2:
            raise SpecError(
                f"param_grid 最多支持 2 个参数轴（热力表是二维的），得到 {len(self.axes)} 个："
                f"{list(self.axes)}——想扫更多维度就分多次运行，每次的规则都事前声明"
            )
        total = 1
        for key, values in self.axes.items():
            if key not in GRIDABLE_PARAMS:
                raise SpecError(f"param_grid 参数名必须是 {GRIDABLE_PARAMS} 之一，得到 {key!r}")
            if len(values) < 2:
                raise SpecError(f"param_grid 轴 {key!r} 至少 2 个值（1 个值不成网格）")
            if len(set(values)) != len(values):
                raise SpecError(f"param_grid 轴 {key!r} 的值有重复：{values}")
            if key in ("take_profit", "trailing") and any(v <= 0 for v in values):
                raise SpecError(f"param_grid.{key} 的值必须是正数，得到 {values}")
            if key == "stop_loss" and any(v >= 0 for v in values):
                raise SpecError(f"param_grid.stop_loss 的值必须是负数，得到 {values}")
            if key == "timeout_bars" and any(int(v) != v or v < 1 for v in values):
                raise SpecError(f"param_grid.timeout_bars 的值必须是 ≥1 的整数，得到 {values}")
            total *= len(values)
        if total > self.max_cells:
            raise SpecError(
                f"param_grid 共 {total} 格，超过 max_cells={self.max_cells}——"
                f"网格是组合爆炸的，先缩小轴范围，或想清楚再看再调大 max_cells"
            )


@dataclass
class Spec:
    name: str
    symbol: str
    interval: str
    venue: str = "spot"
    fee_rate: float = 0.0004
    fill_model: str = "taker"
    worst_case: bool = True
    funding: bool = False
    exit: ExitSpec = field(default_factory=ExitSpec)
    signals: SignalSpec = field(default_factory=SignalSpec)
    control: ControlSpec = field(default_factory=ControlSpec)
    walk_forward: WalkForwardSpec = field(default_factory=WalkForwardSpec)
    param_grid: ParamGridSpec = field(default_factory=ParamGridSpec)
    fee_multiples: tuple[float, ...] = (1.0, 2.0, 5.0)
    falsify_if: dict = field(default_factory=dict)
    hypothesis: str = ""
    source: str = ""
    # Optional: the spec's own candle CSV (columns t,o,h,l,c). Relative paths
    # resolve against the spec's directory, like signals.path. This is what
    # lets --compare mix strategies over DIFFERENT data sources: each spec
    # carries its own series, and the comparison discloses the window of each.
    candles_path: str | None = None
    # Optional: the spec's own funding series (columns t,rate). Same resolution
    # rules as candles_path; together they make a published case replay
    # identically on a machine with no market cache.
    funding_path: str | None = None
    # Free-text port notes for strategies ported from somewhere else: what was
    # replicated verbatim, what was approximated, what was NOT ported. Rendered
    # as section 0 so a regenerated report never loses them (hand-editing the
    # markdown after a run is how artifacts drift from the engine).
    port_notes: str = ""

    def validate(self) -> None:
        if not self.name:
            raise SpecError("name 不能为空")
        if not self.symbol:
            raise SpecError("symbol 不能为空")
        if self.fill_model not in VALID_FILL_MODELS:
            raise SpecError(f"fill_model 必须是 {VALID_FILL_MODELS} 之一，得到 {self.fill_model!r}")
        if self.venue not in ("spot", "futures"):
            raise SpecError("venue 必须是 'spot' 或 'futures'")
        if not 0 <= self.fee_rate < 0.01:
            raise SpecError("fee_rate 是单边小数费率（如 0.0004 = 4bps），必须在 [0, 0.01) 内")
        if self.funding and self.venue != "futures":
            raise SpecError("funding 只对 futures 场所开放（现货没有资金费）")
        if not self.fee_multiples:
            raise SpecError("fee_multiples 不能为空")
        if 1.0 not in self.fee_multiples:
            raise SpecError("fee_multiples 必须包含 1（基准成本），否则敏感性表没有锚点")
        min_ratio = self.falsify_if.get("grid_positive_ratio_min")
        if min_ratio is not None:
            try:
                min_ratio = float(min_ratio)
            except (TypeError, ValueError):
                raise SpecError("falsify_if.grid_positive_ratio_min 必须是 (0, 1] 内的数字")
            if not 0 < min_ratio <= 1:
                raise SpecError(
                    f"falsify_if.grid_positive_ratio_min 必须在 (0, 1] 内，得到 {min_ratio}"
                )
        self.exit.validate()
        self.signals.validate()
        self.control.validate()
        self.walk_forward.validate()
        # an absent block is "not declared"; a declared block must be valid
        if self.param_grid.enabled:
            self.param_grid.validate()

    # -- (de)serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "source": self.source,
            "candles_path": self.candles_path,
            "funding_path": self.funding_path,
            "port_notes": self.port_notes,
            "symbol": self.symbol,
            "interval": self.interval,
            "venue": self.venue,
            "fee_rate": self.fee_rate,
            "fill_model": self.fill_model,
            "worst_case": self.worst_case,
            "funding": self.funding,
            "exit": {
                "take_profit": self.exit.take_profit,
                "stop_loss": self.exit.stop_loss,
                "timeout_bars": self.exit.timeout_bars,
                "trailing": self.exit.trailing,
                "trailing_offset": self.exit.trailing_offset,
                "exit_signals_path": self.exit.exit_signals_path,
            },
            "signals": {
                "kind": self.signals.kind,
                "path": self.signals.path,
                "side": self.signals.side,
                "n_random": self.signals.n_random,
                "seed": self.signals.seed,
                "rule": self.signals.rule,
                "period": self.signals.period,
                "threshold": self.signals.threshold,
            },
            "control": {
                "enabled": self.control.enabled,
                "n_random": self.control.n_random,
                "seed": self.control.seed,
            },
            "walk_forward": {
                "enabled": self.walk_forward.enabled,
                "folds": self.walk_forward.folds,
            },
            "param_grid": {
                "enabled": self.param_grid.enabled,
                "axes": {k: list(v) for k, v in self.param_grid.axes.items()},
                "max_cells": self.param_grid.max_cells,
            },
            "fee_multiples": list(self.fee_multiples),
            "falsify_if": self.falsify_if,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Spec":
        if not isinstance(d, dict):
            raise SpecError("spec 必须是一个 JSON 对象")
        for key in ("name", "symbol", "interval", "exit", "signals"):
            if key not in d:
                raise SpecError(f"spec 缺少必填字段 {key!r}")
        exit_d = d.get("exit") or {}
        sig_d = d.get("signals") or {}
        ctrl_d = d.get("control") or {}
        wf_d = d.get("walk_forward") or {}
        pg_d = d.get("param_grid") or {}
        spec_obj = cls(
            name=str(d["name"]),
            symbol=str(d["symbol"]).upper(),
            interval=str(d["interval"]),
            venue=str(d.get("venue", "spot")),
            fee_rate=float(d.get("fee_rate", 0.0004)),
            fill_model=str(d.get("fill_model", "taker")),
            worst_case=bool(d.get("worst_case", True)),
            funding=bool(d.get("funding", False)),
            exit=ExitSpec(
                take_profit=_opt_float(exit_d.get("take_profit"), "exit.take_profit"),
                stop_loss=_opt_float(exit_d.get("stop_loss"), "exit.stop_loss"),
                timeout_bars=_opt_int(exit_d.get("timeout_bars"), "exit.timeout_bars"),
                trailing=_opt_float(exit_d.get("trailing"), "exit.trailing"),
                trailing_offset=_opt_float(exit_d.get("trailing_offset"), "exit.trailing_offset"),
                exit_signals_path=exit_d.get("exit_signals_path"),
            ),
            signals=SignalSpec(
                kind=str(sig_d.get("kind", "rule")),
                path=sig_d.get("path"),
                side=str(sig_d.get("side", "long")),
                n_random=int(sig_d.get("n_random", 200)),
                seed=int(sig_d.get("seed", 42)),
                rule=sig_d.get("rule"),
                period=int(sig_d.get("period", 14)),
                threshold=float(sig_d.get("threshold", 30.0)),
            ),
            control=ControlSpec(
                enabled=bool(ctrl_d.get("enabled", True)),
                n_random=int(ctrl_d.get("n_random", 200)),
                seed=int(ctrl_d.get("seed", 42)),
            ),
            walk_forward=WalkForwardSpec(
                enabled=bool(wf_d.get("enabled", True)) if "walk_forward" in d else False,
                folds=int(wf_d.get("folds", 3)),
            ),
            param_grid=ParamGridSpec(
                enabled=bool(pg_d.get("enabled", True)) if "param_grid" in d else False,
                axes=_grid_axes(pg_d.get("axes") or {}),
                max_cells=int(pg_d.get("max_cells", 64)),
            ),
            fee_multiples=tuple(float(m) for m in d.get("fee_multiples", (1.0, 2.0, 5.0))),
            falsify_if=dict(d.get("falsify_if") or {}),
            hypothesis=str(d.get("hypothesis", "")),
            source=str(d.get("source", "")),
            candles_path=(str(d["candles_path"]) if d.get("candles_path") else None),
            funding_path=(str(d["funding_path"]) if d.get("funding_path") else None),
            port_notes=str(d.get("port_notes", "")),
        )
        spec_obj.validate()
        return spec_obj

    @classmethod
    def load(cls, path: str | Path) -> "Spec":
        p = Path(path)
        if not p.is_file():
            raise SpecError(f"spec 文件不存在: {p}")
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SpecError(f"spec 不是合法 JSON: {exc}") from exc
        return cls.from_dict(d)


def _opt_float(v, field_name: str) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{field_name} 必须是数字或 null") from exc


def _opt_int(v, field_name: str) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{field_name} 必须是整数或 null") from exc


def _grid_axes(raw: dict) -> dict:
    """Coerce declared grid axis values to their parameter's type (bar counts
    are ints; the rest are fractions). A bad value is a SpecError naming the
    axis, never a silent coercion."""
    axes: dict = {}
    for key, values in raw.items():
        if not isinstance(values, (list, tuple)) or not values:
            raise SpecError(f"param_grid 轴 {key!r} 必须是非空数组，得到 {values!r}")
        if key == "timeout_bars":
            out = []
            for v in values:
                f = _opt_float(v, f"param_grid.{key}")
                if f is None or int(f) != f or f < 1:
                    raise SpecError(
                        f"param_grid.timeout_bars 的值必须是 ≥1 的整数，得到 {v!r}"
                    )
                out.append(int(f))
            axes[key] = out
        else:
            axes[key] = [_opt_float(v, f"param_grid.{key}") for v in values]
            if any(v is None for v in axes[key]):
                raise SpecError(f"param_grid 轴 {key!r} 含有 null 值：{values}")
    return axes

# ==== from control.py ==================================================
"""The random-entry control group and the selectivity comparison.

Same window, same exit rule, same fill model, same costs -- only the entry
timing differs. The comparison is a bootstrap CI on (strategy mean - control
mean); an interval spanning zero means the strategy has not demonstrated it can
beat coin flips, however good its absolute numbers look.

This is the instrument that killed the desk's own conviction (E≈C across six
agents), generalised so any strategy can be held to the same standard.
"""

import random
import statistics
from collections import Counter


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
        "stats": equity_stats(nets),
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

# ==== from excursions.py ===============================================
"""Barrier reachability: are the exit levels prices the market actually visits?

A take-profit at +3% and a stop at -8% are not parameters, they are claims
about the world: "inside my hold window, price travels at least this far this
often". This module checks those claims directly on the candles, independent
of the simulator, so a strategy whose barriers are never touched shows up as a
lottery ticket instead of an edge.

Entry price convention: the next bar's open for taker fills (the signal is only
knowable at the bar close), the signal bar's close for maker limit fills. Both
are disclosed in the report; the maker convention is an approximation of the
limit price, the crossing test itself lives in pathsim.
"""



def barrier_reach(candles: list[dict], entries: dict[int, str], rule: Exit, *,
                  fill_model: str = "taker", horizon_bars: int | None = None) -> dict:
    """For every signal, measure MFE/MAE inside its hold window.

    The hold window ends at the first of: the simulator's exit firing,
    timeout_bars, or the end of data -- the same window pathsim would use, so
    the percentages describe the trades the replay actually took.
    """
    tp, sl = rule.take_profit, rule.stop_loss
    cap = horizon_bars if horizon_bars is not None else rule.timeout_bars
    mfes, maes, reached_tp, reached_sl, neither = [], [], 0, 0, 0
    n_usable = 0

    for i, side in sorted(entries.items()):
        j = i + 1
        if j >= len(candles):
            continue
        if fill_model == "maker":
            limit = candles[i]["c"]
            if not limit or not _crossed(candles[j], limit, side):
                continue  # never filled: not a trade, not a sample
            entry = limit
        else:
            entry = candles[j]["o"]
        if not entry:
            continue
        n_usable += 1
        mfe = mae = 0.0
        hit_tp = hit_sl = False
        for k in range(j, len(candles)):
            bar = candles[k]
            if side == "long":
                mfe = max(mfe, bar["h"] / entry - 1.0)
                mae = min(mae, bar["l"] / entry - 1.0)
                if tp is not None and bar["h"] >= entry * (1 + tp):
                    hit_tp = True
                if sl is not None and bar["l"] <= entry * (1 + sl):
                    hit_sl = True
            else:
                mfe = max(mfe, 1.0 - bar["l"] / entry)
                mae = min(mae, 1.0 - bar["h"] / entry)
                if tp is not None and bar["l"] <= entry * (1 - tp):
                    hit_tp = True
                if sl is not None and bar["h"] >= entry * (1 - sl):
                    hit_sl = True
            if (hit_tp and hit_sl) or (cap is not None and k - j >= cap):
                break
        mfes.append(mfe)
        maes.append(mae)
        if hit_tp:
            reached_tp += 1
        if hit_sl:
            reached_sl += 1
        if not hit_tp and not hit_sl:
            neither += 1

    if not n_usable:
        return {"n": 0, "note": "没有可用的入场（maker 限价单从未被穿越）"}

    def _pct(vals: list[float], q: float) -> float:
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * q))] * 100

    return {
        "n": n_usable,
        "tp_level_pct": tp * 100 if tp is not None else None,
        "sl_level_pct": sl * 100 if sl is not None else None,
        "reached_tp_pct": reached_tp / n_usable * 100,
        "reached_sl_pct": reached_sl / n_usable * 100,
        "reached_neither_pct": neither / n_usable * 100,
        "mfe": {"median": _pct(mfes, 0.5), "p90": _pct(mfes, 0.9)},
        "mae": {"median": _pct(maes, 0.5), "p10": _pct(maes, 0.1)},
        "note": (
            "MFE/MAE 按成交后窗口实测；taker 入场价取信号次根开盘，maker 取信号根收盘（近似挂单价）。"
            "窗口止于退出触发、超时或数据结束。"
        ),
    }

# ==== from grid.py =====================================================
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

import itertools
import statistics



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
        st = equity_stats(sim.net_pct_series())
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

# ==== from data.py =====================================================
"""Data loading for the falsification engine: candles and signals.

Two candle sources, one signal source, all strict:

  * CSV candles  -- columns t,o,h,l,c[,v]; t is ISO-8601 or epoch milliseconds.
                    Rows are validated (ohlc > 0, high >= max(o,c), low <=
                    min(o,c)) and sorted; a bad row is an error, not a skip.
  * market cache -- the same sqlite the research audit downloaded (klines table
                    keyed by symbol/interval/venue), so a report can be replayed
                    offline from data that was already fetched.
  * CSV signals  -- columns t,side; t maps to the first candle at or after it.
                    A signal that cannot be mapped is counted, never dropped.

No network. If the data is not local, the engine says so instead of guessing.
"""

import csv
import sqlite3
from pathlib import Path


class DataError(RuntimeError):
    """Raised when a market cache cannot be read.

    Defined locally (not imported from the audit fetcher) so the engine
    carries no dependency beyond pathsim when it is vendored into a
    standalone skill package.
    """


class CandleError(ValueError):
    """Raised when candle data is missing, malformed, or out of order."""


# Column aliases customers actually have. Exchange downloads, freqtrade
# backtest exports and TradingView CSVs all use different names; demanding a
# literal `t` is how a paying customer decides the tool is not for them.
CANDLE_ALIASES = {
    "t": ("t", "date", "time", "timestamp", "open_time", "opentime", "datetime", "date_time"),
    "o": ("o", "open"),
    "h": ("h", "high"),
    "l": ("l", "low"),
    "c": ("c", "close"),
    "v": ("v", "volume", "vol", "amount", "quote_volume"),
}
SIGNAL_ALIASES = {
    "t": ("t", "date", "time", "timestamp", "datetime", "date_time", "signal_time"),
    "side": ("side", "direction", "signal", "position", "type"),
    "reason": ("reason", "exit_reason", "tag", "note"),
}


def _pick(row: dict, field: str, aliases: tuple[str, ...]) -> str | None:
    """First present alias for a logical column, or None. Extra columns are
    ignored on purpose -- real exports carry dozens of them."""
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for a in aliases:
        if a in lowered and lowered[a] not in (None, ""):
            return lowered[a]
    return None


def _to_ms(t: str) -> int:
    """ISO-8601 or epoch milliseconds -> epoch ms. Anything else is an error."""
    t = t.strip()
    if not t:
        raise CandleError("空时间戳")
    try:
        v = float(t)
    except ValueError:
        pass
    else:
        # Heuristic that is also a rule: seconds-era numbers are ~1e9, ms ~1e12.
        return int(v * 1000) if v < 1e11 else int(v)
    import datetime as _dt

    # Naive timestamps are UTC: exchange data is UTC, and .timestamp() on a
    # naive datetime would silently apply the machine's local zone.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = _dt.datetime.strptime(t.replace("T", " "), fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    raise CandleError(f"无法解析时间戳 {t!r}（支持 ISO-8601 UTC 或 epoch ms）")


def _validate_candle(row: dict, lineno: int) -> dict:
    t_raw = _pick(row, "t", CANDLE_ALIASES["t"])
    if t_raw is None:
        raise CandleError(f"第 {lineno} 行缺少时间列（接受 {CANDLE_ALIASES['t']} 之一）")
    try:
        t = _to_ms(str(t_raw))
        o = float(_pick(row, "o", CANDLE_ALIASES["o"]))
        h = float(_pick(row, "h", CANDLE_ALIASES["h"]))
        l = float(_pick(row, "l", CANDLE_ALIASES["l"]))
        c = float(_pick(row, "c", CANDLE_ALIASES["c"]))
    except (TypeError, ValueError) as exc:
        raise CandleError(f"第 {lineno} 行 OHLC 不是有效数字: {exc}") from exc
    v_raw = _pick(row, "v", CANDLE_ALIASES["v"])
    try:
        v = float(v_raw) if v_raw is not None else 0.0
    except ValueError:
        v = 0.0  # volume is optional context, not a price
    if min(o, h, l, c) <= 0:
        raise CandleError(f"第 {lineno} 行存在非正价格（t={t}）")
    if h < max(o, c) - 1e-12 or l > min(o, c) + 1e-12:
        raise CandleError(f"第 {lineno} 行 OHLC 不自洽: o={o} h={h} l={l} c={c}")
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def load_candles_csv(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"K 线文件不存在: {p}")
    candles: list[dict] = []
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CandleError("K 线 CSV 是空文件")
        lowered = {str(f).strip().lower() for f in reader.fieldnames}
        for field, aliases in CANDLE_ALIASES.items():
            if field != "v" and not lowered & set(aliases):
                raise CandleError(
                    f"K 线 CSV 缺少 {field} 列（接受 {aliases} 之一），实际得到 {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):
            candles.append(_validate_candle(row, i))
    if len(candles) < 2:
        raise CandleError("K 线不足 2 根，无法回放")
    candles.sort(key=lambda c: c["t"])
    return candles


def load_candles_market(db_path: str | Path, symbol: str, interval: str, venue: str) -> list[dict]:
    p = Path(db_path)
    if not p.is_file():
        raise CandleError(f"行情缓存不存在: {p}（可传 --candles CSV 替代）")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT open_time, open, high, low, close, volume FROM klines"
            " WHERE symbol=? AND interval=? AND venue=? ORDER BY open_time",
            (symbol.upper(), interval, venue),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise CandleError(f"缓存中没有 {symbol} {interval} {venue} 的 K 线")
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5] or 0)}
        for r in rows
    ]


def load_funding_csv(path: str | Path) -> list[dict]:
    """Funding series from a CSV (columns t,rate), or [] when unusable.

    Lets a spec carry its own funding series so a published case replays
    identically on a machine with no market cache.
    """
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"资金费文件不存在: {p}")
    out: list[dict] = []
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "t" not in reader.fieldnames or "rate" not in reader.fieldnames:
            raise CandleError(f"资金费 CSV 需要列 t,rate，实际得到 {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):
            try:
                out.append({"t": _to_ms(row["t"]), "rate": float(row["rate"])})
            except (ValueError, KeyError):
                raise CandleError(f"资金费 CSV 第 {i} 行无法解析: {row}") from None
    if not out:
        raise CandleError(f"资金费 CSV 没有数据行: {p}")
    out.sort(key=lambda r: r["t"])
    return out


def load_funding(db_path: str | Path, symbol: str) -> list[dict] | None:
    """Funding series for a symbol, or None when the cache has none.

    Funding only exists on futures books; the cache is keyed by symbol alone,
    which is safe because spot symbols carry no funding rows.
    """
    p = Path(db_path)
    if not p.is_file():
        return None
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT funding_time, funding_rate FROM funding WHERE symbol=? ORDER BY funding_time",
            (symbol.upper(),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return [{"t": int(r[0]), "rate": float(r[1])} for r in rows]


def load_signals_csv(path: str | Path, candles: list[dict]) -> tuple[dict[int, str], dict]:
    """Map signal timestamps onto bar indices.

    Returns (entries, stats). A signal maps to the first candle whose open time
    is at or after the signal time -- acting inside the bar that produced the
    signal is look-ahead. Signals that land after the last usable bar, or
    before the first candle, are counted in stats['skipped'] with reasons.
    """
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"信号文件不存在: {p}")
    entries: dict[int, str] = {}
    stats = {"total": 0, "mapped": 0, "skipped_after_end": 0, "skipped_before_start": 0, "bad_rows": 0}
    times = [c["t"] for c in candles]
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CandleError("信号 CSV 是空文件")
        lowered = {str(f).strip().lower() for f in reader.fieldnames}
        if not lowered & set(SIGNAL_ALIASES["t"]):
            raise CandleError(
                f"信号 CSV 缺少时间列（接受 {SIGNAL_ALIASES['t']} 之一），实际得到 {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):
            stats["total"] += 1
            try:
                t = _to_ms(str(_pick(row, "t", SIGNAL_ALIASES["t"])))
            except (CandleError, TypeError):
                stats["bad_rows"] += 1
                continue
            side_raw = _pick(row, "side", SIGNAL_ALIASES["side"])
            side = (side_raw or "long").strip().lower()
            if side not in ("long", "short"):
                # common spellings customers actually use
                side = {"buy": "long", "sell": "short", "l": "long", "s": "short",
                        "1": "long", "-1": "short"}.get(side, side)
            if side not in ("long", "short"):
                stats["bad_rows"] += 1
                continue
            if t > times[-1]:
                stats["skipped_after_end"] += 1
                continue
            if t < times[0]:
                stats["skipped_before_start"] += 1
                continue
            # first bar at or after the signal
            lo, hi = 0, len(times) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if times[mid] >= t:
                    hi = mid
                else:
                    lo = mid + 1
            idx = lo
            if idx >= len(candles) - 1:
                # pathsim needs the fill bar to exist; the signal is real but
                # unresolvable inside this window -- counted, not dropped.
                stats["skipped_after_end"] += 1
                continue
            entries[idx] = side
            stats["mapped"] += 1
    return entries, stats


def load_exit_signals_csv(path: str | Path, candles: list[dict]) -> dict[int, str]:
    """Map exit-signal timestamps onto bar indices: bar index -> reason.

    Same mapping rule as entries: the first candle at or after the signal
    timestamp, because a signal confirmed on one bar's close acts on the next.
    The reason string rides along into the simulator's exit bookkeeping.
    """
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"退出信号文件不存在: {p}")
    out: dict[int, str] = {}
    times = [c["t"] for c in candles]
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CandleError("退出信号 CSV 是空文件")
        lowered = {str(f).strip().lower() for f in reader.fieldnames}
        if not lowered & set(SIGNAL_ALIASES["t"]):
            raise CandleError(
                f"退出信号 CSV 缺少时间列（接受 {SIGNAL_ALIASES['t']} 之一），实际得到 {reader.fieldnames}")
        for row in reader:
            try:
                t = _to_ms(str(_pick(row, "t", SIGNAL_ALIASES["t"])))
            except (CandleError, TypeError):
                continue
            if t < times[0] or t > times[-1]:
                continue
            lo, hi = 0, len(times) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if times[mid] >= t:
                    hi = mid
                else:
                    lo = mid + 1
            reason = _pick(row, "reason", SIGNAL_ALIASES["reason"])
            out[lo] = (reason or "signal_exit").strip() or "signal_exit"
    return out


def describe_market_cache(db_path: str | Path) -> list[tuple]:
    """What is actually in a market cache: (symbol, interval, venue, bars, from, to)."""
    p = Path(db_path)
    if not p.is_file():
        raise DataError(f"行情缓存不存在: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT symbol, interval, venue, COUNT(*), MIN(open_time), MAX(open_time)"
            " FROM klines GROUP BY symbol, interval, venue ORDER BY COUNT(*) DESC"
        ).fetchall()
    finally:
        conn.close()

# ==== from report.py ===================================================
"""Render a verification result as a markdown falsification report.

The report is the product. Its rules:

  * every number that could be mistaken for a promise is labelled with the
    assumption that produced it (worst/best case, fee multiple, fill model);
  * a "30 秒读懂" block carries the verdict and the three key numbers at the
    very top -- the verdict comes last in reading order but first in the
    summary block, and it quotes the exact criteria that fired;
  * what is NOT modelled is a section, not a footnote -- a reader who does not
    know the limits of the instrument will misuse it.
"""

import datetime as _dt

ENGINE_VERSION = "verify/0.1"


def _pf(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _pct(v: float | None, digits: int = 2) -> str:
    return f"{v:+.{digits}f}%" if v is not None else "—"


def _n(v) -> str:
    """Counts read with thousands separators so 1,532 成交 never scans as
    three numbers. Missing values stay a dash -- never a fake zero."""
    if v is None:
        return "—"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.2f}"


def _fmt_axis(key: str, v) -> str:
    """Axis values read like the spec declares them: bar counts are counts,
    the rest are signed percentages."""
    if key == "timeout_bars":
        return f"{int(v)} 根"
    return f"{v * 100:+.1f}%"


def _params_txt(params: dict) -> str:
    return " · ".join(f"{k}={_fmt_axis(k, v)}" for k, v in params.items())


def _render_grid_scope(g: dict, spec: dict, verdict: dict, a) -> None:
    """One grid scope: the distribution line, the heat table, and -- only
    when the PRE-DECLARED rule fired -- the overfit flag. No threshold is
    invented here; the flag quotes the spec's own declared minimum."""
    axes, order = g["axes"], g["axis_order"]
    case_label = "最坏优先" if spec["worst_case"] else "最好优先"
    if g["cells_with_fills"] == 0:
        a(f"- {g['n_cells']} 格全部无成交——该范围无法评估参数敏感性（裁决见第 7 节）。")
    else:
        a(f"- 分布：{g['cells_with_fills']} 格有成交 · {g['positive_cells']} 格净收益为正"
          f"（{g['positive_ratio']:.0%}）· {case_label}口径中位 {_pct(g['median_net_pct'])} · "
          f"最好 {_pct(g['best']['net_sum_pct'])}（{_params_txt(g['best']['params'])}）· "
          f"最差 {_pct(g['worst']['net_sum_pct'])}（{_params_txt(g['worst']['params'])}）")
        if g["no_fill_cells"]:
            a(f"- 另有 {g['no_fill_cells']} 格无成交，不计入占比——无证据的格子既不算赢也不算输")
    a("")
    if len(order) == 2:
        r_ax, c_ax = order
        a(f"| {r_ax} ↓ ＼ {c_ax} → | " + " | ".join(_fmt_axis(c_ax, v) for v in axes[c_ax]) + " |")
        a("|---|" + "---:|" * len(axes[c_ax]))
        cell_map = {(c["params"][r_ax], c["params"][c_ax]): c for c in g["cells"]}
        best_key = ((g["best"]["params"][r_ax], g["best"]["params"][c_ax])
                    if g["best"] else None)
        for rv in axes[r_ax]:
            row = [f"`{_fmt_axis(r_ax, rv)}`"]
            for cv in axes[c_ax]:
                c = cell_map[(rv, cv)]
                txt = "无成交" if c["fills"] == 0 else _pct(c["net_sum_pct"])
                row.append(f"**{txt}**" if (rv, cv) == best_key else txt)
            a("| " + " | ".join(row) + " |")
    else:
        a(f"| {order[0]} | 净收益（{case_label}） | 成交 |")
        a("|---|---|---:|")
        cell_map = {c["params"][order[0]]: c for c in g["cells"]}
        for v in axes[order[0]]:
            c = cell_map[v]
            txt = "无成交" if c["fills"] == 0 else _pct(c["net_sum_pct"])
            a(f"| `{_fmt_axis(order[0], v)}` | {txt} | {_n(c['fills'])} |")
    fired = [c for c in verdict["checks"]
             if c.get("rule") == "grid_positive_ratio_min"
             and c.get("triggered") and c.get("scope") == g["scope"]]
    if fired:
        min_ratio = float(spec["falsify_if"]["grid_positive_ratio_min"])
        a("")
        if g["positive_cells"] == 0:
            a(f"> 全部 {g['cells_with_fills']} 格均为负——网格内没有任何参数组合赚钱，"
              f"edge 在这个参数空间里不存在，更谈不上稳健；按事前声明规则 "
              f"`grid_positive_ratio_min`（下限 {min_ratio:.0%}）触发证伪，见第 7 节。")
        else:
            a(f"> 只有 {g['positive_cells']}/{g['cells_with_fills']} 格为正——edge 的存在依赖一小撮参数组合，"
              f"这是过拟合的典型签名；按事前声明规则 `grid_positive_ratio_min`（下限 {min_ratio:.0%}）触发证伪，见第 7 节。")


def _key_numbers(result: dict) -> list[str]:
    """The three numbers a reader needs before deciding to read on: the
    baseline net, the control-group delta, and the out-of-sample or grid
    measurement -- whichever of the two the spec actually enabled. Only
    presents numbers already in the result; computes nothing new."""
    worst = result["runs"]["worst_case"]["stats"]
    best = result["runs"]["best_case"]["stats"]
    lines = [
        f"基线净收益（最坏口径）：**{_pct(worst['sum_pct'])}**"
        f"（最好口径 {_pct(best['sum_pct'])} · {_n(worst['n'])} 笔成交）"
    ]
    ctrl = result.get("control")
    if ctrl is None:
        lines.append("对照差值：对照组未启用——没有对照组的回放只是叙事")
    elif ctrl.get("diff_pct") is None:
        lines.append(f"对照差值：{ctrl.get('note') or '样本不足，未计算'}")
    else:
        ci = ctrl.get("diff_ci")
        ci_txt = (f"，95% bootstrap 区间 [{ci[0]:+.2f}%, {ci[1]:+.2f}%]"
                  if ci else "（样本不足，区间未计算）")
        lines.append(
            f"对照差值（策略 − 随机入场）：**{_pct(ctrl['diff_pct'])}/笔**{ci_txt}")
    wf = result.get("walk_forward")
    pg = result.get("param_grid")
    if wf and wf.get("out_sample"):
        oos = wf["out_sample"]["runs"]["worst_case"]["stats"]
        isb = wf.get("in_sample")
        is_txt = (f"（样本内 {_pct(isb['runs']['worst_case']['stats']['sum_pct'])}）"
                  if isb else "")
        lines.append(f"样本外净收益（最坏口径）：**{_pct(oos['sum_pct'])}**{is_txt}")
    elif pg:
        g = pg["scopes"][0]
        if g["cells_with_fills"]:
            lines.append(
                f"参数网格：**{g['positive_cells']}/{g['cells_with_fills']} 格为正**"
                f"（{g['positive_ratio']:.0%}，共 {g['n_cells']} 格）")
        else:
            lines.append(f"参数网格：{g['n_cells']} 格全部无成交——无法评估参数敏感性")
    else:
        lines.append("样本外 / 参数网格：均未启用——以上数字全是窗口内结果")
    return lines


def _plain_sentence(result: dict) -> str:
    """One sentence a non-quant reader can act on. It re-voices the verdict
    and the rules that fired; it invents no new judgement."""
    v = result["verdict"]
    trig = [c for c in v["checks"] if c.get("triggered")]
    if v["outcome"] == "FALSIFIED":
        if any(c.get("scope") == "样本外" for c in trig):
            return ("人话：策略在没见过的时段里先撑不住——样本外段触发了事前证伪规则，"
                    "按裁决规则整体判负。")
        rules = {c["rule"].split("=")[0] for c in trig}
        bits = []
        if "worst_case_net_negative" in rules:
            bits.append("按最保守的成交假设这笔账是亏的")
        if "net_negative_at_fee_multiple" in rules:
            bits.append("成本只要翻倍就由赚转亏")
        if "control_lift_ci_includes_zero" in rules:
            bits.append("表现和随机入场区分不开")
        if "grid_positive_ratio_min" in rules:
            bits.append("只有个别参数组合赚钱，换个参数就不成立")
        tail = "、".join(bits) if bits else "事前写好的证伪规则被触发"
        return f"人话：{tail}——规则先于报告存在，这次没能活下来。"
    if v["outcome"] == "INCONCLUSIVE":
        return "人话：样本不够，这份报告既证不实也证不伪——先去攒数据，别拿它做决定。"
    return ("人话：在声明的规则里活下来了，但'未被证伪'不等于'被证实'——"
            "规则没问的账，引擎不替它吹。")


def unmodeled_notes(result: dict) -> list[str]:
    """The standing disclosures, computed from what this run actually enabled.
    Shared by the markdown and HTML renderers so the two can never drift."""
    wf = bool(result.get("walk_forward"))
    pg = bool(result.get("param_grid"))
    return [
        "滑点与部分成交：只按费率+成交模型计成本，真实盘口的排队与部分成交不在模型内；",
        ("样本外表现：walk-forward 已启用——最后一段按时间切出作样本外，但引擎无法验证策略参数是否曾用全窗口数据调优；"
         "若调参时已看过最后一段，'样本外'名不副实。" if wf else
         "样本外表现：walk-forward 未启用——所有数字都是窗口内的样本内结果；参数网格已启用，"
         "但它测量的是参数脆弱性而非时间维度上的稳健性，且网格本身也在窗口内测量，不能替代样本外验证。" if pg else
         "样本外表现：所有数字都是窗口内的样本内结果，walk-forward 与参数敏感性不在本引擎射程内；"),
        "资金容量：等权回放，单笔金额对盘口的冲击（除费率外）未建模；",
        "同时持仓相关性：信号逐一独立回放，重叠持仓的相关性风险不计入；",
        "交易所/链路风险：宕机、API 限流、插针、宕机重启后的订单状态，一概不在模型内。",
    ]


def render(result: dict) -> str:
    spec = result["spec"]
    v = result["verdict"]
    L: list[str] = []
    a = L.append

    a(f"# 证伪报告 · {spec['name']}")
    a("")
    a("> **30 秒读懂**")
    a(">")
    a(f"> - 裁决：**{v['outcome']}**")
    for ln in _key_numbers(result):
        a(f"> - {ln}")
    a(f"> - {_plain_sentence(result)}")
    a("")
    a(f"> **裁决：{v['outcome']}** —— {v['reason']}")
    a("")
    a(f"- 生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 引擎 {ENGINE_VERSION}")
    a(f"- 标的：{spec['symbol']} {spec['interval']}（{spec['venue']}）· 窗口 {result['window']['from']} → {result['window']['to']}（{_n(result['window']['bars'])} 根 K 线）")
    if spec.get("hypothesis"):
        a(f"- 待检验假设：{spec['hypothesis']}")
    if spec.get("source"):
        a(f"- 策略来源：{spec['source']}")
    ex = spec["exit"]
    exit_txt = " · ".join(
        t for t in [
            f"止盈 {ex['take_profit']*100:+.1f}%" if ex.get("take_profit") is not None else "",
            f"止损 {ex['stop_loss']*100:+.1f}%" if ex.get("stop_loss") is not None else "",
            f"超时 {ex['timeout_bars']} 根" if ex.get("timeout_bars") is not None else "",
            f"移动止损回撤 {ex['trailing']*100:.1f}%" if ex.get("trailing") is not None else "",
            "信号退出（收盘价）" if ex.get("exit_signals_path") else "",
        ] if t
    )
    a(f"- 退出规则：{exit_txt}")
    a(f"- 执行假设：{spec['fill_model']} 成交模型 · 单边费率 {spec['fee_rate']*10000:.0f}bps"
      + (f" · 含资金费" if result.get("funding_applied") else (" · spec 声明含资金费但数据缺失，按 0 处理" if spec.get("funding") else " · 不含资金费"))
      + f" · bar 内先到止损判止损（最坏优先）")
    a(f"- 信号数：{_n(result['n_signals'])}")
    # --- section 0: port notes (strategies ported from somewhere else) -----
    if spec.get("port_notes"):
        a("")
        a("## 0. 移植说明")
        a("")
        for line in spec["port_notes"].splitlines():
            a(line.rstrip())
        a("")
    wf = result.get("walk_forward")
    if wf:
        oos_v = (wf.get("out_sample") or {}).get("verdict", {}).get("outcome", "无信号")
        a(f"- Walk-forward：{wf['folds']} 折（样本外界界 {wf['boundary_ts']}，第 {wf['boundary_bar']} 根）· 样本外段裁决 {oos_v}")
    pg = result.get("param_grid")
    if pg:
        dims = " × ".join(f"{k}({len(pg['axes'][k])})" for k in pg["axis_order"])
        parts = []
        for g in pg["scopes"]:
            if g["cells_with_fills"]:
                parts.append(f"{g['scope']} {g['positive_cells']}/{g['cells_with_fills']} 格为正")
            else:
                parts.append(f"{g['scope']} 无成交")
        a(f"- 参数网格：{dims} 共 {_n(pg['scopes'][0]['n_cells'])} 格 · " + " · ".join(parts))
    a("")

    # -- 1. replay ----------------------------------------------------------
    a("## 1. Path-aware 回放")
    a("")
    a("| 口径 | 成交 | 未成交 | 未决 | 净收益 | 均值/笔 | 胜率 | PF | 最大回撤(时序) |")
    a("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        r = result["runs"][case]
        st = r["stats"]
        a(f"| {label} | {_n(st['n'])} | {_n(r['unfilled'])} | {_n(r['open_signals'])} "
          f"| {_pct(st['sum_pct'])} | {_pct(st['mean_pct'])} | {st['win_pct']:.0f}% "
          f"| {_pf(st['profit_factor'])} | {_pct(st['max_dd_pct'])} |")
    a("")
    worst, best = result["runs"]["worst_case"]["stats"], result["runs"]["best_case"]["stats"]
    if worst["n"] and best["n"] and worst["sum_pct"] > 0 > best["sum_pct"]:
        a("> 同一策略只在乐观口径下赚钱——它赚的是假设，不是价差。")
        a("")
    # The realized drawdown is the chronological curve. The losses-first
    # replay is a BOUND, not a history: both are printed so neither can be
    # mistaken for the other (the sorted-series mistake is exactly how a
    # "worst case" number ends up quoted as a lived experience).
    dd_worst_order = worst.get("max_dd_worst_order_pct")
    if dd_worst_order is not None and worst["n"]:
        a(f"> 回撤口径：表中为**时序**最大回撤（真实经历）；同账面按“先亏后赚”重放的界为 {dd_worst_order:.2f}%——是界，不是历史。")
        a("")
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        by = result["runs"][case]["by_reason"]
        if not by:
            continue
        parts = [f"{k} {_n(b['n'])} 笔({b['net_sum_pct']:+.2f}%)" for k, b in sorted(by.items(), key=lambda kv: -kv[1]["net_sum_pct"])]
        a(f"- {label}退出结构：{' · '.join(parts)}")
    a("")

    # -- 2. cost sensitivity ------------------------------------------------
    a("## 2. 成本敏感性")
    a("")
    a("| 成本倍数 | 往返费率 | 净收益 | PF | 胜率 |")
    a("|---|---:|---:|---:|---:|")
    for row in result["cost_sensitivity"]:
        a(f"| ×{row['fee_multiple']:g} | {row['fee_bps']:.0f}bps | {_pct(row['net_sum_pct'])} "
          f"| {_pf(row['profit_factor'])} | {row['win_pct']:.0f}% |")
    a("")

    # -- 3. barrier reachability -------------------------------------------
    a("## 3. 障碍可达性")
    a("")
    br = result["barrier_reach"]
    if br.get("n"):
        a(f"- 样本 {_n(br['n'])} 笔 · 摸到止盈位的 {br['reached_tp_pct']:.1f}% · 摸到止损位的 {br['reached_sl_pct']:.1f}% · 两者都没摸到的 {br['reached_neither_pct']:.1f}%")
        a(f"- 最大有利偏离（MFE）中位 {_pct(br['mfe']['median'], 1)} / p90 {_pct(br['mfe']['p90'], 1)} · 最大不利偏离（MAE）中位 {_pct(br['mae']['median'], 1)} / p10 {_pct(br['mae']['p10'], 1)}")
        if br.get("tp_level_pct") is not None and br["reached_tp_pct"] < 10:
            a("")
            a(f"> 止盈位 {br['tp_level_pct']:+.1f}% 在持仓窗口内几乎从未被触及——这个策略的大部分盈利期望是彩票结构。")
        if br.get("sl_level_pct") is not None and br["reached_sl_pct"] < 10:
            a("")
            a(f"> 止损位 {br['sl_level_pct']:+.1f}% 在持仓窗口内几乎从未被触及——回测里的'止损'大部分时间没有在工作。")
    else:
        a(f"- {br.get('note', '无样本')}")
    a("")

    # -- 4. control ---------------------------------------------------------
    a("## 4. 随机入场对照组")
    a("")
    ctrl = result.get("control")
    if ctrl:
        cs = ctrl["stats"]
        rng = ctrl.get("bar_range")
        if ctrl.get("note"):
            a(f"- {ctrl['note']}")
        else:
            rng_txt = f"，随机入场范围 第 {rng[0]} → 第 {rng[1]} 根" if rng and rng[0] != 30 else ""
            a(f"- 对照组：{_n(ctrl['n_random'])} 个随机入场（seed={ctrl['seed']}，允许重复，镜像策略主方向 {ctrl.get('side', 'long')}{rng_txt}），同一窗口、同一退出规则、同一成本假设")
            a(f"- 策略均值/笔 {_pct(ctrl['strategy_mean_pct'])} vs 对照组 {_pct(ctrl['control_mean_pct'])}（成交 {_n(cs['n'])} 笔，胜率 {cs['win_pct']:.0f}%）")
            if ctrl.get("diff_pct") is not None:
                ci = ctrl.get("diff_ci")
                ci_txt = f"[{ci[0]:+.2f}%, {ci[1]:+.2f}%]" if ci else "（样本不足，未计算）"
                a(f"- 差值 {_pct(ctrl['diff_pct'])}，95% bootstrap 区间 {ci_txt}")
            sel = ctrl.get("selectivity")
            msg = {
                "proven": "策略显著优于随机入场——选择性成立（在本窗口内）。",
                "refuted": "策略显著差于随机入场——信号方向可能是反的，或成本吃掉了一切。",
                "unproven": "区间跨 0：未能证明策略优于抛硬币。绝对收益数字在这种情况下没有意义。",
                "unknown": "样本不足，无法比较。",
            }[sel]
            a(f"- 判定：**{sel}** —— {msg}")
    else:
        a("- 未启用对照组（spec.control.enabled=false）——没有对照组的回放只是叙事。")
    a("")

    # -- 5. walk-forward ------------------------------------------------------
    if wf:
        a("## 5. Walk-forward（样本内 / 样本外）")
        a("")
        a(f"- 切分：全窗口 {_n(result['window']['bars'])} 根 K 线切 {wf['folds']} 折（每折 {_n(wf['fold_bars'])} 根）；样本外界界 {wf['boundary_ts']}（第 {_n(wf['boundary_bar'])} 根）")
        a(f"- 样本内 = 前 {wf['folds'] - 1} 折的入场 · 样本外 = 最后 1 折的入场；两段各跑完整电池"
          f"（回放 + 成本敏感性 + 障碍可达性 + 随机对照组；样本外对照组的随机入场只在该段内抽取，"
          f"两段的成交都在全窗口上推演——入场之后的 K 线对入场而言仍是未来）")
        a("")
        a("| 段 | 信号 | 成交 | 净收益(最坏) | 均值/笔 | 胜率 | PF | 段内裁决 |")
        a("|---|---:|---:|---:|---:|---:|---:|---|")
        for seg, label in (("in_sample", f"样本内（前 {wf['folds'] - 1} 折）"),
                           ("out_sample", "样本外（最后 1 折）")):
            b = wf.get(seg)
            if b is None:
                a(f"| {label} | 0 | — | — | — | — | — | 无信号 |")
                continue
            st = b["runs"]["worst_case"]["stats"]
            a(f"| {label} | {_n(b['n_signals'])} | {_n(st['n'])} | {_pct(st['sum_pct'])} | {_pct(st['mean_pct'])} "
              f"| {st['win_pct']:.0f}% | {_pf(st['profit_factor'])} | {b['verdict']['outcome']} |")
        a("")
        isb, oosb = wf.get("in_sample"), wf.get("out_sample")
        if isb and oosb:
            is_sum = isb["runs"]["worst_case"]["stats"]["sum_pct"]
            oos_sum = oosb["runs"]["worst_case"]["stats"]["sum_pct"]
            a(f"- 样本内 → 样本外（最坏口径净收益）：{_pct(is_sum)} → {_pct(oos_sum)}（变化 {oos_sum - is_sum:+.2f}pp）")
            if oos_sum <= 0 < is_sum:
                a("")
                a("> 样本外转负——在未见过的时段里，edge 没有活下来。")
            elif 0 < oos_sum < is_sum:
                a("")
                a("> 样本外仍为正但低于样本内——衰减模式与过拟合一致，不能排除。")
            elif is_sum <= 0 and oos_sum <= 0:
                a("")
                a("> 两段均为负——这不是衰减，是稳定地亏；样本内外一致地证伪。")
            a("")
        if v["outcome"] == "FALSIFIED" and any(
            c.get("triggered") and c.get("scope") == "样本外" for c in v["checks"]
        ):
            a("> 样本外段触发了事前证伪规则——按裁决规则，整体判 FALSIFIED：只在看得见的地方成立的 edge 不是 edge。")
            a("")

    # -- 6. parameter grid ---------------------------------------------------
    if pg:
        a("## 6. 参数敏感性网格")
        a("")
        decl = " × ".join(
            f"`{k}` [{', '.join(_fmt_axis(k, v) for v in pg['axes'][k])}]"
            for k in pg["axis_order"]
        )
        a(f"- 事前声明：{decl}（共 {_n(pg['scopes'][0]['n_cells'])} 格；网格值写于 spec，先于本报告存在）")
        if wf:
            a("- walk-forward 已启用：样本内 / 样本外两段各跑一遍全网格（两段入场之和 = 全体入场）；"
              "每格按 spec 声明的成交口径、基准费率 ×1、同一入场集回放——成本敏感性见第 2 节，"
              "网格只回答「edge 是否依赖精确的参数组合」")
        else:
            a("- 测量口径：每格按 spec 声明的成交口径、基准费率 ×1、同一入场集回放——"
              "成本敏感性见第 2 节，网格只回答「edge 是否依赖精确的参数组合」；"
              "网格在全体入场上测量，本身是窗口内（样本内）结果")
        for g in pg["scopes"]:
            if len(pg["scopes"]) > 1:
                seg_label = ("样本内（前 %d 折）" % (wf["folds"] - 1) if g["scope"] == "样本内"
                             else "样本外（最后 1 折）" if g["scope"] == "样本外" else g["scope"])
                a("")
                a(f"**{seg_label}**")
            _render_grid_scope(g, spec, v, a)
        a("")

    # -- 7. criteria --------------------------------------------------------
    a("## 7. 事前证伪规则判定")
    a("")
    if v["checks"]:
        a("| 规则 | 范围 | 结果 | 明细 |")
        a("|---|---|---|---|")
        for c in v["checks"]:
            mark = "触发" if c.get("triggered") else ("样本不足" if c.get("insufficient") else "通过")
            a(f"| `{c['rule']}` | {c.get('scope', '全窗口')} | {mark} | {c['detail']} |")
    else:
        a("- spec 未声明任何 falsify_if 规则——本报告只呈现测量，不下裁决。")
    a("")

    # -- diagnosis ----------------------------------------------------------
    a("## 8. 诊断：死因与处方")
    a("")
    dx = result.get("diagnosis") or {}
    if dx:
        a(f"**主死因：{dx.get('primary_label', dx.get('primary', '?'))}**")
        if dx.get("tag_labels"):
            a(f"**附带问题：{' · '.join(dx['tag_labels'])}**")
        a("")
        if dx.get("evidence"):
            a("证据（全部来自本次测量，无新增假设）：")
            a("")
            for e in dx["evidence"]:
                a(f"- {e}")
            a("")
        if dx.get("prescriptions"):
            a("处方（按优先级；每条都引用本次运行的真实数字）：")
            a("")
            for i, p in enumerate(dx["prescriptions"], 1):
                a(f"{i}. {p}")
            a("")
        a("> 死因归类规则事前声明于 `research/verify/diagnosis.py`，先于任何策略被分类——"
          "与证伪规则同一条纪律。处方是工程建议，不是投资建议。")
        a("")
    else:
        a("- （本报告无诊断信息）")
        a("")

    # -- disclosure ---------------------------------------------------------
    a("## 9. 未建模事项（阅读前必读）")
    a("")
    for t in unmodeled_notes(result):
        a(f"- {t}")
    a("")

    # -- reproduce ----------------------------------------------------------
    repro = result.get("repro")
    if repro:
        a("## 10. 复现命令")
        a("")
        a("本次报告由以下命令产生；在同一目录重跑，可复现同样数字：")
        a("")
        a("```bash")
        a(repro["command"])
        a("```")
        a("")
        a("| 输入 | 路径 / 来源 |")
        a("|---|---|")
        a(f"| spec | {repro['spec']} |")
        a(f"| candles | {repro['candles']} |")
        a(f"| signals | {repro['signals']} |")
        if repro.get("funding"):
            a(f"| funding | {repro['funding']} |")
        a("")

    a("---")
    a(f"*{ENGINE_VERSION} · 本报告由事前证伪规则驱动，规则先于数字写入 spec；未被证伪不等于被证实。*")
    return "\n".join(L)


def render_json(result: dict) -> str:
    import json

    return json.dumps(result, ensure_ascii=False, indent=2, default=str)

# ==== from compare.py ==================================================
"""Multi-strategy comparison: the same battery, several strategies, one table.

The most common real question is not "is my strategy good" but "which of my
variants is the least overfitted". This module answers exactly that: it runs
the declared ranking rule over the per-strategy results and prints the table,
with the rule itself stated BEFORE the table -- the same
rules-before-numbers discipline the single-strategy reports follow.

Ranking rule (declared before any number is shown):
  1. Verdict tier: NOT_FALSIFIED > INCONCLUSIVE > FALSIFIED. The tier IS the
     honesty ranking -- a strategy that survived every declared rule outranks
     one that merely could not be measured, which outranks one that broke.
  2. Within a tier: control-group difference CI lower bound, descending.
     A positive lower bound is PROOF of selectivity; zero-crossing is merely
     unrefuted; negative is worse than coin flips.
  3. Then out-of-sample net return, descending.
  4. Then out-of-sample parameter-grid positive ratio, descending.
  5. Then worst-case fill count, descending (more evidence, more say).
"""


# Verdict tier -> rank. Higher is better. The mapping is the ranking's value
# judgement and lives here, in one place, in the open.
_TIER = {"NOT_FALSIFIED": 3, "INCONCLUSIVE": 2, "FALSIFIED": 1}


def _tier(outcome: str) -> int:
    return _TIER.get(outcome, 0)


def _oos_net(result: dict) -> float | None:
    wf = result.get("walk_forward") or {}
    seg = wf.get("out_sample") or {}
    st = (seg.get("runs") or {}).get("worst_case", {}).get("stats")
    return None if not st else st.get("sum_pct")


def _oos_grid_ratio(result: dict) -> float | None:
    wf = result.get("walk_forward") or {}
    grid_scope = wf.get("out_sample_grid") or {}
    return grid_scope.get("positive_ratio")


def _grid_ratio(result: dict) -> float | None:
    """Best available grid positive ratio: out-of-sample if present, else the
    whole-window grid (top-level param_grid scopes), else None."""
    r = _oos_grid_ratio(result)
    if r is not None:
        return r
    pg = result.get("param_grid") or {}
    for sc in (pg.get("scopes") or []):
        if isinstance(sc, dict) and sc.get("positive_ratio") is not None:
            return sc["positive_ratio"]
    return None


def _ci_lower(result: dict) -> float | None:
    ctrl = result.get("control") or {}
    ci = ctrl.get("diff_ci")
    return ci[0] if ci else None


def _worst_net(result: dict) -> float:
    return (result.get("runs") or {}).get("worst_case", {}).get("stats", {}).get("sum_pct", 0.0)


def _best_net(result: dict) -> float:
    return (result.get("runs") or {}).get("best_case", {}).get("stats", {}).get("sum_pct", 0.0)


def _fee2_net(result: dict) -> float | None:
    for row in result.get("cost_sensitivity") or []:
        if row.get("fee_multiple") == 2:
            return row.get("net_sum_pct")
    return None


def _fills(result: dict) -> int:
    return (result.get("runs") or {}).get("worst_case", {}).get("stats", {}).get("n", 0)


def _selectivity(result: dict) -> str:
    return (result.get("control") or {}).get("selectivity") or "n/a"


def _cmp_pct(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:+.{digits}f}%"


def rank_key(result: dict) -> tuple:
    """Sort key implementing the declared rule. Tuple order IS the rule."""
    ci_lo = _ci_lower(result)
    oos = _oos_net(result)
    grid = _grid_ratio(result)
    return (
        _tier((result.get("verdict") or {}).get("outcome", "")),
        ci_lo if ci_lo is not None else -99.0,
        oos if oos is not None else -9999.0,
        grid if grid is not None else -1.0,
        _fills(result),
    )


def _triggered_rules(result: dict) -> list[str]:
    """Rules that fired, as short 'scope: rule' tags, de-duplicated."""
    seen: list[str] = []
    for c in (result.get("verdict") or {}).get("checks") or []:
        if c.get("triggered"):
            tag = f"{c.get('rule')}({c.get('scope')})"
            if tag not in seen:
                seen.append(tag)
    return seen


def render_comparison(results: list[dict], title: str = "多策略对比") -> str:
    """results: one verify() output per strategy. Order in == order listed;
    the ranking is computed here and shown separately."""
    if not results:
        raise SpecError("对比模式至少需要一个策略的验证结果")
    if len(results) == 1:
        raise SpecError("只有一个策略：请直接用单策略报告模式（--spec）")

    ranked = sorted(results, key=rank_key, reverse=True)
    lines: list[str] = []
    a = lines.append

    a(f"# {title}")
    a("")
    a(f"本表由 **{len(results)} 个策略**过同一套证伪电池产出。每个策略的回放口径、成本敏感性、"
      "随机对照组、walk-forward 样本外折叠、参数网格、事前证伪规则均按其各自 spec 声明执行。")
    a("")
    a("## 排名规则（事前声明，先于任何数字）")
    a("")
    a("1. **裁决档位优先**：`NOT_FALSIFIED` > `INCONCLUSIVE` > `FALSIFIED`——档位本身就是诚实度排序："
      "扛住全部事前规则的策略，排在「测不出结论」的之上，排在「被规则抓住」的之上。")
    a("2. **同档内按对照组差值 CI 下界降序**：下界为正 = 已证明选择性；跨 0 = 未被证伪但也未证明；整体为负 = 显著差于随机。")
    a("3. **再按样本外净收益降序**（只在没见过的数据段上成立过的钱才算数）。")
    a("4. **再按样本外参数网格正格率降序**（换参数还活着的格子的比例）。")
    a("5. **最后按最坏口径成交数降序**（样本量即话语权）。")
    a("")

    a("## 对比矩阵")
    a("")
    a("| 排名 | 策略 | 裁决 | 死因 | 成交 | 最坏净 | 最好净 | ×2 成本净 | 对照组 CI | 选择性 | 样本外净 | 样本外网格 | 触发规则 |")
    a("|---:|---|---|---|---:|---:|---:|---:|---|---|---:|---|---|")
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        name = spec.get("name") or f"strategy-{i}"
        verdict = (r.get("verdict") or {}).get("outcome", "?")
        dx = r.get("diagnosis") or {}
        cause = dx.get("primary_label") or "—"
        if dx.get("tag_labels"):
            cause += f"（+{'、'.join(dx['tag_labels'])}）"
        ci = _ci_lower(r)
        ci_txt = "n/a" if ci is None else f"[{_cmp_pct(ci)}, {_cmp_pct((r.get('control') or {}).get('diff_ci')[1])}]"
        oos = _oos_net(r)
        grid = _grid_ratio(r)
        grid_txt = "n/a" if grid is None else f"{grid * 100:.0f}%"
        fired = _triggered_rules(r)
        fired_txt = "、".join(fired) if fired else "无"
        a(f"| {i} | {name} | {verdict} | {cause} | {_fills(r)} | {_cmp_pct(_worst_net(r))} | {_cmp_pct(_best_net(r))} "
          f"| {_cmp_pct(_fee2_net(r))} | {ci_txt} | {_selectivity(r)} | {_cmp_pct(oos)} | {grid_txt} | {fired_txt} |")
    a("")

    a("## 稳健性排名（一句话版）")
    a("")
    best = ranked[0]
    best_name = (best.get("spec") or {}).get("name") or "第 1 名"
    best_ci = _ci_lower(best)
    if best_ci is not None and best_ci > 0:
        why = "对照组差值 CI 下界为正——它是这一组里唯一（或最能）证明自己胜过随机入场的"
    elif best_ci is not None:
        why = "它的对照组 CI 下界最接近 0——未被证伪，但也远未被证明"
    else:
        why = "无对照组数据，按样本外与网格表现排序"
    a(f"**{best_name}**：{why}。")
    worst = ranked[-1]
    worst_name = (worst.get("spec") or {}).get("name") or "末位"
    a(f"**{worst_name}**：排名最低——详见其触发规则。")
    a("")
    a("> 注意：这个排名只回答\"哪一个最不过拟合\"，不回答\"该不该实盘\"。"
      "第一名同样可能不该实盘——看它自己的报告第 8 节未建模事项。")
    a("")

    a("## 每个策略的裁决理由（原样摘录）")
    a("")
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        name = spec.get("name") or f"strategy-{i}"
        v = r.get("verdict") or {}
        dx = r.get("diagnosis") or {}
        a(f"### {i}. {name} —— {v.get('outcome', '?')}")
        a("")
        a(f"{v.get('reason', '（无理由）')}")
        a("")
        if dx:
            cause = f"**死因：{dx.get('primary_label', dx.get('primary', '?'))}**"
            if dx.get("tag_labels"):
                cause += f"（附带：{'、'.join(dx['tag_labels'])}）"
            a(cause)
            if dx.get("prescriptions"):
                a("")
                a("首要处方：" + dx["prescriptions"][0])
            a("")
        # Per-strategy provenance: what data, what assumptions. A comparison
        # across different symbols/windows must say so on its face.
        a(f"- 标的/周期/场所：{spec.get('symbol', '?')} · {spec.get('interval', '?')} · {spec.get('venue', '?')}"
          f" · 单边费率 {float(spec.get('fee_rate', 0)) * 10000:.0f}bps · {spec.get('fill_model', '?')} 成交"
          f" · {'含资金费' if spec.get('funding') else '不含资金费'}")
        window = r.get("window") or {}
        if window:
            a(f"- 数据窗口：{window.get('bars', '?')} 根（{window.get('from', '?')} → {window.get('to', '?')}）")
        a("")

    return "\n".join(lines)

# ==== from html_report.py ==============================================
"""HTML rendering: the falsification report as a self-contained page.

One file, zero external requests, zero JavaScript. Everything the markdown
says is here, plus three charts markdown cannot carry:
  - realized equity curves (worst vs best case, in trade order)
  - cost-sensitivity bars at each fee multiple
  - the parameter grid as a heatmap (the shape of overfitting at a glance)

The colour language matches the landing page (same CSS variables, light
theme): kill-red for falsified/negative, green for surviving/positive,
amber for "could not tell". Colour never carries meaning alone -- every
coloured cell also states its number in text.
"""


_VERDICT_CLASS = {
    "FALSIFIED": "kill",
    "NOT_FALSIFIED": "ok",
    "INCONCLUSIVE": "warn",
}
_VERDICT_LABEL = {
    "FALSIFIED": "证伪",
    "NOT_FALSIFIED": "未被证伪",
    "INCONCLUSIVE": "无法结论",
}

_CSS = """
:root{--ink:#1a2332;--ink-2:#4a5568;--ink-3:#718096;--line:#e2e8f0;--bg:#ffffff;
--bg-soft:#f7f9fc;--accent:#0b5fff;--accent-ink:#0849c4;--kill:#c0392b;--kill-bg:#fdf0ee;
--ok:#1e7f4f;--ok-bg:#ecf8f1;--warn:#9a6a00;--warn-bg:#fdf6e3;
--mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.page{max-width:880px;margin:0 auto;padding:32px 24px 64px}
header{border-bottom:3px solid var(--ink);padding-bottom:20px;margin-bottom:8px}
h1{font-size:24px;margin:12px 0 6px}
h2{font-size:18px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:20px 0 8px}
.hypo{color:var(--ink-2);margin:4px 0}
.badge{display:inline-block;padding:4px 14px;border-radius:999px;font-weight:700;font-size:14px;letter-spacing:.5px}
.badge.kill{background:var(--kill);color:#fff}
.badge.ok{background:var(--ok);color:#fff}
.badge.warn{background:var(--warn);color:#fff}
.sentence{font-size:16px;background:var(--bg-soft);border-left:4px solid var(--accent);
padding:10px 14px;border-radius:0 8px 8px 0;margin:14px 0}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 0}
.chip{background:var(--bg-soft);border:1px solid var(--line);border-radius:8px;
padding:6px 10px;font-size:13px;color:var(--ink-2)}
.chip b{color:var(--ink);font-family:var(--mono);font-size:13px}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13.5px}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--bg-soft);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-family:var(--mono);font-size:12.5px;white-space:nowrap}
tr.hit td{background:var(--kill-bg)}
tr.pass td{background:var(--ok-bg)}
tr.weak td{background:var(--warn-bg)}
.note{background:var(--bg-soft);border:1px solid var(--line);border-radius:8px;
padding:10px 14px;margin:10px 0;font-size:13.5px;color:var(--ink-2)}
.warnbox{background:var(--warn-bg);border:1px solid #e8d9ac;border-radius:8px;
padding:10px 14px;margin:10px 0;font-size:13.5px}
ul{margin:8px 0;padding-left:22px}
li{margin:3px 0}
code{font-family:var(--mono);font-size:12.5px;background:var(--bg-soft);
border:1px solid var(--line);border-radius:4px;padding:1px 5px}
pre{background:var(--bg-soft);border:1px solid var(--line);border-radius:8px;
padding:12px 14px;overflow-x:auto;font-family:var(--mono);font-size:12.5px;line-height:1.5}
.chart{margin:14px 0}
.chart figcaption{font-size:12.5px;color:var(--ink-3);margin-top:4px}
.legend{display:flex;gap:18px;font-size:12.5px;color:var(--ink-2);margin:6px 0}
.swatch{display:inline-block;width:14px;height:4px;border-radius:2px;vertical-align:middle;margin-right:6px}
.port{background:var(--warn-bg);border:1px solid #e8d9ac;border-radius:8px;padding:14px 16px;margin:12px 0}
.port pre{background:#fff}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);
font-size:13px;color:var(--ink-3)}
footer a{color:var(--accent)}
.pos{color:var(--ok);font-weight:600}
.neg{color:var(--kill);font-weight:600}
.mono{font-family:var(--mono);font-size:12.5px}
"""


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _signed(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="{cls}">{v:+.{digits}f}%</span>'


def _table(headers: list[str], rows: list[list[str]], num_cols: set[int] | None = None,
           row_class: list[str] | None = None) -> str:
    num_cols = num_cols or set()
    out = ["<table>", "<thead><tr>"]
    for i, h in enumerate(headers):
        cls = ' class="num"' if i in num_cols else ""
        out.append(f"<th{cls}>{_esc(h)}</th>")
    out.append("</tr></thead><tbody>")
    for r, row in enumerate(rows):
        cls = f' class="{row_class[r]}"' if row_class and row_class[r] else ""
        out.append(f"<tr{cls}>")
        for i, cell in enumerate(row):
            c = ' class="num"' if i in num_cols else ""
            out.append(f"<td{c}>{cell}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


# --------------------------------------------------------------------------
# SVG charts
# --------------------------------------------------------------------------

def _resample(series: list[float], max_points: int = 640) -> list[float]:
    if len(series) <= max_points:
        return series
    step = (len(series) - 1) / (max_points - 1)
    return [series[min(len(series) - 1, round(i * step))] for i in range(max_points)]


def _svg_equity(worst_nets: list[float], best_nets: list[float]) -> str:
    """Realized equity curves for both replay orders, in trade order."""
    if not worst_nets and not best_nets:
        return ""

    def curve(nets: list[float]) -> list[float]:
        eq, out = 1.0, [1.0]
        for v in _resample(nets):
            eq *= 1.0 + v
            out.append(eq)
        return out

    w_curve = curve(worst_nets)
    b_curve = curve(best_nets)
    all_v = w_curve + b_curve
    lo, hi = min(all_v), max(all_v)
    pad = max((hi - lo) * 0.08, 0.02)
    lo, hi = lo - pad, hi + pad

    W, H = 680, 240
    ml, mr, mt, mb = 64, 14, 12, 26
    pw, ph = W - ml - mr, H - mt - mb

    def xy(i: int, n: int, v: float) -> tuple[float, float]:
        x = ml + (pw * i / max(1, n - 1))
        y = mt + ph * (hi - v) / (hi - lo)
        return x, y

    def path(c: list[float], color: str) -> str:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (xy(i, len(c), v) for i, v in enumerate(c)))
        end_x, end_y = xy(len(c) - 1, len(c), c[-1])
        return (f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8"/>'
                f'<circle cx="{end_x:.1f}" cy="{end_y:.1f}" r="3" fill="{color}"/>'
                f'<text x="{end_x - 6:.1f}" y="{end_y - 7:.1f}" text-anchor="end" '
                f'font-size="11" fill="{color}">{c[-1]:.2f}×</text>')

    y0 = mt + ph * (hi - 1.0) / (hi - lo)
    grid = "".join(
        f'<line x1="{ml}" y1="{mt + ph * f:.1f}" x2="{W - mr}" y2="{mt + ph * f:.1f}" '
        f'stroke="#e2e8f0" stroke-width="1"/>'
        f'<text x="{ml - 8}" y="{mt + ph * f + 4:.1f}" text-anchor="end" font-size="10.5" '
        f'fill="#718096">{hi - f * (hi - lo):.2f}</text>'
        for f in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    svg = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        f'aria-label="逐笔权益曲线：最坏优先与最好优先">',
        grid,
        f'<line x1="{ml}" y1="{y0:.1f}" x2="{W - mr}" y2="{y0:.1f}" stroke="#718096" '
        f'stroke-width="1" stroke-dasharray="4 4"/>',
        f'<text x="{ml - 8}" y="{y0 + 4:.1f}" text-anchor="end" font-size="10.5" '
        f'fill="#718096">1.00</text>',
        path(w_curve, "#c0392b"),
        path(b_curve, "#1e7f4f"),
        f'<text x="{ml}" y="{H - 8}" font-size="10.5" fill="#718096">第 1 笔</text>',
        f'<text x="{W - mr}" y="{H - 8}" text-anchor="end" font-size="10.5" fill="#718096">'
        f'最后 1 笔（按成交顺序）</text>',
        "</svg>",
    ]
    return "".join(svg)


def _svg_cost_bars(rows: list[dict]) -> str:
    if not rows:
        return ""
    W = 680
    bar_h, gap, mt = 26, 10, 8
    H = mt * 2 + len(rows) * (bar_h + gap)
    zero_x = W / 2
    max_abs = max((abs(r["net_sum_pct"]) for r in rows), default=1.0) or 1.0
    half = zero_x - 130  # leave room for labels on the left
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="成本敏感性">']
    out.append(f'<line x1="{zero_x}" y1="0" x2="{zero_x}" y2="{H}" stroke="#94a3b8" stroke-width="1"/>')
    for i, r in enumerate(rows):
        y = mt + i * (bar_h + gap)
        v = r["net_sum_pct"]
        w = abs(v) / max_abs * half
        color = "#1e7f4f" if v > 0 else "#c0392b"
        x = zero_x if v >= 0 else zero_x - w
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{max(w, 1):.1f}" height="{bar_h}" '
                   f'rx="3" fill="{color}" fill-opacity="0.85"/>')
        label = f'×{r["fee_multiple"]:g}（往返 {r["fee_bps"]:.0f}bps）'
        out.append(f'<text x="8" y="{y + bar_h / 2 + 4:.1f}" font-size="12" fill="#4a5568">{label}</text>')
        tx = (zero_x + w + 8) if v >= 0 else (zero_x - w - 8)
        anchor = "start" if v >= 0 else "end"
        out.append(f'<text x="{tx:.1f}" y="{y + bar_h / 2 + 4:.1f}" text-anchor="{anchor}" '
                   f'font-size="12" font-weight="700" fill="{color}">{v:+.1f}%</text>')
    out.append("</svg>")
    return "".join(out)


def _grid_heatmap(scope: dict, axes: dict, axis_order: list[str]) -> str:
    """The first two axes as a heatmap; every cell also prints its number."""
    cells = scope.get("cells") or []
    if len(axis_order) < 2 or not cells:
        return ""
    ax0, ax1 = axis_order[0], axis_order[1]
    v0s, v1s = list(axes[ax0]), list(axes[ax1])
    lookup = {}
    for c in cells:
        key = (c["params"].get(ax0), c["params"].get(ax1))
        lookup[key] = c
    max_abs = max((abs(c["net_sum_pct"]) for c in cells), default=1.0) or 1.0

    n_cols = len(v1s)
    cell_w = min(120.0, (680 - 110) / max(1, n_cols))
    cell_h = 52
    W = 110 + cell_w * n_cols + 10
    H = 34 + cell_h * len(v0s) + 8
    out = [f'<svg viewBox="0 0 {W:.0f} {H}" width="100%" role="img" aria-label="参数网格热力图">']
    # column headers (axis1 values)
    for j, v1 in enumerate(v1s):
        x = 110 + j * cell_w + cell_w / 2
        out.append(f'<text x="{x:.1f}" y="16" text-anchor="middle" font-size="11" '
                   f'fill="#4a5568">{_esc(_fmt_axis(ax1, v1))}</text>')
    out.append(f'<text x="{110 + cell_w * n_cols / 2:.1f}" y="30" text-anchor="middle" '
               f'font-size="10.5" fill="#718096">↑ 列：{_esc(ax1)}</text>')
    for i, v0 in enumerate(v0s):
        y = 34 + i * cell_h
        out.append(f'<text x="102" y="{y + cell_h / 2 + 4:.1f}" text-anchor="end" '
                   f'font-size="11" fill="#4a5568">{_esc(_fmt_axis(ax0, v0))}</text>')
        for j, v1 in enumerate(v1s):
            c = lookup.get((v0, v1))
            x = 110 + j * cell_w
            if c is None:
                continue
            if c["fills"] == 0:
                fill, tcol = "#e2e8f0", "#718096"
            else:
                net = c["net_sum_pct"]
                alpha = min(0.92, 0.25 + abs(net) / max_abs * 0.67)
                fill = (f"rgba(30,127,79,{alpha:.2f})" if net > 0 else f"rgba(192,57,43,{alpha:.2f})")
                tcol = "#fff" if alpha > 0.55 else "#1a2332"
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{cell_w - 4:.1f}" height="{cell_h - 4}" '
                       f'rx="4" fill="{fill}"/>')
            txt = f'{c["net_sum_pct"]:+.0f}%' if c["fills"] else "无成交"
            sub = f'{c["fills"]} 笔' if c["fills"] else ""
            out.append(f'<text x="{x + (cell_w - 4) / 2:.1f}" y="{y + cell_h / 2 - 2:.1f}" '
                       f'text-anchor="middle" font-size="12" font-weight="700" fill="{tcol}">{txt}</text>')
            if sub:
                out.append(f'<text x="{x + (cell_w - 4) / 2:.1f}" y="{y + cell_h / 2 + 13:.1f}" '
                           f'text-anchor="middle" font-size="10" fill="{tcol}" fill-opacity="0.85">{sub}</text>')
    out.append("</svg>")
    return "".join(out)


def _grid_table(scope: dict, axes: dict, axis_order: list[str]) -> str:
    rows = []
    for c in scope.get("cells") or []:
        rows.append([
            _esc(_params_txt(c["params"])),
            _n(c["fills"]),
            _signed(c["net_sum_pct"]),
            _signed(c["mean_pct"]),
            f'{c["win_pct"]:.0f}%',
            _pf(c.get("profit_factor")),
        ])
    return _table(["参数组合", "成交", "净收益", "均值/笔", "胜率", "PF"],
                  rows, num_cols={1, 2, 3, 4, 5})


# --------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------

def render_html(result: dict) -> str:
    spec = result.get("spec") or {}
    verdict = result.get("verdict") or {}
    outcome = verdict.get("outcome", "?")
    cls = _VERDICT_CLASS.get(outcome, "warn")
    vlabel = _VERDICT_LABEL.get(outcome, outcome)
    name = spec.get("name", "strategy")
    worst = result["runs"]["worst_case"]
    best = result["runs"]["best_case"]
    wst, bst = worst["stats"], best["stats"]
    wf = result.get("walk_forward")
    pg = result.get("param_grid")
    ctrl = result.get("control")

    h: list[str] = []
    a = h.append
    a("<!DOCTYPE html>")
    a('<html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{_esc(name)} · 策略证伪报告</title>")
    a(f"<style>{_CSS}</style></head><body><div class='page'>")

    # ---- header -----------------------------------------------------------
    a("<header>")
    a(f'<span class="badge {cls}">{_esc(outcome)} · {_esc(vlabel)}</span>')
    a(f"<h1>{_esc(name)}</h1>")
    if spec.get("hypothesis"):
        a(f'<p class="hypo">{_esc(spec["hypothesis"])}</p>')
    a(f'<p class="sentence">{_esc(_plain_sentence(result))}</p>')
    a('<div class="chips">')
    for k in _key_numbers(result):
        a(f'<span class="chip">{_esc(k)}</span>')
    a("</div>")
    a(f'<p class="hypo" style="margin-top:10px">标的 <b>{_esc(spec.get("symbol", "?"))}</b> · '
      f'周期 {_esc(spec.get("interval", "?"))} · {_esc(spec.get("venue", "?"))} · '
      f'单边费率 {float(spec.get("fee_rate", 0)) * 10000:.0f}bps · '
      f'{_esc(spec.get("fill_model", "?"))} 成交 · '
      f'{"含资金费" if spec.get("funding") else "不含资金费"}</p>')
    a("</header>")

    # ---- 0. port notes ----------------------------------------------------
    if spec.get("port_notes"):
        a('<div class="port">')
        a("<h3 style='margin-top:0'>0. 移植说明（外部策略保真度）</h3>")
        for para in spec["port_notes"].split("\n\n"):
            para = para.strip()
            if para:
                a(f"<p>{_esc(para).replace(chr(10), '<br>')}</p>")
        a("</div>")

    # ---- 1. replay --------------------------------------------------------
    a("<h2>1. Path-aware 回放</h2>")
    a(_table(
        ["口径", "成交", "未成交", "未决", "净收益", "均值/笔", "胜率", "PF", "最大回撤(时序)"],
        [[f"{label}", _n(r["stats"]["n"]), _n(r["unfilled"]), _n(r["open_signals"]),
          _signed(r["stats"]["sum_pct"]), _signed(r["stats"]["mean_pct"]),
          f'{r["stats"]["win_pct"]:.0f}%', _pf(r["stats"]["profit_factor"]),
          _signed(r["stats"]["max_dd_pct"])]
         for label, r in (("最坏优先", worst), ("最好优先", best))],
        num_cols={1, 2, 3, 4, 5, 6, 7, 8},
    ))
    if wst["n"] and bst["n"] and wst["sum_pct"] > 0 > bst["sum_pct"]:
        a('<div class="warnbox">同一策略只在乐观口径下赚钱——它赚的是假设，不是价差。</div>')
    dd = wst.get("max_dd_worst_order_pct")
    if dd is not None and wst["n"]:
        a(f'<p class="note">回撤口径：表中为<b>时序</b>最大回撤（真实经历）；'
          f'同账面按“先亏后赚”重放的界为 {dd:.2f}%——是界，不是历史。</p>')
    for case, label in (("worst_case", "最坏优先"), ("best_case", "最好优先")):
        by = result["runs"][case].get("by_reason") or {}
        if by:
            parts = " · ".join(
                f"{k} {_n(b['n'])} 笔({b['net_sum_pct']:+.2f}%)"
                for k, b in sorted(by.items(), key=lambda kv: -kv[1]["net_sum_pct"]))
            a(f"<p>{label}退出结构：{_esc(parts)}</p>")
    eq = _svg_equity(wst.get("nets") or [], bst.get("nets") or [])
    if eq:
        a('<figure class="chart">')
        a(eq)
        a('<div class="legend">'
          '<span><span class="swatch" style="background:#c0392b"></span>最坏优先（同根 K 线平局判止损）</span>'
          '<span><span class="swatch" style="background:#1e7f4f"></span>最好优先（同根 K 线平局判止盈）</span>'
          "</div>")
        a("<figcaption>逐笔权益曲线（按成交顺序，起点 1.00×）：两条线的分离程度就是"
          "“平局假设”对结论的影响幅度；重合则说明结论不依赖该假设。</figcaption>")
        a("</figure>")

    # ---- 2. cost ----------------------------------------------------------
    a("<h2>2. 成本敏感性</h2>")
    a(_table(["成本倍数", "往返费率", "净收益", "PF", "胜率"],
             [[f'×{r["fee_multiple"]:g}', f'{r["fee_bps"]:.0f}bps', _signed(r["net_sum_pct"]),
               _pf(r.get("profit_factor")), f'{r["win_pct"]:.0f}%']
              for r in result["cost_sensitivity"]],
             num_cols={0, 1, 2, 3, 4}))
    bars = _svg_cost_bars(result["cost_sensitivity"])
    if bars:
        a('<figure class="chart">')
        a(bars)
        a("<figcaption>你的 edge 付得起多贵的过路费。条越过零线向左，"
          "即该成本下策略亏损。</figcaption>")
        a("</figure>")

    # ---- 3. barriers ------------------------------------------------------
    a("<h2>3. 障碍可达性</h2>")
    br = result.get("barrier_reach") or {}
    if br.get("n"):
        a("<ul>")
        a(f"<li>样本 {_n(br['n'])} 笔 · 摸到止盈位的 {br['reached_tp_pct']:.1f}% · "
          f"摸到止损位的 {br['reached_sl_pct']:.1f}% · 两者都没摸到的 {br['reached_neither_pct']:.1f}%</li>")
        a(f"<li>最大有利偏离（MFE）中位 {_pct(br['mfe']['median'], 1)} / p90 {_pct(br['mfe']['p90'], 1)} · "
          f"最大不利偏离（MAE）中位 {_pct(br['mae']['median'], 1)} / p10 {_pct(br['mae']['p10'], 1)}</li>")
        a("</ul>")
        if br.get("tp_level_pct") is not None and br["reached_tp_pct"] < 10:
            a(f'<div class="warnbox">止盈位 {br["tp_level_pct"]:+.1f}% 在持仓窗口内几乎从未被触及'
              "——这个策略的大部分盈利期望是彩票结构。</div>")
        if br.get("sl_level_pct") is not None and br["reached_sl_pct"] < 10:
            a(f'<div class="warnbox">止损位 {br["sl_level_pct"]:+.1f}% 在持仓窗口内几乎从未被触及'
              "——回测里的“止损”大部分时间没有在工作。</div>")
    else:
        a(f'<p class="note">{_esc(br.get("note", "无样本"))}</p>')

    # ---- 4. control -------------------------------------------------------
    a("<h2>4. 随机入场对照组</h2>")
    if ctrl:
        cs = ctrl["stats"]
        a("<ul>")
        if ctrl.get("note"):
            a(f"<li>{_esc(ctrl['note'])}</li>")
        else:
            rng = ctrl.get("bar_range")
            rng_txt = f"，随机入场范围 第 {rng[0]} → 第 {rng[1]} 根" if rng and rng[0] != 30 else ""
            a(f"<li>对照组：{_n(ctrl['n_random'])} 个随机入场（seed={ctrl['seed']}，允许重复，"
              f"镜像策略主方向 {ctrl.get('side', 'long')}{rng_txt}），同一窗口、同一退出规则、同一成本假设</li>")
            a(f"<li>策略均值/笔 {_pct(ctrl['strategy_mean_pct'])} vs 对照组 {_pct(ctrl['control_mean_pct'])}"
              f"（成交 {_n(cs['n'])} 笔，胜率 {cs['win_pct']:.0f}%）</li>")
            if ctrl.get("diff_pct") is not None:
                ci = ctrl.get("diff_ci")
                ci_txt = f"[{ci[0]:+.2f}%, {ci[1]:+.2f}%]" if ci else "（样本不足，未计算）"
                a(f"<li>差值 {_pct(ctrl['diff_pct'])}，95% bootstrap 区间 {ci_txt} · "
                  f"选择性判定：<b>{_esc(ctrl.get('selectivity', 'n/a'))}</b></li>")
        a("</ul>")
    else:
        a('<p class="note">本 run 未启用对照组。</p>')

    # ---- 5. walk-forward --------------------------------------------------
    a("<h2>5. Walk-forward（样本内 / 样本外）</h2>")
    if wf and wf.get("out_sample"):
        isb = wf.get("in_sample") or {}
        oos = wf["out_sample"]
        rows, rc = [], []
        for label, seg in (("样本内", isb), ("样本外", oos)):
            if not seg:
                continue
            st = seg["runs"]["worst_case"]["stats"]
            seg_ctrl = seg.get("control") or {}
            ci = seg_ctrl.get("diff_ci")
            ci_txt = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a"
            seg_verdict = (seg.get("verdict") or {}).get("outcome", "?")
            rows.append([label, _n(st["n"]), _signed(st["sum_pct"]),
                         _signed(st.get("mean_pct")), ci_txt,
                         _esc(seg_ctrl.get("selectivity", "n/a")),
                         f'<b>{_esc(seg_verdict)}</b>'])
            rc.append("hit" if seg_verdict == "FALSIFIED" else "")
        a(_table(["段", "成交", "最坏净", "均值/笔", "对照组 CI", "选择性", "裁决"],
                 rows, num_cols={1, 2, 3}, row_class=rc))
        for label, key in (("样本内", "in_sample_grid"), ("样本外", "out_sample_grid")):
            g = wf.get(key) or {}
            if g.get("cells"):
                a(f"<h3>{label} · 参数网格（{g.get('positive_cells', 0)}/{g.get('n_cells', 0)} 格为正）</h3>")
                hm = _grid_heatmap(g, g.get("axes") or {}, g.get("axis_order") or [])
                if hm:
                    a('<figure class="chart">')
                    a(hm)
                    a("<figcaption>颜色 = 该参数组合的净收益（绿正红负），"
                      "数字同时印在格内——不依赖颜色传达信息。</figcaption>")
                    a("</figure>")
    else:
        a('<p class="note">本 run 未启用 walk-forward——所有数字都是窗口内的样本内结果。</p>')

    # ---- 6. param grid ----------------------------------------------------
    a("<h2>6. 参数敏感性网格</h2>")
    if pg and pg.get("scopes"):
        axes = pg.get("axes") or {}
        axis_order = pg.get("axis_order") or []
        for scope in pg["scopes"]:
            if not scope.get("cells"):
                continue
            a(f"<h3>{_esc(scope.get('scope', '全窗口'))} · "
              f"{scope.get('positive_cells', 0)}/{scope.get('n_cells', 0)} 格为正"
              f"（{scope.get('cells_with_fills', 0)} 格有成交）</h3>")
            hm = _grid_heatmap(scope, axes, axis_order)
            if hm:
                a('<figure class="chart">')
                a(hm)
                a("<figcaption>换一组参数还活着的格子比例，就是策略对参数的依赖度；"
                  "只有个别格子为正 = 曲线拟合，不是 edge。</figcaption>")
                a("</figure>")
            a(_grid_table(scope, axes, axis_order))
    else:
        a('<p class="note">本 run 未启用参数网格。</p>')

    # ---- 7. verdict rules -------------------------------------------------
    a("<h2>7. 事前证伪规则判定</h2>")
    checks = verdict.get("checks") or []
    if checks:
        rows, rc = [], []
        for c in checks:
            if c.get("triggered"):
                mark, klass = "触发", "hit"
            elif c.get("insufficient"):
                mark, klass = "样本不足", "weak"
            else:
                mark, klass = "通过", "pass"
            rows.append([f'<code>{_esc(c["rule"])}</code>', _esc(c.get("scope", "全窗口")),
                         mark, _esc(c["detail"])])
            rc.append(klass)
        a(_table(["规则", "范围", "结果", "明细"], rows, row_class=rc))
    else:
        a('<p class="note">spec 未声明任何 falsify_if 规则——本报告只呈现测量，不下裁决。</p>')

    # ---- 8. diagnosis ------------------------------------------------------
    a("<h2>8. 诊断：死因与处方</h2>")
    dx = result.get("diagnosis") or {}
    if dx:
        a(f'<p class="sentence" style="border-left-color:var(--kill)">'
          f'主死因：<b>{_esc(dx.get("primary_label", dx.get("primary", "?")))}</b>'
          + (f'　·　附带问题：{_esc(" · ".join(dx.get("tag_labels") or []))}'
             if dx.get("tag_labels") else "")
          + "</p>")
        if dx.get("evidence"):
            a("<p><b>证据</b>（全部来自本次测量，无新增假设）：</p><ul>")
            for e in dx["evidence"]:
                a(f"<li>{_esc(e)}</li>")
            a("</ul>")
        if dx.get("prescriptions"):
            a("<p><b>处方</b>（按优先级；每条都引用本次运行的真实数字）：</p><ol>")
            for p in dx["prescriptions"]:
                a(f"<li>{_esc(p)}</li>")
            a("</ol>")
        a('<p class="note">死因归类规则事前声明于 <code>research/verify/diagnosis.py</code>，'
          "先于任何策略被分类——与证伪规则同一条纪律。处方是工程建议，不是投资建议。</p>")
    else:
        a('<p class="note">（本报告无诊断信息）</p>')

    # ---- 9. disclosures ---------------------------------------------------
    a("<h2>9. 未建模事项（阅读前必读）</h2>")
    a("<ul>")
    for t in unmodeled_notes(result):
        a(f"<li>{_esc(t)}</li>")
    a("</ul>")

    # ---- 9. repro ---------------------------------------------------------
    repro = result.get("repro")
    if repro:
        a("<h2>10. 复现命令</h2>")
        a("<p>本次报告由以下命令产生；在同一目录重跑，可复现同样数字：</p>")
        a(f"<pre>{_esc(repro['command'])}</pre>")
        a(_table(["输入", "路径 / 来源"],
                 [["spec", _esc(repro["spec"])],
                  ["candles", _esc(repro["candles"])],
                  ["signals", _esc(repro["signals"])]]
                 + ([["funding", _esc(repro["funding"])]] if repro.get("funding") else [])))

    # ---- footer -----------------------------------------------------------
    a("<footer>")
    a('<p>由策略证伪引擎生成（六件仪器：path-aware 回放 · 成本敏感性 · 障碍可达性 · '
      '随机对照组 · walk-forward · 参数网格）。规则先于数字，失败同样发布。</p>')
    a('<p><a href="https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/">落地页</a> · '
      '<a href="https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2">开源仓库</a> · '
      '服务：带策略来，我出证伪报告 —— 首 10 单免费（换匿名公开案例授权），付费档 $99 / $299。</p>')
    a("</footer>")
    a("</div></body></html>")
    return "\n".join(h)


def render_to(result: dict, path) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_html(result), encoding="utf-8")


# --------------------------------------------------------------------------
# comparison page
# --------------------------------------------------------------------------

def render_comparison_html(results: list[dict], title: str = "多策略对比") -> str:
    """The comparison as a page: the declared rule, the matrix, and one
    collapsible detail block per strategy (native <details>, no JS)."""

    ranked = sorted(results, key=rank_key, reverse=True)
    h: list[str] = []
    a = h.append
    a("<!DOCTYPE html>")
    a('<html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a(f"<title>{_esc(title)} · 策略证伪</title>")
    a(f"<style>{_CSS}</style></head><body><div class='page'>")
    a("<header>")
    a("<h1 style='margin-top:0'>多策略对比</h1>")
    a(f'<p class="hypo">{len(ranked)} 个策略，同一套证伪电池，同一套事前声明的排名规则。</p>')
    a("</header>")

    a("<h2>排名规则（事前声明，先于任何数字）</h2>")
    a("<ol>")
    a("<li><b>裁决档位优先</b>：<code>NOT_FALSIFIED</code> &gt; <code>INCONCLUSIVE</code> &gt; "
      "<code>FALSIFIED</code>——档位本身就是诚实度排序。</li>")
    a("<li><b>同档内按对照组差值 CI 下界降序</b>：下界为正 = 已证明选择性；跨 0 = 未证明；整体为负 = 差于随机。</li>")
    a("<li><b>再按样本外净收益降序</b>（只在没见过的数据段上成立过的钱才算数）。</li>")
    a("<li><b>再按样本外参数网格正格率降序</b>（换参数还活着的格子比例）。</li>")
    a("<li><b>最后按成交数降序</b>（样本量即话语权）。</li>")
    a("</ol>")

    a("<h2>对比矩阵</h2>")
    rows, rc = [], []
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        outcome = (r.get("verdict") or {}).get("outcome", "?")
        ci = _ci_lower(r)
        ctrl = r.get("control") or {}
        ci_txt = "n/a" if ci is None else f"[{ci:+.2f}%, {ctrl.get('diff_ci', [None, None])[1]:+.2f}%]"
        grid = _grid_ratio(r)
        dx = r.get("diagnosis") or {}
        cause = dx.get("primary_label", "—")
        if dx.get("tag_labels"):
            cause += "（+" + "、".join(dx["tag_labels"]) + "）"
        fired = _triggered_rules(r)
        rows.append([
            str(i),
            f'<b>{_esc(spec.get("name", "?"))}</b>',
            f'<span class="badge {_VERDICT_CLASS.get(outcome, "warn")}" '
            f'style="font-size:12px;padding:2px 10px">{_esc(outcome)}</span>',
            _esc(cause),
            _n(_fills(r)),
            _signed(_worst_net(r)),
            _signed(_best_net(r)),
            _signed(_fee2_net(r)),
            ci_txt,
            _esc(_selectivity(r)),
            _signed(_oos_net(r)),
            "n/a" if grid is None else f"{grid * 100:.0f}%",
            _esc("、".join(fired) if fired else "无"),
        ])
        rc.append("hit" if outcome == "FALSIFIED" else
                  ("pass" if outcome == "NOT_FALSIFIED" else "weak"))
    a(_table(["#", "策略", "裁决", "死因", "成交", "最坏净", "最好净", "×2 成本净",
              "对照组 CI", "选择性", "样本外净", "样本外网格", "触发规则"],
             rows, num_cols={0, 4, 5, 6, 7, 10, 11}, row_class=rc))

    best = ranked[0]
    best_name = (best.get("spec") or {}).get("name", "第 1 名")
    best_ci = _ci_lower(best)
    if best_ci is not None and best_ci > 0:
        why = "对照组差值 CI 下界为正——它是这一组里唯一（或最能）证明自己胜过随机入场的。"
    elif best_ci is not None:
        why = "它的对照组 CI 下界最接近 0——未被证伪，但也远未被证明。"
    else:
        why = "无对照组数据，按样本外与网格表现排序。"
    a(f'<p class="sentence">稳健性第一：<b>{_esc(best_name)}</b>。{why}'
      f'<br>注意：这个排名只回答「哪一个最不过拟合」，不回答「该不该实盘」。</p>')

    a("<h2>每个策略的裁决理由</h2>")
    for i, r in enumerate(ranked, 1):
        spec = r.get("spec") or {}
        outcome = (r.get("verdict") or {}).get("outcome", "?")
        a("<details>" + (" open" if i <= 2 else "") + ">")
        a(f"<summary><b>{i}. {_esc(spec.get('name', '?'))}</b> —— "
          f'<span class="badge {_VERDICT_CLASS.get(outcome, "warn")}" '
          f'style="font-size:12px;padding:2px 10px">{_esc(outcome)}</span></summary>')
        a(f'<p>{_esc((r.get("verdict") or {}).get("reason", "（无理由）"))}</p>')
        dx = r.get("diagnosis") or {}
        if dx:
            cause = f'死因：<b>{_esc(dx.get("primary_label", dx.get("primary", "?")))}</b>'
            if dx.get("tag_labels"):
                cause += "（附带：" + _esc("、".join(dx["tag_labels"])) + "）"
            a(f"<p>{cause}</p>")
            if dx.get("prescriptions"):
                a(f'<p class="hypo">首要处方：{_esc(dx["prescriptions"][0])}</p>')
        chips = _key_numbers(r)
        if chips:
            a('<div class="chips">')
            for k in chips:
                a(f'<span class="chip">{_esc(k)}</span>')
            a("</div>")
        a(f'<p class="hypo">标的 {_esc(spec.get("symbol", "?"))} · {_esc(spec.get("interval", "?"))} · '
          f'{_esc(spec.get("venue", "?"))} · 单边费率 {float(spec.get("fee_rate", 0)) * 10000:.0f}bps · '
          f'{_esc(spec.get("fill_model", "?"))} 成交 · '
          f'{"含资金费" if spec.get("funding") else "不含资金费"}</p>')
        window = r.get("window") or {}
        if window:
            a(f'<p class="hypo">数据窗口：{window.get("bars", "?")} 根'
              f'（{_esc(window.get("from", "?"))} → {_esc(window.get("to", "?"))}）</p>')
        a("</details>")

    a("<footer>")
    a('<p>由策略证伪引擎生成。规则先于数字，失败同样发布。</p>')
    a('<p><a href="https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/">落地页</a> · '
      '<a href="https://github.com/Zhuyuyangyy/PaperTrade-Agent-V2">开源仓库</a></p>')
    a("</footer>")
    a("</div></body></html>")
    return "\n".join(h)


def render_comparison_html_to(results: list[dict], path, title: str = "多策略对比") -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_comparison_html(results, title=title), encoding="utf-8")

# ==== from diagnosis.py ================================================
"""Diagnosis: not just "the strategy died" but HOW, and what to do next.

The falsification battery answers "is this strategy real". A customer with a
real strategy on the line needs the next two answers: "which failure is this"
and "what would I even change". This module derives both from the SAME
measurements the battery already made -- no new runs, no new assumptions.

The classification rules below are the contract; they are declared here, in
code, before any strategy is classified -- the same rules-before-numbers
discipline the falsification rules follow. Priority order is part of the
contract: a strategy can trip several causes at once, and the PRIMARY cause
is the most fundamental one, because fixing a symptom while the root cause
stands is how people waste another six months.

Cause taxonomy (primary, by priority):
  insufficient  verdict is INCONCLUSIVE: not a death, a non-measurement.
                Nothing below it can be assessed honestly.
  randomness    entries cannot beat random entries (CI spans zero) or lose
                to them (CI entirely negative). The most fundamental
                failure: the entry logic contributes nothing (or worse).
  cost          the edge is REAL (control proves selectivity) but the book
                only survives at the declared fee -- doubling it kills the
                strategy. A toll problem, not an edge problem.
  edge_absent   the book loses even at 1x fees AND selectivity is not
                proven: no measurable edge at all.
  overfitting   neither of the above fired (selectivity IS proven), but the
                edge collapses out of sample or only exists in a few grid
                cells. Real in-sample, absent where it matters.
  barrier       the take-profit level is essentially never visited inside
                the hold window: a lottery structure wearing a backtest.
  survived      NOT_FALSIFIED. Reported with the standing caveat that this
                is the absence of disproof, not the presence of proof.

Prescriptions are actionable and numeric: each one quotes the customer's own
numbers from THIS run, because "reduce fees" is noise and "your +0.03%/trade
becomes -0.41%/trade at 8bps round trip" is a decision.
"""

# The taxonomy, in priority order. Kept as data so the report can print the
# whole ladder (a reader deserves to see what was ruled out and why).
CAUSES = [
    ("insufficient", "样本不足"),
    ("randomness", "随机性杀死"),
    ("cost", "成本杀死"),
    ("edge_absent", "无 edge"),
    ("overfitting", "过拟合杀死"),
    ("barrier", "结构杀死（彩票）"),
    ("survived", "未被证伪"),
]


def _rule_triggered(verdict: dict, rule_prefix: str) -> bool:
    return any(
        c.get("triggered") and str(c.get("rule", "")).startswith(rule_prefix)
        for c in verdict.get("checks") or []
    )


def _dx_pct(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:+.{digits}f}%"


def diagnose(result: dict) -> dict:
    """Classify the failure mode(s) of one verified strategy and prescribe.

    Returns {"primary": str, "primary_label": str, "tags": [...],
             "evidence": [str, ...], "prescriptions": [str, ...]}.
    """
    verdict = result.get("verdict") or {}
    outcome = verdict.get("outcome", "?")
    runs = result.get("runs") or {}
    worst = (runs.get("worst_case") or {}).get("stats") or {}
    ctrl = result.get("control") or {}
    selectivity = ctrl.get("selectivity")
    diff = ctrl.get("diff_pct")
    ci = ctrl.get("diff_ci") or [None, None]
    cost_rows = result.get("cost_sensitivity") or []
    row_x2 = next((r for r in cost_rows if r.get("fee_multiple") == 2), None)
    wf = result.get("walk_forward") or {}
    oos = (wf.get("out_sample") or {}).get("runs", {}).get("worst_case", {}).get("stats") or {}
    isb = (wf.get("in_sample") or {}).get("runs", {}).get("worst_case", {}).get("stats") or {}
    grid = wf.get("out_sample_grid") or {}
    br = result.get("barrier_reach") or {}

    worst_net = worst.get("sum_pct")
    x2_net = row_x2.get("net_sum_pct") if row_x2 else None
    evidence: list[str] = []
    tags: list[str] = []

    def ev(s: str) -> None:
        if s not in evidence:
            evidence.append(s)

    # ---- the classification tree, in priority order -----------------------
    if outcome == "INCONCLUSIVE":
        primary = "insufficient"
        min_fills = next(
            (c for c in verdict.get("checks") or [] if c.get("insufficient")), None)
        ev(f"成交 {worst.get('n', 0)} 笔，低于 spec 声明的证据门槛——"
           + (min_fills["detail"] if min_fills else "样本不足"))
    elif outcome == "NOT_FALSIFIED":
        # Nothing died: no cause below applies, however the numbers look.
        primary = "survived"
    elif selectivity == "refuted":
        primary = "randomness"
        ev(f"对照组：策略 − 随机 = {_dx_pct(diff)}/笔，95% 区间 [{_dx_pct(ci[0])}, {_dx_pct(ci[1])}] "
           f"整体为负——入场逻辑显著差于随机")
    elif selectivity == "unproven":
        primary = "randomness"
        ev(f"对照组：策略 − 随机 = {_dx_pct(diff)}/笔，95% 区间 [{_dx_pct(ci[0])}, {_dx_pct(ci[1])}] "
           f"跨 0——无法证明入场逻辑优于随机")
        if worst_net is not None and worst_net > 0:
            ev(f"账面净收益 {_dx_pct(worst_net)} 是真实的，但它与随机入场的差异在统计上不存在："
               "赚的是 beta，包装成 alpha")
    elif x2_net is not None and x2_net <= 0 < (worst_net or 0):
        primary = "cost"
        ev(f"×1 费率净 {_dx_pct(worst_net)}，×2 费率净 {_dx_pct(x2_net)}——edge 真实存在，"
           "但付不起加倍的 toll")
    elif worst_net is not None and worst_net <= 0:
        primary = "edge_absent"
        ev(f"×1 费率下净收益 {_dx_pct(worst_net)}，且对照组未能证明选择性——"
           "没有可测量的正期望")
    elif oos.get("sum_pct") is not None and isb.get("sum_pct") is not None:
        primary = "overfitting"
        ev(f"样本内净 {_dx_pct(isb['sum_pct'])} vs 样本外净 {_dx_pct(oos['sum_pct'])}——"
           "edge 在没见过的数据上撑不住")
    elif br.get("reached_tp_pct") is not None and br["reached_tp_pct"] < 10:
        primary = "barrier"
        ev(f"止盈位在持仓窗口内仅被摸到 {br['reached_tp_pct']:.1f}%——盈利期望是彩票结构")
    else:
        primary = "insufficient"  # FALSIFIED but none of the above: say so honestly
        ev("裁决为 FALSIFIED 但死因无法归入上述类别——请人工看第 7 节规则判定表")

    # ---- secondary tags (a strategy can fail several ways at once) --------
    def add_tag(t: str) -> None:
        if t not in tags:
            tags.append(t)

    if primary != "insufficient" and outcome == "INCONCLUSIVE":
        add_tag("insufficient")
    if selectivity in ("unproven", "refuted") and primary != "randomness":
        add_tag("randomness")
    if x2_net is not None and x2_net <= 0 < (worst_net or 0) and primary != "cost":
        add_tag("cost")
    if oos.get("sum_pct") is not None and isb.get("sum_pct") is not None \
            and oos["sum_pct"] < isb["sum_pct"] * 0.5 and primary != "overfitting":
        add_tag("overfitting")
        ev(f"样本外净 {_dx_pct(oos['sum_pct'])} 不足样本内 {_dx_pct(isb['sum_pct'])} 的一半")
    if grid.get("positive_ratio") is not None and grid["positive_ratio"] < 0.5 \
            and primary != "overfitting":
        add_tag("overfitting")
        ev(f"样本外参数网格仅 {grid.get('positive_cells', 0)}/{grid.get('n_cells', 0)} 格为正——"
           "edge 只在一小撮参数组合下存在")
    if br.get("reached_tp_pct") is not None and br["reached_tp_pct"] < 10 \
            and primary not in ("barrier", "insufficient"):
        add_tag("barrier")

    # ---- prescriptions: numeric, actionable, tied to THIS run --------------
    rx: list[str] = []
    if primary == "insufficient":
        rx.append(f"先解决样本量：成交 {worst.get('n', 0)} 笔不足以支撑任何结论。"
                  "扩窗口、加标的，或放宽入场阈值——但放宽后要在报告里重新声明 min_fills，"
                  "不允许用同一个宽松阈值既扩样本又保裁决")
    elif primary == "randomness":
        rx.append("把入场条件加严一个量级：趋势过滤（如只在 EMA 多头侧做多）+ 时段过滤 + "
                  "量能确认。信号数降到现在的 1/5 再测——宁少勿滥，30 笔真信号胜过 300 笔噪声")
        if worst_net is not None and worst_net > 0:
            rx.append(f"承认定位：这份账面（{_dx_pct(worst_net)}）大概率是 beta。若目标就是赚 beta，"
                      "该做的是定投/资产配置工具，不是策略引擎——先把目的想清楚，再决定要不要继续")
        else:
            rx.append(f"账面 {_dx_pct(worst_net)} 且打不过随机：回到假设阶段——这个 edge 的"
                      "市场学基础是什么（谁在犯错、错误定价为什么持续）？写不出来就不要回测了")
    elif primary == "cost":
        rx.append("三条路选一条：① 降频——当前每笔净 "
                  f"{_dx_pct((worst.get('mean_pct')))}，频率砍半则 toll 占比同步砍半；"
                  "② 改 maker 限价成交，单边费率通常减半；③ 拉长持仓周期，"
                  "让每笔毛利润覆盖 toll（当前 ×2 成本净 "
                  f"{_dx_pct(x2_net)}，这就是 toll 预算的上限）")
    elif primary == "edge_absent":
        rx.append("1x 费率下就亏损且无选择性证据：不要调参，调参是把噪声拟合得更漂亮。"
                  "回到策略假设本身，或换标的/周期重测")
    elif primary == "overfitting":
        rx.append("砍自由度：参数空间砍到 1-2 个自由度再测；若必须多参数，"
                  "用滚动前向验证（每折内重新选参）而不是单次划分")
        if grid.get("n_cells"):
            rx.append(f"当前网格 {grid.get('positive_cells', 0)}/{grid.get('n_cells', 0)} 格为正："
                      "把表现最好的格子附近再加密采样——如果 edge 真实，邻域应该活着；"
                      "邻域全死说明你拟合了一个尖峰")
    elif primary == "barrier":
        rx.append(f"止盈位只被摸到 {br.get('reached_tp_pct', 0):.1f}%：把止盈放到市场真实波动"
                  "距离上（用 ATR 或近期已实现波动校准），别用整数百分比的心理价位")
    elif primary == "survived":
        rx.append("未被证伪 ≠ 被证实。下一步是模拟盘跑满一个完整的样本外周期——"
                  "本引擎回答统计问题，不回答执行问题（滑点、成交率、心理）")

    for t in tags:
        if t == "cost" and primary != "cost":
            rx.append(f"附带问题：成本翻倍即亏损（×2 净 {_dx_pct(x2_net)}）——即使主死因修好了，"
                      "toll 预算仍是硬约束")
        if t == "overfitting" and primary != "overfitting":
            rx.append("附带问题：样本外/网格显示参数脆弱——主死因修好后，用更少的参数重测")

    label = dict(CAUSES).get(primary, primary)
    return {
        "primary": primary,
        "primary_label": label,
        "tags": tags,
        "tag_labels": [dict(CAUSES).get(t, t) for t in tags],
        "evidence": evidence,
        "prescriptions": rx,
    }

# ==== from __init__.py =================================================
"""Strategy falsification engine: path-aware replay with pre-declared kill criteria.

This package is the productised form of everything the PaperTrade research line
learned the hard way. A strategy comes in as a spec; what comes out is a report
that tries to KILL it, using the four instruments that survived contact with
real data:

  1. path-aware replay    -- bars are walked, not sampled; a bar containing both
                             a stop and a target resolves to the stop (worst
                             case) unless you ask for the optimistic run too.
  2. cost sensitivity     -- the toll is re-priced at 1x/2x/5x the assumed fee,
                             because the fee is the one parameter nobody
                             stress-tests and everybody inherits.
  3. barrier reachability -- are the take-profit and stop levels prices the
                             market actually visits inside the hold window? A
                             strategy whose barriers are never touched is a
                             lottery ticket, not an edge.
  4. random-entry control -- the same window, the same exit rule, entries drawn
                             at random. If the strategy cannot separate itself
                             from coin flips, its selectivity is unproven,
                             whatever the backtest curve says.
  5. walk-forward          -- the window is split into N folds and the last one
                             is re-measured with the full battery as an
                             out-of-sample segment. A criterion firing there
                             falsifies the whole strategy: the industry's
                             number-one cause of backtest->live decay
                             (median -14.3pp) is an edge that only existed
                             where its parameters were chosen.
  6. parameter grid        -- the exit parameters are gridded (values declared
                             in the spec, before the numbers) and every cell
                             is replayed. An edge that survives at one exact
                             TP/SL combination and nowhere else is a curve
                             fit; the distribution across cells, not the best
                             cell, is the measurement.

The verdict is not a score. It is the outcome of falsification criteria that
were written into the spec BEFORE the numbers were read. That ordering is the
whole point: a criterion chosen after seeing the report is marketing.

Nothing here predicts anything. It measures whether a claimed edge survives
honest execution, honest costs, and an honest control group.
"""


__all__ = ["Spec", "SpecError", "verify", "verify_file"]


def verify(spec: Spec, candles: list[dict], entries: dict[int, str],
           funding: list[dict] | None = None,
           exit_signals: dict[int, str] | None = None) -> dict:
    """Run the full falsification battery for one strategy over one price series.

    candles : [{'t': ms, 'o','h','l','c': float, 'v': float}, ...] ascending.
    entries : bar index -> 'long' | 'short', as produced by a signal source.
    exit_signals : optional bar index -> reason, for strategies whose exit
    logic is itself a signal (fills at the bar close).
    Returns a plain dict (JSON-serialisable) ready for report rendering.

    With spec.walk_forward.enabled the window is split into N equal folds; the
    last fold is out-of-sample and gets its own full battery (replay, cost
    sensitivity, barrier reachability, control) next to the in-sample battery
    of the earlier folds. A criterion firing on the out-of-sample segment
    falsifies the whole strategy: surviving only where you looked is the
    failure mode this instrument exists to catch.

    With spec.param_grid.enabled the declared exit parameters are gridded and
    every cell replayed. The grid follows the same scope convention as the
    rest of the engine: the full entry set when walk-forward is off, the
    in-sample and out-of-sample segments when it is on.
    """
    if not candles:
        raise ValueError("没有 K 线数据")
    if not entries:
        raise ValueError("没有入场信号")


    exit_rule = Exit(
        take_profit=spec.exit.take_profit,
        stop_loss=spec.exit.stop_loss,
        timeout_bars=spec.exit.timeout_bars,
        trailing=spec.exit.trailing,
        trailing_offset=spec.exit.trailing_offset,
        exit_signals=exit_signals or None,
    )

    full = _battery(spec, candles, entries, exit_rule, funding)
    result = {
        "spec": spec.to_dict(),
        "window": _window(candles),
        "n_signals": len(entries),
        "funding_applied": bool(spec.funding and funding),
        "runs": full["runs"],
        "cost_sensitivity": full["cost_sensitivity"],
        "barrier_reach": full["barrier_reach"],
        "control": full["control"],
        "verdict": full["verdict"],
        "walk_forward": None,
        "param_grid": None,
    }

    wf = None
    if spec.walk_forward.enabled:
        wf = _walk_forward(spec, candles, entries, exit_rule, funding)
        result["walk_forward"] = wf

    if spec.param_grid.enabled:
        result["param_grid"] = _param_grid_result(spec, candles, entries, exit_rule, funding, wf)

    result["verdict"] = _verdict(
        spec, full["runs"], full["cost_sensitivity"], full["control"],
        wf=wf,
        grids=result["param_grid"]["scopes"] if result["param_grid"] else None,
    )
    # Diagnosis: HOW it died and what to change. Same measurements, no new
    # assumptions -- the classification rules live in diagnosis.py, declared
    # before any strategy is classified.

    result["diagnosis"] = diagnose(result)
    return result


def _param_grid_result(spec: Spec, candles: list[dict], entries: dict[int, str],
                       exit_rule, funding: list[dict] | None, wf: dict | None) -> dict:
    """Assemble the param_grid block: the declaration plus one grid per scope
    (the full entry set, or the walk-forward segments when it is on)."""

    if wf is not None:
        scopes = [g for g in (wf.get("in_sample_grid"), wf.get("out_sample_grid")) if g]
    else:
        scopes = [run(spec, candles, entries, exit_rule, funding, scope="全窗口")]
    return {
        "axes": {k: list(v) for k, v in spec.param_grid.axes.items()},
        "axis_order": list(spec.param_grid.axes),
        "max_cells": spec.param_grid.max_cells,
        "scopes": scopes,
    }


def _battery(spec: Spec, candles: list[dict], entries: dict[int, str], exit_rule,
             funding: list[dict] | None = None, *,
             control_range: tuple[int, int] | None = None,
             scope: str = "全窗口") -> dict:
    """The full instrument battery over ONE entry set (one segment or the
    whole window). Kept separate from verify() so the walk-forward segments
    run exactly the same measurements as the headline run -- an instrument
    that measures the sample differently from the sample-out is not a control.
    """

    # --- 1. path-aware replay, worst case and best case side by side ---------
    runs = {}
    for case_name, worst in (("worst_case", spec.worst_case), ("best_case", not spec.worst_case)):
        sim = simulate(
            candles, entries, exit_rule,
            symbol=spec.symbol, fee_rate=spec.fee_rate,
            funding_series=funding if spec.funding else None,
            worst_case=worst, fill_model=spec.fill_model,
        )
        nets = sim.net_pct_series()
        runs[case_name] = {
            "fills": sim.closed,
            "unfilled": sim.unfilled_entries,
            "open_signals": sim.open_signals,
            "bars": sim.bars,
            "stats": equity_stats(nets),
            "by_reason": _by_reason(sim),
        }

    # --- 2. cost sensitivity: does the edge survive its own toll? -----------
    cost_rows = []
    for mult in spec.fee_multiples:
        sim = simulate(
            candles, entries, exit_rule,
            symbol=spec.symbol, fee_rate=spec.fee_rate * mult,
            funding_series=funding if spec.funding else None,
            worst_case=spec.worst_case, fill_model=spec.fill_model,
        )
        st = equity_stats(sim.net_pct_series())
        cost_rows.append({
            "fee_multiple": mult,
            # Round trip = both sides. The simulator deducts fee_rate*2 per
            # trade; showing the one-side number under a "round-trip" label
            # understated every toll in the table by exactly half.
            "fee_bps": spec.fee_rate * mult * 2 * 10_000,
            "net_sum_pct": st["sum_pct"],
            "profit_factor": st["profit_factor"],
            "win_pct": st["win_pct"],
        })

    # --- 3. barrier reachability -------------------------------------------
    reach = barrier_reach(candles, entries, exit_rule, fill_model=spec.fill_model)

    # --- 4. random-entry control -------------------------------------------
    ctrl = None
    if spec.control.enabled:
        try:
            ctrl = random_control(
                candles, exit_rule, spec, entries,
                n_random=spec.control.n_random, seed=spec.control.seed,
                funding=funding if spec.funding else None,
                bar_range=control_range,
            )
            ctrl = compare(
                runs["worst_case"]["stats"], ctrl, seed=spec.control.seed
            )
        except ValueError as exc:
            # A degenerate segment (e.g. a 1-bar out-of-sample fold) cannot
            # host a control group. Say so; never invent one.
            ctrl = {
                "n_random": 0, "seed": spec.control.seed, "side": None,
                "bar_range": list(control_range) if control_range else None,
                "note": f"该段无法抽取随机对照组：{exc}",
                "fills": 0, "unfilled": 0, "open_signals": 0,
                "stats": equity_stats([]),
                "strategy_mean_pct": None, "control_mean_pct": None,
                "diff_pct": None, "diff_ci": None, "selectivity": "unknown",
            }

    return {
        "n_signals": len(entries),
        "runs": runs,
        "cost_sensitivity": cost_rows,
        "barrier_reach": reach,
        "control": ctrl,
        "verdict": _verdict(spec, runs, cost_rows, ctrl, scope=scope),
    }


def _walk_forward(spec: Spec, candles: list[dict], entries: dict[int, str],
                  exit_rule, funding: list[dict] | None) -> dict:
    """Split the window into N folds and battery the last one as out-of-sample.

    In-sample = entries in folds 1..N-1, out-of-sample = entries in fold N.
    Both batteries simulate over the FULL candle series: a trade entered in
    the last fold exits whenever its exit rule says, and the bars after the
    fold are still the future relative to that entry -- truncating them would
    manufacture 'unresolved' trades that real trading would have closed.

    When the parameter grid is declared it runs per segment too (same entry
    split), and each segment's own verdict is recomputed with its grid --
    otherwise the WF table's per-segment verdict would disagree with the
    criteria table whenever a grid rule fires on one segment.
    """
    folds = spec.walk_forward.folds
    if len(candles) < folds:
        raise ValueError(
            f"窗口只有 {len(candles)} 根 K 线，不够切 {folds} 折"
            f"（walk_forward.folds 必须 ≤ 窗口 K 线数）"
        )
    fold_bars = len(candles) // folds
    boundary = (folds - 1) * fold_bars
    is_entries = {i: s for i, s in entries.items() if i < boundary}
    oos_entries = {i: s for i, s in entries.items() if i >= boundary}
    wf = {
        "folds": folds,
        "fold_bars": fold_bars,
        "boundary_bar": boundary,
        "boundary_ts": _fmt_ts(candles[boundary]["t"]),
        # the in-sample control may only draw from the in-sample segment: a
        # control that reaches into the out-of-sample fold would book that
        # fold's random losses as "control" and inflate the in-sample
        # selectivity it is supposed to be blind to
        "in_sample": _battery(spec, candles, is_entries, exit_rule, funding,
                              control_range=(CONTROL_WARMUP, boundary - 1),
                              scope="样本内") if is_entries else None,
        # the out-of-sample control may only draw entries from the out-of-
        # sample segment, or it is not a control for that segment
        "out_sample": _battery(spec, candles, oos_entries, exit_rule, funding,
                               control_range=(boundary, len(candles) - 2),
                               scope="样本外") if oos_entries else None,
        "in_sample_grid": None,
        "out_sample_grid": None,
    }
    if spec.param_grid.enabled:

        if is_entries:
            wf["in_sample_grid"] = run(
                spec, candles, is_entries, exit_rule, funding, scope="样本内")
        if oos_entries:
            wf["out_sample_grid"] = run(
                spec, candles, oos_entries, exit_rule, funding, scope="样本外")
        for seg, grid_key in (("in_sample", "in_sample_grid"),
                              ("out_sample", "out_sample_grid")):
            battery, seg_grid = wf.get(seg), wf.get(grid_key)
            if battery is not None and seg_grid is not None:
                battery["verdict"] = _verdict(
                    spec, battery["runs"], battery["cost_sensitivity"], battery["control"],
                    scope=seg_grid["scope"], grids=[seg_grid],
                )
    return wf


def verify_file(spec_path, candles, entries, funding=None) -> dict:
    """Convenience: load a spec from JSON and run the battery."""
    return verify(Spec.load(spec_path), candles, entries, funding=funding)


def _by_reason(sim) -> dict:
    out: dict[str, dict] = {}
    for f in sim.fills:
        b = out.setdefault(f.reason, {"n": 0, "net_sum_pct": 0.0})
        b["n"] += 1
        b["net_sum_pct"] += f.net_pct * 100
    return out


def _fmt_ts(ms: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _window(candles: list[dict]) -> dict:
    return {"from": _fmt_ts(candles[0]["t"]), "to": _fmt_ts(candles[-1]["t"]), "bars": len(candles)}


def _outcome(checks: list[dict]) -> str:
    if any(c.get("triggered") for c in checks):
        return "FALSIFIED"
    if any(c.get("insufficient") for c in checks):
        return "INCONCLUSIVE"
    return "NOT_FALSIFIED"


def _evaluate_criteria(spec: Spec, runs: dict, cost_rows: list[dict], ctrl: dict | None,
                       scope: str, prefix: str = "") -> list[dict]:
    """Apply the spec's PRE-DECLARED criteria to one battery.

    The criteria live in the spec and were fixed before the report existed;
    this function only reads them and the numbers, it never invents a new
    rule. `scope` labels which segment the numbers came from so the report
    can show it; the same declared rule applied to the out-of-sample segment
    is the same rule, not a new one.
    """
    checks = []
    worst = runs["worst_case"]["stats"]

    # The spec declared its own evidence bar (min_fills). Below it, NOTHING
    # else is evaluated: a net rule firing on 8 fills would let a weak sample
    # produce a strong verdict the spec itself said it did not want, and
    # "insufficient" would stop being a verdict and become a footnote.
    min_fills = spec.falsify_if.get("min_fills", 30)
    if worst["n"] < min_fills:
        checks.append({
            "rule": "min_fills",
            "scope": scope,
            "detail": (f"{prefix}成交 {worst['n']} 笔 < 声明下限 {min_fills}，"
                       f"样本不足以下结论（其余规则未评估）"),
            "triggered": False,
            "insufficient": True,
        })
        return checks

    # A segment with ZERO fills has no measured net: sum_pct is 0.0 because
    # nothing traded, not because the strategy broke even. Firing a net rule
    # there would falsify on no evidence -- the same line the grid draws
    # ("无证据不算输"). With at least one fill the net IS measured and the
    # declared rule stands.
    measured = worst["n"] > 0

    if spec.falsify_if.get("worst_case_net_negative") and measured and worst["sum_pct"] <= 0:
        checks.append({
            "rule": "worst_case_net_negative",
            "scope": scope,
            "detail": f"{prefix}最坏优先口径净收益 {worst['sum_pct']:+.2f}% ≤ 0",
            "triggered": True,
        })

    mult = spec.falsify_if.get("net_negative_at_fee_multiple")
    if mult is not None:
        row = next((r for r in cost_rows if r["fee_multiple"] == mult), None)
        if row is not None and measured and row["net_sum_pct"] <= 0:
            checks.append({
                "rule": f"net_negative_at_fee_multiple={mult}",
                "scope": scope,
                "detail": f"{prefix}成本 ×{mult:g}（往返 {row['fee_bps']:.0f}bps）时净收益 {row['net_sum_pct']:+.2f}% ≤ 0，edge 付不起 toll",
                "triggered": True,
            })

    if spec.falsify_if.get("control_lift_ci_includes_zero") and ctrl is not None:
        lo, hi = ctrl.get("diff_ci") or [None, None]
        # "Includes zero" was too narrow: an interval entirely BELOW zero is
        # not a pass, it is the strategy being significantly WORSE than coin
        # flips. The rule fires unless selectivity is PROVEN (lo > 0).
        if lo is not None and not lo > 0:
            if hi is not None and hi < 0:
                detail = (f"{prefix}策略 − 随机对照组 = {ctrl['diff_pct']:+.2f}%，"
                          f"95% 区间 [{lo:+.2f}, {hi:+.2f}] 整体为负——显著差于随机入场")
            else:
                detail = (f"{prefix}策略 − 随机对照组 = {ctrl['diff_pct']:+.2f}%，"
                          f"95% 区间 [{lo:+.2f}, {hi:+.2f}] 跨 0，未能证明选择性")
            checks.append({
                "rule": "control_lift_ci_includes_zero",
                "scope": scope,
                "detail": detail,
                "triggered": True,
            })
    return checks


def _grid_checks(spec: Spec, grid: dict, scope: str, prefix: str = "") -> list[dict]:
    """Apply the PRE-DECLARED grid criterion to one grid scope.

    The rule (`falsify_if.grid_positive_ratio_min`) lives in the spec and was
    fixed before the report existed; this function only reads it and the
    measured ratio -- it never invents a threshold after seeing the cells. A
    grid where nothing traded is INCONCLUSIVE: no evidence is not a failure,
    and it is certainly not a pass.
    """
    checks: list[dict] = []
    min_ratio = spec.falsify_if.get("grid_positive_ratio_min")
    if min_ratio is None:
        return checks
    if grid["cells_with_fills"] == 0:
        checks.append({
            "rule": "grid_positive_ratio_min",
            "scope": scope,
            "detail": (f"{prefix}参数网格 {grid['n_cells']} 格全部无成交，"
                       f"无法评估参数敏感性"),
            "triggered": False,
            "insufficient": True,
        })
        return checks
    if grid["positive_ratio"] < float(min_ratio):
        no_fill = (f"（另有 {grid['no_fill_cells']} 格无成交，不计入占比）"
                   if grid["no_fill_cells"] else "")
        if grid["positive_cells"] == 0:
            tail = (f"参数网格 0/{grid['cells_with_fills']} 格为正{no_fill}——"
                    f"网格内没有任何参数组合赚钱，edge 在这个参数空间里不存在，更谈不上稳健")
        else:
            tail = (f"参数网格 {grid['positive_cells']}/{grid['cells_with_fills']} 格为正"
                    f"（{grid['positive_ratio']:.0%}）< 声明下限 {float(min_ratio):.0%}{no_fill}——"
                    f"edge 只在一小撮参数组合下存在，是曲线拟合不是 edge")
        checks.append({
            "rule": "grid_positive_ratio_min",
            "scope": scope,
            "detail": f"{prefix}{tail}",
            "triggered": True,
        })
    return checks


def _verdict(spec: Spec, runs: dict, cost_rows: list[dict], ctrl: dict | None,
             scope: str = "全窗口", wf: dict | None = None,
             grids: list[dict] | None = None) -> dict:
    """Evaluate the pre-declared falsification criteria. Criteria live in the
    spec and were fixed before the report existed; this function only reads
    them and the numbers, it never invents a new rule.

    When a walk-forward split is present, the same declared criteria are also
    evaluated on the out-of-sample segment, and a rule firing THERE falsifies
    the whole strategy -- surviving only where you looked is the failure mode
    the walk-forward instrument exists to catch. Too few out-of-sample fills
    is INCONCLUSIVE, never a pass.

    `grids` carries one parameter-grid result per scope (the full entry set,
    or the walk-forward segments). The declared grid criterion is evaluated
    on every scope; a knife-edge grid falsifies wherever it was measured.
    """
    checks = _evaluate_criteria(spec, runs, cost_rows, ctrl, scope=scope)
    for grid in (grids or []):
        prefix = ""
        if wf is not None and grid["scope"] == "样本外":
            prefix = f"样本外段（第 {wf['boundary_bar']} 根起）："
        checks.extend(_grid_checks(spec, grid, scope=grid["scope"], prefix=prefix))

    if wf is not None:
        oos = wf.get("out_sample")
        if oos is None:
            checks.append({
                "rule": "min_fills",
                "scope": "样本外",
                "detail": (f"样本外段（第 {wf['boundary_bar']} 根起）没有任何入场信号"
                           f"——规则在该段未触发，无法评估样本外表现"),
                "triggered": False,
                "insufficient": True,
            })
        else:
            checks.extend(_evaluate_criteria(
                spec, oos["runs"], oos["cost_sensitivity"], oos["control"],
                scope="样本外",
                prefix=f"样本外段（第 {wf['boundary_bar']} 根起）：",
            ))

    triggered = [c for c in checks if c.get("triggered")]
    insufficient = [c for c in checks if c.get("insufficient")]
    if triggered:
        outcome = "FALSIFIED"
        # Group by scope and say the scope ONCE: four lines each starting
        # "样本外段（第 19564 根起）：" read like a broken record, and a
        # record that skips is how readers skim past the actual findings.
        by_scope: dict[str, list[str]] = {}
        for c in triggered:
            by_scope.setdefault(c.get("scope", "全窗口"), []).append(c["detail"])
        parts = []
        for scope, details in by_scope.items():
            if scope == "全窗口":
                parts.extend(details)
            else:
                stripped = [d.split("：", 1)[1] if "：" in d else d for d in details]
                parts.append(f"{scope}段：" + "；".join(stripped))
        reason = f"{len(triggered)} 条事前证伪规则被触发：" + "；".join(parts)
    elif insufficient:
        outcome = "INCONCLUSIVE"
        scopes = "、".join(dict.fromkeys(c["scope"] for c in insufficient))
        reason = (f"样本量不足（{scopes}），既不能证实也不能证伪"
                  f"——先去拿更多数据，不要凭这份报告做任何决定。")
    else:
        outcome = "NOT_FALSIFIED"
        reason = (
            f"在本窗口、本成本假设、本对照组下未触发任何事前证伪规则"
            f"（最坏口径净 {runs['worst_case']['stats']['sum_pct']:+.2f}% / "
            f"最好口径 {runs['best_case']['stats']['sum_pct']:+.2f}%）。"
        )
        if wf is not None:
            reason += "样本外段同样未触发任何规则。"
            reason += "注意：未被证伪 ≠ 被证实；参数是否曾用全窗口数据调优，引擎无法验证。"
        elif not grids:
            reason += "注意：未被证伪 ≠ 被证实，样本外表现与参数过拟合不在本引擎的射程内。"
        else:
            reason += "注意：未被证伪 ≠ 被证实；参数网格只测量参数脆弱性（且在窗口内），不构成样本外证据。"
        for grid in (grids or []):
            if grid["cells_with_fills"]:
                reason += (f"参数网格（{grid['scope']}）"
                           f"{grid['positive_cells']}/{grid['cells_with_fills']} 格为正。")
    return {"outcome": outcome, "reason": reason, "checks": checks}

# ==== from __main__.py =================================================
"""CLI: run the falsification battery from the command line.

    python -m research.verify --list-market
    python -m research.verify --spec spec.json [--candles klines.csv] [--out md]
    python -m research.verify --demo [--out md]

Data resolution order for candles: --candles CSV first, then the local market
cache (runtime/audit/market.sqlite) filtered by the spec's symbol/interval/
venue. Signals come from the spec: kind=csv reads --signals (or signals.path),
kind=rule computes from the candles, kind=random is the control by itself.
"""

import argparse
import json
import sys
from pathlib import Path


# Dual-layout path resolution. The SAME file runs in two places:
#   in-repo:      research/verify/__main__.py      (repo root has runtime/)
#   vendored:     skill/strategy-falsifier/engine/__main__.py (no repo around)
# In both layouts the demo bundle sits next to this file's package root, so a
# standalone ZIP runs with zero repository context.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
ROOT = _REPO_ROOT if (_REPO_ROOT / "runtime" / "audit").is_dir() else _HERE.parent
DEFAULT_MARKET = (ROOT / "runtime" / "audit" / "market.sqlite"
                  if (ROOT / "runtime" / "audit").is_dir() else None)
# Self-contained demo bundle: a fresh clone has no market cache (sqlite is
# gitignored), so the demo must work from committed CSVs alone.
_SAMPLES = next((p for p in (_HERE / "samples", ROOT / "samples") if (p / "demo_candles.csv").is_file()),
                _HERE / "samples")
BUNDLED_CANDLES = _SAMPLES / "demo_candles.csv"
BUNDLED_SPEC = _SAMPLES / "demo_spec_csv.json"
# The funnel tail printed after every run: where to see the 30-second demo
# and the case matrix, and what the first 10 orders cost (nothing).
LANDING_PAGE = "https://zhuyuyangyy.github.io/PaperTrade-Agent-V2/"

# The spec file currently being processed, so relative paths inside it resolve
# against the spec's own directory. Set by main() before any loading happens.
_spec_path: list = [None]

DEMO_SPEC = {
    "name": "rsi-dip-btc-1h-demo",
    "hypothesis": "BTC 1 小时 RSI(14) 低于 30 后做多，捕捉均值回复",
    "source": "内置演示（规则信号；Agensi 单文件版使用文件内确定性合成 K 线，真实审计请用 --candles 提供 CSV）",
    "symbol": "BTCUSDT",
    "interval": "1h",
    "venue": "futures",
    "fee_rate": 0.0004,
    "fill_model": "taker",
    "funding": True,
    "exit": {"take_profit": 0.03, "stop_loss": -0.08, "timeout_bars": 24},
    "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 30, "side": "long"},
    "control": {"enabled": True, "n_random": 200, "seed": 42},
    "walk_forward": {"enabled": True, "folds": 3},
    "param_grid": {"axes": {"take_profit": [0.02, 0.03, 0.04],
                            "stop_loss": [-0.06, -0.08, -0.10]}},
    "fee_multiples": [1, 2, 5],
    "falsify_if": {
        "control_lift_ci_includes_zero": True,
        "net_negative_at_fee_multiple": 2,
        "worst_case_net_negative": True,
        "grid_positive_ratio_min": 0.5,
        "min_fills": 30,
    },
}


def _load_candles(args, spec: Spec) -> list[dict]:
    if args.candles:
        return load_candles_csv(args.candles)
    # The spec's own candle file (relative to the spec's directory) travels
    # with the spec folder -- this is how --compare mixes strategies over
    # different data sources, each disclosed in the comparison.
    if spec.candles_path:
        p = _resolve_against_spec(_spec_path[0], spec.candles_path)
        if p.is_file():
            return load_candles_csv(p)
        raise CandleError(f"spec 声明的 candles_path 不存在: {p}")
    market = Path(args.market) if args.market else DEFAULT_MARKET
    if market is not None and market.is_file():
        return load_candles_market(market, spec.symbol, spec.interval, spec.venue)
    # No cache (fresh clone or standalone skill): fall back to the committed
    # demo bundle so the demo always runs. Numbers differ slightly from the
    # cached run (no funding series ships in the bundle) and the report
    # discloses that.
    if args.demo:
        print(f"[提示] 无行情缓存，使用内置演示数据 {BUNDLED_CANDLES.name}", file=sys.stderr)
        return _synthetic_demo_candles()
    where = market if market is not None else "（独立运行，无仓库行情缓存）"
    raise CandleError(
        f"找不到行情缓存 {where}。请用 --candles 提供 CSV（列 t,o,h,l,c），"
        "或用 --list-market 查看可用缓存。"
    )


def _build_entries(args, spec: Spec, candles: list[dict]) -> dict[int, str]:

    # A signal file passed to a rule/random spec used to be SILENTLY ignored:
    # the customer's own signals never got tested while the report quietly
    # audited the built-in rule instead. That is buying the wrong answer, so
    # it is now a hard error.
    if args.signals and spec.signals.kind in ("rule", "random"):
        raise SpecError(
            f"spec 声明 signals.kind='{spec.signals.kind}'，但又传了 --signals {args.signals}。"
            f"引擎不会猜测你意图：要么把 spec 改成 kind='csv' 测你自己的信号，"
            f"要么去掉 --signals 测内置规则。"
        )

    if spec.signals.kind == "rule":
        entries = rule_signals(
            candles, rule=spec.signals.rule, period=spec.signals.period,
            threshold=spec.signals.threshold, side=spec.signals.side,
        )
        if not entries:
            raise SpecError(f"规则 {spec.signals.rule} 在窗口内没有产生任何信号（阈值 {spec.signals.threshold}）")
        return entries
    if spec.signals.kind == "random":
        return random_signals(candles, spec.signals.n_random, spec.signals.seed, side=spec.signals.side)
    path = args.signals or spec.signals.path
    if not path:
        raise SpecError("signals.kind='csv' 需要 --signals 或 spec.signals.path")
    # A relative path inside the spec resolves against the SPEC's directory
    # first (a spec folder travels together), not the engine's cwd -- the
    # exit-signal loader already did this; entries silently did not.
    path = str(_resolve_against_spec(_spec_path[0], path))
    entries, stats = load_signals_csv(path, candles)
    if stats["total"] and not entries:
        raise CandleError(
            f"信号文件 {path} 有 {stats['total']} 行但无一映射到 K 线窗口内"
            f"（窗口 {candles[0]['t']} → {candles[-1]['t']}）"
        )
    if stats["bad_rows"] or stats["skipped_after_end"] or stats["skipped_before_start"]:
        print(
            f"[信号提示] 共 {stats['total']} 行：映射 {stats['mapped']}，"
            f"窗口外跳过 {stats['skipped_after_end'] + stats['skipped_before_start']}，"
            f"坏行 {stats['bad_rows']}",
            file=sys.stderr,
        )
    return entries


def _resolve_against_spec(spec_path: str | None, rel: str) -> Path:
    """A relative path in a spec resolves against the spec's own directory
    first (so a spec folder travels together), then against the CWD."""
    p = Path(rel)
    if p.is_absolute() or p.is_file():
        return p
    if spec_path:
        cand = Path(spec_path).resolve().parent / rel
        if cand.is_file():
            return cand
    return p


def _load_exit_signals(spec: Spec, candles: list[dict]) -> dict[int, str]:
    path = _resolve_against_spec(_spec_path[0], spec.exit.exit_signals_path)
    sigs = load_exit_signals_csv(path, candles)
    if not sigs:
        raise CandleError(f"退出信号文件 {path} 没有映射到 K 线窗口内的信号")
    return sigs


def _q(s: str) -> str:
    """Quote a path only when it needs it, so the repro command stays
    copy-pasteable on Windows shells."""
    return f'"{s}"' if " " in s else s


def _repro_info(args, spec: Spec, raw_argv: list[str]) -> dict:
    """The exact command and input paths behind this run, so the report ends
    with its own recipe. Built from what was actually passed -- never guessed."""
    # Layout-aware: the same file runs in-repo (python -m research.verify) and
    # vendored inside the standalone skill (python -m engine).
    cmd = "python -m research.verify" if (ROOT / "research").is_dir() else "python -m engine"
    if raw_argv:
        cmd += " " + " ".join(_q(a) for a in raw_argv)
    market_path = Path(args.market) if args.market else DEFAULT_MARKET
    has_cache = market_path is not None and market_path.is_file()
    if args.demo:
        if has_cache or not BUNDLED_SPEC.is_file():
            spec_src = "内置演示 spec（--demo；DEMO_SPEC 定义于 research/verify/__main__.py）"
        else:
            spec_src = f"{BUNDLED_SPEC}（内置离线演示 spec）"
    else:
        spec_src = str(args.spec)
    if args.candles:
        candles_src = str(args.candles)
    elif spec.candles_path:
        candles_src = f"{spec.candles_path}（spec 自带，相对 spec 目录解析）"
    elif has_cache:
        candles_src = f"{market_path}（本地行情缓存 sqlite）"
    elif args.demo:
        candles_src = "_synthetic_demo_candles()（本文件内确定性合成数据，seed=42）"
    else:
        candles_src = f"{market_path}（未找到）"
    k = spec.signals.kind
    if k == "rule":
        sig_src = (f"规则信号（{spec.signals.rule}，period={spec.signals.period:g}，"
                   f"threshold={spec.signals.threshold:g}，由 K 线实时计算，无需外部文件）")
    elif k == "random":
        sig_src = f"随机信号（n={spec.signals.n_random}，seed={spec.signals.seed}）"
    else:
        path = args.signals or spec.signals.path
        sig_src = str(path) if path else "未提供（signals.kind=csv 但无路径）"
    if spec.funding_path:
        funding_src = f"{spec.funding_path}（spec 自带资金费序列）"
    elif spec.funding and not (args.candles or spec.candles_path) and market_path is not None:
        funding_src = f"{market_path}（本地行情缓存 funding 表）"
    elif spec.funding:
        funding_src = "无序列，按 0 计"
    else:
        funding_src = "不适用（spec.funding=false）"
    return {"command": cmd, "spec": spec_src, "candles": candles_src, "signals": sig_src,
            "funding": funding_src}


def _print_footer_promo() -> None:
    """The funnel tail: every run -- report to stdout or to a file -- ends
    with the landing page and the free-first-10-orders offer."""
    print()
    print("-" * 60)
    print(f"落地页（30 秒演示 · 真实案例矩阵）：{LANDING_PAGE}")
    print("服务：带策略来，我出证伪报告 —— 首 10 单免费（换匿名公开案例授权），付费档 $99 / $299。")
    print("-" * 60)


def _run_one(args, spec_path: str | None, raw_argv: list[str]) -> dict:
    """Run the full battery for ONE spec and return the result dict.

    Shared by the single-strategy path and --compare (which calls it once per
    spec). spec_path=None means the built-in demo.
    """
    market_path = Path(args.market) if args.market else DEFAULT_MARKET
    if args.demo and spec_path is None:
        # With a cache the demo runs the full 3-year series; without one it
        # falls back to the committed bundle (no funding series ships with
        # it, so the bundled spec declares funding: false and says so).
        if True:
            spec = Spec.from_dict(DEMO_SPEC)
        else:
            spec = Spec.load(BUNDLED_SPEC)
    else:
        spec = Spec.load(spec_path)
        _spec_path[0] = spec_path
    candles = _load_candles(args, spec)
    entries = _build_entries(args, spec, candles)
    exit_signals = None
    if spec.exit.exit_signals_path:
        exit_signals = _load_exit_signals(spec, candles)
    funding = None
    # Funding resolution: the spec's own funding file first (it travels with
    # the spec), then the cache -- but only when the candles ALSO come from
    # that cache, so rates never get applied across mismatched windows.
    if spec.funding_path:
        fp = _resolve_against_spec(_spec_path[0], spec.funding_path)
        if fp.is_file():
            funding = load_funding_csv(fp)
        else:
            raise CandleError(f"spec 声明的 funding_path 不存在: {fp}")
    elif spec.funding and not (args.candles or spec.candles_path) and market_path is not None:
        funding = load_funding(market_path, spec.symbol)
        if not funding:
            print("[提示] 缓存中没有该 symbol 的资金费数据，按 0 处理", file=sys.stderr)

    result = verify(spec, candles, entries, funding=funding, exit_signals=exit_signals)
    result["repro"] = _repro_info(args, spec, raw_argv)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="research.verify", description="策略证伪引擎：path-aware 回放 + 成本敏感性 + 障碍可达性 + 随机对照组")
    ap.add_argument("--spec", help="策略 spec JSON 路径")
    ap.add_argument("--demo", action="store_true", help="运行内置演示（BTC 1h RSI 规则，数据取自本地行情缓存）")
    ap.add_argument("--compare", nargs="+", metavar="SPEC",
                    help="对比模式：2 个以上 spec 一次跑，输出多策略对比矩阵（排名规则事前声明）")
    ap.add_argument("--candles", help="K 线 CSV（列 t,o,h,l,c），优先于行情缓存；对比模式下所有策略共用")
    ap.add_argument("--signals", help="信号 CSV（列 t[,side]），kind=csv 时使用；对比模式下对所有 spec 生效")
    ap.add_argument("--market", help=f"行情缓存 sqlite（默认 {DEFAULT_MARKET}）")
    ap.add_argument("--out", help="报告输出路径（markdown；.html 输出 HTML 版）；缺省打印到 stdout")
    ap.add_argument("--json", action="store_true", help="额外输出机器可读 JSON（与 --out 同名 .json）")
    ap.add_argument("--list-market", action="store_true", help="列出本地行情缓存内容后退出")
    args = ap.parse_args(argv)
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    if args.compare:
        if args.demo:
            ap.error("--compare 与 --demo 不能同用（对比需要显式 spec）")
        if args.spec:
            ap.error("--compare 与 --spec 不能同用（把全部 spec 都给 --compare）")
        if len(args.compare) < 2:
            ap.error("--compare 至少需要 2 个 spec；单策略请用 --spec")
    elif not args.spec and not args.demo:
        ap.error("必须提供 --spec 或 --demo（或用 --list-market 查看缓存）")

    market = Path(args.market) if args.market else DEFAULT_MARKET
    if args.list_market:
        if market is None:
            print("独立运行模式：无仓库行情缓存可用（--list-market 需要缓存）", file=sys.stderr)
            return 2
        try:
            rows = describe_market_cache(market)
        except Exception as exc:  # noqa: BLE001 - CLI boundary, message is the product
            print(f"无法读取行情缓存 {market}: {exc}", file=sys.stderr)
            return 2
        print(f"{market} · {len(rows)} 组序列")
        print(f"{'symbol':<12}{'interval':<10}{'venue':<10}{'bars':>8}  from → to")
        import datetime as _dt

        for sym, itv, ven, n, t0, t1 in rows:
            f = lambda ms: _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
            print(f"{sym:<12}{itv:<10}{ven:<10}{n:>8}  {f(t0)} → {f(t1)}")
        return 0

    try:
        if args.compare:

            results = []
            for sp in args.compare:
                _spec_path[0] = None  # each spec resolves its own relative paths
                results.append(_run_one(args, sp, raw_argv))
            md = render_comparison(results)
            payload = results
        else:
            result = _run_one(args, args.spec, raw_argv)
            md = render(result)
            payload = result
    except (SpecError, CandleError, ValueError) as exc:
        print(f"[输入错误] {exc}", file=sys.stderr)
        return 2

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".html":

            if args.compare:
                render_comparison_html_to(results, out)
            else:
                render_to(result, out)
            print(f"HTML 报告已写入 {out}")
        else:
            out.write_text(md, encoding="utf-8")
            print(f"报告已写入 {out}")
        if args.json:
            jpath = out.with_suffix(".json")
            jpath.write_text(render_json(payload), encoding="utf-8")
            print(f"JSON 已写入 {jpath}")
    else:
        print(md)
        if args.json:
            print(render_json(payload))
    _print_footer_promo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

if __name__ == "__main__":
    raise SystemExit(main())
