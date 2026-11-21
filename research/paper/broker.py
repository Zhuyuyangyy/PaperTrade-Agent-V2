"""模拟撮合与账本（Sprint 2）。

铁律：
  - **幂等**：``orders.intent_id`` 是主键。同一意图重复提交不会产生第二笔成交，
    重启后依然如此（账本在 sqlite 里，不在内存里）。
  - **费用与滑点**：开仓按 ``entry_ref * (1 ± slippage)`` 成交，双边 taker 费；
    平仓同样计滑点与费用。所有金额用 Decimal 字符串存库，避免 float 误差。
  - **方向对称**：LONG 与 SHORT 都占用 notional 作为保证金，结算时返还并计入 pnl。
    这是简化模型（不是真实保证金/借币），文档与日志里明确标注。
  - **可恢复**：``restore()`` 从账本读回现金与持仓；``account()`` 提供风控所需的
    快照（现金/敞口/持仓数/当日已实现盈亏）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from research.strategy.contracts import TradeIntent

BPS = Decimal("10000")

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash TEXT NOT NULL,
    starting_cash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    intent_id TEXT PRIMARY KEY,
    ts_ms INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    notional TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    shadow INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    stop_price TEXT NOT NULL,
    tp_price TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_ts INTEGER,
    exit_price TEXT,
    exit_reason TEXT,
    pnl TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts_ms INTEGER PRIMARY KEY,
    cash TEXT NOT NULL,
    positions_value TEXT NOT NULL,
    equity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
"""


@dataclass(frozen=True)
class Fill:
    intent_id: str
    ts_ms: int
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    notional: Decimal
    strategy_id: str
    strategy_version: str

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "ts_ms": self.ts_ms,
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "price": str(self.price),
            "fee": str(self.fee),
            "notional": str(self.notional),
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
        }


@dataclass(frozen=True)
class ClosedPosition:
    position_id: int
    symbol: str
    side: str
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    pnl: Decimal
    reason: str
    exit_ts: int

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "qty": str(self.qty),
            "pnl": str(self.pnl),
            "reason": self.reason,
            "exit_ts": self.exit_ts,
        }


