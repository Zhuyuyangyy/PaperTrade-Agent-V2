"""The C account, rebuilt as a fair control group.

The old C was a freqtrade dry-run on BTC/ETH with an EMA strategy that never
filled a single order in 48 hours -- it carried no information. This version
runs the *same* candidate pool the E desk sees, at the *same* moment, with the
simplest possible rule:

    rank by organic_score, take the top N, buy.

No survival veto, no score floor tuned for this asset class, no smart-money
boost, no cooldown. Whatever gap remains between C and E is therefore the
value the six-agent pipeline adds -- or proof that it adds none. Both readings
are useful; a dead account is not.

The ledger is its own SQLite file so the two desks can never share a position.
"""
from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from .memehunter import Desk, Token, entry_slippage_bps, now_ms, score

DB_NAME = "counterpart.sqlite"
DEFAULT_CONFIG: dict = {
    "starting_cash_usdt": 1000.0,
    "notional_usdt": 100.0,
    "max_open_positions": 5,
    "cooldown_minutes": 45,
    "round_trip_cost_bps": 120.0,
    "take_profit_pct": 55.0,
    "stop_loss_pct": -22.0,
    "timeout_minutes": 180,
    "trailing_giveback_pct": 30.0,
    # The whole point of the control: rank by organic score only.
    "top_n": 3,
    "min_liquidity_usd": 10_000.0,
}


