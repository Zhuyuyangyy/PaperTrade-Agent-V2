"""Polymarket 情报（Sprint 3）——先筛"配研究"，再谈信号。

复用 ``research.market.registry`` 的准入（过期/规则不明/视界不匹配），再叠加
盘口质量（点差、流动性）。任一不满足 → ``researchable=False`` 并写原因，
绝不因为"市场存在"就把它当成概率证据。
"""
from __future__ import annotations

from decimal import Decimal

from research.cross_market.market_mapping import MarketMapping
from research.market.registry import assess

from .contracts import PredictionMarketEvent

BPS = Decimal("10000")
DEFAULT_MAX_SPREAD_BPS = Decimal("200")
DEFAULT_MIN_LIQUIDITY_USD = Decimal("5000")


def _dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v))
    except ArithmeticError:
        return None


def assess_market(m: MarketMapping, book: dict | None, now_ms: int,
                  *, max_spread_bps: Decimal = DEFAULT_MAX_SPREAD_BPS,
                  min_liquidity_usd: Decimal = DEFAULT_MIN_LIQUIDITY_USD) -> PredictionMarketEvent:
    reg = assess(m, now_ms)
    reasons = list(reg.reasons)
    bid = _dec((book or {}).get("yes_bid"))
    ask = _dec((book or {}).get("yes_ask"))
    prev = _dec((book or {}).get("prev_yes_mid"))
    liquidity = _dec((book or {}).get("liquidity_usd"))

    yes_mid = delta = spread_bps = None
    quality = "OK"
    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid >= ask:
        quality = "UNKNOWN"
        reasons.append("book_missing_or_crossed")
    else:
        yes_mid = (bid + ask) / 2
        spread_bps = (ask - bid) / yes_mid * BPS
        if prev is not None:
            delta = yes_mid - prev
        if spread_bps > max_spread_bps:
            quality = "WIDE_SPREAD"
            reasons.append(f"spread_too_wide:{spread_bps:.0f}bps")
        if liquidity is None or liquidity < min_liquidity_usd:
            quality = "LOW_LIQUIDITY"
            reasons.append("liquidity_too_low")

    researchable = reg.researchable and quality == "OK"
    if not reg.horizon_match and "horizon_mismatch" not in " ".join(reasons):
        reasons.append("horizon_mismatch")

    return PredictionMarketEvent(
        market_id=m.market_id,
        question=(book or {}).get("question", m.market_id),
        yes_mid=yes_mid,
        prev_yes_mid=prev,
        delta=delta,
        spread_bps=spread_bps,
        liquidity_usd=liquidity,
        ends_at_ms=_expiry_ms(m.event_expiry),
        resolution_source=m.resolution_source,
        researchable=researchable,
        horizon_match=reg.horizon_match,
        reasons=tuple(dict.fromkeys(reasons)),
        data_quality=quality,
        advisory_only=True,
    )


def _expiry_ms(expiry: str) -> int:
    from research.market.registry import _expiry_ms as parse

    v = parse(expiry)
    return v if v is not None else 0


def screen(mappings: dict[str, MarketMapping], books: dict[str, dict], now_ms: int,
           **kwargs) -> tuple[PredictionMarketEvent, ...]:
    return tuple(assess_market(m, books.get(k), now_ms, **kwargs)
                 for k, m in mappings.items())