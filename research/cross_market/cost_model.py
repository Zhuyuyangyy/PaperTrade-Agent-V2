"""成本模型（P1/P1.1，规格 §13）——EV_net = EV_gross − fee − spread − slippage − latency − impact − funding。

铁律：统计显著 ≠ 经济显著；EV_net > threshold 才允许 ECONOMIC_SUPPORTED。

Sprint 0：资金费进入 EV_net（旧实现只在单独的 funding_drag_bps 里算，评估函数
从不扣它——做多资金费为正时 EV 被系统性高估）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class CostInput:
    ev_gross_bps: Decimal        # 毛信号收益（基点）
    taker_fee_bps: Decimal       # 单边 taker 手续费（基点，往返×2）
    spread_bps: Decimal          # 穿越价差（基点）
    slippage_bps: Decimal        # 滑点（基点）
    latency_cost_bps: Decimal    # 延迟成本（基点）
    impact_bps: Decimal          # 市场冲击（基点）
    horizon_ms: int              # 持有期（用于资金费折算）
    funding_bps: Decimal = Decimal("0")   # 持有期资金费拖累（基点，正=成本）


@dataclass
class CostVerdict:
    ev_net_bps: Decimal
    components_bps: dict[str, Decimal]
    economic: bool               # EV_net > 0（threshold 由调用方给）


def evaluate_cost(inp: CostInput, threshold_bps: Decimal = Decimal("0")) -> CostVerdict:
    fees = inp.taker_fee_bps * 2  # 往返
    components = {
        "fee": fees,
        "spread": inp.spread_bps,
        "slippage": inp.slippage_bps,
        "latency": inp.latency_cost_bps,
        "impact": inp.impact_bps,
        "funding": inp.funding_bps,
    }
    total_cost = sum(components.values(), Decimal("0"))
    net = inp.ev_gross_bps - total_cost
    return CostVerdict(ev_net_bps=net, components_bps=components, economic=net > threshold_bps)


def funding_drag_bps(funding_rate_per_8h: Decimal, horizon_ms: int) -> Decimal:
    """资金费拖累（基点）= 每 8h 费率 × 期数 × 10000（bps）。

    多头支付正费率；空头方向相反，调用方取符号时自行处理。
    """
    periods = Decimal(horizon_ms) / Decimal(28_800_000)
    return funding_rate_per_8h * periods * Decimal("10000")