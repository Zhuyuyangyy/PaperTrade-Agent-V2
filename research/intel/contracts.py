"""情报层契约（Sprint 3）。

三条研究线（新闻 / Polymarket / 聪明钱）各自产出结构化事件，统一汇总为
``IntelDigest``。默认全部 ``advisory_only=True``：只写日志、供公示与复盘，
**不改变** ``Selection`` 与 ``RiskDecision``。只有通过独立样本外增量验证后，
才允许把某类信号接进策略适用条件——这是 Sprint 3 的唯一验收标准。

外部文本一律当作**数据**：不执行其中任何指令，疑似注入只做标记。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ADVISORY_ONLY = True


def _num(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


@dataclass(frozen=True)
class SourceRef:
    url: str
    publisher: str
    published_at_ms: int
    first_seen_ms: int
    retrieved_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "publisher": self.publisher,
            "published_at_ms": self.published_at_ms,
            "first_seen_ms": self.first_seen_ms,
            "retrieved_ms": self.retrieved_ms,
        }


@dataclass(frozen=True)
class Fact:
    key: str
    value: str
    unit: str = ""


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    headline: str
    facts: tuple[Fact, ...]
    occurred_at_ms: int
    first_seen_ms: int
    sources: tuple[SourceRef, ...]
    credibility: Decimal
    horizon_ms: int
    tags: tuple[str, ...] = ()
    injection_suspected: bool = False
    advisory_only: bool = ADVISORY_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "headline": self.headline,
            "facts": [{"key": f.key, "value": f.value, "unit": f.unit} for f in self.facts],
            "occurred_at_ms": self.occurred_at_ms,
            "first_seen_ms": self.first_seen_ms,
            "sources": [s.to_dict() for s in self.sources],
            "credibility": _num(self.credibility),
            "horizon_ms": self.horizon_ms,
            "tags": list(self.tags),
            "injection_suspected": self.injection_suspected,
            "advisory_only": self.advisory_only,
        }


@dataclass(frozen=True)
class PredictionMarketEvent:
    market_id: str
    question: str
    yes_mid: Decimal | None
    prev_yes_mid: Decimal | None
    delta: Decimal | None
    spread_bps: Decimal | None
    liquidity_usd: Decimal | None
    ends_at_ms: int
    resolution_source: str
    researchable: bool
    horizon_match: bool
    reasons: tuple[str, ...] = ()
    data_quality: str = "OK"          # OK / STALE / WIDE_SPREAD / LOW_LIQUIDITY / UNKNOWN
    advisory_only: bool = ADVISORY_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "yes_mid": _num(self.yes_mid),
            "prev_yes_mid": _num(self.prev_yes_mid),
            "delta": _num(self.delta),
            "spread_bps": _num(self.spread_bps),
            "liquidity_usd": _num(self.liquidity_usd),
            "ends_at_ms": self.ends_at_ms,
            "resolution_source": self.resolution_source,
            "researchable": self.researchable,
            "horizon_match": self.horizon_match,
            "reasons": list(self.reasons),
            "data_quality": self.data_quality,
            "advisory_only": self.advisory_only,
        }


@dataclass(frozen=True)
class WalletSignal:
    wallet: str
    token: str
    side: str                       # BUY / SELL
    tx_ts_ms: int
    price_usd: Decimal
    size_usd: Decimal
    realized_pnl_usd: Decimal = Decimal("0")
    unrealized_pnl_usd: Decimal = Decimal("0")
    delay_ms: int = 0
    copyable: bool = False
    reasons: tuple[str, ...] = ()
    advisory_only: bool = ADVISORY_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "token": self.token,
            "side": self.side,
            "tx_ts_ms": self.tx_ts_ms,
            "price_usd": _num(self.price_usd),
            "size_usd": _num(self.size_usd),
            "realized_pnl_usd": _num(self.realized_pnl_usd),
            "unrealized_pnl_usd": _num(self.unrealized_pnl_usd),
            "delay_ms": self.delay_ms,
            "copyable": self.copyable,
            "reasons": list(self.reasons),
            "advisory_only": self.advisory_only,
        }


@dataclass(frozen=True)
class IntelDigest:
    as_of_ms: int
    news: tuple[NewsEvent, ...] = ()
    markets: tuple[PredictionMarketEvent, ...] = ()
    wallets: tuple[WalletSignal, ...] = ()
    warnings: tuple[str, ...] = ()
    advisory_only: bool = ADVISORY_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_ms": self.as_of_ms,
            "news": [n.to_dict() for n in self.news],
            "markets": [m.to_dict() for m in self.markets],
            "wallets": [w.to_dict() for w in self.wallets],
            "warnings": list(self.warnings),
            "advisory_only": self.advisory_only,
        }

    def summary(self) -> str:
        inj = sum(1 for n in self.news if n.injection_suspected)
        return (
            f"intel @ {self.as_of_ms}: news={len(self.news)} "
            f"(injection_suspected={inj}), markets={len(self.markets)}, "
            f"wallets={len(self.wallets)}, advisory_only={self.advisory_only}"
        )