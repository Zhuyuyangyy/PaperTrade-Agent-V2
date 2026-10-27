"""The arm table must list every book, including the ones with no results."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.memehunter as mh  # noqa: E402
from research.replay.arms import compare, format_compare  # noqa: E402


@pytest.fixture()
def books(tmp_path: Path) -> Path:
    (tmp_path / "user_data").mkdir()
    (tmp_path / "user_data" / "config.memehunter.json").write_text(
        json.dumps({"round_trip_cost_bps": 120.0}), encoding="utf-8"
    )
    runtime = tmp_path / "runtime" / "memehunter"
    runtime.mkdir(parents=True)
    for name in ("memehunter.sqlite", "counterpart.sqlite", "arm_quiet.sqlite"):
        conn = mh.connect(runtime / name)
        conn.commit()
        conn.close()
    # One closed trade in the live book, run from a $100k pool.
    conn = mh.connect(runtime / "memehunter.sqlite")
    conn.execute(
        "INSERT INTO candidates (scan_id, mint, symbol, scanned_at_ms, price_usd,"
        " liquidity_usd, fdv_usd, organic_score, holder_count, top_holders_pct,"
        " chg_5m, chg_1h, chg_24h, organic_buy_5m, organic_sell_5m, net_buyers_5m,"
        " vol_1h, score, eligible) VALUES (1,'m','AAA',1,1.0,100000.0,500000.0,60.0,"
        " 1000,20.0,0.5,2.0,5.0,100.0,10.0,5,50000.0,60.0,1)"
    )
    conn.execute(
        "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd,"
        " entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd,"
        " entry_reason, status, exit_at_ms, exit_reason, exit_fill_price,"
        " exit_cost_usd, pnl_usd, pnl_pct)"
        " VALUES (1,'m','AAA',60.0,100.0,1.0,1.0,100.0,35.0,0.6,'test','closed',"
        " 2,'TIMEOUT',1.05,0.6,3.8,3.8)"
    )
    conn.execute("INSERT INTO equity (at_ms, cash_usd, positions_usd, equity_usd, complete) VALUES (2, 900, 103, 1003.0, 1)")
    conn.commit()
    conn.close()
    return tmp_path


def test_every_ledger_appears_even_without_a_single_result(books: Path):
    rows = compare(books)
    labels = {r["label"] for r in rows}
    assert labels == {"E 现役", "C 对照组", "arm_quiet"}
    quiet = next(r for r in rows if r["label"] == "arm_quiet")
    assert quiet["n_closed"] == 0 and quiet["equity"] == 0.0


def test_gross_and_the_two_cost_views(books: Path):
    rows = {r["label"]: r for r in compare(books)}["E 现役"]
    assert rows["n_closed"] == 1
    assert rows["gross"] == pytest.approx(5.0)      # 100 shares, 1.00 -> 1.05
    assert rows["net_assumed"] == pytest.approx(5.0 - 1.2)
    # $100 into a $100k pool: 25bps LP + 20bps impact + 2bps gas, both sides.
    assert rows["net_model"] == pytest.approx(5.0 - 0.94, abs=0.01)
    assert rows["equity"] == pytest.approx(1003.0)
    assert "TIMEOUT×1" in format_compare(compare(books))


def test_no_ledgers_means_an_empty_table(tmp_path: Path):
    assert compare(tmp_path) == []
