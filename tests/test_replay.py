"""Replay engine invariants: no future data, no invented returns, no shuffled time."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.replay.engine import DEFAULT_PARAMS, ReplayEngine  # noqa: E402
from research.replay.labels import candidate_returns, ensure_schema  # noqa: E402
from research.replay.report import _split_by_time, build_report  # noqa: E402

T0 = 1_700_000_000_000
MIN = 60_000


@pytest.fixture()
def ledger(tmp_path: Path):
    """A tiny hand-built ledger with a known answer."""
    db = tmp_path / "memehunter.sqlite"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, scanned_at_ms INTEGER,
                            tokens_seen INTEGER, tokens_eligible INTEGER, errors TEXT);
        CREATE TABLE candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER,
            scanned_at_ms INTEGER, mint TEXT, symbol TEXT, price_usd REAL, liquidity_usd REAL,
            fdv_usd REAL, organic_score REAL, organic_label TEXT, holder_count INTEGER,
            top_holders_pct REAL, chg_5m REAL, chg_1h REAL, chg_24h REAL, organic_buy_5m REAL,
            organic_sell_5m REAL, net_buyers_5m INTEGER, vol_1h REAL, score REAL,
            eligible INTEGER, reject_reason TEXT);
        CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at_ms INTEGER,
            mint TEXT, symbol TEXT, score REAL, notional_usd REAL, entry_ref_price REAL,
            entry_fill_price REAL, qty REAL, entry_slippage_bps REAL, entry_cost_usd REAL,
            entry_reason TEXT, status TEXT, mark_price REAL, mark_at_ms INTEGER, peak_price REAL,
            exit_at_ms INTEGER, exit_reason TEXT, exit_fill_price REAL, exit_cost_usd REAL,
            pnl_usd REAL, pnl_pct REAL);
        """
    )
    # Two eligible candidates, ten minutes apart.
    for i, (symbol, price) in enumerate((("AAA", 100.0), ("BBB", 50.0))):
        scan_id = conn.execute(
            "INSERT INTO scans (scanned_at_ms, tokens_seen, tokens_eligible, errors)"
            " VALUES (?,?,?,?)",
            (T0 + i * 10 * MIN, 10, 1, "[]"),
        ).lastrowid
        conn.execute(
            "INSERT INTO candidates (scan_id, scanned_at_ms, mint, symbol, price_usd,"
            " liquidity_usd, fdv_usd, organic_score, organic_label, holder_count,"
            " top_holders_pct, chg_5m, chg_1h, chg_24h, organic_buy_5m, organic_sell_5m,"
            " net_buyers_5m, vol_1h, score, eligible, reject_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scan_id, T0 + i * 10 * MIN, f"mint{i}", symbol, price, 500_000.0, 1e6, 70.0,
             "good", 5000, 10.0, 0, 0, 0, 100, 50, 40, 1e5, 60.0, 1, None),
        )
    conn.commit()
    conn.close()

    marks = sqlite3.connect(db)
    ensure_schema(marks)
    marks.commit()
    marks.close()
    return db


def test_replay_never_reads_a_mark_from_the_future(ledger: Path) -> None:
    """AAA is priced 100, then 130 at +30min. A scan at +5min must not see it."""
    conn = sqlite3.connect(ledger)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO candidate_marks (candidate_id, marked_at_ms, price_usd, source)"
        " VALUES (1,?,?, 'test')",
        (T0 + 30 * MIN, 130.0),
    )
    conn.commit()
    conn.close()
    engine = ReplayEngine(ledger.parent, dict(DEFAULT_PARAMS, take_profit_pct=20.0), db_path=ledger)
    try:
        result = engine.run()
    finally:
        engine.close()
    aaa = [t for t in result.trades if t.symbol == "AAA"]
    assert aaa, "AAA should have been replayed"
    # The +30% mark only exists at +30min, and no scan runs that late, so the
    # trade cannot have closed on it.
    assert aaa[0].exit_ms is None


def test_replay_closes_on_a_mark_that_already_existed(ledger: Path) -> None:
    """The same mark, once a later scan exists, must be usable."""
    conn = sqlite3.connect(ledger)
    # A third scan at +40min gives the engine a clock past the mark.
    conn.execute(
        "INSERT INTO scans (scanned_at_ms, tokens_seen, tokens_eligible, errors)"
        " VALUES (?,?,?,?)",
        (T0 + 40 * MIN, 10, 0, "[]"),
    )
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO candidate_marks (candidate_id, marked_at_ms, price_usd, source)"
        " VALUES (1,?,?, 'test')",
        (T0 + 30 * MIN, 130.0),
    )
    conn.commit()
    conn.close()
    engine = ReplayEngine(ledger.parent, dict(DEFAULT_PARAMS, take_profit_pct=20.0), db_path=ledger)
    try:
        result = engine.run()
    finally:
        engine.close()
    aaa = [t for t in result.trades if t.symbol == "AAA"][0]
    assert aaa.exit_reason == "TAKE_PROFIT"
    assert aaa.pnl_usd is not None and aaa.pnl_usd > 0


