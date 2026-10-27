"""Deterministic risk gate for CopyWatch.

Every check is a boolean with a stable machine code, so a skipped signal can always be
explained after the fact. There is no LLM in this path: a signal only becomes a paper
fill when every check passes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REASON_LABELS = {
    "WALLET_UNVERIFIED": "钱包未达跟踪门槛（历史笔数/规模不足）",
    "ANOMALOUS": "数据源标记为异常交易",
    "NO_FRESH_PRICE": "无法获得新鲜池子价格",
    "POOL_RESERVE": "池子流动性不足",
    "PRICE_IMPACT": "名义金额占池子比例过高",
    "COST_TOO_HIGH": "往返成本超过上限",
    "INSUFFICIENT_CASH": "虚拟账户现金不足",
    "MAX_POSITIONS": "已达到最大同时持仓数",
    "ALREADY_OPEN": "同一标的已有未平仓仓位",
    "COOLDOWN": "冷却期未结束",
    "DAILY_LOSS": "当日已实现亏损触及上限",
}


@dataclass
class Check:
    code: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"code": self.code, "ok": self.ok, "detail": self.detail}


@dataclass
class Decision:
    verdict: str
    signal_class: str | None
    checks: list[Check] = field(default_factory=list)
    cost_bps: float = 0.0
    # The notional every gate was evaluated against. The fill must use this
    # same number: checking impact at one size and trading at another is how a
    # position slips past its own risk gate.
    notional: float = 0.0
    size_note: str = ""

    @property
    def primary_code(self) -> str | None:
        for item in self.checks:
            if not item.ok:
                return item.code
        return None

    @property
    def detail(self) -> str:
        failed = [item for item in self.checks if not item.ok]
        if not failed:
            passed = len(self.checks)
            return f"全部 {passed} 项风控通过 · 往返成本 {self.cost_bps:.1f} bps"
        first = failed[0]
        return f"{first.code} · {first.detail}"

    def as_codes(self) -> list[dict]:
        return [item.as_dict() for item in self.checks]


def round_trip_cost_bps(*, fee_bps: float, slippage_bps: float, gas_usd: float, notional_usd: float) -> float:
    """Cost of entering and exiting, expressed in basis points of notional."""
    if notional_usd <= 0:
        return float("inf")
    gas_bps = gas_usd * 2 / notional_usd * 10_000
    return 2 * (float(fee_bps) + float(slippage_bps)) + gas_bps


def evaluate(
    *,
    event,
    pool,
    wallet: dict | None,
    policy: dict,
    risk: dict,
    slippage_bps: float,
    fee_bps: float,
    gas_usd: float,
    context: dict,
) -> Decision:
    """Run every gate and return a decision; never mutates state."""
    checks: list[Check] = []
    from .sizing import dynamic_notional

    # The clip scales with the target wallet's own fill: a wallet putting $5k
    # behind a token says more than one putting $50, and a fixed size discards
    # that. Bounds live in sizing.dynamic_notional.
    notional, size_note = dynamic_notional(float(event.usd), risk)
    whale_usd = float(policy.get("whale_usd", 0) or 0)
    tracked = bool(wallet and wallet.get("tracked"))
    pinned = bool(wallet and (wallet.get("note") or "").startswith("pinned"))

    if tracked or pinned:
        signal_class = "copy"
    elif whale_usd > 0 and float(event.usd) >= whale_usd:
        signal_class = "whale"
    else:
        signal_class = None

    if signal_class is None:
        swaps = int(wallet.get("swaps") or 0) if wallet else 0
        notional_seen = float(wallet.get("notional_usd") or 0.0) if wallet else 0.0
        checks.append(Check(
            "WALLET_UNVERIFIED", False,
            f"该钱包已观察 {swaps} 笔 / ${notional_seen:,.0f}，本次 ${float(event.usd):,.0f} "
            f"未达巨鲸阈值 ${whale_usd:,.0f}，只观察不跟单",
        ))
    else:
        checks.append(Check("WALLET_UNVERIFIED", True, f"signal_class={signal_class}"))

    checks.append(Check(
        "ANOMALOUS", not bool(event.anomalous),
        "数据源未标记异常" if not event.anomalous else "数据源标记 is_anomalous=true",
    ))

    price = pool.get("price_usd") if isinstance(pool, dict) else getattr(pool, "price_usd", None)
    price_age_ms = context["now_ms"] - int(pool.get("fetched_at_ms", 0) if isinstance(pool, dict) else getattr(pool, "fetched_at_ms", 0))
    max_price_age_ms = int(risk.get("max_price_age_seconds", 180)) * 1000
    fresh = price is not None and price > 0 and price_age_ms <= max_price_age_ms
    checks.append(Check(
        "NO_FRESH_PRICE", fresh,
        f"池价 ${price:,.4f} · {price_age_ms / 1000:.0f}s 前读取" if fresh else "池价缺失或已过期，禁止模拟成交",
    ))

    reserve = pool.get("reserve_usd") if isinstance(pool, dict) else getattr(pool, "reserve_usd", None)
    min_reserve = float(risk.get("min_pool_reserve_usd", 0))
    checks.append(Check(
        "POOL_RESERVE", reserve is not None and reserve >= min_reserve,
        f"池子储备 ${reserve:,.0f}" if reserve is not None else "池子储备未知",
    ))

    impact_bps = (notional / reserve * 10_000) if (reserve and reserve > 0) else float("inf")
    checks.append(Check(
        "PRICE_IMPACT", impact_bps <= float(risk.get("max_pool_impact_bps", 50)),
        f"名义额占池子储备 {impact_bps:.2f} bps（上限 {float(risk.get('max_pool_impact_bps', 50)):.0f} bps）",
    ))

    cost_bps = round_trip_cost_bps(
        fee_bps=fee_bps, slippage_bps=slippage_bps, gas_usd=gas_usd, notional_usd=notional
    )
    max_cost = float(risk.get("max_cost_bps", 60))
    checks.append(Check(
        "COST_TOO_HIGH", cost_bps <= max_cost,
        f"往返成本 {cost_bps:.1f} bps = 手续费 {fee_bps:.1f} + 滑点 {slippage_bps:.1f} ×2 + gas ${gas_usd:.2f}×2；"
        f"上限 {max_cost:.0f} bps",
    ))

    checks.append(Check(
        "INSUFFICIENT_CASH", float(context["cash_usd"]) >= notional,
        f"可用现金 ${float(context['cash_usd']):,.2f} / 需要 ${notional:,.2f}",
    ))
    checks.append(Check(
        "MAX_POSITIONS", int(context["open_count"]) < int(risk.get("max_open_positions", 1)),
        f"当前持仓 {int(context['open_count'])} / 上限 {int(risk.get('max_open_positions', 1))}",
    ))
    checks.append(Check(
        "ALREADY_OPEN", not context["has_open_symbol"],
        "同一标的已有持仓" if context["has_open_symbol"] else "同一标的无持仓",
    ))
    cooldown_ms = int(risk.get("cooldown_minutes", 0)) * 60_000
    last_open = context.get("last_open_ms")
    cooled = last_open is None or (context["now_ms"] - int(last_open)) >= cooldown_ms
    checks.append(Check(
        "COOLDOWN", cooled,
        "冷却期已过" if cooled else f"距上次开仓 {(context['now_ms'] - int(last_open)) / 60_000:.1f} 分钟",
    ))
    checks.append(Check(
        "DAILY_LOSS", float(context["today_realized"]) > -abs(float(risk.get("max_daily_loss_usdt", 0))),
        f"当日已实现 ${float(context['today_realized']):,.2f}",
    ))

    verdict = "PAPER" if all(item.ok for item in checks) else "SKIP"
    return Decision(verdict=verdict, signal_class=signal_class, checks=checks, cost_bps=cost_bps,
                    notional=notional, size_note=size_note)
