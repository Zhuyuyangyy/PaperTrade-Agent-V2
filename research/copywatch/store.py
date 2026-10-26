"""SQLite ledger for the CopyWatch D account.

The ledger is completely separate from the A/B/C Freqtrade databases: its own file,
its own starting cash, its own tables. Nothing in this module can read or write the
Freqtrade accounts.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS swaps (
    event_id TEXT PRIMARY KEY,
    network TEXT NOT NULL,
    pool TEXT NOT NULL,
    wallet TEXT NOT NULL,
    kind TEXT NOT NULL,
    usd REAL NOT NULL,
    price_usd REAL NOT NULL,
    symbol TEXT NOT NULL,
    block_ts_ms INTEGER NOT NULL,
    block_number INTEGER,
    tx_hash TEXT NOT NULL,
    anomalous INTEGER NOT NULL DEFAULT 0,
    discovered_at_ms INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    actionable INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_swaps_block_ts ON swaps(block_ts_ms);
CREATE INDEX IF NOT EXISTS idx_swaps_wallet ON swaps(wallet);

CREATE TABLE IF NOT EXISTS wallets (
    address TEXT NOT NULL,
    network TEXT NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    swaps INTEGER NOT NULL DEFAULT 0,
    buys INTEGER NOT NULL DEFAULT 0,
    sells INTEGER NOT NULL DEFAULT 0,
    notional_usd REAL NOT NULL DEFAULT 0,
    tracked INTEGER NOT NULL DEFAULT 0,
    promoted_at_ms INTEGER,
    note TEXT,
    PRIMARY KEY (address, network)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    decided_at_ms INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    signal_class TEXT,
    primary_code TEXT,
    codes TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    network TEXT NOT NULL,
    pool TEXT NOT NULL,
    symbol TEXT NOT NULL,
    wallet TEXT NOT NULL,
    signal_class TEXT NOT NULL,
    fee_bps REAL NOT NULL,
    gas_usd REAL NOT NULL,
    opened_at_ms INTEGER NOT NULL,
    entry_ref_price REAL NOT NULL,
    entry_fill_price REAL NOT NULL,
    qty REAL NOT NULL,
    notional_usd REAL NOT NULL,
    entry_fee_usd REAL NOT NULL,
    entry_gas_usd REAL NOT NULL,
    entry_cost_usd REAL NOT NULL,
    signal_price_usd REAL NOT NULL,
    drift_bps REAL NOT NULL,
    latency_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    mark_price REAL,
    mark_at_ms INTEGER,
    exit_at_ms INTEGER,
    exit_reason TEXT,
    exit_fill_price REAL,
    exit_fee_usd REAL,
    exit_gas_usd REAL,
    exit_proceeds_usd REAL,
    pnl_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    tag TEXT NOT NULL,
    wallet TEXT,
    symbol TEXT,
    usd REAL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_ms);

CREATE TABLE IF NOT EXISTS equity_samples (
    ts_ms INTEGER PRIMARY KEY,
    cash_usd REAL NOT NULL,
    positions_usd REAL NOT NULL,
    equity_usd REAL NOT NULL,
    complete INTEGER NOT NULL,
    open_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

MAX_EVENT_ROWS = 3000


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Ledgers created before wallet demotion existed need the new column.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(wallets)")}
    if "demoted_at_ms" not in columns:
        conn.execute("ALTER TABLE wallets ADD COLUMN demoted_at_ms INTEGER")
    conn.commit()


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))


def insert_swap(conn: sqlite3.Connection, event, actionable: bool) -> bool:
    """Insert a swap event; return True only when it was not already stored."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO swaps(event_id, network, pool, wallet, kind, usd, price_usd, symbol,"
        " block_ts_ms, block_number, tx_hash, anomalous, discovered_at_ms, latency_ms, actionable)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event.event_id, event.network, event.pool, event.wallet, event.kind, event.usd,
            event.price_usd, event.base_symbol, event.block_ts_ms, event.block_number,
            event.tx_hash, 1 if event.anomalous else 0, event.discovered_at_ms, event.latency_ms,
            1 if actionable else 0,
        ),
    )
    return cursor.rowcount == 1


