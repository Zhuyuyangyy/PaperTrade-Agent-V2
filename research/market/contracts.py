"""OpenAlpha L2 契约（Sprint 1）。

这一层的唯一职责：把"市场现在是什么状态、凭什么这么说、什么情况下这个判断失效"
变成一个**可序列化、可回放、可审计**的结构。它不是策略，不产生交易信号。

设计约束：
  - 全部数值用 Decimal（与 cross_market 一致，禁 float 混算）；
  - 所有字段只描述**已收盘 K 线**与决策时刻已知的信息（无前视）；
  - `UNKNOWN` 是合法状态：数据不足时宁可不知道，也不许硬猜；
  - 每个判断都必须带 evidence；冲突必须显式记录在 contradictions，而不是被平均掉；
  - 每个状态都必须给出 invalidations（什么条件会让它失效），否则不可复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

SCHEMA_VERSION = 1

TF_5M_MS = 300_000
TF_15M_MS = 900_000
TF_1H_MS = 3_600_000
TF_4H_MS = 14_400_000

TIMEFRAMES: dict[str, int] = {
    "5m": TF_5M_MS,
    "15m": TF_15M_MS,
    "1h": TF_1H_MS,
    "4h": TF_4H_MS,
}

TREND_STATES = ("TREND_UP", "TREND_DOWN", "RANGE", "UNKNOWN")
VOL_STATES = ("LOW_VOL", "NORMAL_VOL", "HIGH_VOL", "UNKNOWN")
MOMENTUM_STATES = ("MOMENTUM_UP", "MOMENTUM_DOWN", "FLAT", "UNKNOWN")
RISK_STATES = ("RISK_LOW", "RISK_NORMAL", "RISK_HIGH", "RISK_EXTREME", "UNKNOWN")


@dataclass(frozen=True)
class Candle:
    """一根**已收盘**的 K 线。open_ms 为 UTC 毫秒，且必须对齐到 timeframe 边界。"""

    open_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timeframe_ms: int

    @property
    def close_ms(self) -> int:
        return self.open_ms + self.timeframe_ms

    def __post_init__(self) -> None:
        if self.open_ms <= 0 or self.open_ms % self.timeframe_ms != 0:
            raise ValueError(f"Candle 未对齐到 {self.timeframe_ms}ms 边界: {self.open_ms}")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError("Candle OHLC 不自洽")
        if self.low <= 0 or self.volume < 0:
            raise ValueError("Candle 价格/成交量非法")


@dataclass(frozen=True)
class Evidence:
    """一条支持某判断的观测。code 稳定（给机器用），note 给人看。"""

    code: str
    timeframe: str
    supports: str
    value: Decimal | None = None
    threshold: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "timeframe": self.timeframe,
            "supports": self.supports,
            "value": _num(self.value),
            "threshold": self.threshold,
            "note": self.note,
        }


@dataclass(frozen=True)
class Contradiction:
    """两条互相打架的观测。禁止悄悄取平均；必须被记录并降低 confidence。"""

    code: str
    a: str
    b: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "a": self.a, "b": self.b, "note": self.note}


@dataclass(frozen=True)
class TimeframeView:
    """单个周期在决策时刻的指标快照（只来自已收盘 K 线）。

    ``last_closed_ms`` 是最后一根的**收盘时刻**（open + timeframe），不是开盘时刻：
    陈旧度必须按"数据什么时候变得完整"算，按 open 会把一根刚收盘的 K 线误判成
    已经存在了一整个周期。
    """

    timeframe: str
    last_closed_ms: int          # 最后一根已收盘 K 线的 close_ms
    close: Decimal
    ema20: Decimal | None = None
    ema60: Decimal | None = None
    ema200: Decimal | None = None
    macd_hist: Decimal | None = None
    rsi: Decimal | None = None
    adx: Decimal | None = None
    atr_pct: Decimal | None = None
    volume_z: Decimal | None = None
    slope_bps: Decimal | None = None
    bars: int = 0
    last_open_ms: int = 0
    age_ms: int = 0
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "last_closed_ms": self.last_closed_ms,
            "last_open_ms": self.last_open_ms,
            "age_ms": self.age_ms,
            "stale": self.stale,
            "close": _num(self.close),
            "ema20": _num(self.ema20),
            "ema60": _num(self.ema60),
            "ema200": _num(self.ema200),
            "macd_hist": _num(self.macd_hist),
            "rsi": _num(self.rsi),
            "adx": _num(self.adx),
            "atr_pct": _num(self.atr_pct),
            "volume_z": _num(self.volume_z),
            "slope_bps": _num(self.slope_bps),
            "bars": self.bars,
        }


@dataclass(frozen=True)
class DataQuality:
    """数据层的事实：每个周期够不够、断没断、旧不旧。

    ``critical_stale`` 只看**趋势周期**（1h）：5m 新鲜不能证明 1h 趋势数据新鲜。
    ``contiguous`` 同样只看 1h：趋势周期有缺口 → 风险层拒绝新开仓。
    ``per_timeframe`` 保留每个周期自己的 age/stale/gaps，供看板与复盘使用。
    """

    timeframes_seen: tuple[str, ...]
    gaps: tuple[dict[str, int], ...] = ()
    last_closed_ms: int = 0
    age_ms: int = 0
    stale: bool = False
    complete: bool = True
    notes: tuple[str, ...] = ()
    per_timeframe: dict[str, dict] = field(default_factory=dict)
    critical_stale: bool = False
    contiguous: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframes_seen": list(self.timeframes_seen),
            "gaps": [dict(g) for g in self.gaps],
            "last_closed_ms": self.last_closed_ms,
            "age_ms": self.age_ms,
            "stale": self.stale,
            "complete": self.complete,
            "notes": list(self.notes),
            "per_timeframe": {k: dict(v) for k, v in self.per_timeframe.items()},
            "critical_stale": self.critical_stale,
            "contiguous": self.contiguous,
        }


@dataclass(frozen=True)
class MarketState:
    """L2 的唯一输出契约。策略层只读这个，不再自己算指标。"""

    symbol: str
    as_of_ms: int
    trend: str
    volatility: str
    momentum: str
    risk: str
    confidence: Decimal
    reason: str
    evidence: tuple[Evidence, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    invalidations: tuple[str, ...] = ()
    data_quality: DataQuality | None = None
    timeframes: dict[str, TimeframeView] = field(default_factory=dict)
    # 策略层直接消费的特征（区间上下沿/突破参考/ATR 绝对值等），禁止策略重算指标
    features: dict[str, Decimal | None] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.trend not in TREND_STATES:
            raise ValueError(f"trend 非法: {self.trend}")
        if self.volatility not in VOL_STATES:
            raise ValueError(f"volatility 非法: {self.volatility}")
        if self.momentum not in MOMENTUM_STATES:
            raise ValueError(f"momentum 非法: {self.momentum}")
        if self.risk not in RISK_STATES:
            raise ValueError(f"risk 非法: {self.risk}")
        if not (Decimal("0") <= self.confidence <= Decimal("1")):
            raise ValueError("confidence 必须在 [0,1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "as_of_ms": self.as_of_ms,
            "trend": self.trend,
            "volatility": self.volatility,
            "momentum": self.momentum,
            "risk": self.risk,
            "confidence": _num(self.confidence),
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
            "contradictions": [c.to_dict() for c in self.contradictions],
            "invalidations": list(self.invalidations),
            "data_quality": self.data_quality.to_dict() if self.data_quality else None,
            "timeframes": {k: v.to_dict() for k, v in self.timeframes.items()},
            "features": {k: _num(v) for k, v in self.features.items()},
        }

    def describe(self) -> str:
        """一行人类可读摘要（看板/日志用）。"""
        return (
            f"{self.symbol} @ {self.as_of_ms}: {self.trend} / {self.volatility} / "
            f"{self.momentum} / {self.risk} (conf={self.confidence:.2f}) — {self.reason}"
        )


def _num(v: Decimal | None) -> str | None:
    """Decimal 序列化为字符串，避免 JSON float 精度损失。"""
    return None if v is None else str(v)


def as_list(items: Iterable[Any]) -> list[Any]:
    return list(items)