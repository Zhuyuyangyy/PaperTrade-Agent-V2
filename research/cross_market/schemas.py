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
    coverage: str = "PARTIAL"  # PARTIAL | UNKNOWN（禁写 FULL——我们没有全网数据）
    sampling_mode: str = "FORCE_ORDER_STREAM"

    def describe(self) -> str:
        return (
            f"Liquidation: {self.venue} / {self.coverage}_OBSERVATION / "
            f"observed_notional={self.observed_notional_usdt} USDT (n={self.observation_count})"
        )


# ---------- OI 状态机（观察性语言，规格 §5） ----------

OI_STATES = (
    "PRICE_UP_OI_EXPANSION_BUY_FLOW",
    "PRICE_DOWN_OI_EXPANSION_SELL_FLOW",
    "PRICE_UP_OI_CONTRACTION_BUY_FLOW",
    "PRICE_DOWN_OI_CONTRACTION_SELL_FLOW",
    "PRICE_FLAT_OI_FLAT",
)


def classify_oi_state(price_delta_pct: Decimal, oi_delta_pct: Decimal) -> str:
    """按观察到的价格/OI 变动分类。描述市场看到了什么，不猜交易员在干什么。"""
    p_threshold = Decimal("0.01")
    o_threshold = Decimal("0.01")
    up = price_delta_pct > p_threshold
    down = price_delta_pct < -p_threshold
    expand = oi_delta_pct > o_threshold
    contract = oi_delta_pct < -o_threshold
    if up and expand:
        return OI_STATES[0]
    if down and expand:
        return OI_STATES[1]
    if up and contract:
        return OI_STATES[2]
    if down and contract:
        return OI_STATES[3]
    return OI_STATES[4]


# ---------- Feature / Label 分离（规格 §7，无前视） ----------

@dataclass(frozen=True)
class FeatureSnapshot:
    """t 时刻已知的信息。禁含任何 t 之后的数据。"""

    feature_ts_ms: int
    polymarket: dict = field(default_factory=dict)  # probability/spread_abs/spread_bps/book_imbalance/trade_flow
    binance: dict = field(default_factory=dict)      # price/oi/oi_delta/cvd/funding/liquidation_observed


@dataclass(frozen=True)
class ResearchLabel:
    """只能从未来数据生成。t+H 的标签。"""

    origin_ts_ms: int
    horizon: str
    future_return: Decimal | None = None
    realized_volatility: Decimal | None = None
    max_adverse_excursion: Decimal | None = None
    tail_event: bool = False


def assert_no_lookahead(feature: FeatureSnapshot, label: ResearchLabel) -> None:
    """P0 安全断言：label.origin_ts_ms 必须 >= feature.feature_ts_ms。"""
    if label.origin_ts_ms < feature.feature_ts_ms:
        raise LookaheadBiasError(
            f"FAIL: LOOK_AHEAD_BIAS — label origin {label.origin_ts_ms} < feature ts {feature.feature_ts_ms}"
        )


class LookaheadBiasError(RuntimeError):
    """前视偏差——研究系统的头等罪行，直接 FAIL。"""
