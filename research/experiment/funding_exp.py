"""实验 E4 回测：资金费率极值信号 + 真实 carry 结算。

与 E2/E3 的区别：
  - 信号来自资金费率（持仓成本），不是价格形态；
  - **carry 用真实费率逐次结算**（正费率多头付、空头收），不用固定 1bps——
    否则等于把这套策略的经济来源抹掉；
  - 数据 2 年（17,521 根 1h、2,190 条费率），样本量比 E2 的 19 笔高一个数量级。

防前视：
  - 信号在结算时刻 ts 可用；成交在**开盘时间严格晚于 ts** 的第一根 K 线开盘；
  - z 值用滚动窗口（不含当次），不使用全样本统计量。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal as D

from research.market.contracts import Candle

from .funding import FundingSignal, funding_signals
from .ledger import BPS, CashLedger, TradeResult, _fill_price

TF_MS = 3_600_000


@dataclass(frozen=True)
class FundingParams:
    z_threshold: D = D("2.0")
    lookback: int = 90
    stop_atr: D = D("2.0")
    trail_atr: D = D("1.0")
    trail_start_atr: D = D("1.0")
    hold_bars: int = 72
    #: True = 顺势（高费率做多）；False = 拥挤反向（高费率做空）
    flip: bool = False
    #: level = 费率水平；delta = 费率变化率（拥挤加速度）
    mode: str = "level"
    notional: D = D("100")
    fee_bps: D = D("4")
    slippage_bps: D = D("5")

    @property
    def key(self) -> str:
        return (f"{self.mode}-z{self.z_threshold}-stop{self.stop_atr}"
                f"-trail{self.trail_atr}-h{self.hold_bars}"
                f"{'-flip' if self.flip else ''}")


def _atr14(bars: list[Candle], i: int, period: int = 14) -> D:
    if i < period:
        return D("0")
    trs = []
    for k in range(i - period + 1, i + 1):
        c, p = bars[k], bars[k - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return sum(trs, D("0")) / D(period)


def _index_at(bars: list[Candle], ts: int) -> int:
    """最后一根 open_ms <= ts 的下标；成交用它的下一根（严格晚于 ts）。"""
    lo, hi = 0, len(bars) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if int(bars[mid].open_ms) <= ts:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _carry(side: str, qty: D, base: D, f_times: list[int], f_rates: list[D],
           t0: int, t1: int) -> tuple[D, int]:
    """持有期 [t0, t1] 内逐次结算的真实资金费（正数=成本，负数=收入）。"""
    i = bisect_right(f_times, t0)
    total = D("0")
    n = 0
    while i < len(f_times) and f_times[i] <= t1:
        amt = qty * base * f_rates[i]
        total += amt if side == "LONG" else -amt
        n += 1
        i += 1
    return total, n


def run_funding_variant(bars: list[Candle], funding: list[dict],
                        p: FundingParams,
                        *, single_position: bool = True) -> dict:
    """跑一组参数，返回逐笔 + 研究/样本外统计。"""
    sigs: list[FundingSignal] = funding_signals(
        funding, bars, lookback=p.lookback, z_threshold=p.z_threshold,
        mode=p.mode)
    f_times = [int(x["funding_ms"]) for x in funding]
    f_rates = [D(x["rate"]) for x in funding]

    trades: list[dict] = []
    busy_until = -1
    for s in sigs:
        idx = _index_at(bars, s.ts_ms)
        if idx < 20 or idx + 1 >= len(bars):
            continue
        if single_position and idx + 1 <= busy_until:
            continue
        atr = _atr14(bars, idx)
        if atr <= 0:
            continue
        entry_bar = bars[idx + 1]
        side = s.side
        if p.flip:                       # 反转方向假设
            side = "LONG" if side == "SHORT" else "SHORT"
        sign = D("1") if side == "LONG" else D("-1")
        ref = entry_bar.open
        stop = ref - sign * p.stop_atr * atr
        entry_fill = _fill_price(ref, side, p.slippage_bps)
        qty = p.notional / entry_fill

        stop_cur = stop
        best = entry_fill
        armed = False
        exit_fill = exit_reason = ""
        exit_ts = 0
        last = min(len(bars) - 1, idx + 1 + p.hold_bars)
        for j in range(idx + 1, last + 1):
            bar = bars[j]
            gap_stop = (bar.open <= stop_cur) if sign > 0 else (bar.open >= stop_cur)
            hit_stop = (bar.low <= stop_cur) if sign > 0 else (bar.high >= stop_cur)
            if gap_stop or hit_stop:
                raw = bar.open if gap_stop else stop_cur
                exit_fill = _fill_price(raw, "SHORT" if sign > 0 else "LONG", p.slippage_bps)
                exit_reason = "TRAIL_STOP" if armed else "STOP"
                exit_ts = int(bar.open_ms if gap_stop else bar.close_ms)
                break
            if sign > 0:
                best = max(best, bar.high)
                if not armed and (best - entry_fill) >= p.trail_start_atr * atr:
                    armed = True
                if armed:
                    stop_cur = max(stop_cur, best - p.trail_atr * atr)
            else:
                best = min(best, bar.low)
                if not armed and (entry_fill - best) >= p.trail_start_atr * atr:
                    armed = True
                if armed:
                    stop_cur = min(stop_cur, best + p.trail_atr * atr)
        if not exit_reason:
            b = bars[last]
            exit_fill = _fill_price(b.close, "SHORT" if sign > 0 else "LONG", p.slippage_bps)
            exit_reason = "TIME_EXIT"
            exit_ts = int(b.close_ms)

        gross = (exit_fill - entry_fill) * qty * sign
        entry_fee = p.notional * p.fee_bps / BPS
        exit_fee = qty * exit_fill * p.fee_bps / BPS
        carry, n_carry = _carry(side, qty, entry_fill, f_times, f_rates,
                                int(entry_bar.open_ms), exit_ts)
        net = gross - entry_fee - exit_fee - carry

        ledger = CashLedger()
        ledger.record(int(entry_bar.open_ms), "ENTRY_NOTIONAL", -p.notional)
        ledger.record(int(entry_bar.open_ms), "ENTRY_FEE", -entry_fee)
        ledger.record(exit_ts, "EXIT_PROCEEDS", qty * entry_fill + gross)
        ledger.record(exit_ts, "EXIT_FEE", -exit_fee)
        ledger.record(exit_ts, "FUNDING", -carry)
        assert abs((ledger.cash - ledger.starting_cash) - net) < D("0.0000001"), \
            f"流水与净盈亏不符: {ledger.cash} vs {net}"

        r = TradeResult(
            signal_bar=idx, entry_bar=idx + 1, side=side, signal_close=D(bars[idx].close),
            entry_fill=entry_fill, exit_fill=exit_fill, qty=qty, gross=gross,
            entry_fee=entry_fee, exit_fee=exit_fee, funding=carry, net_pnl=net,
            exit_reason=exit_reason, holding_ms=max(0, exit_ts - int(entry_bar.open_ms)),
            mfe=D("0"), mae=D("0"),
            flows=[{"kind": f.kind, "amount": str(f.amount)} for f in ledger.flows])
        d = r.to_dict()
        d.update({"z": str(s.z), "rate": str(s.rate), "carry_events": n_carry,
                  "atr_bps": str((atr / ref * BPS).quantize(D("0.1")))})
        trades.append(d)
        if single_position:
            busy_until = idx + 1 + int(r.holding_ms // TF_MS)

    cut = int(len(bars) * 0.7)
    def stat(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0}
        pnl = sum((D(x["net_pnl"]) for x in rows), D("0"))
        carry = sum((D(x["funding"]) for x in rows), D("0"))
        wins = sum(1 for x in rows if D(x["net_pnl"]) > 0)
        eq, peak, dd = D("1000"), D("1000"), D("0")
        for x in sorted(rows, key=lambda z: z["signal_bar"]):
            eq += D(x["net_pnl"]); peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak if peak > 0 else D("0"))
        holds = [int(x["holding_ms"]) for x in rows]
        return {"n": len(rows), "net": str(pnl.quantize(D("0.0001"))),
                "avg": str((pnl / len(rows)).quantize(D("0.0001"))),
                "carry": str(carry.quantize(D("0.0001"))),
                "carry_share_pct": str((carry / abs(pnl) * 100).quantize(D("0.1")))
                                   if pnl != 0 else "0",
                "win_pct": str((D(wins) / len(rows) * 100).quantize(D("0.01"))),
                "avg_hold_h": str((D(sum(holds)) / len(holds) / 3_600_000
                                   ).quantize(D("0.01"))),
                "max_dd_pct": str((dd * 100).quantize(D("0.0001"))),
                "exits": _counts(x["exit_reason"] for x in rows)}

    research = [x for x in trades if x["signal_bar"] < cut]
    oos = [x for x in trades if x["signal_bar"] >= cut]
    return {"key": p.key, "params": {"z": str(p.z_threshold), "lookback": p.lookback,
                                     "stop_atr": str(p.stop_atr), "trail_atr": str(p.trail_atr),
                                     "trail_start_atr": str(p.trail_start_atr),
                                     "hold_bars": p.hold_bars},
            "signals": len(sigs), "all": stat(trades),
            "research": stat(research), "oos": stat(oos), "trades": trades}


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def grid(params_cls=FundingParams) -> list[FundingParams]:
    """2 模式 × 2 z × 3 stop × 2 trail × 2 hold × 2 方向 = 96 组全因子。

    方向（拥挤反向 / 顺势）必须进网格：只测一种方向等于预设结论，
    而置换检验会按网格大小自动校正多重比较。
    """
    out = []
    for mode in ("level", "delta"):
        for z in ("2.0", "2.5"):
            for stop in ("1.5", "2.0", "2.5"):
                for trail in ("0.75", "1.0"):
                    for hold in (24, 72):
                        for flip in (False, True):
                            out.append(params_cls(z_threshold=D(z), stop_atr=D(stop),
                                                  trail_atr=D(trail), hold_bars=hold,
                                                  flip=flip, mode=mode))
    return out


__all__ = ["FundingParams", "run_funding_variant", "grid"]
