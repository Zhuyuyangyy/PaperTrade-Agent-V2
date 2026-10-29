"""Signal sources: rule-based, random (control), and CSV.

The random generator is not a toy -- it IS the control group. It draws entry
bars uniformly from the same window with the same count as the strategy under
test, so the only difference between the two books is where the entries came
from. Anything the strategy shows beyond that difference is selectivity;
anything it does not show is noise with extra steps.
"""
from __future__ import annotations

import random

from ..audit.pathsim import Exit


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


def random_signals(candles: list[dict], n: int, seed: int, side: str = "long",
                   warmup: int = 30) -> dict[int, str]:
    """Uniform random entry bars in [warmup, len-2], with replacement allowed.

    With replacement: the control is about entry *timing* distribution, not
    about cardinality. Two random signals may share a bar; that is fine and it
    is disclosed in the report.
    """
    rng = random.Random(seed)
    hi = len(candles) - 2
    if hi <= warmup:
        raise ValueError(f"数据窗口太短（{len(candles)} 根），无法生成随机信号")
    return {rng.randint(warmup, hi): side for _ in range(n)}


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
