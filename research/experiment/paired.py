"""实验 M：配对历史事件回放。

与"等 AI 当天开仓"不同，这里先用**事先固定、无前视**的确定性规则从已收盘
K 线生成一批入场事件；同一批事件分别交给 A/B/C/D 管理，每组从完全相同的
入场时间、方向、成交价、数量、费用和初始风险预算开始。

铁律：
  - 事件 i 只使用 bar[0..i] 的已收盘数据；
  - 之后的管理只使用 bar[i+1..j] 的已收盘数据；
  - 新闻必须带 occurred_at_ms <= 决策时刻，否则不带；
  - 不允许事后挑选盈利事件：事件规则固定、全量生成、全量回放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path
from typing import Callable

from research.decision.position import (
    PositionDecision, PositionSnapshot, rules_position_action, snapshot_from_row,
    validate_against_position,
)
from research.market.contracts import Candle
from research.market.indicators import atr, ema
from research.paper.broker import PaperBroker
from research.strategy.contracts import TradeIntent



@dataclass(frozen=True)
class EntryEvent:
    event_id: str
    as_of_ms: int
    symbol: str
    side: str
    entry_price: D
    qty: D
    notional: D
    stop_price: D
    tp_price: D
    fee_bps: D
    slippage_bps: D
    risk_budget: D
    reason: str
    trend_1h: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id, "as_of_ms": self.as_of_ms, "symbol": self.symbol,
            "side": self.side, "entry_price": str(self.entry_price), "qty": str(self.qty),
            "notional": str(self.notional), "stop_price": str(self.stop_price),
            "tp_price": str(self.tp_price), "fee_bps": str(self.fee_bps),
            "slippage_bps": str(self.slippage_bps), "risk_budget": str(self.risk_budget),
            "reason": self.reason, "trend_1h": self.trend_1h,
        }


@dataclass
class GroupResult:
    group: str
    event_id: str
    pnl: D = D("0")
    exit_reason: str = ""
    actions: list[dict] = field(default_factory=list)
    ai_calls: int = 0
    ai_ok: int = 0

    def to_dict(self) -> dict:
        return {"group": self.group, "event_id": self.event_id, "pnl": str(self.pnl),
                "exit_reason": self.exit_reason, "actions": self.actions,
                "ai_calls": self.ai_calls, "ai_ok": self.ai_ok}


def _trend(closes: list[D]) -> str:
    if len(closes) < 21:
        return "UNKNOWN"
    e = ema(closes, 20)
    if not e or e[-1] is None:
        return "UNKNOWN"
    if closes[-1] > e[-1]:
        return "TREND_UP"
    if closes[-1] < e[-1]:
        return "TREND_DOWN"
    return "FLAT"


def generate_events(bars: list[Candle], *, notional: D = D("100"),
                    fee_bps: D = D("4"), slippage_bps: D = D("5"),
                    stop_atr: D = D("1.5"), tp_atr: D = D("3"),
                    cooldown_bars: int = 3, max_events: int | None = None,
                    symbol: str = "BTC/USDT") -> list[EntryEvent]:
    """固定规则：5m EMA20 上穿做多 / 下穿做空。全量生成，无前视、无挑选。"""
    if len(bars) < 40:
        return []
    out: list[EntryEvent] = []
    last_i = -10 ** 9
    for i in range(20, len(bars) - 1):
        upto = bars[:i + 1]
        closes = [c.close for c in upto]
        highs = [c.high for c in upto]
        lows = [c.low for c in upto]
        e_now = ema(closes, 20)[-1]
        e_prev = ema(closes[:-1], 20)[-1]
        if e_now is None or e_prev is None:
            continue
        c, prev = upto[-1], upto[-2]
        side = None
        if prev.close <= e_prev and c.close > e_now:
            side = "LONG"
        elif prev.close >= e_prev and c.close < e_now:
            side = "SHORT"
        if side is None or i - last_i < cooldown_bars:
            continue
        a = atr(highs, lows, closes, 14)
        a14 = a[-1] if a and a[-1] else None
        if not a14 or a14 <= 0:
            continue
        sign = D("1") if side == "LONG" else D("-1")
        entry = c.close
        # 保护距离必须大于一次滑点，否则反推 ref 后可以落到 stop 之外
        min_risk = entry * slippage_bps / D("10000") * D("2")
        stop_dist = max(stop_atr * a14, min_risk)
        tp_dist = max(tp_atr * a14, min_risk * D("2"))
        qty = notional / entry
        event = EntryEvent(
            event_id=f"{symbol.replace('/', '')}-{i}",
            as_of_ms=int(c.close_ms), symbol=symbol, side=side,
            entry_price=entry, qty=qty, notional=notional,
            stop_price=entry - sign * stop_dist,
            tp_price=entry + sign * tp_dist,
            fee_bps=fee_bps, slippage_bps=slippage_bps,
            risk_budget=D("20"), reason="fixed EMA20 cross (no lookahead)",
            trend_1h=_trend(closes),
        )
        out.append(event)
        last_i = i
        if max_events and len(out) >= max_events:
            break
    return out


def _make_broker(path: Path, event: EntryEvent) -> tuple[PaperBroker, str]:
    b = PaperBroker(path, starting_cash=D("1000"), fee_bps=event.fee_bps,
                    slippage_bps=event.slippage_bps, shadow_mode="allow")
    # PaperBroker 会按 side 加减滑点；反推 ref 让**实际成交价**与事件完全一致
    factor = (D("1") + event.slippage_bps / D("10000")) if event.side == "LONG" \
        else (D("1") - event.slippage_bps / D("10000"))
    ref = event.entry_price / factor
    # 事件止损/止盈基于真实成交价；ref 只差几 bps，不会破坏 stop<ref<tp
    intent = TradeIntent(
        event.event_id, event.as_of_ms, event.symbol, event.side, "paired", "v1",
        ref, event.stop_price, event.tp_price, 4 * 3_600_000, D("0.7"),
        event.reason, shadow=True)
    fill = b.submit(intent, event.notional)
    if fill is None:
        raise RuntimeError(f"无法在配对初始条件开仓: {event.event_id}")
    return b, event.event_id


def _snapshot(b: PaperBroker, mark: D, as_of_ms: int, event: EntryEvent) -> PositionSnapshot:
    pos = b.open_positions()[0]
    return snapshot_from_row(
        row=pos, mark=mark, as_of_ms=as_of_ms, fee_bps=b.fee_bps,
        trends={"5m": "", "15m": "", "1h": event.trend_1h},
        risk_budget_left=event.risk_budget, data_as_of_ms=as_of_ms)


def _apply(b: PaperBroker, pos_id: int, snap: PositionSnapshot,
           dec: PositionDecision, as_of_ms: int, mark: D, result: GroupResult) -> None:
    if dec.action == "CLOSE_ALL":
        b.close_position(pos_id, mark, as_of_ms, "MGR_CLOSE")
    elif dec.action == "CLOSE_PARTIAL":
        b.close_partial(pos_id, snap.qty * D(dec.fraction), mark, as_of_ms, "MGR_PARTIAL")
    elif dec.action == "MOVE_STOP":
        b.move_stop(pos_id, D(dec.new_stop))
    elif dec.action == "UPDATE_TP":
        b.update_tp(pos_id, D(dec.new_tp))
    result.actions.append({"ts": as_of_ms, "action": dec.action,
                           "reason": dec.rationale[:120], "provider": dec.provider})


def run_event(event: EntryEvent, future: list[Candle], root: Path,
              *, ai_manager=None, horizon: int = 12,
              ai_only_when_success: bool = True) -> list[GroupResult]:
    """对同一事件跑 A/B/C/D，每组独立的 broker，初始条件完全一致。"""
    results: list[GroupResult] = []
    for group in ("A", "B", "C", "D"):
        r = GroupResult(group, event.event_id)
        b, _ = _make_broker(root / f"{group}_{event.event_id}.sqlite", event)
        stage = ""
        for j, bar in enumerate(future[:horizon]):
            ts = int(bar.close_ms)
            b.mark_bar(event.symbol, bar.open, bar.high, bar.low, ts,
                       bar_open_ms=int(bar.open_ms))
            pos = b.open_positions()
            if not pos:
                break
            pos = pos[0]
            snap = _snapshot(b, bar.close, ts, event)
            if group == "A":
                dec = PositionDecision("fixed_atr", "v1", "HOLD", "let stop/tp work",
                                       D("1"), data_as_of_ms=ts)
            elif group == "B":
                loss_bps = ((bar.close - snap.entry_price) / snap.entry_price
                            * D("10000") * (D("1") if event.side == "LONG" else D("-1")))
                if j >= 2 and loss_bps <= -D("15"):
                    dec = PositionDecision("cost_aware", "v1", "CLOSE_ALL",
                                           f"loss {loss_bps:.1f}bps > cost+buffer",
                                           D("0.8"), data_as_of_ms=ts)
                else:
                    dec = PositionDecision("cost_aware", "v1", "HOLD", "noise",
                                           D("0.6"), data_as_of_ms=ts)
            elif group == "C":
                dec = rules_position_action(snap, stage=stage)
            else:
                if ai_manager is None:
                    # 没有成功模型调用就不许冒充 AI
                    r.actions.append({"ts": ts, "action": "NO_AI",
                                      "reason": "no provider", "provider": None})
                    break
                try:
                    dec = ai_manager.decide(snap)
                    r.ai_calls += 1
                    if str(getattr(dec, "status", "OK")) == "OK":
                        r.ai_ok += 1
                    else:
                        if ai_only_when_success:
                            r.actions.append({"ts": ts, "action": "AI_ERROR",
                                              "reason": str(dec.rationale)[:120],
                                              "provider": dec.provider})
                            break
                except Exception as exc:  # noqa: BLE001
                    r.actions.append({"ts": ts, "action": "AI_EXCEPTION",
                                      "reason": f"{type(exc).__name__}: {exc}"[:120],
                                      "provider": None})
                    break
            try:
                validate_against_position(dec, snap)
            except Exception as exc:  # noqa: BLE001
                r.actions.append({"ts": ts, "action": "REJECTED",
                                  "reason": str(exc)[:120], "provider": dec.provider})
                continue
            _apply(b, int(pos["id"]), snap, dec, ts, bar.close, r)
            if dec.action == "CLOSE_PARTIAL":
                stage = "partial_done"
        rows = b._db.execute(
            "SELECT pnl, exit_reason FROM positions WHERE symbol = ?", (event.symbol,)).fetchall()
        fills = b._db.execute("SELECT pnl FROM position_fills").fetchall()
        r.pnl = sum((D(str(x["pnl"] or 0)) for x in rows), D("0")) + \
            sum((D(str(x["pnl"] or 0)) for x in fills), D("0"))
        closed = [x for x in rows if x["exit_reason"]]
        r.exit_reason = str(closed[-1]["exit_reason"]) if closed else (
            "MGR_PARTIAL" if fills else "OPEN")
        results.append(r)
        b.close()
    return results