def upsert_wallet(conn: sqlite3.Connection, event) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO wallets(address, network, first_seen_ms, last_seen_ms, swaps, buys, sells, notional_usd)"
        " VALUES(?,?,?,?,1,?,?,?)"
        " ON CONFLICT(address, network) DO UPDATE SET"
        " last_seen_ms = MAX(wallets.last_seen_ms, excluded.last_seen_ms),"
        " swaps = wallets.swaps + 1,"
        " buys = wallets.buys + excluded.buys,"
        " sells = wallets.sells + excluded.sells,"
        " notional_usd = wallets.notional_usd + excluded.notional_usd",
        (
            event.wallet, event.network, event.block_ts_ms, event.block_ts_ms,
            1 if event.kind == "buy" else 0, 1 if event.kind == "sell" else 0, event.usd,
        ),
    )
    return conn.execute(
        "SELECT * FROM wallets WHERE address = ? AND network = ?", (event.wallet, event.network)
    ).fetchone()


def promote_wallet(conn: sqlite3.Connection, address: str, network: str, now_ms: int, note: str) -> None:
    conn.execute(
        "UPDATE wallets SET tracked = 1, promoted_at_ms = ?, note = COALESCE(note, ?)"
        " WHERE address = ? AND network = ?",
        (now_ms, note, address, network),
    )


def demote_wallet(
    conn: sqlite3.Connection, address: str, network: str, now_ms: int, note: str
) -> bool:
    """Drop a wallet from tracking. Returns True only when it was tracked.

    A demoted wallet keeps its activity history; only `tracked` flips to 0 and
    `demoted_at_ms` is stamped so a cooldown can be enforced before it is ever
    promoted again.
    """
    cursor = conn.execute(
        "UPDATE wallets SET tracked = 0, demoted_at_ms = ?, note = ?"
        " WHERE address = ? AND network = ? AND tracked = 1",
        (now_ms, note[:400], address, network),
    )
    return cursor.rowcount == 1


def wallet_closed_trades(
    conn: sqlite3.Connection, address: str, network: str, limit: int = 8
) -> list[sqlite3.Row]:
    """Closed paper positions that came from this wallet, newest first."""
    return list(
        conn.execute(
            "SELECT * FROM positions WHERE wallet = ? AND network = ?"
            " AND status = 'closed' AND pnl_usd IS NOT NULL"
            " ORDER BY COALESCE(exit_at_ms, 0) DESC, id DESC LIMIT ?",
            (address, network, limit),
        )
    )


def wallet_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS observed, COALESCE(SUM(tracked), 0) AS tracked,"
        " COALESCE(SUM(CASE WHEN demoted_at_ms IS NOT NULL THEN 1 ELSE 0 END), 0) AS demoted"
        " FROM wallets"
    ).fetchone()
    return {
        "observed": int(row["observed"]),
        "tracked": int(row["tracked"]),
        "demoted": int(row["demoted"]),
    }


def add_event(
    conn: sqlite3.Connection, *, ts_ms: int, tag: str, title: str, detail: str,
    wallet: str | None = None, symbol: str | None = None, usd: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(ts_ms, tag, wallet, symbol, usd, title, detail) VALUES(?,?,?,?,?,?,?)",
        (ts_ms, tag, wallet, symbol, usd, title[:180], detail[:400]),
    )


def record_decision(
    conn: sqlite3.Connection, *, event_id: str, now_ms: int, verdict: str,
    signal_class: str | None, codes: list[dict], detail: str,
) -> None:
    failed = [item["code"] for item in codes if not item["ok"]]
    conn.execute(
        "INSERT INTO decisions(event_id, decided_at_ms, verdict, signal_class, primary_code, codes, detail)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            event_id, now_ms, verdict, signal_class, failed[0] if failed else None,
            json.dumps(codes, ensure_ascii=False), detail[:400],
        ),
    )


def open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM positions WHERE status = 'open' ORDER BY id").fetchall()


def has_open_for(conn: sqlite3.Connection, symbol: str, network: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM positions WHERE status = 'open' AND symbol = ? AND network = ? LIMIT 1",
        (symbol, network),
    ).fetchone()
    return row is not None


def last_open_ms(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(opened_at_ms) AS ts FROM positions").fetchone()
    return int(row["ts"]) if row and row["ts"] is not None else None


def insert_position(conn: sqlite3.Connection, *, event, signal_class: str, ref_price: float,
                    fill_price: float, qty: float, notional_usd: float, fee_usd: float,
                    gas_usd: float, cost_usd: float, fee_bps: float, now_ms: int) -> None:
    drift_bps = 0.0 if event.price_usd <= 0 else (ref_price - event.price_usd) / event.price_usd * 10_000
    conn.execute(
        "INSERT INTO positions(event_id, network, pool, symbol, wallet, signal_class, fee_bps, gas_usd,"
        " opened_at_ms, entry_ref_price, entry_fill_price, qty, notional_usd, entry_fee_usd,"
        " entry_gas_usd, entry_cost_usd, signal_price_usd, drift_bps, latency_ms)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event.event_id, event.network, event.pool, event.base_symbol, event.wallet,
            signal_class, fee_bps, gas_usd, now_ms, ref_price, fill_price, qty, notional_usd,
            fee_usd, gas_usd, cost_usd, event.price_usd, drift_bps, event.latency_ms,
        ),
    )


