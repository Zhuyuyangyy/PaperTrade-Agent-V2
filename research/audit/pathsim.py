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
from __future__ import annotations

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
