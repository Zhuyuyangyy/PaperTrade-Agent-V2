"""Selectivity audit: it may only claim what the shadow prices can support."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.replay.selectivity import (  # noqa: E402
    MIN_FORWARD_S,
    _round_trip,
    _stats,
    audit,
    format_audit,
)

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
        """
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, scanned_at_ms INTEGER,
            price_usd REAL, score REAL, llm_score REAL, eligible INTEGER,
            holder_count INTEGER, top_holders_pct REAL, liquidity_usd REAL,
            fdv_usd REAL, vol_1h REAL, organic_score REAL, chg_5m REAL, reject_reason TEXT
        );
        CREATE TABLE candidate_marks (
            candidate_id INTEGER, marked_at_ms INTEGER, price_usd REAL, source TEXT,
            PRIMARY KEY (candidate_id, marked_at_ms)
        );
        """
    )
    # A candidate that doubles over the first 60 minutes: 1.0 -> 2.0.
    conn.execute(
        "INSERT INTO candidates VALUES (1,'m1','AAA',?,1.0,60,0,1,5000,15,900000,1000000,90000,70,0.5,NULL)",
        (T0,),
    )
    for offset, px in [(0, 1.0), (5, 1.1), (15, 1.3), (30, 1.6), (60, 2.0)]:
        conn.execute(
            "INSERT INTO candidate_marks VALUES (1, ?, ?, 'scan-reuse')", (T0 + offset * MIN, px)
        )
    # A candidate whose only mark after the scan is too early to be a forward fill.
    conn.execute(
        "INSERT INTO candidates VALUES (2,'m2','BBB',?,1.0,10,0,1,100,40,10000,1000000,500,30,-2.0,NULL)",
        (T0,),
    )
    conn.execute("INSERT INTO candidate_marks VALUES (2, ?, 1.0, 'scan-reuse')", (T0,))
    conn.execute(
        "INSERT INTO candidate_marks VALUES (2, ?, 1.05, 'scan-reuse')",
        (T0 + (MIN_FORWARD_S - 30) * 1000,),
    )
    # A candidate last quoted 20 minutes after the scan: too late to have bought.
    conn.execute(
        "INSERT INTO candidates VALUES (3,'m3','CCC',?,1.0,50,0,1,8000,18,500000,900000,40000,55,0.1,NULL)",
        (T0,),
    )
    conn.execute("INSERT INTO candidate_marks VALUES (3, ?, 1.0, 'scan-reuse')", (T0 + 20 * MIN,))
    conn.execute("INSERT INTO candidate_marks VALUES (3, ?, 1.2, 'scan-reuse')", (T0 + 25 * MIN,))
    conn.commit()
    conn.close()
    return tmp_path


def test_forward_mark_must_be_after_the_entry_round():
    path = [(T0, 1.0), (T0 + (MIN_FORWARD_S - 30) * 1000, 1.05)]
    assert _round_trip(path, T0, 5) is None


def test_entry_must_be_near_the_scan():
    path = [(T0 + 20 * MIN, 1.0), (T0 + 25 * MIN, 1.2)]
    assert _round_trip(path, T0, 5) is None


def test_returns_are_net_of_round_trip_cost():
    st = _stats([0.0, 0.0, 0.0], cost=0.012)
    assert st["mean_pct"] == pytest.approx(-1.2)
    assert st["win_pct"] == 0.0


def test_profit_factor_is_none_when_nothing_lost():
    st = _stats([0.05, 0.02], cost=0.012)
    assert st["profit_factor"] is None


def test_audit_counts_cohorts_and_survives_a_missing_horizon(desk: Path):
    result = audit(desk, horizons=(5, 60))
    five = result["horizons"]["5m"]
    # Only candidate 1 has a usable 5m round trip; 2 and 3 are excluded.
    assert five["n_all"] == 1
    names = {c["name"]: c for c in five["cohorts"]}
    assert names["score>0"]["n"] == 1
    assert names["llm≥60"]["n"] == 0
    # 60m the same candidate still qualifies (doubling, well past cost).
    sixty = result["horizons"]["60m"]
    assert sixty["all"]["mean_pct"] > 50
    assert "选择性审计" in format_audit(result)
    assert "score>0" in format_audit(result)
