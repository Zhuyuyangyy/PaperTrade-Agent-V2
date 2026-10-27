"""Counterfactual timeout replay: only the deadline moves, never the stop."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.replay.timeout_replay import TIMEOUTS, counterfactual  # noqa: E402

T0 = 1_700_000_000_000
MIN = 60_000


@pytest.fixture()
def desk(tmp_path: Path):
    (tmp_path / "runtime" / "memehunter").mkdir(parents=True)
    (tmp_path / "user_data").mkdir(parents=True)
    (tmp_path / "user_data" / "config.memehunter.json").write_text(
        json.dumps({"round_trip_cost_bps": 120.0}), encoding="utf-8"
    )
    db = tmp_path / "runtime" / "memehunter" / "memehunter.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE candidates (id INTEGER PRIMARY KEY, symbol TEXT, scanned_at_ms INTEGER,"
        " price_usd REAL, eligible INTEGER);"
        "CREATE TABLE candidate_marks (candidate_id INTEGER, marked_at_ms INTEGER, price_usd REAL,"
        " source TEXT, PRIMARY KEY (candidate_id, marked_at_ms));"
        "CREATE TABLE positions (symbol TEXT, opened_at_ms INTEGER, entry_fill_price REAL,"
        " qty REAL, entry_cost_usd REAL, exit_at_ms INTEGER, exit_reason TEXT, pnl_usd REAL,"
        " status TEXT);"
    )
    return conn, db


def _candidate(conn: sqlite3.Connection, cid: int, symbol: str, marks: list[tuple[int, float]]) -> None:
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,1)", (cid, symbol, T0, marks[0][1]))
    for t, p in marks:
        conn.execute("INSERT INTO candidate_marks VALUES (?,?,?,'t')", (cid, t, p))
    conn.commit()


def test_a_risk_exit_keeps_its_outcome_at_every_deadline(desk) -> None:
    conn, _ = desk
    # A stop-loss that fired at 10 minutes is the same trade at any timeout.
    conn.execute(
        "INSERT INTO positions VALUES ('PEPE',?,1.0,100.0,1.3,?, 'STOP_LOSS', -11.3, 'closed')",
        (T0, T0 + 10 * MIN),
    )
    _candidate(conn, 1, "PEPE", [(T0, 1.0), (T0 + 45 * MIN, 0.9), (T0 + 90 * MIN, 0.8)])
    report = counterfactual(desk[1].parents[2])
    for key in (f"{t}m" for t in TIMEOUTS):
        assert report[key]["net_usd"] == pytest.approx(-11.3), key


def test_the_deadline_exit_uses_the_first_price_after_it(desk) -> None:
    conn, _ = desk
    # A timeout trade: the desk sells at the first visible price past the deadline.
    conn.execute(
        "INSERT INTO positions VALUES ('DOPE',?,1.0,100.0,1.3,?, 'TIMEOUT', -0.5, 'closed')",
        (T0, T0 + 180 * MIN),
    )
    _candidate(conn, 1, "DOPE", [(T0, 1.0), (T0 + 46 * MIN, 1.02), (T0 + 92 * MIN, 1.05)])
    report = counterfactual(desk[1].parents[2])
    # 45m exits at 1.02: 60bps exit slippage + 60bps exit toll, minus the
    # recorded entry cost. (half of the 120bps round trip is 60bps = 0.006)
    expected = 100 * (1.02 * (1 - 0.006) - 1.0) - 1.3 - 100 * 1.02 * 0.006
    assert report["45m"]["net_usd"] == pytest.approx(expected, abs=0.01)


def test_a_vanished_token_pays_its_last_visible_price(desk) -> None:
    conn, _ = desk
    conn.execute(
        "INSERT INTO positions VALUES ('GONE',?,1.0,100.0,1.3,?, 'TIMEOUT', -0.5, 'closed')",
        (T0, T0 + 180 * MIN),
    )
    # Quoted at 45m, gone by 90m: the 90m exit sells the last visible price,
    # it does not get to pretend the trade never happened.
    _candidate(conn, 1, "GONE", [(T0, 1.0), (T0 + 46 * MIN, 0.95)])
    report = counterfactual(desk[1].parents[2])
    assert report["45m"]["n"] == 1
    assert report["90m"]["n"] == 1
    assert report["90m"]["net_usd"] == pytest.approx(-7.44, abs=0.05)
    assert report["vanished_priced_at_last"] >= 1


def test_the_report_states_its_own_bias(desk) -> None:
    conn, _ = desk
    conn.execute(
        "INSERT INTO positions VALUES ('BIAS',?,1.0,100.0,1.3,?, 'TIMEOUT', -0.5, 'closed')",
        (T0, T0 + 180 * MIN),
    )
    report = counterfactual(desk[1].parents[2])
    assert "NOT unbiased" in report["note"]
