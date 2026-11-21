"""模拟撮合与账本（Sprint 2.1）。

Sprint 2.1 修掉：
  1. ``expire()`` 用当前可用价结算（旧实现用开仓价），无价格时**跳过**而不是猜；
  2. ``realized_pnl_since()`` 使用已扣**双边**手续费的净 pnl；
  3. ``submit()`` 自身拒绝 ``shadow=True``（不依赖编排层）；
  4. 新增 ``mark_bar()``：跳空按开盘价成交（更差），同 bar 止损/止盈同触发按止损；
     止盈跳空不按比止盈价更优的价格成交；
  5. 持仓行记录 ``entry_fee`` 与 ``max_holding_ms``，超时按持仓自己的持有期判断。

铁律不变：订单幂等（intent_id 主键）、Decimal 存库、双向名义记账（不是真实保证金）。
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
    entry_fee TEXT NOT NULL DEFAULT '0',
    max_holding_ms INTEGER NOT NULL DEFAULT 0,
    strategy_id TEXT NOT NULL DEFAULT '',
    funding_paid TEXT NOT NULL DEFAULT '0',
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
            "intent_id": self.intent_id, "ts_ms": self.ts_ms, "symbol": self.symbol,
            "side": self.side, "qty": str(self.qty), "price": str(self.price),
            "fee": str(self.fee), "notional": str(self.notional),
            "strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
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
            "position_id": self.position_id, "symbol": self.symbol, "side": self.side,
            "entry_price": str(self.entry_price), "exit_price": str(self.exit_price),
            "qty": str(self.qty), "pnl": str(self.pnl), "reason": self.reason,
            "exit_ts": self.exit_ts,
        }


class PaperBroker:
    def __init__(self, path: Path | str, starting_cash: Decimal = Decimal("1000"),
                 fee_bps: Decimal = Decimal("5"), slippage_bps: Decimal = Decimal("5"),
                 funding_bps_per_8h: Decimal = Decimal("0"),
                 shadow_mode: str = "reject"):
        """shadow_mode:
        - ``reject``（默认，验证盘）：拒绝 shadow 意图；
        - ``shadow_only``（实验盘）：只接受 shadow 意图；
        - ``allow``：都接受（测试/特殊用途）。
        """
        if shadow_mode not in ("reject", "shadow_only", "allow"):
            raise ValueError(f"shadow_mode 非法: {shadow_mode}")
        self.shadow_mode = shadow_mode
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.funding_bps_per_8h = funding_bps_per_8h
        self.expire_skipped = 0
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.execute(
            "INSERT OR IGNORE INTO account (id, cash, starting_cash) VALUES (1, ?, ?)",
            (str(starting_cash), str(starting_cash)),
        )
        self._db.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(positions)")}
        if "entry_fee" not in cols:
            self._db.execute("ALTER TABLE positions ADD COLUMN entry_fee TEXT NOT NULL DEFAULT '0'")
        if "max_holding_ms" not in cols:
            self._db.execute("ALTER TABLE positions ADD COLUMN max_holding_ms INTEGER NOT NULL DEFAULT 0")
        if "strategy_id" not in cols:
            self._db.execute("ALTER TABLE positions ADD COLUMN strategy_id TEXT NOT NULL DEFAULT ''")
        if "funding_paid" not in cols:
            self._db.execute("ALTER TABLE positions ADD COLUMN funding_paid TEXT NOT NULL DEFAULT '0'")
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
            cash=self.cash(), gross_exposure=self.gross_exposure(),
            open_positions=len(self.open_positions()),
            realized_pnl_today=self.realized_pnl_since(day_start_ms),
        )

    # ---------- 开仓 ----------

    def submit(self, intent: TradeIntent, notional: Decimal) -> Fill | None:
        """幂等开仓。账本类型决定是否接受影子意图（验证盘拒绝，实验盘只收影子）。"""
        if self.shadow_mode == "reject" and intent.shadow:
            return None
        if self.shadow_mode == "shadow_only" and not intent.shadow:
            return None
        fee = notional * self.fee_bps / BPS
        slip = self.slippage_bps / BPS
        price = intent.entry_ref_price * (Decimal("1") + slip if intent.side == "LONG"
                                          else Decimal("1") - slip)
        qty = notional / price
        cur = self._db.execute(
            "INSERT OR IGNORE INTO orders (intent_id, ts_ms, symbol, side, qty, price, fee,"
            " notional, strategy_id, strategy_version, shadow) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (intent.intent_id, intent.decision_ts_ms, intent.symbol, intent.side, str(qty),
             str(price), str(fee), str(notional), intent.strategy_id, intent.strategy_version,
             1 if intent.shadow else 0),
        )
        if cur.rowcount == 0:
            self._db.rollback()
            return None
        cash = self.cash()
        if cash < notional + fee:
            self._db.rollback()
            return None
        self._db.execute("UPDATE account SET cash = ? WHERE id = 1", (str(cash - notional - fee),))
        self._db.execute(
            "INSERT INTO positions (intent_id, symbol, side, qty, entry_price, entry_ts,"
            " stop_price, tp_price, status, entry_fee, max_holding_ms, strategy_id)"
            " VALUES (?,?,?,?,?,?,?,?,'open',?,?,?)",
            (intent.intent_id, intent.symbol, intent.side, str(qty), str(price),
             intent.decision_ts_ms, str(intent.stop_price), str(intent.take_profit_price),
             str(fee), int(intent.expected_holding_ms), intent.strategy_id),
        )
        self._db.commit()
        return Fill(intent.intent_id, intent.decision_ts_ms, intent.symbol, intent.side,
                    qty, price, fee, notional, intent.strategy_id, intent.strategy_version)

    # ---------- 平仓 ----------

    def _close(self, pos: sqlite3.Row, price: Decimal, ts_ms: int, reason: str) -> ClosedPosition:
        qty = Decimal(pos["qty"])
        entry = Decimal(pos["entry_price"])
        entry_fee = Decimal(pos["entry_fee"] or "0")
        notional = qty * entry
        slip = self.slippage_bps / BPS
        fill = price * (Decimal("1") - slip if pos["side"] == "LONG" else Decimal("1") + slip)
        direction = Decimal("1") if pos["side"] == "LONG" else Decimal("-1")
        gross = (fill - entry) * qty * direction
        exit_fee = qty * fill * self.fee_bps / BPS
        # 资金费：按持有小时数折算（多头支付正费率，空头收取）
        hours = Decimal(max(0, ts_ms - int(pos["entry_ts"]))) / Decimal(3_600_000)
        funding = notional * (self.funding_bps_per_8h / BPS) * (hours / Decimal("8"))
        funding_cost = funding if pos["side"] == "LONG" else -funding
        net_pnl = gross - exit_fee - entry_fee - funding_cost
        cash = self.cash() + notional + gross - exit_fee - funding_cost
        self._db.execute("UPDATE account SET cash = ? WHERE id = 1", (str(cash),))
        self._db.execute(
            "UPDATE positions SET status='closed', exit_ts=?, exit_price=?, exit_reason=?, pnl=?,"
            " funding_paid=? WHERE id=?",
            (ts_ms, str(fill), reason, str(net_pnl), str(funding_cost), pos["id"]),
        )
        self._db.commit()
        return ClosedPosition(pos["id"], pos["symbol"], pos["side"], entry, fill, qty,
                              net_pnl, reason, ts_ms)

    def mark_bar(self, symbol: str, bar_open: Decimal, bar_high: Decimal, bar_low: Decimal,
                 ts_ms: int, bar_open_ms: int | None = None) -> list[ClosedPosition]:
        """按一根 K 线的 OHLC 检查止损/止盈（保守成交假设）。

        - ``bar_open_ms`` 给出时：**开仓在本根 K 线内**的持仓跳过本根检查
          （无法知道开仓前后的价格路径，用整根 OHLC 会把开仓前波动算进去），
          下一根起正常判断；
        - 跳空：开盘已越过止损 → 按开盘价成交（更差），不假装在止损价成交；
        - 同 bar 止损与止盈都触发 → 按止损（最坏情形）；
        - 止盈跳空 → 最多按止盈价成交（不取更优价）。
        """
        closed: list[ClosedPosition] = []
        for pos in self.open_positions():
            if pos["symbol"] != symbol:
                continue
            if bar_open_ms is not None and int(pos["entry_ts"]) > bar_open_ms:
                continue                     # 开仓当根：路径未知，跳过
            stop, tp = Decimal(pos["stop_price"]), Decimal(pos["tp_price"])
            if pos["side"] == "LONG":
                if bar_open <= stop:
                    closed.append(self._close(pos, bar_open, ts_ms, "STOP_LOSS"))
                elif bar_low <= stop:
                    closed.append(self._close(pos, stop, ts_ms, "STOP_LOSS"))
                elif bar_open >= tp or bar_high >= tp:
                    closed.append(self._close(pos, tp, ts_ms, "TAKE_PROFIT"))
            else:
                if bar_open >= stop:
                    closed.append(self._close(pos, bar_open, ts_ms, "STOP_LOSS"))
                elif bar_high >= stop:
                    closed.append(self._close(pos, stop, ts_ms, "STOP_LOSS"))
                elif bar_open <= tp or bar_low <= tp:
                    closed.append(self._close(pos, tp, ts_ms, "TAKE_PROFIT"))
        return closed

    def mark(self, symbol: str, price: Decimal, ts_ms: int) -> list[ClosedPosition]:
        """只有收盘价时的兼容入口（信息更少，跳空判断退化）。"""
        return self.mark_bar(symbol, price, price, price, ts_ms)

    def expire(self, ts_ms: int, price_map: dict[str, Decimal]) -> list[ClosedPosition]:
        """持有超时的持仓按**当前可用价**（price_map）平仓；无价格则跳过并计数。"""
        closed: list[ClosedPosition] = []
        for pos in self.open_positions():
            holding = int(pos["max_holding_ms"] or 0)
            if holding <= 0:
                continue
            if ts_ms - int(pos["entry_ts"]) < holding:
                continue
            price = price_map.get(pos["symbol"])
            if price is None:
                self.expire_skipped += 1
                continue
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
        return {"cash": str(self.cash()), "open_positions": len(self.open_positions()),
                "orders": int(self._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0])}

    def close(self) -> None:
        self._db.close()