"""Dynamic copy sizing: the position scales with the target wallet's own fill.

Idea from a production Solana copy-trading bot (BUY_AMOUNT_PERCENTAGE): a wallet
putting $5k behind a token is making a stronger statement than one putting $50,
and a fixed clip throws that information away. Same rule here, with the bounds
a paper book needs: a floor so a small signal is still worth filling, a cap so a
whale's size can never empty the book, and a written reason for every size so
the replay can tell 'sized down' from 'fixed'.
"""
from __future__ import annotations


def dynamic_notional(event_usd: float, risk: dict) -> tuple[float, str]:
    """Return (notional, reason). Falls back to the fixed clip when unset."""
    base = float(risk.get("notional_usdt", 100.0))
    ratio = risk.get("copy_ratio")
    if ratio is None:
        return base, "fixed"
    try:
        scaled = float(event_usd) * float(ratio)
    except (TypeError, ValueError):
        return base, "fixed"
    lo = float(risk.get("min_notional_usdt", 25.0))
    hi = float(risk.get("max_notional_usdt", 150.0))
    if hi < lo:  # a misconfigured cap must not invert the range
        lo, hi = hi, lo
    if scaled < lo:
        return lo, f"{ratio:g}x ${float(event_usd):,.0f} -> floor ${lo:,.0f}"
    if scaled > hi:
        return hi, f"{ratio:g}x ${float(event_usd):,.0f} -> cap ${hi:,.0f}"
    return round(scaled, 2), f"{ratio:g}x ${float(event_usd):,.0f}"
