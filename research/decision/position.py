"""V3 持仓管理契约：AI 不只看方向，还要看"这一笔现在怎么样了"。

设计约束（来自实盘复盘）：

1. **动作空间是有界的**：模型只能在 ``ACTIONS`` 里选，且每个动作必须给出
   目标（``new_stop`` / ``fraction`` / ``notional``）、理由、依据、数据时间、失效条件。
   结构不合法直接拒收，绝不"猜一个默认值"继续执行。
2. **风险底线不能由模型撤掉**：``MOVE_STOP`` 只允许**收紧**（多头抬高、空头压低），
   想放宽必须走独立的人工/风控通道；``CLOSE_ALL`` 之后保护单由执行层清理。
3. **逆势加仓不允许**：``ADD_TO_WINNER`` 只在浮盈时可用，且加仓后整体风险
   必须仍在预算内（由 ``research.risk`` 复核，不由模型自证）。
4. 所有字段都是**可回放**的：``data_as_of_ms`` 说明模型看到的数据截止时间，
   ``invalid_if`` 说明什么条件下原判断作废。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ACTIONS = ("HOLD", "CLOSE_ALL", "CLOSE_PARTIAL", "MOVE_STOP", "UPDATE_TP",
           "ADD_TO_WINNER")

#: 需要给出新止损价的动作
NEEDS_STOP = ("MOVE_STOP",)
#: 需要给出减仓比例的动作
NEEDS_FRACTION = ("CLOSE_PARTIAL",)
#: 需要给出加仓名义金额的动作
NEEDS_NOTIONAL = ("ADD_TO_WINNER",)

D = Decimal


class PositionDecisionError(ValueError):
    """结构不合法。宁可拒绝也不猜。"""


@dataclass(frozen=True)
class PositionSnapshot:
    """一次持仓评估的完整输入。模型看不到这些，就只能重复输出 LONG/SHORT/HOLD。"""

    as_of_ms: int
    symbol: str
    side: str                        # LONG / SHORT
    qty: D
    entry_price: D                   # **实际成交均价**，不是信号价
    mark_price: D                    # 当前价格
    notional: D                      # qty * mark_price
    unrealized_net: D                # 扣双边预估手续费后的浮盈亏
    realized_net: D = D("0")         # 该笔已实现（分批减仓后累计）
    est_fees_roundtrip: D = D("0")   # 按当前 qty 估的双边手续费
    mfe: D = D("0")                  # 最大有利波动（价格口径）
    mae: D = D("0")                  # 最大不利波动
    holding_ms: int = 0
    stop_price: D | None = None
    tp_price: D | None = None
    entry_reason: str = ""
    invalid_if: tuple[str, ...] = ()
    news_delta: tuple[str, ...] = ()
    #: 新闻来源状态。没有历史新闻时必须显式 NEWS_UNAVAILABLE，不能假装空列表=没有新消息。
    news_status: str = "OK"
    trend_5m: str = ""
    trend_15m: str = ""
    trend_1h: str = ""
    risk_budget_left: D = D("0")     # 当日剩余风险预算（USDT）
    data_as_of_ms: int = 0
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.side not in ("LONG", "SHORT"):
            raise PositionDecisionError(f"side 非法: {self.side}")
        if self.qty <= 0:
            raise PositionDecisionError(f"qty 必须 > 0: {self.qty}")
        if self.entry_price <= 0 or self.mark_price <= 0:
            raise PositionDecisionError("价格必须 > 0")

    # ---------- 给模型看的摘要 ----------
    def summary(self) -> dict:
        return {
            "as_of_ms": self.as_of_ms,
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "entry_price": str(self.entry_price),
            "mark_price": str(self.mark_price),
            "notional": str(self.notional),
            "unrealized_net": str(self.unrealized_net),
            "realized_net": str(self.realized_net),
            "est_fees_roundtrip": str(self.est_fees_roundtrip),
            "mfe": str(self.mfe),
            "mae": str(self.mae),
            "holding_ms": self.holding_ms,
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "tp_price": str(self.tp_price) if self.tp_price is not None else None,
            "entry_reason": self.entry_reason[:200],
            "invalid_if": list(self.invalid_if)[:5],
            "news_delta": list(self.news_delta)[:5],
            "news_status": self.news_status,
            "trend": {"5m": self.trend_5m, "15m": self.trend_15m, "1h": self.trend_1h},
            "risk_budget_left": str(self.risk_budget_left),
            "data_as_of_ms": self.data_as_of_ms,
        }


@dataclass(frozen=True)
class PositionDecision:
    """持仓管理动作。字段校验比方向决策严格得多。"""

    provider: str
    version: str
    action: str
    rationale: str
    confidence: D = D("0")
    new_stop: D | None = None
    new_tp: D | None = None
    fraction: D | None = None        # CLOSE_PARTIAL：0<fraction<1
    notional: D | None = None        # ADD_TO_WINNER：加仓名义
    evidence: tuple[str, ...] = ()
    data_as_of_ms: int = 0
    invalid_if: tuple[str, ...] = ()
    latency_ms: int = 0
    status: str = "OK"
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise PositionDecisionError(f"action 非法: {self.action}")
        if not str(self.rationale or "").strip():
            raise PositionDecisionError("必须给出 rationale")
        if int(self.data_as_of_ms or 0) <= 0:
            raise PositionDecisionError("必须给出 data_as_of_ms（模型看到的数据截止时间）")
        if self.action in NEEDS_STOP:
            if self.new_stop is None or self.new_stop <= 0:
                raise PositionDecisionError("MOVE_STOP 必须给出 new_stop > 0")
            if not self.invalid_if:
                raise PositionDecisionError("MOVE_STOP 必须给出 invalid_if")
        if self.action in NEEDS_FRACTION:
            if self.fraction is None or not (D("0") < self.fraction < D("1")):
                raise PositionDecisionError("CLOSE_PARTIAL 的 fraction 必须在 (0,1)")
        if self.action in NEEDS_NOTIONAL:
            if self.notional is None or self.notional <= 0:
                raise PositionDecisionError("ADD_TO_WINNER 必须给出 notional > 0")
        if self.action == "UPDATE_TP" and (self.new_tp is None or self.new_tp <= 0):
            raise PositionDecisionError("UPDATE_TP 必须给出 new_tp > 0")
        if self.confidence < 0 or self.confidence > 1:
            raise PositionDecisionError(f"confidence 越界: {self.confidence}")

    @property
    def actionable(self) -> bool:
        return self.status == "OK" and self.action != "HOLD"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "version": self.version, "status": self.status,
            "action": self.action, "confidence": str(self.confidence),
            "rationale": self.rationale,
            "new_stop": str(self.new_stop) if self.new_stop is not None else None,
            "new_tp": str(self.new_tp) if self.new_tp is not None else None,
            "fraction": str(self.fraction) if self.fraction is not None else None,
            "notional": str(self.notional) if self.notional is not None else None,
            "evidence": list(self.evidence), "data_as_of_ms": self.data_as_of_ms,
            "invalid_if": list(self.invalid_if), "latency_ms": self.latency_ms,
        }


def validate_against_position(d: PositionDecision, snap: PositionSnapshot) -> None:
    """动作和持仓状态是否自洽。不合规直接抛，不修正。"""
    if d.action == "MOVE_STOP":
        stop = D(d.new_stop)
        if snap.side == "LONG" and stop >= snap.mark_price:
            raise PositionDecisionError(f"多头止损 {stop} 必须低于现价 {snap.mark_price}")
        if snap.side == "SHORT" and stop <= snap.mark_price:
            raise PositionDecisionError(f"空头止损 {stop} 必须高于现价 {snap.mark_price}")
        if snap.stop_price is not None:
            # 只允许收紧：多头往上、空头往下；放宽必须走人工通道
            if snap.side == "LONG" and stop <= D(snap.stop_price):
                raise PositionDecisionError(
                    f"多头止损只能上移（原 {snap.stop_price} → 新 {stop}）")
            if snap.side == "SHORT" and stop >= D(snap.stop_price):
                raise PositionDecisionError(
                    f"空头止损只能下移（原 {snap.stop_price} → 新 {stop}）")
    if d.action == "ADD_TO_WINNER":
        if snap.unrealized_net <= 0:
            raise PositionDecisionError("只有浮盈时才允许顺势加仓（禁止逆势摊平）")
    if d.action == "UPDATE_TP":
        tp = D(d.new_tp)
        if snap.side == "LONG" and tp <= snap.mark_price:
            raise PositionDecisionError(f"多头止盈 {tp} 必须高于现价 {snap.mark_price}")
        if snap.side == "SHORT" and tp >= snap.mark_price:
            raise PositionDecisionError(f"空头止盈 {tp} 必须低于现价 {snap.mark_price}")
    if d.action == "CLOSE_PARTIAL" and d.fraction * snap.qty <= 0:
        raise PositionDecisionError("减仓数量必须 > 0")


def rules_position_action(snap: PositionSnapshot,
                          *, trail_atr: D = D("1.0"),
                          partial_at_r: D = D("1.0"),
                          partial_fraction: D = D("0.5"),
                          stage: str = "") -> PositionDecision:
    """确定性持仓管理（对照组 C，不依赖任何模型）。

    规则（刻意保守、可解释）：
      1. 浮盈达到 1R（R = 入场到止损的距离）→ 减仓 50%，并止损移到成本价；
      2. 之后按 1×ATR 距离移动止损（这里用 entry 与 stop 的距离近似 ATR）；
      3. 浮亏且原始止损未破 → HOLD（不做"两根 K 线认错"）。
    """
    risk = abs(snap.entry_price - snap.stop_price) if snap.stop_price is not None else D("0")
    direction = D("1") if snap.side == "LONG" else D("-1")
    move = (snap.mark_price - snap.entry_price) * direction      # 每单位浮盈
    r_multiple = (move / risk) if risk > 0 else D("0")
    trail = risk * trail_atr if risk > 0 else D("0")

    already_partial = "partial_done" in str(stage or "")
    if snap.stop_price is not None and r_multiple >= partial_at_r and not already_partial:
        # 只在**首次**达到 1R 时减仓一次；阶段状态持久化在账本里，
        # 否则下一根 K 线仍满足条件，会把剩余仓位一路减到零，永远走不到移动止损。
        return PositionDecision(
            "rules_position", "v1", "CLOSE_PARTIAL",
            f"浮盈 {r_multiple:.2f}R 达到 {partial_at_r}R，先减 {partial_fraction:.0%} 落袋",
            D("1"), fraction=partial_fraction,
            evidence=(f"r_multiple={r_multiple:.2f}",),
            data_as_of_ms=snap.data_as_of_ms,
            invalid_if=("价格回落到入场价以下",),
        )
    if snap.stop_price is not None and r_multiple > D("0.3") and trail > 0:
        new_stop = (snap.mark_price - trail) if snap.side == "LONG" else (snap.mark_price + trail)
        tighter = (snap.side == "LONG" and new_stop > D(snap.stop_price)) or \
                  (snap.side == "SHORT" and new_stop < D(snap.stop_price))
        if tighter:
            return PositionDecision(
                "rules_position", "v1", "MOVE_STOP",
                f"浮盈 {r_multiple:.2f}R，止损跟到 {new_stop:.2f}（{trail_atr}×R 距离）",
                D("0.8"), new_stop=new_stop,
                evidence=(f"trail={trail:.2f}",),
                data_as_of_ms=snap.data_as_of_ms,
                invalid_if=("价格反抽超过 1R",),
            )
    return PositionDecision(
        "rules_position", "v1", "HOLD",
        f"浮盈 {r_multiple:.2f}R、未触发减仓/移动止损条件，原逻辑未见失效",
        D("0.6"), data_as_of_ms=snap.data_as_of_ms,
        invalid_if=("跌破原始止损", "1h 趋势反转"),
    )


def snapshot_from_row(*, row: dict, mark: D, as_of_ms: int, fee_bps: D,
                      trends: dict[str, str] | None = None,
                      news_delta: tuple[str, ...] = (),
                      news_status: str = "OK",
                      risk_budget_left: D = D("0"),
                      mfe: D = D("0"), mae: D = D("0"),
                      data_as_of_ms: int | None = None) -> PositionSnapshot:
    """把券商持仓行 + 当前价装成快照。所有数字都来自账本，不猜。"""
    qty = D(str(row["qty"]))
    entry = D(str(row["entry_price"]))
    side = str(row["side"])
    direction = D("1") if side == "LONG" else D("-1")
    notional = qty * mark
    gross = (mark - entry) * qty * direction
    fees = notional * fee_bps / D("10000") * D("2")
    realized = D(str(row.get("realized_net") or "0"))
    invalid = row.get("invalid_if") or ()
    if isinstance(invalid, str):
        try:
            import json
            invalid = tuple(json.loads(invalid))
        except Exception:  # noqa: BLE001 - 老账本里不是 JSON 就当一段文字
            invalid = (invalid,)
    trends = trends or {}
    return PositionSnapshot(
        as_of_ms=as_of_ms, symbol=str(row["symbol"]), side=side, qty=qty,
        entry_price=entry, mark_price=mark, notional=notional,
        unrealized_net=gross - fees, realized_net=realized,
        est_fees_roundtrip=fees, mfe=mfe, mae=mae,
        holding_ms=max(0, as_of_ms - int(row["entry_ts"])),
        stop_price=D(str(row["stop_price"])) if row.get("stop_price") else None,
        tp_price=D(str(row["tp_price"])) if row.get("tp_price") else None,
        entry_reason=str(row.get("reason") or ""),
        invalid_if=tuple(invalid),
        news_delta=tuple(news_delta),
        news_status=news_status,
        trend_5m=trends.get("5m", ""), trend_15m=trends.get("15m", ""),
        trend_1h=trends.get("1h", ""),
        risk_budget_left=risk_budget_left,
        data_as_of_ms=int(data_as_of_ms or as_of_ms),
    )


__all__: list[Any] = [
    "ACTIONS", "PositionSnapshot", "PositionDecision", "PositionDecisionError",
    "validate_against_position", "rules_position_action", "snapshot_from_row",
]