def test_higher_cost_lowers_net(ledger: Path) -> None:
    """Cost sensitivity is a first-class output, not a footnote."""
    nets = []
    for bps in (0, 120, 240):
        engine = ReplayEngine(ledger.parent, dict(DEFAULT_PARAMS, round_trip_cost_bps=float(bps)), db_path=ledger)
        try:
            result = engine.run()
        finally:
            engine.close()
        nets.append(result.net_usd)
    assert nets[0] >= nets[1] >= nets[2]


def test_time_split_is_never_random(ledger: Path) -> None:
    from research.replay.engine import ReplayTrade

    trades = [
        ReplayTrade(symbol=str(i), mint=str(i), opened_ms=T0 + i * MIN, entry_price=1.0,
                    qty=1.0, notional_usd=1.0, entry_cost_usd=0.0, score=1.0, entry_reason="")
        for i in range(10)
    ]
    dev, test = _split_by_time(trades)
    assert len(dev) == 7 and len(test) == 3
    assert max(t.opened_ms for t in dev) < min(t.opened_ms for t in test)


def test_returns_only_report_horizons_with_a_mark(ledger: Path) -> None:
    conn = sqlite3.connect(ledger)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO candidate_marks (candidate_id, marked_at_ms, price_usd, source)"
        " VALUES (1,?,?, 'test')",
        (T0 + 16 * MIN, 110.0),  # only the 15m horizon is covered
    )
    conn.commit()
    out = candidate_returns(conn, 1)
    conn.close()
    assert out.get("15m") == pytest.approx(10.0)
    assert "5m" not in out and "30m" not in out and "60m" not in out


def test_report_states_coverage_before_any_number(ledger: Path) -> None:
    engine = ReplayEngine(ledger.parent, db_path=ledger)
    try:
        result = engine.run()
    finally:
        engine.close()
    text = build_report(result, {"with_marks": 0, "candidates": 100, "eligible": 5,
                                 "eligible_with_marks": 0, "coverage_pct": 0.0})
    assert "标注覆盖" in text.splitlines()[0]
    assert "不构成" not in text  # the report does not pretend to a verdict
    assert "未计入平仓统计" in text or "无平仓" in text


def test_old_ledger_migrates_to_one_mark_per_interval(tmp_path: Path) -> None:
    """A ledger whose candidate_marks had candidate_id as the sole PRIMARY KEY
    could only ever hold ONE price per candidate -- the birth price. The
    migration rebuilds the table with the composite key and keeps every row,
    so a candidate finally gets a 5m/15m/30m/1h curve instead of a dot.
    """
    import sqlite3

    from research.replay.labels import ensure_schema

    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY, scan_id INTEGER,"
        " scanned_at_ms INTEGER, mint TEXT, symbol TEXT, price_usd REAL,"
        " liquidity_usd REAL, fdv_usd REAL, organic_score REAL, organic_label TEXT,"
        " holder_count INTEGER, top_holders_pct REAL, chg_5m REAL, chg_1h REAL,"
        " chg_24h REAL, organic_buy_5m REAL, organic_sell_5m REAL, net_buyers_5m INTEGER,"
        " vol_1h REAL, score REAL, eligible INTEGER, reject_reason TEXT);"
        # the legacy shape: single-column primary key
        "CREATE TABLE candidate_marks (candidate_id INTEGER PRIMARY KEY,"
        " marked_at_ms INTEGER, price_usd REAL, source TEXT);"
    )
    t0 = 1_700_000_000_000
    conn.execute("INSERT INTO candidates VALUES (1,1,?,'m','S',1.0,1e5,1e6,60,'good',5000,10,"
                 "0,0,0,1,1,10,1e5,60,1,NULL)", (t0,))
    conn.execute("INSERT INTO candidate_marks VALUES (1,?,1.0,'jupiter')", (t0,))
    conn.commit()

    ensure_schema(conn)

    pk = {row[1]: row[5] for row in conn.execute("PRAGMA table_info(candidate_marks)")}
    assert pk.get("marked_at_ms") == 2, "marked_at_ms must be part of the primary key"
    # the historical row survived
    assert conn.execute("SELECT COUNT(*) FROM candidate_marks").fetchone()[0] == 1
    # and now the same candidate can hold a second, later price
    conn.execute(
        "INSERT OR REPLACE INTO candidate_marks (candidate_id, marked_at_ms, price_usd, source)"
        " VALUES (1,?,1.5,'jupiter')",
        (t0 + 15 * 60_000,),
    )
    conn.commit()
    marks = conn.execute("SELECT price_usd FROM candidate_marks ORDER BY marked_at_ms").fetchall()
    assert [m[0] for m in marks] == [1.0, 1.5], "both prices must coexist"
    conn.close()
