"""实验 E 的确定性撮合与持仓模拟。

无前视铁律：
  - 信号在 bar[i] 收盘后产生；
  - 成交在 bar[i+1] 开盘（ledger.open_at_next_bar）；
  - 之后只用 bar[i+1..] 的 high/low/close 判定止损止盈与结算。

同根双向触发：若一根 K 线的 high 与 low 同时穿过止损和止盈，
**保守按止损先触发**（宁可少赚不可少亏）。跳空开盘越过止损/止盈时按更差价成交。

资金费按持有小时数线性折算（多头付、空头收）；手续费双边；滑点双边不利方向。
"""
from __future__ import annotations

from decimal import Decimal as D

from research.market.contracts import Candle

from .ledger import BPS, CashLedger, OpenPosition, TradeResult, _fill_price, open_at_next_bar


def _funding(pos: OpenPosition, hours: D, bps_per_8h: D) -> D:
    """返回带符号资金费：正数=费用（多头付），负数=收入（空头收）。"""
    base = pos.qty * pos.entry_fill * D(bps_per_8h) / BPS * (hours / D("8"))
    return base if pos.side == "LONG" else -base


def simulate_hold(bars: list[Candle], signal_bar: int, side: str, notional: D,
                  stop_price: D, tp_price: D, *, root: str = "",
                  fee_bps: D = D("4"), slippage_bps: D = D("5"),
                  funding_bps_per_8h: D = D("1"),
                  max_hold_bars: int = 72) -> TradeResult | None:
    """从信号 bar 开仓，持有至止损/止盈/超时，返回逐笔可复算的结果。"""
    ledger = CashLedger()
    try:
        pos, entry_flows = open_at_next_bar(
            bars, signal_bar, side, notional, stop_price, tp_price,
            fee_bps=fee_bps, slippage_bps=slippage_bps, ref=root)
    except ValueError:
        return None
    for f in entry_flows:
        ledger.record(f.ts_ms, f.kind, f.amount, f.ref)

    entry = pos.entry_fill
    direction = D("1") if side == "LONG" else D("-1")
    mfe = mae = D("0")
    exit_fill = exit_reason = ""
    exit_ts = 0
    last_bar = min(len(bars) - 1, signal_bar + max_hold_bars)

    for j in range(signal_bar + 1, last_bar + 1):
        bar = bars[j]
        fav = ((bar.high - entry) if direction > 0 else (entry - bar.low)) * pos.qty
        adv = ((entry - bar.low) if direction > 0 else (bar.high - entry)) * pos.qty
        mfe, mae = max(mfe, fav), max(mae, adv)

        hit_stop = (bar.low <= pos.stop_price) if direction > 0 else (bar.high >= pos.stop_price)
        hit_tp = (bar.high >= pos.tp_price) if direction > 0 else (bar.low <= pos.tp_price)
        # 跳空：用开盘价判断是否已经穿越，避免用盘中价粉饰
        gap_stop = (bar.open <= pos.stop_price) if direction > 0 else (bar.open >= pos.stop_price)
        gap_tp = (bar.open >= pos.tp_price) if direction > 0 else (bar.open <= pos.tp_price)

        if hit_stop or gap_stop:
            raw = bar.open if gap_stop else pos.stop_price
            exit_fill = _fill_price(raw, "SHORT" if direction > 0 else "LONG", slippage_bps)
            exit_reason = "STOP"
            exit_ts = int(bar.open_ms if gap_stop else bar.close_ms)
            break
        if hit_tp or gap_tp:
            raw = bar.open if gap_tp else pos.tp_price
            exit_fill = _fill_price(raw, "SHORT" if direction > 0 else "LONG", slippage_bps)
            exit_reason = "TAKE_PROFIT"
            exit_ts = int(bar.open_ms if gap_tp else bar.close_ms)
            break

    if not exit_reason:
        # 超时：按最后一根已收盘 bar 的收盘价平仓（含双边滑点）
        last = bars[last_bar]
        exit_fill = _fill_price(last.close, "SHORT" if direction > 0 else "LONG", slippage_bps)
        exit_reason = "TIME_EXIT"
        exit_ts = int(last.close_ms)

    gross = (exit_fill - entry) * pos.qty * direction
    exit_fee = pos.qty * exit_fill * D(fee_bps) / BPS
    hours = D(max(0, exit_ts - pos.entry_ts_ms)) / D("3_600_000")
    funding = _funding(pos, hours, funding_bps_per_8h)
    net = gross - pos.entry_fee - exit_fee - funding

    sign_exit = "SHORT" if direction > 0 else "LONG"
    ledger.record(exit_ts, "EXIT_PROCEEDS", pos.qty * entry + gross, root)
    ledger.record(exit_ts, "EXIT_FEE", -exit_fee, root)
    ledger.record(exit_ts, "FUNDING", -funding, root)   # funding>0=费用，从现金扣

    # 独立核验：全部平仓后，流水权益必须等于现金，且现金变化==净盈亏
    assert abs((ledger.cash - ledger.starting_cash) - net) < D("0.0000001"), \
        f"流水与净盈亏不符: cash={ledger.cash} net={net}"

    return TradeResult(
        signal_bar=int(signal_bar), entry_bar=int(signal_bar + 1), side=side,
        signal_close=D(bars[signal_bar].close), entry_fill=entry, exit_fill=exit_fill,
        qty=pos.qty, gross=gross, entry_fee=pos.entry_fee, exit_fee=exit_fee,
        funding=funding, net_pnl=net, exit_reason=exit_reason,
        holding_ms=max(0, exit_ts - pos.entry_ts_ms), mfe=mfe, mae=mae,
        flows=[{"kind": f.kind, "amount": str(f.amount), "ts_ms": f.ts_ms}
               for f in ledger.flows],
    )


