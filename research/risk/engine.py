"""确定性风控（Sprint 2）——LLM 与策略都没有权限绕过这里。

检查顺序固定（任一失败即返回，不继续）：

  1. KILL_SWITCH            人工/熔断开关
  2. DATA_STALE             1h 数据陈旧（critical_stale）→ FREEZE
  3. DATA_GAPS              1h 缺口（contiguous=False）→ DENY
  4. RISK_EXTREME           状态机判定极端风险 → DENY
  5. SPREAD_TOO_WIDE        点差超过上限 → DENY
  6. COST_TOO_HIGH          EV_net < 门槛 → DENY
  7. MAX_POSITIONS          持仓数上限 → DENY
  8. EXPOSURE_LIMIT         总敞口上限 → DENY
  9. DAILY_LOSS             当日已实现亏损超限 → FREEZE
 10. INSUFFICIENT_CASH      现金不足（含手续费）→ DENY

返回的 ``RiskDecision`` 带 ``codes``（稳定机器码）与 ``checks``（逐项布尔），
每一条拒绝都能在日志里复盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from research.market.contracts import MarketState
from research.strategy.contracts import TradeIntent

BPS = Decimal("10000")


@dataclass(frozen=True)
class RiskLimits:
    max_notional: Decimal = Decimal("100")
    max_gross_exposure: Decimal = Decimal("300")
    max_open_positions: int = 3
    daily_loss_limit: Decimal = Decimal("20")
    max_spread_bps: Decimal = Decimal("20")
    min_ev_bps: Decimal = Decimal("5")
    fee_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("5")
    kill_switch: bool = False


@dataclass(frozen=True)
class AccountSnapshot:
    cash: Decimal = Decimal("0")
    gross_exposure: Decimal = Decimal("0")
    open_positions: int = 0
    realized_pnl_today: Decimal = Decimal("0")


@dataclass(frozen=True)
class RiskDecision:
    decision: str                 # ALLOW / DENY / FREEZE
    codes: tuple[str, ...] = ()
    checks: dict[str, bool] = field(default_factory=dict)
    notional: Decimal = Decimal("0")
    reason: str = ""
    shadow: bool = False

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "codes": list(self.codes),
            "checks": dict(self.checks),
            "notional": str(self.notional),
            "reason": self.reason,
            "shadow": self.shadow,
        }


def _deny(code: str, reason: str, checks: dict[str, bool], shadow: bool,
          notional: Decimal = Decimal("0")) -> RiskDecision:
    return RiskDecision("DENY", (code,), checks, notional, reason, shadow)


def _freeze(code: str, reason: str, checks: dict[str, bool], shadow: bool) -> RiskDecision:
    return RiskDecision("FREEZE", (code,), checks, Decimal("0"), reason, shadow)


def expected_move_bps(state: MarketState) -> Decimal | None:
    """用 1h ATR% 的半个 ATR 作为"一笔交易的目标移动"（保守、可解释）。"""
    atr_pct = state.features.get("atr_pct")
    if not isinstance(atr_pct, Decimal) or atr_pct <= 0:
        return None
    return atr_pct * BPS * Decimal("0.5")


def evaluate_market_gate(state: MarketState, limits: RiskLimits) -> RiskDecision | None:
    """没有意图时也执行的市场级门禁。

    数据门（陈旧/缺口）与极端风险必须在风控层留下明确记录，而不是只让策略弃权：
    这样日志里能看到"风控拒绝"，而不是一个语焉不详的 HOLD。
    """
    checks: dict[str, bool] = {}
    if limits.kill_switch:
        checks["kill_switch"] = False
        return _freeze("KILL_SWITCH", "人工/熔断开关处于关闭状态", checks, False)
    checks["kill_switch"] = True
    dq = state.data_quality
    if dq is None or dq.critical_stale:
        checks["data_fresh"] = False
        return _freeze("DATA_STALE", "1h 数据陈旧，趋势周期不可信", checks, False)
    checks["data_fresh"] = True
    if not dq.contiguous:
        checks["data_contiguous"] = False
        return _deny("DATA_GAPS", "1h 数据存在缺口", checks, False)
    checks["data_contiguous"] = True
    if state.risk == "RISK_EXTREME":
        checks["risk_extreme"] = False
        return _deny("RISK_EXTREME", "状态机判定极端风险", checks, False)
    return None


def evaluate(intent: TradeIntent, state: MarketState, account: AccountSnapshot,
             limits: RiskLimits, spread_bps: Decimal = Decimal("5")) -> RiskDecision:
    checks: dict[str, bool] = {}

    if limits.kill_switch:
        checks["kill_switch"] = False
        return _freeze("KILL_SWITCH", "人工/熔断开关处于关闭状态", checks, intent.shadow)
    checks["kill_switch"] = True

    dq = state.data_quality
    if dq is None or dq.critical_stale:
        checks["data_fresh"] = False
        return _freeze("DATA_STALE", "1h 数据陈旧，趋势周期不可信", checks, intent.shadow)
    checks["data_fresh"] = True

    if not dq.contiguous:
        checks["data_contiguous"] = False
        return _deny("DATA_GAPS", "1h 数据存在缺口", checks, intent.shadow)
    checks["data_contiguous"] = True

    if state.risk == "RISK_EXTREME":
        checks["risk_extreme"] = False
        return _deny("RISK_EXTREME", "状态机判定极端风险", checks, intent.shadow)
    checks["risk_extreme"] = True

    if spread_bps > limits.max_spread_bps:
        checks["spread_ok"] = False
        return _deny("SPREAD_TOO_WIDE", f"点差 {spread_bps}bps > {limits.max_spread_bps}",
                     checks, intent.shadow)
    checks["spread_ok"] = True

    gross = expected_move_bps(state)
    cost = limits.fee_bps * 2 + spread_bps + limits.slippage_bps
    if gross is None:
        checks["cost_ok"] = False
        return _deny("COST_UNKNOWN", "缺少 ATR%，无法估算成本", checks, intent.shadow)
    ev_net = gross - cost
    if ev_net < limits.min_ev_bps:
        checks["cost_ok"] = False
        return _deny("COST_TOO_HIGH",
                     f"EV_net={ev_net:.1f}bps < {limits.min_ev_bps}bps（毛 {gross:.1f} − 成本 {cost:.1f}）",
                     checks, intent.shadow)
    checks["cost_ok"] = True

    if account.open_positions >= limits.max_open_positions:
        checks["positions_ok"] = False
        return _deny("MAX_POSITIONS", f"持仓 {account.open_positions} >= {limits.max_open_positions}",
                     checks, intent.shadow)
    checks["positions_ok"] = True

    room = limits.max_gross_exposure - account.gross_exposure
    notional = min(limits.max_notional, room)
    if notional <= 0:
        checks["exposure_ok"] = False
        return _deny("EXPOSURE_LIMIT", "总敞口已达上限", checks, intent.shadow)
    checks["exposure_ok"] = True

    if account.realized_pnl_today <= -limits.daily_loss_limit:
        checks["daily_loss_ok"] = False
        return _freeze("DAILY_LOSS", "当日亏损达到上限", checks, intent.shadow)
    checks["daily_loss_ok"] = True

    fee = notional * limits.fee_bps / BPS
    if account.cash < notional + fee:
        checks["cash_ok"] = False
        return _deny("INSUFFICIENT_CASH", "现金不足（含手续费）", checks, intent.shadow)
    checks["cash_ok"] = True

    return RiskDecision(
        "ALLOW", ("OK",), checks, notional,
        f"放行 {notional} USDT（EV_net={ev_net:.1f}bps）", intent.shadow,
    )