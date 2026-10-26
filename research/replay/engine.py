"""Deterministic replay of the E desk from recorded decisions.

What this engine does *not* do is re-run today's code over yesterday's data. The
verdicts, scores and prices stored in `candidates` at decision time are the
inputs; recomputing them with current parameters would rewrite history and make
the replay flatter than reality.

What it does do:

* Walk scans in timestamp order.
* Open a paper position when the recorded decision was eligible and the
  position/cooldown/cash gates passed *at that moment*.
* Drive exits from the shadow marks in `candidate_marks`, in order, and never
  from a mark whose timestamp is at or after the simulated clock.
* Charge the same round-trip cost the live desk charges, with a configurable
  bps so cost sensitivity can be measured instead of assumed.

Everything lands in a virtual account. No order is ever sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3

from ..memehunter import entry_slippage_bps

REPLAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms INTEGER NOT NULL,
    label TEXT NOT NULL,
    params TEXT NOT NULL,
    trades INTEGER NOT NULL DEFAULT 0,
    net_usd REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS replay_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    mint TEXT NOT NULL,
    opened_ms INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    qty REAL NOT NULL,
    notional_usd REAL NOT NULL,
    entry_cost_usd REAL NOT NULL,
    exit_ms INTEGER,
    exit_price REAL,
    exit_reason TEXT,
    pnl_usd REAL,
    pnl_pct REAL,
    score REAL,
    entry_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_replay_trades_run ON replay_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_replay_trades_time ON replay_trades(opened_ms);
"""

DEFAULT_PARAMS: dict = {
    "starting_cash_usdt": 1000.0,
    "notional_usdt": 100.0,
    "max_open_positions": 5,
    "cooldown_minutes": 45,
    "round_trip_cost_bps": 120.0,
    "stop_loss_pct": -12.0,
    "take_profit_pct": 55.0,
    "timeout_minutes": 180,
    "trailing_giveback_pct": 30.0,
    # Only candidates whose recorded verdict was eligible may be replayed.
    "min_recorded_score": 45.0,
    # Shadow marks older than this are too stale to price an exit with.
    "max_mark_age_minutes": 10,
}


@dataclass
class ReplayTrade:
    symbol: str
    mint: str
    opened_ms: int
    entry_price: float
    qty: float
    notional_usd: float
    entry_cost_usd: float
    score: float
    entry_reason: str
    exit_ms: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_usd: float | None = None
    pnl_pct: float | None = None

    @property
    def closed(self) -> bool:
        return self.exit_ms is not None


@dataclass
class ReplayResult:
    label: str
    params: dict
    trades: list[ReplayTrade] = field(default_factory=list)
    started_ms: int = 0
    finished_ms: int = 0
    notes: list[str] = field(default_factory=list)

    # ---------- metrics ----------
    @property
    def closed(self) -> list[ReplayTrade]:
        return [t for t in self.trades if t.closed]

    @property
    def wins(self) -> list[ReplayTrade]:
        return [t for t in self.closed if (t.pnl_usd or 0.0) > 0]

    @property
    def net_usd(self) -> float:
        return round(sum((t.pnl_usd or 0.0) for t in self.closed), 4)

    @property
    def win_rate(self) -> float | None:
        if not self.closed:
            return None
        return round(len(self.wins) / len(self.closed) * 100.0, 2)

    @property
    def expectancy_usd(self) -> float | None:
        if not self.closed:
            return None
        return round(self.net_usd / len(self.closed), 4)

    @property
    def profit_factor(self) -> float | None:
        gains = sum((t.pnl_usd or 0.0) for t in self.closed if (t.pnl_usd or 0.0) > 0)
        losses = -sum((t.pnl_usd or 0.0) for t in self.closed if (t.pnl_usd or 0.0) < 0)
        if losses <= 0:
            return None if gains <= 0 else float("inf")
        return round(gains / losses, 3)

    @property
    def timeout_share(self) -> float | None:
        if not self.closed:
            return None
        n = sum(1 for t in self.closed if t.exit_reason == "TIMEOUT")
        return round(n / len(self.closed) * 100.0, 2)

    @property
    def avg_hold_minutes(self) -> float | None:
        if not self.closed:
            return None
        return round(
            sum((t.exit_ms or 0) - t.opened_ms for t in self.closed) / len(self.closed) / 60_000.0, 1
        )

    def equity_curve(self) -> list[tuple[int, float]]:
        """(exit_ms, equity) after each close, in order. Open trades excluded."""
        curve: list[tuple[int, float]] = []
        equity = float(self.params["starting_cash_usdt"])
        for trade in sorted(self.closed, key=lambda t: t.exit_ms or 0):
            equity += trade.pnl_usd or 0.0
            curve.append((int(trade.exit_ms or 0), round(equity, 4)))
        return curve

    def max_drawdown_pct(self) -> float | None:
        curve = self.equity_curve()
        if len(curve) < 2:
            return None
        peak = curve[0][1]
        worst = 0.0
        for _, value in curve:
            peak = max(peak, value)
            if peak > 0:
                worst = min(worst, (value / peak - 1.0) * 100.0)
        return round(worst, 3)


