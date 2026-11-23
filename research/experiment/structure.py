"""实验 E2：入场结构对照（周期 × 退出方式），零 API 成本。

核心假设：5m ATR（17–35bps）覆盖不了 23bps 往返成本（占比 65–135%），
所以 5m 入场在结构上就难以盈利。换到 15m/1h，ATR 相对成本更大，
同时把固定止盈换成移动止损，让盈利单跑起来。

铁律：
  - 只在「某周期刚收盘」的位置产生信号，成交在下一根 5m 开盘（无前视）；
  - 同一变体单仓，前一笔未平不接新信号；
  - 研究集（前 70%）选结构，样本外（后 30%）只评估一次；
  - 费用/滑点/资金费/撮合与实验 E 完全一致，唯一变化是结构与退出。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D

from research.market.contracts import Candle

from .ledger import BPS
from .sim import simulate_hold, simulate_trail

TF_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


@dataclass(frozen=True)
class StructVariant:
    tf: str
    exit_mode: str          # fixed | trail
    stop_atr: D = D("1.5")
    tp_atr: D = D("3")
    trail_atr: D = D("1.0")
    trail_start_atr: D = D("1.0")
    gate_mult: D = D("1.0")     # ATR 必须 >= gate_mult × roundtrip_bps
    notional: D = D("100")
    fee_bps: D = D("4")
    slippage_bps: D = D("5")
    hold_tf_bars: int = 24      # 最多持有 N 根「入场周期」K 线

    @property
    def key(self) -> str:
        return f"{self.tf}-{self.exit_mode}"


def _aggregate(bars: list[Candle], minutes: int) -> list[Candle]:
    step = minutes * 60_000
    buckets: dict[int, list[Candle]] = {}
    for c in bars:
        buckets.setdefault(int(c.open_ms // step) * step, []).append(c)
    return [Candle(k, rows[0].open, max(x.high for x in rows), min(x.low for x in rows),
                   rows[-1].close, sum((x.volume for x in rows), D("0")), step)
            for k, rows in sorted(buckets.items())]


def _ema(closes: list[D], period: int = 20) -> list[D]:
    out: list[D] = []
    e = None
    for c in closes:
        e = c if e is None else c * D("2") / D(period + 1) + e * D(period - 1) / D(period + 1)
        out.append(e)
    return out


def _atr(candles: list[Candle], period: int = 14) -> list[D]:
    out: list[D] = [D("0")] * len(candles)
    trs: list[D] = []
    atr = None
    for i, c in enumerate(candles):
        if i == 0:
            tr = c.high - c.low
        else:
            p = candles[i - 1]
            tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        trs.append(tr)
        if len(trs) == period:
            atr = sum(trs) / D(period)
        elif len(trs) > period and atr is not None:
            atr = (atr * D(period - 1) + tr) / D(period)
        out[i] = atr if atr is not None else D("0")
    return out


def run_variant(bars: list[Candle], v: StructVariant) -> dict:
    """跑一个结构变体，返回逐笔结果与研究/样本外统计。"""
    minutes = TF_MINUTES[v.tf]
    k = minutes // 5
    agg = _aggregate(bars, minutes)
    closes = [c.close for c in agg]
    emas = _ema(closes)
    atrs = _atr(agg)
    roundtrip = v.fee_bps * D("2") + v.slippage_bps * D("2")
    hold_bars = v.hold_tf_bars * k

    trades: list[dict] = []
    last_end = -1
    for t in range(21, len(agg) - 1):
        i = (t + 1) * k - 1
        if i + 1 >= len(bars):
            break
        if i <= last_end:
            continue
        prev_c, cur_c = closes[t - 1], closes[t]
        prev_e, cur_e = emas[t - 1], emas[t]
        side = None
        if prev_c <= prev_e and cur_c > cur_e:
            side = "LONG"
        elif prev_c >= prev_e and cur_c < cur_e:
            side = "SHORT"
        if side is None:
            continue
        atr = atrs[t]
        if atr <= 0 or cur_c <= 0:
            continue
        atr_bps = atr / cur_c * BPS
        if atr_bps < roundtrip * v.gate_mult:
            continue
        sign = D("1") if side == "LONG" else D("-1")
        ref = bars[i].close
        stop = ref - sign * v.stop_atr * atr
        tp = ref + sign * v.tp_atr * atr
        if v.exit_mode == "trail":
            r = simulate_trail(bars, i, side, v.notional, stop,
                               fee_bps=v.fee_bps, slippage_bps=v.slippage_bps,
                               max_hold_bars=hold_bars,
                               trail_atr=v.trail_atr, atr_abs=atr,
                               trail_start_atr=v.trail_start_atr)
        else:
            r = simulate_hold(bars, i, side, v.notional, stop, tp,
                              fee_bps=v.fee_bps, slippage_bps=v.slippage_bps,
                              max_hold_bars=hold_bars)
        if r is None:
            continue
        d = r.to_dict()
        d.update({"variant": v.key, "atr_bps": str(atr_bps.quantize(D("0.1"))),
                  "roundtrip_bps": str(roundtrip)})
        trades.append(d)
        last_end = i + int(r.holding_ms // 300_000)

    cut = int(len(bars) * 0.7)
    def stat(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0}
        pnl = sum((D(x["net_pnl"]) for x in rows), D("0"))
        wins = sum(1 for x in rows if D(x["net_pnl"]) > 0)
        fees = sum((D(x["entry_fee"]) + D(x["exit_fee"]) for x in rows), D("0"))
        holds = [int(x["holding_ms"]) for x in rows]
        eq, peak, dd = D("1000"), D("1000"), D("0")
        for x in sorted(rows, key=lambda z: z["signal_bar"]):
            eq += D(x["net_pnl"]); peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak if peak > 0 else D("0"))
        return {"n": len(rows), "net": str(pnl.quantize(D("0.0001"))),
                "avg": str((pnl / len(rows)).quantize(D("0.0001"))),
                "win_pct": str((D(wins) / len(rows) * 100).quantize(D("0.01"))),
                "fees": str(fees.quantize(D("0.0001"))),
                "avg_hold_h": str((D(sum(holds)) / len(holds) / 3_600_000
                                   ).quantize(D("0.01"))),
                "max_dd_pct": str((dd * 100).quantize(D("0.0001"))),
                "exits": _counts(x["exit_reason"] for x in rows)}

    research = [x for x in trades if x["signal_bar"] < cut]
    oos = [x for x in trades if x["signal_bar"] >= cut]
    return {"variant": v.key, "tf": v.tf, "exit_mode": v.exit_mode,
            "config": {"stop_atr": str(v.stop_atr), "tp_atr": str(v.tp_atr),
                       "trail_atr": str(v.trail_atr), "gate_mult": str(v.gate_mult),
                       "hold_tf_bars": v.hold_tf_bars, "roundtrip_bps": str(roundtrip)},
            "all": stat(trades), "research": stat(research), "oos": stat(oos),
            "trades": trades}


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def default_variants() -> list[StructVariant]:
    out = []
    for tf in ("5m", "15m", "1h"):
        out.append(StructVariant(tf=tf, exit_mode="fixed"))
        out.append(StructVariant(tf=tf, exit_mode="trail"))
    return out


__all__ = ["StructVariant", "run_variant", "default_variants", "TF_MINUTES"]