class CounterpartDesk:
    """Paper-only control group. Owns its ledger, shares nothing with E."""

    kind = "counterpart"

    def __init__(self, root: Path, cfg: dict | None = None):
        self.root = root
        self.cfg = {**DEFAULT_CONFIG, **(cfg or {})}
        self.desk = Desk(root, self.cfg, db_name=DB_NAME)

    # ---------- selection ----------
    # Known dollar pegs are excluded so the comparison stays about strategy, not
    # about whether a desk knows USDC is a stablecoin. The first control run
    # proved the point: organic_score ranks USDC/USDT/SOL top, which would make
    # the control a permanent 0.00% and the comparison meaningless.
    STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "PYUSD", "USDS", "FDUSD", "TUSD", "USDE", "USDD"}

    def _rank(self, tokens: list[Token]) -> list[Token]:
        """The entire 'strategy': organic score, highest first. No vetoes."""
        floor = float(self.cfg["min_liquidity_usd"])
        usable = [
            t for t in tokens
            if t.price_usd > 0
            and t.liquidity_usd >= floor
            and t.symbol.upper() not in self.STABLE_SYMBOLS
        ]
        usable.sort(key=lambda t: t.organic_score, reverse=True)
        return usable[: int(self.cfg["top_n"])]

    def can_open(self, token: Token, at_ms: int) -> tuple[bool, str]:
        if len(self.desk.open_positions()) >= int(self.cfg["max_open_positions"]):
            return False, "MAX_POSITIONS"
        if any(p["mint"] == token.mint for p in self.desk.open_positions()):
            return False, "ALREADY_OPEN"
        last = self.desk._last_open_ms(token.mint)
        if last and at_ms - last < int(self.cfg["cooldown_minutes"]) * 60_000:
            return False, "COOLDOWN"
        if self.desk.cash() < float(self.cfg["notional_usdt"]):
            return False, "INSUFFICIENT_CASH"
        return True, ""

    # ---------- one round ----------
    def _record_choices(self, tokens: list[Token], at_ms: int) -> int:
        """Persist what the control ranked, rejects included.

        Same shape as the E desk's candidates table so a replay can join them:
        eligible here means "the control would buy it", not "it passed a veto".
        """
        scan_id = self.desk.conn.execute(
            "INSERT INTO scans (scanned_at_ms, tokens_seen, tokens_eligible, errors) "
            "VALUES (?,?,?,?)",
            (at_ms, len(tokens), 0, json.dumps([], ensure_ascii=False)),
        ).lastrowid
        ranked = self._rank(tokens)
        wanted = {t.mint for t in ranked}
        for token in tokens:
            organic = token.organic_score
            self.desk.conn.execute(
                "INSERT INTO candidates (scan_id, scanned_at_ms, mint, symbol, price_usd,"
                " liquidity_usd, fdv_usd, organic_score, organic_label, holder_count,"
                " top_holders_pct, chg_5m, chg_1h, chg_24h, organic_buy_5m, organic_sell_5m,"
                " net_buyers_5m, vol_1h, score, eligible, reject_reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id, at_ms, token.mint, token.symbol, token.price_usd,
                    token.liquidity_usd, token.fdv_usd, token.organic_score,
                    token.organic_label, token.holder_count, token.top_holders_pct,
                    token.chg_5m, token.chg_1h, token.chg_24h, token.organic_buy_5m,
                    token.organic_sell_5m, token.net_buyers_5m, token.vol_1h,
                    organic, 1 if token.mint in wanted else 0,
                    None if token.mint in wanted else "NOT_IN_TOP_N",
                ),
            )
        self.desk.conn.commit()
        return scan_id

    def run_round(self, tokens: list[Token]) -> dict:
        """Record the ranking, open the top picks, then mark and exit."""
        at = now_ms()
        opened, skipped = [], []
        if tokens:
            self._record_choices(tokens, at)
        for token in self._rank(tokens):
            ok, reason = self.can_open(token, at)
            if not ok:
                skipped.append({"symbol": token.symbol, "reason": reason})
                continue
            notional = float(self.cfg["notional_usdt"])
            slip = entry_slippage_bps(notional, token.liquidity_usd)
            fill = token.price_usd * (1.0 + slip / 10_000.0)
            qty = notional / fill
            cost = notional * (float(self.cfg["round_trip_cost_bps"]) / 2.0) / 10_000.0
            self.desk.conn.execute(
                "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd,"
                " entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd,"
                " entry_reason, status, mark_price, mark_at_ms, peak_price)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    at, token.mint, token.symbol, score(token, self.desk.cfg), notional,
                    token.price_usd, fill, qty, slip, cost,
                    f"CONTROL·organic={token.organic_score:.0f}", "open",
                    token.price_usd, at, token.price_usd,
                ),
            )
            self.desk.conn.commit()
            opened.append({"symbol": token.symbol, "organic": token.organic_score, "fill": fill})

        marked = self.desk.update()
        return {"opened": opened, "skipped": skipped, "closed": marked.get("closed", []),
                "marked": marked.get("marked", 0)}

    # ---------- reporting ----------
    def snapshot(self) -> dict:
        conn = self.desk.conn
        opens = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        closed = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) p,"
            " COALESCE(SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END),0) w FROM positions"
            " WHERE status='closed'"
        ).fetchone()
        spent = conn.execute(
            "SELECT COALESCE(SUM(notional_usd + entry_cost_usd),0) FROM positions"
        ).fetchone()[0]
        back = conn.execute(
            "SELECT COALESCE(SUM(exit_fill_price*qty - exit_cost_usd),0) FROM positions"
            " WHERE status='closed' AND exit_fill_price IS NOT NULL"
        ).fetchone()[0]
        unpriced = [p["symbol"] for p in opens if not p["mark_price"]]
        holdings = sum(float(p["mark_price"]) * float(p["qty"]) for p in opens if p["mark_price"])
        starting = float(self.cfg["starting_cash_usdt"])
        cash = starting - float(spent) + float(back)
        equity = cash + holdings
        return {
            "kind": "counterpart",
            "has_ledger": True,
            # Equity is only complete when every holding has a mark; callers
            # must show "incomplete" rather than a number that is quietly low.
            "equity_complete": not unpriced,
            "starting_cash_usdt": starting,
            "cash_usdt": round(cash, 4),
            "equity_usdt": round(equity, 4),
            "pnl_usdt": round(equity - starting, 4),
            "pnl_pct": round((equity - starting) / starting * 100.0, 3),
            "open_count": len(opens),
            "unpriced_count": len(unpriced),
            "closed_count": int(closed["n"]),
            "win_count": int(closed["w"]),
            "realized_usd": round(float(closed["p"]), 4),
            "open_positions": [
                {
                    "symbol": p["symbol"],
                    "entry": float(p["entry_fill_price"]),
                    "mark": float(p["mark_price"] or 0.0),
                    "change_pct": round((float(p["mark_price"] or 0.0) / float(p["entry_fill_price"]) - 1.0) * 100.0, 2),
                    "value_usd": round(float(p["mark_price"] or 0.0) * float(p["qty"]), 4),
                    "age_min": round((now_ms() - int(p["opened_at_ms"])) / 60_000.0, 1),
                    "reason": p["entry_reason"],
                }
                for p in opens
            ],
        }

    def close(self) -> None:
        try:
            self.desk.conn.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass


def load_config(path: Path) -> dict:
    import json

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    if isinstance(raw, dict):
        cfg.update(raw)
    return cfg