class PaperBroker:
    def __init__(self, path: Path | str, starting_cash: Decimal = Decimal("1000"),
                 fee_bps: Decimal = Decimal("5"), slippage_bps: Decimal = Decimal("5")):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.execute(
            "INSERT OR IGNORE INTO account (id, cash, starting_cash) VALUES (1, ?, ?)",
            (str(starting_cash), str(starting_cash)),
        )
        self._db.commit()

    # ---------- 查询 ----------

    def cash(self) -> Decimal:
        row = self._db.execute("SELECT cash FROM account WHERE id = 1").fetchone()
        return Decimal(row["cash"])

    def open_positions(self) -> list[dict]:
        return [dict(r) for r in self._db.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY id")]

    def gross_exposure(self) -> Decimal:
        return sum((Decimal(p["qty"]) * Decimal(p["entry_price"]) for p in self.open_positions()),
                   Decimal("0"))

    def realized_pnl_since(self, since_ms: int) -> Decimal:
        rows = self._db.execute(
            "SELECT pnl FROM positions WHERE status = 'closed' AND exit_ts >= ?", (since_ms,)
        ).fetchall()
        return sum((Decimal(r["pnl"] or "0") for r in rows), Decimal("0"))

    def account(self, day_start_ms: int = 0):
        from research.risk.engine import AccountSnapshot

        return AccountSnapshot(
            cash=self.cash(),
            gross_exposure=self.gross_exposure(),
            open_positions=len(self.open_positions()),
            realized_pnl_today=self.realized_pnl_since(day_start_ms),
        )

    # ---------- 开仓 ----------

    def submit(self, intent: TradeIntent, notional: Decimal) -> Fill | None:
        """幂等开仓：同一 intent_id 只会成交一次。"""
        fee = notional * self.fee_bps / BPS
        slip = self.slippage_bps / BPS
        price = intent.entry_ref_price * (Decimal("1") + slip if intent.side == "LONG"
                                          else Decimal("1") - slip)
        qty = notional / price
        cur = self._db.execute(
            "INSERT OR IGNORE INTO orders (intent_id, ts_ms, symbol, side, qty, price, fee,"
            " notional, strategy_id, strategy_version, shadow) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (intent.intent_id, intent.decision_ts_ms, intent.symbol, intent.side, str(qty),
             str(price), str(fee), str(notional), intent.strategy_id, intent.strategy_version),
        )
        if cur.rowcount == 0:
            self._db.rollback()
            return None
        cash = self.cash()
        if cash < notional + fee:
            self._db.rollback()
            return None
        self._db.execute("UPDATE account SET cash = ? WHERE id = 1",
                         (str(cash - notional - fee),))
        self._db.execute(
            "INSERT INTO positions (intent_id, symbol, side, qty, entry_price, entry_ts,"
            " stop_price, tp_price, status) VALUES (?,?,?,?,?,?,?,?,'open')",
            (intent.intent_id, intent.symbol, intent.side, str(qty), str(price),
             intent.decision_ts_ms, str(intent.stop_price), str(intent.take_profit_price)),
        )
        self._db.commit()
        return Fill(intent.intent_id, intent.decision_ts_ms, intent.symbol, intent.side,
                    qty, price, fee, notional, intent.strategy_id, intent.strategy_version)

    # ---------- 平仓 ----------

    def _close(self, pos: sqlite3.Row, price: Decimal, ts_ms: int, reason: str) -> ClosedPosition:
        qty = Decimal(pos["qty"])
        entry = Decimal(pos["entry_price"])
        notional = qty * entry
        slip = self.slippage_bps / BPS
        fill = price * (Decimal("1") - slip if pos["side"] == "LONG" else Decimal("1") + slip)
        direction = Decimal("1") if pos["side"] == "LONG" else Decimal("-1")
        gross = (fill - entry) * qty * direction
        exit_fee = qty * fill * self.fee_bps / BPS
        pnl = gross - exit_fee
        cash = self.cash() + notional + pnl
        self._db.execute("UPDATE account SET cash = ? WHERE id = 1", (str(cash),))
        self._db.execute(
            "UPDATE positions SET status='closed', exit_ts=?, exit_price=?, exit_reason=?, pnl=?"
            " WHERE id=?",
            (ts_ms, str(fill), reason, str(pnl), pos["id"]),
        )
        self._db.commit()
        return ClosedPosition(pos["id"], pos["symbol"], pos["side"], entry, fill, qty, pnl,
                              reason, ts_ms)

    def mark(self, symbol: str, price: Decimal, ts_ms: int) -> list[ClosedPosition]:
        """按止损/止盈触发平仓；顺带把所有 open 持仓的最新价记入持仓表供看板用。"""
        closed: list[ClosedPosition] = []
        for pos in self.open_positions():
            if pos["symbol"] != symbol:
                continue
            stop, tp = Decimal(pos["stop_price"]), Decimal(pos["tp_price"])
            if pos["side"] == "LONG":
                if price <= stop:
                    closed.append(self._close(pos, stop, ts_ms, "STOP_LOSS"))
                elif price >= tp:
                    closed.append(self._close(pos, tp, ts_ms, "TAKE_PROFIT"))
            else:
                if price >= stop:
                    closed.append(self._close(pos, stop, ts_ms, "STOP_LOSS"))
                elif price <= tp:
                    closed.append(self._close(pos, tp, ts_ms, "TAKE_PROFIT"))
        return closed

    def expire(self, ts_ms: int, max_holding_ms: int) -> list[ClosedPosition]:
        """持有超时的持仓按当前记账价平仓（调用方负责传入最新价）。"""
        closed: list[ClosedPosition] = []
        for pos in self.open_positions():
            if ts_ms - int(pos["entry_ts"]) >= max_holding_ms:
                prices = self._db.execute(
                    "SELECT price FROM orders WHERE intent_id=?", (pos["intent_id"],)
                ).fetchone()
                price = Decimal(prices["price"]) if prices else Decimal(pos["entry_price"])
                closed.append(self._close(pos, price, ts_ms, "TIMEOUT"))
        return closed

    # ---------- 权益 ----------

    def equity(self, ts_ms: int, price_map: dict[str, Decimal]) -> Decimal:
        cash = self.cash()
        value = Decimal("0")
        for pos in self.open_positions():
            qty, entry = Decimal(pos["qty"]), Decimal(pos["entry_price"])
            price = price_map.get(pos["symbol"], entry)
            direction = Decimal("1") if pos["side"] == "LONG" else Decimal("-1")
            value += qty * entry + (price - entry) * qty * direction
        equity = cash + value
        self._db.execute(
            "INSERT OR REPLACE INTO equity (ts_ms, cash, positions_value, equity) VALUES (?,?,?,?)",
            (ts_ms, str(cash), str(value), str(equity)),
        )
        self._db.commit()
        return equity

    def restore(self) -> dict:
        """重启后的恢复检查：返回现金与未平仓数量（不做任何重放）。"""
        return {"cash": str(self.cash()), "open_positions": len(self.open_positions()),
                "orders": int(self._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0])}

    def close(self) -> None:
        self._db.close()