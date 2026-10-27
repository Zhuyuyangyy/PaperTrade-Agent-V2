"""Cost calibration: the toll is a curve, not a constant."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.replay.costs import (  # noqa: E402
    calibrate,
    format_calibration,
    impact_one_way,
    modelled_round_trip,
)

T0 = 1_700_000_000_000


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
        CREATE TABLE candidates (mint TEXT, scanned_at_ms INTEGER, liquidity_usd REAL);
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, opened_at_ms INTEGER,
            notional_usd REAL, qty REAL, entry_fill_price REAL, exit_fill_price REAL,
            pnl_usd REAL, exit_reason TEXT, status TEXT
        );
        """
    )
    conn.execute("INSERT INTO candidates VALUES ('deep', ?, 100000.0)", (T0 - 60_000,))
    # A $100 round trip into a $100k pool: 25bps LP + 20bps impact + 2bps tx.
    conn.execute(
        "INSERT INTO positions VALUES (1,'deep','AAA',?,100.0,100.0,1.0,1.1,8.8,'TIMEOUT','closed')",
        (T0,),
    )
    # No candidate snapshot for this mint: depth unknown, must not be guessed.
    conn.execute(
        "INSERT INTO positions VALUES (2,'ghost','BBB',?,100.0,100.0,1.0,1.0,-1.2,'TIMEOUT','closed')",
        (T0,),
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_impact_uses_half_the_pool_as_the_quote_side():
    # $100 into $100k of total liquidity is 20 bps, not 10: only the quote
    # reserve absorbs the buy.
    assert impact_one_way(100.0, 100_000.0) == pytest.approx(0.002)


def test_round_trip_is_two_sides_of_fee_impact_and_gas():
    rt = modelled_round_trip(100.0, 100_000.0)
    assert rt == pytest.approx(2 * (0.0025 + 0.002 + 0.0002))


def test_bigger_orders_pay_more_per_dollar():
    assert modelled_round_trip(1_000.0, 100_000.0) > modelled_round_trip(100.0, 100_000.0)


def test_calibration_reprices_the_book_and_skips_unknown_depth(desk: Path):
    rep = calibrate(desk)
    assert rep["n_trades"] == 2
    assert rep["n_priced"] == 1
    assert rep["n_missing_depth"] == 1
    assert rep["assumed_bps"] == pytest.approx(120.0)
    assert rep["model_bps"]["median"] == pytest.approx(94.0)
    # gross +10.00, modelled toll 0.94 -> +9.06; the recorded pnl was +8.80.
    assert rep["book"]["net_assumed"] == pytest.approx(8.8)
    assert rep["book"]["net_model"] == pytest.approx(10.0 - 0.94, abs=1e-6)
    text = format_calibration(rep)
    assert "成本校准" in text and "假设成本净" in text
