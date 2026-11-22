"""实验 M：配对历史事件回放（可复算、无前视）。

铁律：
  - 事件 i 只使用 bar[0..i] 的已收盘数据；
  - 管理只使用 bar[i+1..j] 的已收盘数据；
  - 所有组在**同一个固定观察窗口**结束时统一结算；
  - 未平仓必须按窗口末价做盯市，并扣除预计平仓成本，不能记 0；
  - 没有历史新闻就写 NEWS_UNAVAILABLE，不虚构；
  - 每次实验独立 run_id / 目录，禁止复用旧 SQLite 或追加旧 JSONL。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from research.decision.position import (
    PositionDecision, PositionSnapshot, rules_position_action, snapshot_from_row,
    validate_against_position,
)
from research.market.contracts import Candle
from research.paper.broker import PaperBroker
from research.strategy.contracts import TradeIntent

_GROUPS = ("A", "B", "C", "D")
_REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL")
BPS = D("10000")


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
    regime: str = "RANGE"
    atr_pct: str = "0"
    bar_index: int = 0

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id, "as_of_ms": self.as_of_ms, "symbol": self.symbol,
            "side": self.side, "entry_price": str(self.entry_price), "qty": str(self.qty),
            "notional": str(self.notional), "stop_price": str(self.stop_price),
            "tp_price": str(self.tp_price), "fee_bps": str(self.fee_bps),
            "slippage_bps": str(self.slippage_bps), "risk_budget": str(self.risk_budget),
            "reason": self.reason, "trend_1h": self.trend_1h, "regime": self.regime,
            "atr_pct": self.atr_pct, "bar_index": self.bar_index,
        }


@dataclass
class GroupResult:
    group: str
    event_id: str
    realized_pnl: D = D("0")
    unrealized_pnl: D = D("0")
    equity_end: D = D("1000")
    fees_paid: D = D("0")
    funding: D = D("0")
    exit_reason: str = "OPEN"
    holding_ms: int = 0
    mfe: D = D("0")
    mae: D = D("0")
    actions: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    ai_calls: int = 0
    ai_ok: int = 0
    ai_hold: int = 0
    ai_fail: int = 0

    @property
    def pnl(self) -> D:
        return self.realized_pnl + self.unrealized_pnl

    def to_dict(self) -> dict:
        return {
            "group": self.group, "event_id": self.event_id,
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "net_pnl": str(self.pnl), "equity_end": str(self.equity_end),
            "fees_paid": str(self.fees_paid), "funding": str(self.funding),
            "exit_reason": self.exit_reason, "holding_ms": self.holding_ms,
            "mfe": str(self.mfe), "mae": str(self.mae),
            "actions": self.actions, "trades": self.trades,
            "ai_calls": self.ai_calls, "ai_ok": self.ai_ok,
            "ai_hold": self.ai_hold, "ai_fail": self.ai_fail,
        }


def _trend_series(closes: list[D], period: int = 20) -> list[str]:
    """增量 EMA：避免每根 K 线重算整段历史。"""
    out: list[str] = []
    last = None
    for c in closes:
        last = c if last is None else c * D(2) / D(period + 1) + last * D(period - 1) / D(period + 1)
        out.append("TREND_UP" if c > last else ("TREND_DOWN" if c < last else "FLAT"))
    return out


def generate_events(bars: list[Candle], *, notional: D = D("100"),
                    fee_bps: D = D("4"), slippage_bps: D = D("5"),
                    stop_atr: D = D("1.5"), tp_atr: D = D("3"),
                    cooldown_bars: int = 3, max_events: int | None = None,
                    symbol: str = "BTC/USDT") -> list[EntryEvent]:
    """固定规则：5m EMA20 上穿做多 / 下穿做空；增量指标 O(n)，无前视。"""
    if len(bars) < 40:
        return []
    out: list[EntryEvent] = []
    last_i = -10 ** 9
    ema20 = None
    trs: list[D] = []
    atr14 = None
    prev_close = None
    closes: list[D] = []
    trends: list[str] = []

    for i, c in enumerate(bars):
        closes.append(c.close)
        # EMA20 增量
        new_ema = c.close if ema20 is None else (
            c.close * D(2) / D(21) + ema20 * D(19) / D(21))
        ema_prev = ema20
        ema20 = new_ema
        trend_now = "TREND_UP" if c.close > new_ema else (
            "TREND_DOWN" if c.close < new_ema else "FLAT")
        trends.append(trend_now)
        # Wilder ATR14 增量
        if prev_close is None:
            tr = c.high - c.low
        else:
            tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        prev_close = c.close
        trs.append(tr)
        if len(trs) == 14:
            atr14 = sum(trs) / D("14")
        elif len(trs) > 14 and atr14 is not None:
            atr14 = (atr14 * D("13") + tr) / D("14")

        if i < 20 or atr14 is None or atr14 <= 0 or ema_prev is None:
            continue
        prev = bars[i - 1]
        side = None
        if prev.close <= ema_prev and c.close > new_ema:
            side = "LONG"
        elif prev.close >= ema_prev and c.close < new_ema:
            side = "SHORT"
        if side is None or i - last_i < cooldown_bars:
            continue

        atr_pct = atr14 / c.close
        # 事先固定的分层规则（不用收益挑选）：
        # 高波动优先；其次价格贴着 EMA20（±0.5ATR）算震荡；否则按趋势方向。
        if atr_pct >= D("0.004"):
            regime = "HIGH_VOL"
        elif abs(c.close - new_ema) <= D("0.5") * atr14:
            regime = "RANGE"
        elif trend_now == "TREND_UP":
            regime = "TREND_UP"
        elif trend_now == "TREND_DOWN":
            regime = "TREND_DOWN"
        else:
            regime = "RANGE"
        sign = D("1") if side == "LONG" else D("-1")
        min_risk = c.close * slippage_bps / BPS * D("2")
        stop_dist = max(stop_atr * atr14, min_risk)
        tp_dist = max(tp_atr * atr14, min_risk * D("2"))
        trend_1h = "TREND_UP" if i >= 12 and trends[i - 12] == "TREND_UP" else (
            "TREND_DOWN" if i >= 12 and trends[i - 12] == "TREND_DOWN" else "RANGE")
        out.append(EntryEvent(
            event_id=f"{symbol.replace('/', '')}-{i}", as_of_ms=int(c.close_ms),
            symbol=symbol, side=side, entry_price=c.close, qty=notional / c.close,
            notional=notional, stop_price=c.close - sign * stop_dist,
            tp_price=c.close + sign * tp_dist, fee_bps=fee_bps,
            slippage_bps=slippage_bps, risk_budget=D("20"),
            reason="fixed EMA20 cross (incremental, no lookahead)",
            trend_1h=trend_1h, regime=regime, atr_pct=str(atr_pct), bar_index=i))
        last_i = i
        if max_events and len(out) >= max_events:
            break
    return out


def aggregate(bars: list[Candle], minutes: int) -> list[Candle]:
    """从 5m 聚合 15m/1h，只用已收盘数据。"""
    step = minutes * 60_000
    buckets: dict[int, list[Candle]] = {}
    for c in bars:
        buckets.setdefault(int(c.open_ms // step) * step, []).append(c)
    out = []
    for k in sorted(buckets):
        rows = buckets[k]
        out.append(Candle(k, rows[0].open,
                          max(x.high for x in rows), min(x.low for x in rows),
                          rows[-1].close, sum((x.volume for x in rows), D("0")), step))
    return out


def select_stratified(events: list[EntryEvent], n: int = 40, seed: int = 20261122) -> list[EntryEvent]:
    """预先固定的分层抽样：按 regime × 时间四分位，均匀选，不按收益挑选。"""
    if n <= 0 or not events:
        return []
    buckets: dict[tuple[str, int], list[EntryEvent]] = {}
    ordered = sorted(events, key=lambda e: e.as_of_ms)
    q = max(1, len(ordered) // 4)
    for e in ordered:
        time_bin = min(3, (e.as_of_ms - ordered[0].as_of_ms) // (q * 300_000))
        buckets.setdefault((e.regime, time_bin), []).append(e)
    keys = sorted(buckets)
    picked: list[EntryEvent] = []
    i = 0
    while len(picked) < min(n, len(events)) and keys:
        k = keys[i % len(keys)]
        rows = buckets[k]
        if rows:
            picked.append(rows.pop(0))
        else:
            keys.remove(k)
            if not keys:
                break
        i += 1
    return sorted(picked, key=lambda e: e.as_of_ms)


def _make_broker(path: Path, event: EntryEvent, funding_bps: D) -> PaperBroker:
    b = PaperBroker(path, starting_cash=D("1000"), fee_bps=event.fee_bps,
                    slippage_bps=event.slippage_bps,
                    funding_bps_per_8h=funding_bps, shadow_mode="allow")
    factor = (D("1") + event.slippage_bps / BPS) if event.side == "LONG" \
        else (D("1") - event.slippage_bps / BPS)
    ref = event.entry_price / factor
    intent = TradeIntent(
        event.event_id, event.as_of_ms, event.symbol, event.side, "paired", "v1",
        ref, event.stop_price, event.tp_price, 4 * 3_600_000, D("0.7"),
        event.reason, shadow=True)
    if b.submit(intent, event.notional) is None:
        raise RuntimeError(f"配对初始条件开仓失败: {event.event_id}")
    return b


def _real_snapshot(b: PaperBroker, mark: D, ts: int, event: EntryEvent,
                   trends: dict[str, str], mfe: D, mae: D) -> PositionSnapshot:
    pos = b.open_positions()[0]
    return snapshot_from_row(
        row=pos, mark=mark, as_of_ms=ts, fee_bps=b.fee_bps,
        trends=trends, news_delta=(), news_status="NEWS_UNAVAILABLE",
        risk_budget_left=event.risk_budget, mfe=mfe, mae=mae,
        data_as_of_ms=ts)


def _apply(b: PaperBroker, pos_id: int, snap: PositionSnapshot,
           dec: PositionDecision, ts: int, mark: D, r: GroupResult) -> None:
    if dec.action == "CLOSE_ALL":
        b.close_position(pos_id, mark, ts, "MGR_CLOSE")
    elif dec.action == "CLOSE_PARTIAL":
        b.close_partial(pos_id, snap.qty * D(dec.fraction), mark, ts, "MGR_PARTIAL")
    elif dec.action == "MOVE_STOP":
        b.move_stop(pos_id, D(dec.new_stop))
    elif dec.action == "UPDATE_TP":
        b.update_tp(pos_id, D(dec.new_tp))
    r.actions.append({"ts": ts, "action": dec.action, "reason": dec.rationale[:160],
                      "provider": dec.provider, "new_stop": str(dec.new_stop) if dec.new_stop else None,
                      "new_tp": str(dec.new_tp) if dec.new_tp else None,
                      "fraction": str(dec.fraction) if dec.fraction else None,
                      "news_status": getattr(snap, "news_status", "UNKNOWN")})


def run_event(event: EntryEvent, future: list[Candle], root: Path,
              *, ai_manager=None, horizon: int = 72,
              ai_only_when_success: bool = True,
              funding_bps_per_8h: D = D("1")) -> list[GroupResult]:
    """同一事件跑 A/B/C/D，统一窗口末结算。"""
    window = future[:horizon]
    if not window:
        raise ValueError("观察窗口为空；horizon 必须 >= 1")
    end_ms = int(window[-1].close_ms)
    end_mark = window[-1].close
    t5 = _trend_series([c.close for c in future], 20)
    t15 = _trend_series([c.close for c in aggregate(future, 15)], 20)
    t1h = _trend_series([c.close for c in aggregate(future, 60)], 20)
    agg15, agg1h = aggregate(future, 15), aggregate(future, 60)

    def trends_at(j: int) -> dict[str, str]:
        bar = window[j]
        a15 = max((x for x in agg15 if x.close_ms <= bar.close_ms),
                  default=None, key=lambda x: x.close_ms)
        a1 = max((x for x in agg1h if x.close_ms <= bar.close_ms),
                 default=None, key=lambda x: x.close_ms)
        i15 = agg15.index(a15) if a15 is not None else -1
        i1 = agg1h.index(a1) if a1 is not None else -1
        return {"5m": t5[j] if j < len(t5) else "UNKNOWN",
                "15m": t15[i15] if i15 >= 0 else "UNKNOWN",
                "1h": t1h[i1] if i1 >= 0 else "UNKNOWN"}

    results: list[GroupResult] = []
    for group in _GROUPS:
        r = GroupResult(group, event.event_id)
        b = _make_broker(root / f"{group}_{event.event_id}.sqlite", event,
                         funding_bps_per_8h)
        stage = ""
        mfe = mae = D("0")
        direction = D("1") if event.side == "LONG" else D("-1")
        for j, bar in enumerate(window):
            ts = int(bar.close_ms)
            # broker 内部已处理同根止损/止盈保守顺序与跳空更差成交
            b.mark_bar(event.symbol, bar.open, bar.high, bar.low, ts,
                       bar_open_ms=int(bar.open_ms))
            pos_rows = b.open_positions()
            if not pos_rows:
                break
            pos = pos_rows[0]
            entry = D(pos["entry_price"])
            qty = D(pos["qty"])
            fav = ((bar.high - entry) if direction > 0 else (entry - bar.low)) * qty
            adv = ((entry - bar.low) if direction > 0 else (bar.high - entry)) * qty
            mfe, mae = max(mfe, fav), max(mae, adv)
            snap = _real_snapshot(b, bar.close, ts, event, trends_at(j), mfe, mae)

            if group == "A":
                # A = 原始退出规则：固定 ATR 止损止盈 + 满 2 根后只要有浮亏就认错
                gross = (bar.close - snap.entry_price) * snap.qty * direction
                dec = (PositionDecision("fixed_atr_early_exit", "v1", "CLOSE_ALL",
                                        f"原始规则：{j} 根后浮亏 {gross:.4f}", D("1"),
                                        data_as_of_ms=ts)
                       if j >= 2 and gross < 0 else
                       PositionDecision("fixed_atr_early_exit", "v1", "HOLD",
                                        "固定 ATR 保护，未到认错条件", D("1"),
                                        data_as_of_ms=ts))
            elif group == "B":
                loss_bps = ((bar.close - snap.entry_price) / snap.entry_price
                            * BPS * direction)
                dec = (PositionDecision("cost_aware", "v1", "CLOSE_ALL",
                                        f"loss {loss_bps:.1f}bps > cost+buffer", D("0.8"),
                                        data_as_of_ms=ts)
                       if j >= 2 and loss_bps <= -D("15") else
                       PositionDecision("cost_aware", "v1", "HOLD", "noise",
                                        D("0.6"), data_as_of_ms=ts))
            elif group == "C":
                dec = rules_position_action(snap, stage=stage)
            else:
                if ai_manager is None:
                    r.actions.append({"ts": ts, "action": "NO_AI", "provider": None,
                                      "reason": "no provider", "news_status": "NEWS_UNAVAILABLE"})
                    break
                try:
                    dec = ai_manager.decide(snap)
                    r.ai_calls += 1
                    st = str(getattr(dec, "status", "OK"))
                    if st == "OK":
                        r.ai_ok += 1
                    else:
                        r.ai_fail += 1
                    if dec.action == "HOLD":
                        r.ai_hold += 1
                    if st != "OK" and ai_only_when_success:
                        r.actions.append({"ts": ts, "action": "AI_ERROR",
                                          "reason": str(dec.rationale)[:160],
                                          "provider": dec.provider,
                                          "news_status": "NEWS_UNAVAILABLE"})
                        break
                except Exception as exc:  # noqa: BLE001
                    r.ai_calls += 1
                    r.ai_fail += 1
                    r.actions.append({"ts": ts, "action": "AI_EXCEPTION",
                                      "reason": f"{type(exc).__name__}: {exc}"[:160],
                                      "provider": None})
                    break
            try:
                validate_against_position(dec, snap)
            except Exception as exc:  # noqa: BLE001
                if group == "D":
                    r.ai_fail += 1
                r.actions.append({"ts": ts, "action": "REJECTED",
                                  "reason": str(exc)[:160], "provider": dec.provider})
                continue
            _apply(b, int(pos["id"]), snap, dec, ts, bar.close, r)
            if dec.action == "CLOSE_PARTIAL":
                stage = "partial_done"

        # ---------- 统一窗口末结算 ----------
        rows = b._db.execute(
            "SELECT pnl, exit_reason, qty, entry_price, entry_fee, exit_ts, exit_price"
            " FROM positions WHERE symbol = ?", (event.symbol,)).fetchall()
        fills = b._db.execute("SELECT pnl, fee, qty, price, ts_ms FROM position_fills").fetchall()
        orders = b._db.execute("SELECT fee, notional FROM orders").fetchall()
        r.realized_pnl = sum((D(str(x["pnl"] or 0)) for x in rows), D("0")) + \
            sum((D(str(x["pnl"] or 0)) for x in fills), D("0"))
        r.fees_paid = sum((D(str(x["fee"] or 0)) for x in orders), D("0")) + \
            sum((D(str(x["fee"] or 0)) for x in fills), D("0"))

        opened = b.open_positions()
        if opened:
            p = opened[0]
            qty, entry = D(p["qty"]), D(p["entry_price"])
            gross = (end_mark - entry) * qty * direction
            exit_fee = qty * end_mark * event.fee_bps / BPS
            exit_slip = qty * end_mark * event.slippage_bps / BPS
            hours = D(max(0, end_ms - int(p["entry_ts"]))) / D("3_600_000")
            funding = qty * entry * funding_bps_per_8h / BPS * (hours / D("8")) * direction
            r.unrealized_pnl = gross - exit_fee - exit_slip \
                - D(p["entry_fee"] or "0") - funding
            r.funding = funding
            r.exit_reason = "OPEN_AT_WINDOW_END"
            r.holding_ms = end_ms - int(p["entry_ts"])
        closed = [x for x in rows if x["exit_reason"]]
        if closed:
            last = closed[-1]
            r.exit_reason = str(last["exit_reason"])
            r.holding_ms = int(last["exit_ts"] or 0) - event.as_of_ms
        elif fills:
            r.exit_reason = "PARTIAL_AT_WINDOW_END"

        r.mfe, r.mae = mfe, mae
        r.equity_end = D("1000") + r.pnl
        r.trades = [{"order_fee": str(x["fee"]), "notional": str(x["notional"])}
                    for x in orders] + [
                       {"fill_qty": str(x["qty"]), "fill_price": str(x["price"]),
                        "fill_fee": str(x["fee"]), "pnl": str(x["pnl"])} for x in fills]
        results.append(r)
        b.close()
    return results
