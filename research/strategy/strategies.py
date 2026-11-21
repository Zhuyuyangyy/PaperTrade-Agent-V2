"""三类候选策略（Sprint 2）——纯函数，输入 MarketState，输出方向与价位。

共同前置（任一不满足就弃权，并给出稳定原因码）：

- ``CRITICAL_STALE``：1h 数据陈旧（趋势判断不可信）；
- ``NON_CONTIGUOUS``：1h 有缺口；
- ``UNKNOWN_STATE``：趋势/波动为 UNKNOWN；
- ``INSUFFICIENT_FEATURES``：缺少策略所需特征（区间边界/ATR）。

策略只使用 ``MarketState.features`` 与 ``TimeframeView``，**不得**自己重算指标。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from research.market.contracts import MarketState

BPS = Decimal("10000")


@dataclass(frozen=True)
class Signal:
    side: str
    reason: str
    stop_price: Decimal
    take_profit_price: Decimal
    evidence_codes: tuple[str, ...] = ()


def _gate(state: MarketState) -> str | None:
    dq = state.data_quality
    if dq is None or dq.critical_stale:
        return "CRITICAL_STALE"
    if not dq.contiguous:
        return "NON_CONTIGUOUS"
    if state.trend == "UNKNOWN" or state.volatility == "UNKNOWN":
        return "UNKNOWN_STATE"
    return None


def _num(state: MarketState, key: str) -> Decimal | None:
    v = state.features.get(key)
    return v if isinstance(v, Decimal) else None


def _atr(state: MarketState) -> Decimal | None:
    atr = _num(state, "atr_abs")
    return atr if atr is not None and atr > 0 else None


def trend_follow(state: MarketState) -> tuple[Signal | None, str]:
    gate = _gate(state)
    if gate:
        return None, gate
    if state.trend not in ("TREND_UP", "TREND_DOWN"):
        return None, "TREND_NOT_APPLICABLE"
    atr = _atr(state)
    close = _num(state, "close")
    ema20, ema60 = _num(state, "ema20"), _num(state, "ema60")
    if atr is None or close is None or ema20 is None or ema60 is None:
        return None, "INSUFFICIENT_FEATURES"
    up = state.trend == "TREND_UP"
    if up and state.momentum not in ("MOMENTUM_UP", "FLAT"):
        return None, "MOMENTUM_CONFLICT"
    if not up and state.momentum not in ("MOMENTUM_DOWN", "FLAT"):
        return None, "MOMENTUM_CONFLICT"
    if up and not (ema20 > ema60):
        return None, "EMA_STACK_BROKEN"
    if not up and not (ema20 < ema60):
        return None, "EMA_STACK_BROKEN"
    if up:
        return Signal("LONG", f"趋势跟随：{state.reason}",
                      close - atr * Decimal("1.5"), close + atr * Decimal("3"),
                      ("ema_stack_up", "adx_strong")), "TREND_FOLLOW_LONG"
    return Signal("SHORT", f"趋势跟随：{state.reason}",
                  close + atr * Decimal("1.5"), close - atr * Decimal("3"),
                  ("ema_stack_down", "adx_strong")), "TREND_FOLLOW_SHORT"


def range_revert(state: MarketState) -> tuple[Signal | None, str]:
    gate = _gate(state)
    if gate:
        return None, gate
    if state.trend != "RANGE":
        return None, "RANGE_NOT_APPLICABLE"
    atr = _atr(state)
    close = _num(state, "close")
    high, low, mid = _num(state, "range_high"), _num(state, "range_low"), _num(state, "range_mid")
    if None in (atr, close, high, low, mid) or high <= low:
        return None, "INSUFFICIENT_FEATURES"
    width = high - low
    if close <= low + width * Decimal("0.25"):
        return Signal("LONG", f"区间回归：价格接近区间下沿（{close} <= {low}+25%）",
                      low - atr * Decimal("0.5"), mid, ("range_low_touch",)), "RANGE_REVERT_LONG"
    if close >= high - width * Decimal("0.25"):
        return Signal("SHORT", f"区间回归：价格接近区间上沿（{close} >= {high}-25%）",
                      high + atr * Decimal("0.5"), mid, ("range_high_touch",)), "RANGE_REVERT_SHORT"
    return None, "NOT_AT_RANGE_EDGE"


def breakout_confirm(state: MarketState) -> tuple[Signal | None, str]:
    gate = _gate(state)
    if gate:
        return None, gate
    atr = _atr(state)
    close = _num(state, "close")
    ref_high, ref_low = _num(state, "breakout_ref_high"), _num(state, "breakout_ref_low")
    vz = _num(state, "volume_z")
    if None in (atr, close, ref_high, ref_low):
        return None, "INSUFFICIENT_FEATURES"
    eps = Decimal("0.0005")                      # 5bps 缓冲，过滤噪声突破
    if close > ref_high * (Decimal("1") + eps):
        if vz is None or vz < Decimal("1"):
            return None, "BREAKOUT_WITHOUT_VOLUME"
        return Signal("LONG", f"突破确认：收盘 {close} 突破前高 {ref_high} 且量能 z={vz}",
                      ref_high - atr * Decimal("0.5"), close + atr * Decimal("2"),
                      ("breakout_high", "volume_confirm")), "BREAKOUT_LONG"
    if close < ref_low * (Decimal("1") - eps):
        if vz is None or vz < Decimal("1"):
            return None, "BREAKOUT_WITHOUT_VOLUME"
        return Signal("SHORT", f"突破确认：收盘 {close} 跌破前低 {ref_low} 且量能 z={vz}",
                      ref_low + atr * Decimal("0.5"), close - atr * Decimal("2"),
                      ("breakout_low", "volume_confirm")), "BREAKOUT_SHORT"
    return None, "NO_BREAKOUT"


FUNCTIONS = {
    "trend_follow": trend_follow,
    "range_revert": range_revert,
    "breakout_confirm": breakout_confirm,
}