def position_opened(conn: sqlite3.Connection, event_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM positions WHERE event_id = ? LIMIT 1", (event_id,)).fetchone()
    return row is not None


def update_marks(conn: sqlite3.Connection, marks: dict[int, tuple[float, int]]) -> None:
    for position_id, (price, at_ms) in marks.items():
        conn.execute(
            "UPDATE positions SET mark_price = ?, mark_at_ms = ? WHERE id = ?",
            (price, at_ms, position_id),
        )


def close_position(conn: sqlite3.Connection, *, position_id: int, now_ms: int, reason: str,
                   exit_fill: float, fee_usd: float, gas_usd: float, proceeds: float,
                   pnl: float) -> None:
    conn.execute(
        "UPDATE positions SET status = 'closed', exit_at_ms = ?, exit_reason = ?, exit_fill_price = ?,"
        " exit_fee_usd = ?, exit_gas_usd = ?, exit_proceeds_usd = ?, pnl_usd = ? WHERE id = ?",
        (now_ms, reason, exit_fill, fee_usd, gas_usd, proceeds, pnl, position_id),
    )


def realized_between(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(pnl_usd), 0) AS total FROM positions"
        " WHERE status = 'closed' AND exit_at_ms >= ? AND exit_at_ms < ?",
        (start_ms, end_ms),
    ).fetchone()
    return float(row["total"] or 0.0)


def closed_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_usd), 0) AS total,"
        " COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) AS wins,"
        " COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END), 0) AS losses"
        " FROM positions WHERE status = 'closed'"
    ).fetchone()
    return {
        "closed_count": int(row["n"]),
        "realized_pnl_usd": float(row["total"] or 0.0),
        "win_count": int(row["wins"]),
        "loss_count": int(row["losses"]),
    }


def recent_events(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT ts_ms, tag, wallet, symbol, usd, title, detail FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def prune_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
        (MAX_EVENT_ROWS,),
    )


def add_equity_sample(conn: sqlite3.Connection, *, ts_ms: int, cash: float, positions: float,
                      equity: float, complete: bool, open_count: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO equity_samples(ts_ms, cash_usd, positions_usd, equity_usd, complete, open_count)"
        " VALUES(?,?,?,?,?,?)",
        (ts_ms, cash, positions, equity, 1 if complete else 0, open_count),
    )


def latest_equity(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM equity_samples ORDER BY ts_ms DESC LIMIT 1").fetchone()


def equity_history(conn: sqlite3.Connection, limit: int = 720) -> list[dict]:
    rows = conn.execute(
        "SELECT ts_ms, equity_usd, complete FROM equity_samples ORDER BY ts_ms DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"ts_ms": int(row["ts_ms"]), "equity": float(row["equity_usd"]), "complete": bool(row["complete"])}
        for row in reversed(rows)
    ]


def skip_breakdown(conn: sqlite3.Connection, since_ms: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT primary_code, COUNT(*) AS n FROM decisions"
        " WHERE verdict = 'SKIP' AND decided_at_ms >= ? AND primary_code IS NOT NULL"
        " GROUP BY primary_code ORDER BY n DESC",
        (since_ms,),
    ).fetchall()
    return {str(row["primary_code"]): int(row["n"]) for row in rows}


def decision_counts(conn: sqlite3.Connection, since_ms: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN verdict = 'PAPER' THEN 1 ELSE 0 END), 0) AS paper,"
        " COALESCE(SUM(CASE WHEN verdict = 'SKIP' THEN 1 ELSE 0 END), 0) AS skipped"
        " FROM decisions WHERE decided_at_ms >= ?",
        (since_ms,),
    ).fetchone()
    return {"decisions": int(row["total"]), "paper": int(row["paper"]), "skipped": int(row["skipped"])}


def swap_count(conn: sqlite3.Connection, since_ms: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " COALESCE(SUM(CASE WHEN kind = 'buy' THEN 1 ELSE 0 END), 0) AS buys,"
        " COALESCE(SUM(CASE WHEN actionable = 1 THEN 1 ELSE 0 END), 0) AS actionable"
        " FROM swaps WHERE block_ts_ms >= ?",
        (since_ms,),
    ).fetchone()
    return {
        "swaps": int(row["total"]),
        "buys": int(row["buys"]),
        "actionable": int(row["actionable"]),
    }
