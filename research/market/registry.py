"""市场准入（Sprint 1）——一个已登记市场是否配进入研究。

三个独立条件，全部 fail-closed：
  1. 未过期（``expired``）；
  2. 到期规则明确（``expiry_unknown`` 为 False）且有 resolution source；
  3. **视界匹配**：市场到期剩余时间与预注册视界同量级。用 5 分钟视界去研究一个
     半年后到期的"BTC 到 100 万"市场，概率变化对 5 分钟价格几乎不敏感——
     代码全对也只会得到"无增量"，因为因果链不存在。

阈值 ``max_horizon_ratio=500``：视界 5m × 500 ≈ 41.7 小时；1h × 500 ≈ 20.8 天。
超出即不匹配。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from research.cross_market.market_mapping import MarketMapping

DEFAULT_MAX_HORIZON_RATIO = 500

_HORIZON_MS = {
    "30s": 30_000, "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def horizon_ms(primary_horizon: str) -> int | None:
    return _HORIZON_MS.get(primary_horizon.strip().lower())


def _expiry_ms(expiry: str) -> int | None:
    try:
        dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


@dataclass(frozen=True)
class MarketAssessment:
    market_id: str
    researchable: bool
    horizon_match: bool
    reasons: tuple[str, ...]
    primary_horizon: str
    horizon_ratio: Decimal | None = None
    days_to_expiry: Decimal | None = None

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "researchable": self.researchable,
            "horizon_match": self.horizon_match,
            "reasons": list(self.reasons),
            "primary_horizon": self.primary_horizon,
            "horizon_ratio": None if self.horizon_ratio is None else str(self.horizon_ratio),
            "days_to_expiry": None if self.days_to_expiry is None else str(self.days_to_expiry),
        }


def assess(m: MarketMapping, now_ms: int,
           max_horizon_ratio: int = DEFAULT_MAX_HORIZON_RATIO) -> MarketAssessment:
    reasons: list[str] = []
    researchable = True

    if m.expired:
        researchable = False
        reasons.append("market_expired")
    if m.expiry_unknown:
        researchable = False
        reasons.append("expiry_unparseable")
    if not m.resolution_source.strip():
        researchable = False
        reasons.append("resolution_source_missing")

    hm = horizon_ms(m.primary_horizon)
    ratio: Decimal | None = None
    days: Decimal | None = None
    expiry = _expiry_ms(m.event_expiry)
    if expiry is None:
        researchable = False
        if "expiry_unparseable" not in reasons:
            reasons.append("expiry_unparseable")
    if hm is None:
        researchable = False
        reasons.append(f"unknown_primary_horizon:{m.primary_horizon}")
    elif expiry is not None:
        remaining = expiry - now_ms
        if remaining <= 0:
            researchable = False
            reasons.append("market_expired_by_clock")
        else:
            ratio = Decimal(remaining) / Decimal(hm)
            days = (Decimal(remaining) / Decimal(86_400_000)).quantize(Decimal("0.01"))
            if ratio > Decimal(max_horizon_ratio):
                researchable = False
                reasons.append(
                    f"horizon_mismatch:到期剩 {days} 天，视界 {m.primary_horizon}，"
                    f"比值 {ratio:.0f} > {max_horizon_ratio}"
                )

    horizon_match = ratio is not None and ratio <= Decimal(max_horizon_ratio)
    if researchable and not reasons:
        reasons.append("ok")
    return MarketAssessment(
        market_id=m.market_id,
        researchable=researchable,
        horizon_match=horizon_match,
        reasons=tuple(reasons),
        primary_horizon=m.primary_horizon,
        horizon_ratio=ratio,
        days_to_expiry=days,
    )


def assess_all(mapping: dict[str, MarketMapping], now_ms: int,
               max_horizon_ratio: int = DEFAULT_MAX_HORIZON_RATIO) -> list[MarketAssessment]:
    return [assess(m, now_ms, max_horizon_ratio) for m in mapping.values()]


def usable(mapping: dict[str, MarketMapping], now_ms: int,
           max_horizon_ratio: int = DEFAULT_MAX_HORIZON_RATIO) -> dict[str, MarketMapping]:
    """只返回三个条件都满足的市场。"""
    return {
        k: m for k, m in mapping.items()
        if assess(m, now_ms, max_horizon_ratio).researchable
    }