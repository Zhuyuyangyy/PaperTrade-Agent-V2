"""Cross-Market v2.2 数据模型（P0）。

设计纪律（规格 §2/§3/§4/§5/§7）：
  - 价格/价差一律 Decimal（禁 float 混算）；
  - 三个时间戳分置：event_ts / exchange_ts / ingest_ts；
  - 强平是"观测值"（单所做市所 PARTIAL 观测），禁写成"全网清算"；
  - OI 状态机只用观察性语言（市场看到了什么），禁推断交易员意图；
  - FeatureSnapshot 与 ResearchLabel 严格分离（无前视）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


# ---------- 原始事件层（只存事实，不存解释） ----------

@dataclass(frozen=True)
class PolyBookEvent:
    """Polymarket 订单簿事件（原始）。"""

    market_id: str
    token_id: str
    event_type: str  # book | trade | tick_size_change ...
    bid: Decimal | None
    ask: Decimal | None
    spread_abs: Decimal | None
    spread_bps: Decimal | None
    event_ts_ms: int
    exchange_ts_ms: int | None
    ingest_ts_ms: int
    sequence_id: str | None = None
    raw: str = ""


@dataclass(frozen=True)
class BinanceSnapshot:
    """Binance 微结构快照（原始观测）。"""

    symbol: str
    price: Decimal
    volume: Decimal
    oi_snapshot: Decimal | None
    cvd_delta: Decimal | None
    liquidation_observed_usdt: Decimal
    event_ts_ms: int
    exchange_ts_ms: int | None
    ingest_ts_ms: int
    raw: str = ""


@dataclass(frozen=True)
class LiquidationObservation:
    """强平观测——永远是"单所做市所的部分观测"，不是全网清算。"""

    symbol: str
    observed_notional_usdt: Decimal
    observation_count: int
    venue: str = "BINANCE"
    coverage: str = "PARTIAL"  # PARTIAL | UNKNOWN——受约束枚举（禁 FULL：我们没有全网数据）
    sampling_mode: str = "FORCE_ORDER_STREAM"

    def __post_init__(self) -> None:
        if self.coverage not in COVERAGE_VALUES:
            raise ValueError(f"coverage 必须是 {COVERAGE_VALUES} 之一（禁伪造 FULL）")

    def describe(self) -> str:
        return (
            f"Liquidation: {self.venue} / {self.coverage}_OBSERVATION / "
            f"observed_notional={self.observed_notional_usdt} USDT (n={self.observation_count})"
        )


COVERAGE_VALUES = ("PARTIAL", "UNKNOWN")


# ---------- OI 状态机（观察性语言，规格 §5；2026-11-18 审查后修正） ----------
# 旧实现的错误：价格涨+OI 平 → PRICE_FLAT_OI_FLAT（把价格方向吞了）；
# 且 BUY_FLOW/SELL_FLOW 是推断（价格+OI 推不出主动成交方向）。
# 修正：三个独立观测字段，方向必须来自对应数据；taker 方向缺数据就是 None。

PRICE_DIRECTIONS = ("UP", "DOWN", "FLAT")
OI_DIRECTIONS = ("EXPANSION", "CONTRACTION", "FLAT")
TAKER_FLOW_DIRECTIONS = ("BUY_FLOW", "SELL_FLOW")  # 必须来自实际 taker 成交数据


def classify_price_direction(price_delta_pct: Decimal, threshold_pct: Decimal = Decimal("0.01")) -> str:
    if price_delta_pct > threshold_pct:
        return "UP"
    if price_delta_pct < -threshold_pct:
        return "DOWN"
    return "FLAT"


def classify_oi_direction(oi_delta_pct: Decimal, threshold_pct: Decimal = Decimal("0.01")) -> str:
    if oi_delta_pct > threshold_pct:
        return "EXPANSION"
    if oi_delta_pct < -threshold_pct:
        return "CONTRACTION"
    return "FLAT"


@dataclass(frozen=True)
class MicrostructureObservation:
    """三个独立观测字段的组合——描述市场看到了什么，不猜交易员在干什么。"""

    price_direction: str          # PRICE_DIRECTIONS
    oi_direction: str             # OI_DIRECTIONS
    taker_flow_direction: str | None  # TAKER_FLOW_DIRECTIONS or None（无成交方向数据时不猜）

    def describe(self) -> str:
        flow = self.taker_flow_direction or "UNKNOWN_FLOW"
        return f"PRICE_{self.price_direction} / OI_{self.oi_direction} / TAKER_{flow}"


# ---------- Feature / Label 分离（规格 §7，无前视） ----------

@dataclass(frozen=True)
class FeatureSnapshot:
    """t 时刻已知的信息。禁含任何 t 之后的数据。

    schema_version=2（Sprint 0）：
      - polymarket 侧统一成 YES 概率口径（yes_bid/yes_ask/yes_mid/no_mid/
        consistent/prob_usable），不再把任一 token 的报价当概率；
      - 两侧都带相对 lookback 的变化量（price_delta_bps / yes_mid_delta_bps），
        方向信号必须用变化量，不能用水平值。
    """

    feature_ts_ms: int
    polymarket: dict = field(default_factory=dict)  # yes_bid/yes_ask/yes_mid/no_mid/consistent/prob_usable/yes_mid_delta_bps/spread_bps
    binance: dict = field(default_factory=dict)      # price/price_delta_bps/oi_snapshot/oi_delta_bps/cvd_delta/funding_rate/liquidation_*
    schema_version: int = 2


@dataclass(frozen=True)
class ResearchLabel:
    """只能从未来数据生成。t+H 的标签。

    future_return 是**相对收益**（future/origin − 1），不是价格。Sprint 0 之前
    这里写的是价格，导致方向准确率恒为"全对"（任何正价格都 > 0）。
    """

    origin_ts_ms: int
    horizon: str
    future_return: Decimal | None = None
    realized_volatility: Decimal | None = None
    max_adverse_excursion: Decimal | None = None
    tail_event: bool = False
    origin_price: Decimal | None = None
    label_ts_ms: int | None = None      # 生成标签所用未来快照的可用时间（可追溯）


def assert_no_lookahead(feature: FeatureSnapshot, label: ResearchLabel) -> None:
    """P0 安全断言：label.origin_ts_ms 必须 >= feature.feature_ts_ms。"""
    if label.origin_ts_ms < feature.feature_ts_ms:
        raise LookaheadBiasError(
            f"FAIL: LOOK_AHEAD_BIAS — label origin {label.origin_ts_ms} < feature ts {feature.feature_ts_ms}"
        )


class LookaheadBiasError(RuntimeError):
    """前视偏差——研究系统的头等罪行，直接 FAIL。"""
