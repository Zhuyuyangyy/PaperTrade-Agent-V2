"""技术指标（Sprint 1）——纯函数、Decimal、与输入等长。

纪律：
  - 所有函数返回与输入等长的列表，前导不足处为 ``None``（不缩短序列，
    否则调用方无法把指标和 K 线对齐）；
  - 不做"看起来更平滑"的自创口径：EMA/MACD/RSI/ATR/ADX 全部按标准定义；
  - 只吃调用方给的已收盘序列（本模块不判断 K 线是否收盘）；
  - 同一输入 → 同一输出（无随机、无全局状态）。
"""
from __future__ import annotations

from decimal import Decimal

BPS = Decimal("10000")


def _d(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def sma(values: list[Decimal], period: int) -> list[Decimal | None]:
    if period <= 0:
        raise ValueError("period 必须 > 0")
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    window = sum(values[:period], Decimal("0"))
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: list[Decimal], period: int) -> list[Decimal | None]:
    """标准 EMA：SMA 种子，之后 k = 2/(period+1)。"""
    if period <= 0:
        raise ValueError("period 必须 > 0")
    out: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period], Decimal("0")) / period
    out[period - 1] = seed
    k = Decimal("2") / Decimal(period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (Decimal("1") - k)
        out[i] = prev
    return out


def macd(values: list[Decimal], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """MACD 线 / 信号线 / 柱（柱 = MACD − 信号）。"""
    if fast >= slow:
        raise ValueError("fast 必须小于 slow")
    ef, es = ema(values, fast), ema(values, slow)
    line: list[Decimal | None] = [
        (a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)
    ]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[Decimal | None] = [None] * len(values)
    if first is not None:
        segment = line[first:]
        seg_sig = ema([v for v in segment if v is not None], signal)
        for j, v in enumerate(seg_sig):
            sig[first + j] = v
    hist: list[Decimal | None] = [
        (l - s) if (l is not None and s is not None) else None for l, s in zip(line, sig)
    ]
    return line, sig, hist


def rsi(values: list[Decimal], period: int = 14) -> list[Decimal | None]:
    """Wilder RSI。"""
    out: list[Decimal | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = Decimal("0"), Decimal("0")
    for i in range(1, period + 1):
        ch = values[i] - values[i - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        ch = values[i] - values[i - 1]
        gain = ch if ch > 0 else Decimal("0")
        loss = -ch if ch < 0 else Decimal("0")
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal("100") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - Decimal("100") / (Decimal("1") + rs)


def true_ranges(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal]) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * len(highs)
    for i in range(1, len(highs)):
        prev_close = closes[i - 1]
        out[i] = max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
    return out


def atr(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal],
        period: int = 14) -> list[Decimal | None]:
    """Wilder ATR（TR 从 index 1 起）。"""
    tr = true_ranges(highs, lows, closes)
    out: list[Decimal | None] = [None] * len(highs)
    if len(highs) <= period:
        return out
    first = sum((tr[i] for i in range(1, period + 1)), Decimal("0")) / period
    out[period] = first
    prev = first
    for i in range(period + 1, len(highs)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal],
        period: int = 14) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """Wilder ADX / +DI / −DI（全部与输入等长，前导 None）。"""
    n = len(highs)
    plus_di: list[Decimal | None] = [None] * n
    minus_di: list[Decimal | None] = [None] * n
    adx_out: list[Decimal | None] = [None] * n
    if n <= period * 2:
        return adx_out, plus_di, minus_di

    tr = true_ranges(highs, lows, closes)
    plus_dm: list[Decimal | None] = [None] * n
    minus_dm: list[Decimal | None] = [None] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else Decimal("0")
        minus_dm[i] = down if (down > up and down > 0) else Decimal("0")

    sm_tr = sum((tr[i] for i in range(1, period + 1)), Decimal("0"))
    sm_plus = sum((plus_dm[i] for i in range(1, period + 1)), Decimal("0"))
    sm_minus = sum((minus_dm[i] for i in range(1, period + 1)), Decimal("0"))

    def fill(i: int) -> Decimal | None:
        if sm_tr == 0:
            return None
        p = Decimal("100") * sm_plus / sm_tr
        m = Decimal("100") * sm_minus / sm_tr
        plus_di[i] = p
        minus_di[i] = m
        denom = p + m
        return (Decimal("100") * abs(p - m) / denom) if denom != 0 else Decimal("0")

    dx: list[tuple[int, Decimal | None]] = []
    d = fill(period)
    dx.append((period, d))
    for i in range(period + 1, n):
        sm_tr = sm_tr - sm_tr / period + tr[i]
        sm_plus = sm_plus - sm_plus / period + plus_dm[i]
        sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        dx.append((i, fill(i)))

    first_adx_idx = period + period - 1
    window = [v for idx, v in dx if v is not None and idx <= first_adx_idx]
    if len(window) < period:
        return adx_out, plus_di, minus_di
    prev = sum(window[:period], Decimal("0")) / period
    adx_out[first_adx_idx] = prev
    for idx, v in dx:
        if idx <= first_adx_idx or v is None:
            continue
        prev = (prev * (period - 1) + v) / period
        adx_out[idx] = prev
    return adx_out, plus_di, minus_di


def bollinger(values: list[Decimal], period: int = 20,
              k: Decimal = Decimal("2")) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """中轨 / 上轨 / 下轨（总体标准差）。"""
    mid = sma(values, period)
    upper: list[Decimal | None] = [None] * len(values)
    lower: list[Decimal | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        m = mid[i]
        if m is None:
            continue
        var = sum(((v - m) ** 2 for v in window), Decimal("0")) / period
        sd = var.sqrt()
        upper[i], lower[i] = m + k * sd, m - k * sd
    return mid, upper, lower


def volume_z(volumes: list[Decimal], period: int = 20) -> list[Decimal | None]:
    """成交量 z-score（窗口含当前根；std=0 时为 None）。"""
    out: list[Decimal | None] = [None] * len(volumes)
    for i in range(period - 1, len(volumes)):
        window = volumes[i - period + 1:i + 1]
        mean = sum(window, Decimal("0")) / period
        var = sum(((v - mean) ** 2 for v in window), Decimal("0")) / period
        if var == 0:
            continue
        out[i] = (volumes[i] - mean) / var.sqrt()
    return out


def slope_bps(values: list[Decimal], period: int = 20) -> list[Decimal | None]:
    """窗口内净变化（基点）= (末/首 − 1) × 10000；首值为 0 时为 None。"""
    out: list[Decimal | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        first, last = values[i - period + 1], values[i]
        if first == 0:
            continue
        out[i] = (last / first - Decimal("1")) * BPS
    return out


def percentile_rank(history: list[Decimal], value: Decimal) -> Decimal | None:
    """value 在 history 中的分位（0..1）。空历史 → None。"""
    if not history:
        return None
    below = sum(1 for h in history if h <= value)
    return Decimal(below) / Decimal(len(history))