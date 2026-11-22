"""实验 E 的独立账务与撮合（与冻结的实验 M / PaperBroker 完全隔离）。

两条铁律（P0 验收）：

1. **独立现金流水核验权益**：权益只能由 ``starting_cash + Σ现金流水`` 得出，
   再用「现金 + 未平仓仓位估值」交叉核验。不信任任何 ``positions.pnl`` 汇总字段，
   手续费/滑点/资金费逐笔进流水，防止"手续费明细对不上"。

2. **收盘信号按下一可交易价成交**：信号来自 bar[i] 收盘（已收盘，无前视），
   成交价必须是 bar[i+1] 的**开盘价**（加滑点），绝不能用 bar[i] 自己的收盘价——
   那等于在信号产生的同一瞬间按已知价格成交，是典型前视偏差。

所有金额用 Decimal，禁止 float 参与账务。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal as D
from typing import Iterable

from research.market.contracts import Candle

BPS = D("10000")


@dataclass(frozen=True)
class CashFlow:
    ts_ms: int
    kind: str            # ENTRY_NOTIONAL / ENTRY_FEE / EXIT_PROCEEDS / EXIT_FEE / FUNDING
    amount: D            # 带符号：正=现金流入，负=流出
    ref: str = ""


@dataclass
class CashLedger:
    """独立现金账：权益 = 期初现金 + 所有流水之和。"""
    starting_cash: D = D("1000")
    flows: list[CashFlow] = field(default_factory=list)

    def record(self, ts_ms: int, kind: str, amount: D, ref: str = "") -> None:
        self.flows.append(CashFlow(int(ts_ms), kind, D(amount), ref))

    @property
    def cash(self) -> D:
        return self.starting_cash + sum((f.amount for f in self.flows), D("0"))

    def by_kind(self) -> dict[str, str]:
        out: dict[str, D] = {}
        for f in self.flows:
            out[f.kind] = out.get(f.kind, D("0")) + f.amount
        return {k: str(v) for k, v in sorted(out.items())}

    def to_dict(self) -> dict:
        return {"starting_cash": str(self.starting_cash), "cash": str(self.cash),
                "flows": [{"ts_ms": f.ts_ms, "kind": f.kind,
                           "amount": str(f.amount), "ref": f.ref} for f in self.flows]}


@dataclass
class OpenPosition:
    side: str
    qty: D
    entry_fill: D         # 含滑点的实际成交价
    entry_ts_ms: int
    stop_price: D
    tp_price: D
    entry_fee: D
    signal_bar: int       # 产生信号的 bar 下标（用于审计）


@dataclass
class TradeResult:
    signal_bar: int
    entry_bar: int        # 实际成交的下一根 bar
    side: str
    signal_close: D       # 信号 bar 收盘（仅供参考，绝不作成交价）
    entry_fill: D         # 下一根开盘 + 滑点
    exit_fill: D
    qty: D
    gross: D
    entry_fee: D
    exit_fee: D
    funding: D
    net_pnl: D
    exit_reason: str
    holding_ms: int
    mfe: D
    mae: D
    flows: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: (str(v) if isinstance(v, D) else v) for k, v in self.__dict__.items()}
        return d


def _fill_price(px: D, side: str, slippage_bps: D) -> D:
    """滑点永远朝不利方向：多头买高、空头卖低。"""
    slip = D(slippage_bps) / BPS
    return px * (D("1") + slip if side == "LONG" else D("1") - slip)


def open_at_next_bar(bars: list[Candle], signal_bar: int, side: str,
                     notional: D, stop_price: D, tp_price: D,
                     *, fee_bps: D = D("4"), slippage_bps: D = D("5"),
                     ref: str = "") -> tuple[OpenPosition, list[CashFlow]]:
    """在 bar[signal_bar+1] 开盘成交，返回仓位与本次入场现金流水。

    若信号在最后一根 bar 产生，无下一根可交易 → 抛 ValueError（不伪造成交）。
    """
    if signal_bar < 0 or signal_bar + 1 >= len(bars):
        raise ValueError("没有下一根可交易 K 线，不得按信号 bar 收盘价成交")
    next_bar = bars[signal_bar + 1]
    fill = _fill_price(next_bar.open, side, slippage_bps)
    if fill <= 0:
        raise ValueError("成交价非法")
    qty = D(notional) / fill
    fee = D(notional) * D(fee_bps) / BPS
    pos = OpenPosition(side=side, qty=qty, entry_fill=fill,
                       entry_ts_ms=int(next_bar.open_ms),
                       stop_price=D(stop_price), tp_price=D(tp_price),
                       entry_fee=fee, signal_bar=int(signal_bar))
    flows = [
        CashFlow(int(next_bar.open_ms), "ENTRY_NOTIONAL", -D(notional), ref),
        CashFlow(int(next_bar.open_ms), "ENTRY_FEE", -fee, ref),
    ]
    return pos, flows


def mark_position(pos: OpenPosition, mark: D) -> D:
    """未平仓仓位估值 = 本金 + 浮动盈亏（含方向）。"""
    direction = D("1") if pos.side == "LONG" else D("-1")
    return pos.qty * pos.entry_fill + (D(mark) - pos.entry_fill) * pos.qty * direction


def verify_equity(ledger: CashLedger, positions: Iterable[OpenPosition],
                  mark_prices: dict[str, D]) -> dict:
    """两个**独立口径**交叉核验权益，任何一个对不上都能查出来：

    - 流水口径：``starting_cash + Σ所有现金流水``（已实现权益，浮动盈亏尚未入账）。
    - 估值口径：``现金 + Σ未平仓仓位盯市价值``（含浮动盈亏）。

    平仓后二者必须完全相等（差 0）；有未平仓时，估值口径 − 流水口径 = 未实现浮盈亏。
    另外核对「流水里付出的名义本金」==「仓位本金」，防止手续费明细串账。
    """
    positions = list(positions)
    cash = ledger.cash
    pos_value = sum((mark_position(p, mark_prices.get(p.side, p.entry_fill))
                     for p in positions), D("0"))
    equity_mark = cash + pos_value
    equity_flows = ledger.starting_cash + sum((f.amount for f in ledger.flows), D("0"))
    paid_notional = sum((p.qty * p.entry_fill for p in positions), D("0"))
    ledger_notional_out = -sum((f.amount for f in ledger.flows
                                if f.kind == "ENTRY_NOTIONAL"), D("0"))
    return {
        "cash": str(cash),
        "positions_value": str(pos_value),
        "equity_mark_to_market": str(equity_mark),
        "equity_from_cash_flows": str(equity_flows),
        "unrealized_gap": str(equity_mark - equity_flows),
        "position_notional": str(paid_notional),
        "ledger_notional_out": str(ledger_notional_out),
        "notional_match": abs(paid_notional - ledger_notional_out) < D("0.0000001"),
    }


__all__ = ["CashFlow", "CashLedger", "OpenPosition", "TradeResult",
           "open_at_next_bar", "mark_position", "verify_equity", "BPS"]