def simulate_trail(bars: list[Candle], signal_bar: int, side: str, notional: D,
                   stop_price: D, *, root: str = "",
                   fee_bps: D = D("4"), slippage_bps: D = D("5"),
                   funding_bps_per_8h: D = D("1"),
                   max_hold_bars: int = 72,
                   trail_atr: D = D("1.0"), atr_abs: D | None = None,
                   trail_start_atr: D = D("1.0")) -> TradeResult | None:
    """移动止损退出：盈利达 trail_start_atr×ATR 后启动，止损跟随最优价 trail_atr×ATR。

    只收紧不放松；跳空按更差开盘价成交；其余（流水/费用/资金费）与 simulate_hold 同口径。
    """
    if atr_abs is None or atr_abs <= 0:
        return simulate_hold(bars, signal_bar, side, notional, stop_price,
                             stop_price, root=root, fee_bps=fee_bps,
                             slippage_bps=slippage_bps,
                             funding_bps_per_8h=funding_bps_per_8h,
                             max_hold_bars=max_hold_bars)
    ledger = CashLedger()
    try:
        # 先用 stop_price 开仓（tp 用不到，传 stop 占位）
        pos, entry_flows = open_at_next_bar(
            bars, signal_bar, side, notional, stop_price, stop_price,
            fee_bps=fee_bps, slippage_bps=slippage_bps, ref=root)
    except ValueError:
        return None
    for f in entry_flows:
        ledger.record(f.ts_ms, f.kind, f.amount, f.ref)

    entry = pos.entry_fill
    direction = D("1") if side == "LONG" else D("-1")
    mfe = mae = D("0")
    stop = D(stop_price)
    best = entry
    armed = False
    exit_fill = exit_reason = ""
    exit_ts = 0
    last_bar = min(len(bars) - 1, signal_bar + max_hold_bars)

    for j in range(signal_bar + 1, last_bar + 1):
        bar = bars[j]
        fav = ((bar.high - entry) if direction > 0 else (entry - bar.low)) * pos.qty
        adv = ((entry - bar.low) if direction > 0 else (bar.high - entry)) * pos.qty
        mfe, mae = max(mfe, fav), max(mae, adv)

        # 先判跳空/止损（保守：同一根内先看止损）
        gap_stop = (bar.open <= stop) if direction > 0 else (bar.open >= stop)
        hit_stop = (bar.low <= stop) if direction > 0 else (bar.high >= stop)
        if gap_stop or hit_stop:
            raw = bar.open if gap_stop else stop
            exit_fill = _fill_price(raw, "SHORT" if direction > 0 else "LONG", slippage_bps)
            exit_reason = "TRAIL_STOP" if armed else "STOP"
            exit_ts = int(bar.open_ms if gap_stop else bar.close_ms)
            break

        # 更新最优价与移动止损（只用本根已收盘信息，下一根生效）
        if direction > 0:
            best = max(best, bar.high)
            if not armed and (best - entry) >= trail_start_atr * atr_abs:
                armed = True
            if armed:
                stop = max(stop, best - trail_atr * atr_abs)
        else:
            best = min(best, bar.low)
            if not armed and (entry - best) >= trail_start_atr * atr_abs:
                armed = True
            if armed:
                stop = min(stop, best + trail_atr * atr_abs)

    if not exit_reason:
        last = bars[last_bar]
        exit_fill = _fill_price(last.close, "SHORT" if direction > 0 else "LONG", slippage_bps)
        exit_reason = "TIME_EXIT"
        exit_ts = int(last.close_ms)

    gross = (exit_fill - entry) * pos.qty * direction
    exit_fee = pos.qty * exit_fill * D(fee_bps) / BPS
    hours = D(max(0, exit_ts - pos.entry_ts_ms)) / D("3_600_000")
    funding = _funding(pos, hours, funding_bps_per_8h)
    net = gross - pos.entry_fee - exit_fee - funding

    ledger.record(exit_ts, "EXIT_PROCEEDS", pos.qty * entry + gross, root)
    ledger.record(exit_ts, "EXIT_FEE", -exit_fee, root)
    ledger.record(exit_ts, "FUNDING", -funding, root)
    assert abs((ledger.cash - ledger.starting_cash) - net) < D("0.0000001"), \
        f"流水与净盈亏不符: cash={ledger.cash} net={net}"

    return TradeResult(
        signal_bar=int(signal_bar), entry_bar=int(signal_bar + 1), side=side,
        signal_close=D(bars[signal_bar].close), entry_fill=entry, exit_fill=exit_fill,
        qty=pos.qty, gross=gross, entry_fee=pos.entry_fee, exit_fee=exit_fee,
        funding=funding, net_pnl=net, exit_reason=exit_reason,
        holding_ms=max(0, exit_ts - pos.entry_ts_ms), mfe=mfe, mae=mae,
        flows=[{"kind": f.kind, "amount": str(f.amount), "ts_ms": f.ts_ms}
               for f in ledger.flows],
    )


__all__ = ["simulate_hold", "simulate_trail"]
