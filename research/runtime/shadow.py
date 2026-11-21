"""影子信号追踪（Sprint 4.1）：影子意图之后市场怎么走。

影子策略不进账本，但不能"只说不看"。每条影子意图都要能被事后评估：

- 结算价：决策时刻之后第 1/4/24 小时最近的一根已收盘 K 线；
- 止损/止盈优先：如果窗口内先触及止损（保守：同根双触发按止损），按止损算；
- 收益按方向计（SHORT 价格下跌为正）；
- 缺数据就返回 None，不猜。

输出是研究记录，不是交易指令。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from research.market.contracts import Candle

HOUR = 3_600_000
DEFAULT_HORIZONS_H = (1, 4, 24)


@dataclass(frozen=True)
class ShadowSignal:
    intent_id: str
    ts_ms: int
    side: str
    strategy_id: str
    entry: Decimal
    stop: Decimal
    tp: Decimal

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1


def load_shadow_signals(log_path: Path | str) -> list[ShadowSignal]:
    p = Path(log_path)
    if not p.is_file():
        return []
    out: list[ShadowSignal] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        sel = entry.get("selection") or {}
        intent = sel.get("intent") or {}
        if not intent or not intent.get("shadow"):
            continue
        try:
            out.append(ShadowSignal(
                intent_id=str(intent["intent_id"]),
                ts_ms=int(intent["decision_ts_ms"]),
                side=str(intent["side"]),
                strategy_id=str(intent["strategy_id"]),
                entry=Decimal(str(intent["entry_ref_price"])),
                stop=Decimal(str(intent["stop_price"])),
                tp=Decimal(str(intent["take_profit_price"])),
            ))
        except (KeyError, ArithmeticError):
            continue
    return out


def evaluate_signal(sig: ShadowSignal, candles: list[Candle],
                    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H) -> dict:
    out: dict = {"intent_id": sig.intent_id, "ts_ms": sig.ts_ms, "side": sig.side,
                 "strategy_id": sig.strategy_id, "entry": str(sig.entry), "horizons": {}}
    after = sorted((c for c in candles if c.close_ms > sig.ts_ms), key=lambda c: c.open_ms)
    for h in horizons_h:
        target = sig.ts_ms + h * HOUR
        window = [c for c in after if c.open_ms < target]
        settle = next((c for c in after if c.close_ms >= target), None)
        outcome = _outcome(sig, window, settle)
        out["horizons"][f"{h}h"] = outcome
    return out


def _outcome(sig: ShadowSignal, window: list[Candle], settle: Candle | None) -> dict | None:
    d = sig.direction
    for c in window:
        if d > 0:
            if c.low <= sig.stop:
                return _ret(sig, sig.stop, "STOP", c.open_ms)
            if c.high >= sig.tp:
                return _ret(sig, sig.tp, "TAKE_PROFIT", c.open_ms)
        else:
            if c.high >= sig.stop:
                return _ret(sig, sig.stop, "STOP", c.open_ms)
            if c.low <= sig.tp:
                return _ret(sig, sig.tp, "TAKE_PROFIT", c.open_ms)
    if settle is None or settle.close <= 0:
        return None
    return _ret(sig, settle.close, "HORIZON", settle.close_ms)


def _ret(sig: ShadowSignal, price: Decimal, reason: str, ts_ms: int) -> dict:
    bps = (price / sig.entry - Decimal("1")) * Decimal("10000") * sig.direction
    return {"exit_price": str(price), "return_bps": str(bps.quantize(Decimal("0.01"))),
            "reason": reason, "at_ms": ts_ms}


def report(signals: list[ShadowSignal], candles: list[Candle],
           horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H) -> dict:
    rows = [evaluate_signal(s, candles, horizons_h) for s in signals]
    summary: dict = {"n": len(rows), "horizons": {}}
    for h in horizons_h:
        key = f"{h}h"
        vals = [Decimal(r["horizons"][key]["return_bps"]) for r in rows
                if r["horizons"].get(key)]
        wins = sum(1 for v in vals if v > 0)
        summary["horizons"][key] = {
            "n": len(vals),
            "mean_bps": str((sum(vals, Decimal("0")) / len(vals)).quantize(Decimal("0.01")))
            if vals else None,
            "win_rate": str((Decimal(wins) / Decimal(len(vals))).quantize(Decimal("0.001")))
            if vals else None,
        }
    return {"summary": summary, "signals": rows}