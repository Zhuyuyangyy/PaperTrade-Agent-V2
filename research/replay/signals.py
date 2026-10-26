"""Signal-source comparison: is "smart money bought it" a better signal than
"organic score is high"?

The operator's insight: every moment where D (follow real on-chain wallets) and
E (self-selected by organic score) choose differently is a natural experiment --
two selection rules facing the same market at the same time. Those divergences
are a training set for adjusting the agents.

Two corrections to that framing, both learned the hard way:

1. "D did not pick it" is not evidence the token was bad. D's own gates (cost,
   reserve, price freshness, symbol cooldown, wallet dedupe) reject most of what
   it sees. So the comparison uses what D *observed*, not what D *filled*.
2. This is a hypothesis test, not a model. With a few hundred paired samples,
   fitting a predictor would be memorising noise. The first question is
   narrower and answerable: which signal source's picks had the better forward
   return, after the same cost assumption?

D's ledger records the price at observation, so no backfill is needed there;
forward prices come from the same shadow marks E uses.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SIGNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS d_candidate_marks (
    symbol TEXT NOT NULL,
    marked_at_ms INTEGER NOT NULL,
    price_usd REAL NOT NULL,
    PRIMARY KEY (symbol, marked_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_d_marks_time ON d_candidate_marks(marked_at_ms);
"""

# How close two observations must be in time to count as "same window".
WINDOW_MS = 30 * 60 * 1000
# Forward horizons reported, in minutes.
HORIZONS = (30, 60)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SIGNAL_SCHEMA)
    conn.commit()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def d_observations(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    """What D saw (buys it evaluated), not what it filled.

    One row per (symbol, window): the largest buy in that window, with the price
    D recorded at observation time. That price is the baseline -- no backfill.
    """
    rows = conn.execute(
        "SELECT symbol, MAX(usd) AS usd, MAX(price_usd) AS price, MAX(block_ts_ms) AS ts"
        " FROM swaps WHERE kind = 'buy' AND block_ts_ms >= ? AND price_usd > 0"
        " GROUP BY symbol, block_ts_ms / ? ORDER BY ts",
        (since_ms, WINDOW_MS),
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "symbol": row[0],
            "usd": float(row[1]),
            "price": float(row[2]),
            "ts": int(row[3]),
        })
    return out