class ReplayEngine:
    """Replays recorded decisions against shadow marks. Read-only on the source."""

    def __init__(self, root: Path, params: dict | None = None, db_name: str = "memehunter.sqlite",
                 db_path: Path | None = None):
        self.root = root
        self.params = {**DEFAULT_PARAMS, **(params or {})}
        self.db_path = Path(db_path) if db_path else root / "runtime" / "memehunter" / db_name
        # as_uri() percent-encodes the path (Chinese usernames, spaces) which a
        # hand-built "file:..." string does not.
        self.conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    # ---------- data ----------
    def _scans(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute("SELECT id, scanned_at_ms FROM scans ORDER BY scanned_at_ms")
        )

    def _candidates(self, scan_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM candidates WHERE scan_id = ? ORDER BY score DESC", (scan_id,)
            )
        )

    def _marks_between(self, candidate_id: int, from_ms: int, until_ms: int) -> list[sqlite3.Row]:
        """Marks in (from_ms, until_ms] -- the upper bound is the hard guarantee
        that no future price ever reaches a decision made at until_ms."""
        return list(
            self.conn.execute(
                "SELECT marked_at_ms, price_usd FROM candidate_marks"
                " WHERE candidate_id = ? AND marked_at_ms >= ? AND marked_at_ms <= ?"
                " ORDER BY marked_at_ms",
                (candidate_id, from_ms, until_ms),
            )
        )

    # ---------- gates (evaluated at the simulated clock, never later) ----------
    def _can_open(self, symbol_mint: str, at_ms: int, open_mints: list[str], cash: float) -> tuple[bool, str]:
        if len(open_mints) >= int(self.params["max_open_positions"]):
            return False, "MAX_POSITIONS"
        if symbol_mint in open_mints:
            return False, "ALREADY_OPEN"
        row = self.conn.execute(
            "SELECT MAX(opened_at_ms) AS ts FROM positions WHERE mint = ?", (symbol_mint,)
        ).fetchone()
        # The live desk's own cooldown history is part of the recorded state.
        if row and row["ts"] and at_ms - int(row["ts"]) < int(self.params["cooldown_minutes"]) * 60_000:
            return False, "COOLDOWN"
        if cash < float(self.params["notional_usdt"]):
            return False, "INSUFFICIENT_CASH"
        return True, ""

    # ---------- the replay ----------
    def run(self, label: str = "replay") -> ReplayResult:
        started = self._now_ms()
        result = ReplayResult(label=label, params=dict(self.params), started_ms=started)
        cost_bps = float(self.params["round_trip_cost_bps"])
        notional = float(self.params["notional_usdt"])
        stop = float(self.params["stop_loss_pct"])
        take = float(self.params["take_profit_pct"])
        timeout_ms = int(self.params["timeout_minutes"]) * 60_000
        giveback = float(self.params["trailing_giveback_pct"])
        min_score = float(self.params["min_recorded_score"])
        max_mark_age = int(self.params["max_mark_age_minutes"]) * 60_000

        cash = float(self.params["starting_cash_usdt"])
        open_trades: list[ReplayTrade] = []
        last_open_ms: dict[str, int] = {}

        for scan in self._scans():
            scan_ms = int(scan["scanned_at_ms"])

            # 1. exits first: mark every open trade with any shadow price that
            #    existed at or before this scan's clock.
            for trade in list(open_trades):
                marks = self._marks_between(self._candidate_id(trade.mint), scan_ms - max_mark_age, scan_ms)
                if not marks:
                    continue  # no fresh-enough price: the position stays open
                price = float(marks[-1]["price_usd"])
                change = (price / trade.entry_price - 1.0) * 100.0
                peak = max(trade.entry_price, price)
                reason = None
                if change >= take:
                    reason = "TAKE_PROFIT"
                elif change <= stop:
                    reason = "STOP_LOSS"
                elif scan_ms - trade.opened_ms >= timeout_ms:
                    reason = "TIMEOUT"
                elif peak > trade.entry_price:
                    fall = (peak - price) / peak * 100.0
                    if fall >= giveback and change > 0:
                        reason = "TRAILING_STOP"
                if reason:
                    self._close(trade, price, reason, scan_ms, cost_bps)
                    cash += trade.notional_usd + (trade.pnl_usd or 0.0)
                    open_trades.remove(trade)

            # 2. entries: recorded eligible candidates, best score first.
            for row in self._candidates(int(scan["id"])):
                if not row["eligible"] or float(row["score"] or 0.0) < min_score:
                    continue
                mint = row["mint"]
                ok, _why = self._can_open(mint, scan_ms, [t.mint for t in open_trades], cash)
                if not ok:
                    continue
                last = last_open_ms.get(mint)
                if last and scan_ms - last < int(self.params["cooldown_minutes"]) * 60_000:
                    continue
                price = float(row["price_usd"] or 0.0)
                if price <= 0:
                    continue
                slip = entry_slippage_bps(notional, float(row["liquidity_usd"] or 0.0))
                fill = price * (1.0 + slip / 10_000.0)
                qty = notional / fill
                cost = notional * (cost_bps / 2.0) / 10_000.0
                trade = ReplayTrade(
                    symbol=row["symbol"], mint=mint, opened_ms=scan_ms,
                    entry_price=fill, qty=qty, notional_usd=notional, entry_cost_usd=cost,
                    score=float(row["score"] or 0.0),
                    entry_reason=f"REPLAY·recorded_score={float(row['score'] or 0.0):.1f}",
                )
                open_trades.append(trade)
                result.trades.append(trade)
                last_open_ms[mint] = scan_ms
                cash -= notional + cost
                if len(open_trades) >= int(self.params["max_open_positions"]):
                    break

        # 3. anything still open at the end of the data is reported, not closed.
        if open_trades:
            result.notes.append(f"{len(open_trades)} 个仓位在数据末尾仍为持仓状态，未计入平仓统计")
        result.finished_ms = self._now_ms()
        return result

    def _candidate_id(self, mint: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM candidates WHERE mint = ? ORDER BY scanned_at_ms DESC LIMIT 1", (mint,)
        ).fetchone()
        return int(row["id"]) if row else 0

    def _close(self, trade: ReplayTrade, price: float, reason: str, at_ms: int, cost_bps: float) -> None:
        exit_cost = trade.notional_usd * (cost_bps / 2.0) / 10_000.0
        proceeds = price * trade.qty - exit_cost
        pnl = proceeds - trade.notional_usd - trade.entry_cost_usd
        trade.exit_ms = at_ms
        trade.exit_price = price
        trade.exit_reason = reason
        trade.pnl_usd = round(pnl, 4)
        trade.pnl_pct = round(pnl / trade.notional_usd * 100.0, 3)

    @staticmethod
    def _now_ms() -> int:
        from datetime import datetime, timezone

        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
