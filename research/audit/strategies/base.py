"""Shared indicators and the strategy contract.

Indicators are plain pandas -- the repo has no TA-Lib and a research audit
should not depend on a C extension it cannot rebuild. Each function is small
enough to eyeball against a reference implementation.

The strategy contract: given candles (and, for the funding strategy, the
funding series), produce a mapping of bar index -> side. That is all. Exits
are declared separately as an Exit rule so the simulator, not the strategy,
decides what a bar containing both a stop and a target means.
"""
from __future__ import annotations

from dataclasses import dataclass


def ema(series, period: int):
    return series.ewm(span=period, adjust=False).mean()


def sma(series, period: int):
    return series.rolling(period, min_periods=max(2, period // 2)).mean()


def rsi(series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, 1e-12)
    return 100 - 100 / (1 + rs)


def macd(series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bollinger(series, period: int = 20, stds: float = 2.0):
    mid = sma(series, period)
    sd = series.rolling(period, min_periods=max(2, period // 2)).std()
    return mid + stds * sd, mid, mid - stds * sd


def true_range(df):
    prev_close = df["c"].shift(1)
    return _frame_max(
        df["h"] - df["l"],
        (df["h"] - prev_close).abs(),
        (df["l"] - prev_close).abs(),
    )


def atr(df, period: int = 14):
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df, period: int = 14):
    """Wilder's ADX. +DI/-DI from smoothed directional movement."""
    up = df["h"].diff()
    down = -df["l"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0.0, 1e-12)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / tr.replace(0.0, 1e-12)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-12)
    return dx.ewm(alpha=1 / period, adjust=False).mean(), plus_di, minus_di


def obv(df):
    direction = df["c"].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * df["v"]).cumsum()


def _frame_max(*series):
    out = series[0]
    for s in series[1:]:
        out = out.where(out >= s, s)
    return out


@dataclass
class Strategy:
    """A candidate: signals, an exit rule, and where it came from."""

    name: str
    source: str            # repo + file it was ported from
    timeframe: str
    pairs: list[str]
    exit_rule: object      # research.audit.pathsim.Exit
    hypothesis: str
    falsify: str
    venue: str = "spot"    # which Binance book this strategy actually trades

    def signals(self, candles: list[dict], **kwargs) -> dict[int, str]:
        """bar index -> 'long' | 'short'. Override in subclasses."""
        raise NotImplementedError