def e_observations(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    """What E saw: every candidate in a scan window, with its recorded price."""
    rows = conn.execute(
        "SELECT symbol, MAX(price_usd) AS price, MAX(scanned_at_ms) AS ts, MAX(eligible) AS elig"
        " FROM candidates WHERE scanned_at_ms >= ? AND price_usd > 0"
        " GROUP BY symbol, scanned_at_ms / ? ORDER BY ts",
        (since_ms, WINDOW_MS),
    ).fetchall()
    return [
        {"symbol": r[0], "price": float(r[1]), "ts": int(r[2]), "eligible": bool(r[3])}
        for r in rows
    ]


def forward_return(marks_conn: sqlite3.Connection, symbol: str, base_price: float,
                   base_ts: int, horizon_min: int) -> float | None:
    """Return over one horizon from shadow marks, or None when unobserved."""
    target = base_ts + horizon_min * 60 * 1000
    tolerance = max(horizon_min * 0.5, 10) * 60 * 1000
    row = marks_conn.execute(
        "SELECT marked_at_ms, price_usd FROM d_candidate_marks"
        " WHERE symbol = ? AND marked_at_ms >= ? AND marked_at_ms <= ?"
        " ORDER BY marked_at_ms LIMIT 1",
        (symbol, target, target + tolerance),
    ).fetchone()
    if not row:
        return None
    return round((float(row[1]) / base_price - 1.0) * 100.0, 3)


def compare(root, lookback_hours: int = 24) -> dict:
    """Pair D and E observations by window and compare forward returns."""
    import shutil
    import tempfile

    from ..memehunter import _get  # Jupiter quote, same source E marks with

    now = _now_ms()
    since = now - lookback_hours * 3_600_000

    e_db = root / "runtime" / "memehunter" / "memehunter.sqlite"
    d_db = root / "runtime" / "copywatch" / "copywatch.sqlite"
    if not e_db.is_file() or not d_db.is_file():
        return {"error": "账本缺失"}

    e_conn = sqlite3.connect(e_db)
    e_conn.row_factory = sqlite3.Row
    ensure_schema(e_conn)

    # D's ledger is WAL-mode; copy before reading.
    tmp_dir = tempfile.mkdtemp()
    try:
        d_copy = Path(tmp_dir) / d_db.name
        shutil.copy(d_db, d_copy)
        d_conn = sqlite3.connect(str(d_copy))
        d_conn.row_factory = sqlite3.Row
        try:
            d_obs = d_observations(d_conn, since)
        finally:
            d_conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    e_obs = e_observations(e_conn, since)

    # Symbol sets per window.
    def by_window(rows):
        out = {}
        for r in rows:
            w = int(r["ts"] / WINDOW_MS)
            out.setdefault(w, {})[r["symbol"]] = r
        return out

    dw, ew = by_window(d_obs), by_window(e_obs)
    windows = sorted(set(dw) | set(ew))

    pairs = []
    for w in windows:
        d_syms = set((dw.get(w) or {}).keys())
        e_syms = set((ew.get(w) or {}).keys())
        both = d_syms & e_syms
        only_d = d_syms - e_syms
        only_e = e_syms - d_syms
        for sym in both:
            pairs.append(("both", sym, dw[w][sym], ew[w][sym]))
        for sym in only_d:
            pairs.append(("d_only", sym, dw[w][sym], None))
        for sym in only_e:
            pairs.append(("e_only", sym, None, ew[w][sym]))

    # Forward returns. Load marks once and match in memory: pairing 19k rows
    # with a query each turned a 2-second report into a two-minute one.
    marks = sqlite3.connect(e_db)
    marks.row_factory = sqlite3.Row
    cand_by_key = {}
    for row in marks.execute(
        "SELECT id, symbol, scanned_at_ms FROM candidates WHERE scanned_at_ms >= ?", (since,)
    ):
        cand_by_key.setdefault((row[1], int(row[2] / WINDOW_MS)), row[0])
    mark_by_cid: dict[int, list] = {}
    for row in marks.execute(
        "SELECT candidate_id, marked_at_ms, price_usd FROM candidate_marks"
    ):
        mark_by_cid.setdefault(int(row[0]), []).append((int(row[1]), float(row[2])))
    d_marks = {}
    for row in marks.execute("SELECT symbol, marked_at_ms, price_usd FROM d_candidate_marks"):
        d_marks.setdefault(row[0], []).append((int(row[1]), float(row[2])))
    marks.close()

    def _fwd(prices, base_price, base_ts, horizon_min):
        if not prices:
            return None
        target = base_ts + horizon_min * 60 * 1000
        tol = max(horizon_min * 0.5, 10) * 60 * 1000
        for t, v in prices:
            if target <= t <= target + tol:
                return round((v / base_price - 1.0) * 100.0, 3)
        return None

    scored = []
    for kind, sym, drow, erow in pairs:
        w = int((drow or erow)["ts"] / WINDOW_MS)
        if drow:
            prices = d_marks.get(sym)
            for h in HORIZONS:
                r = _fwd(prices, drow["price"], drow["ts"], h)
                if r is not None:
                    scored.append({"side": "D", "kind": kind, "symbol": sym, "h": h, "ret": r})
        if erow:
            cid = cand_by_key.get((sym, w))
            prices = mark_by_cid.get(cid) if cid else None
            for h in HORIZONS:
                r = _fwd(prices, erow["price"], erow["ts"], h)
                if r is not None:
                    scored.append({"side": "E", "kind": kind, "symbol": sym, "h": h, "ret": r})

    e_conn.close()

    def _mean(rows: list[dict]) -> float | None:
        return round(sum(r["ret"] for r in rows) / len(rows), 3) if rows else None

    d_rows = [x for x in scored if x["side"] == "D"]
    e_rows = [x for x in scored if x["side"] == "E"]
    return {
        "windows": len(windows),
        "pairs": len(pairs),
        "both": sum(1 for p in pairs if p[0] == "both"),
        "d_only": sum(1 for p in pairs if p[0] == "d_only"),
        "e_only": sum(1 for p in pairs if p[0] == "e_only"),
        "scored": scored,
        "d_mean": _mean(d_rows),
        "d_n": len(d_rows),
        "e_mean": _mean(e_rows),
        "e_n": len(e_rows),
        "symbols": sorted({p[1] for p in pairs})[:40],
    }


def collect_d_marks(root, lookback_hours: int = 24) -> dict:
    """Shadow-mark the symbols D observed, so forward returns become computable.

    D's ledger already stores the price at observation, so this only adds later
    prices -- never a replacement for the ones already recorded.
    """
    import shutil
    import tempfile

    from ..memehunter import Token, _get, ensure_candidate_columns  # noqa: F401

    now = _now_ms()
    since = now - lookback_hours * 3_600_000
    e_db = root / "runtime" / "memehunter" / "memehunter.sqlite"
    d_db = root / "runtime" / "copywatch" / "copywatch.sqlite"
    if not e_db.is_file() or not d_db.is_file():
        return {"error": "账本缺失"}

    tmp_dir = tempfile.mkdtemp()
    try:
        d_copy = Path(tmp_dir) / d_db.name
        shutil.copy(d_db, d_copy)
        d_conn = sqlite3.connect(str(d_copy))
        d_conn.row_factory = sqlite3.Row
        try:
            obs = d_observations(d_conn, since)
        finally:
            d_conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Resolve symbol -> mint through E's candidate table (same universe), then
    # price the mints.
    e_conn = sqlite3.connect(e_db)
    e_conn.row_factory = sqlite3.Row
    ensure_schema(e_conn)
    symbol_mint = {}
    for row in e_conn.execute("SELECT DISTINCT symbol, mint FROM candidates"):
        symbol_mint.setdefault(row[0], row[1])

    wanted = sorted({o["symbol"] for o in obs if o["symbol"] in symbol_mint})
    marked = 0
    for i in range(0, len(wanted), 50):
        chunk = wanted[i : i + 50]
        mints = [symbol_mint[s] for s in chunk]
        try:
            payload = _get("https://quote-api.jup.ag/price/v3?ids=" + ",".join(mints), timeout=25.0)
        except Exception:  # noqa: BLE001
            continue
        data = (payload or {}).get("data") or {}
        for sym, mint in zip(chunk, mints):
            row = data.get(mint) or {}
            price = row.get("price")
            if isinstance(price, (int, float)) and float(price) > 0:
                e_conn.execute(
                    "INSERT OR REPLACE INTO d_candidate_marks (symbol, marked_at_ms, price_usd)"
                    " VALUES (?,?,?)",
                    (sym, now, float(price)),
                )
                marked += 1
    e_conn.commit()
    e_conn.close()
    return {"observed": len(obs), "symbols_with_mint": len(wanted), "marked": marked}
