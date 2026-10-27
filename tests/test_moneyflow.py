"""Money-flow shadow signals: observed buys, deduped, honestly unknown when unpriced."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.replay.moneyflow import collect_signals, compare_sources  # noqa: E402


@pytest.fixture()
def desk(tmp_path: Path):
    """Two ledgers: D's copywatch with tracked wallets, E's memehunter."""
    (tmp_path / "runtime" / "copywatch").mkdir(parents=True)
    (tmp_path / "runtime" / "memehunter").mkdir(parents=True)
    cw = tmp_path / "runtime" / "copywatch" / "copywatch.sqlite"
    conn = sqlite3.connect(cw)
    conn.executescript(
        "CREATE TABLE wallets (address TEXT, network TEXT, tracked INTEGER, swaps INTEGER,"
        " notional_usd REAL);"
        "CREATE TABLE swaps (event_id TEXT, network TEXT, wallet TEXT, kind TEXT, usd REAL,"
        " price_usd REAL, symbol TEXT, block_ts_ms INTEGER);"
    )
    conn.execute("INSERT INTO wallets VALUES ('0xbig','base',1,10,900000.0)")
    conn.execute("INSERT INTO wallets VALUES ('0xsmall','base',0,2,500.0)")
    conn.commit()
    conn.close()
    return tmp_path, cw


def _swap(conn, wallet, symbol, usd, price, ts):
    conn.execute(
        "INSERT INTO swaps (event_id, network, wallet, kind, usd, price_usd, symbol, block_ts_ms)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (f"ev{ts}{symbol}", "base", wallet, "buy", usd, price, symbol, ts),
    )
    conn.commit()


def test_only_tracked_wallet_buys_become_signals(desk) -> None:
    tmp, cw = desk
    conn = sqlite3.connect(cw)
    from datetime import datetime, timezone

    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    _swap(conn, "0xbig", "PEPE", 5000.0, 0.001, now - 600_000)
    _swap(conn, "0xsmall", "RUG", 9000.0, 0.002, now - 600_000)  # untracked: not a signal
    conn.close()

    result = collect_signals(tmp, window_hours=2)
    assert result["recorded"] == 1, "an untracked wallet's buy is not a money-flow signal"


def test_repeat_buys_by_one_wallet_are_one_opinion(desk) -> None:
    tmp, cw = desk
    conn = sqlite3.connect(cw)
    from datetime import datetime, timezone

    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    _swap(conn, "0xbig", "PEPE", 5000.0, 0.001, now - 3_600_000)
    _swap(conn, "0xbig", "PEPE", 6000.0, 0.0011, now - 3_000_000)  # 10 min later: same hour
    _swap(conn, "0xbig", "PEPE", 7000.0, 0.0012, now - 1_800_000)  # 30 min later: same hour
    conn.close()

    result = collect_signals(tmp, window_hours=2, dedupe_minutes=60)
    assert result["recorded"] == 1, "three buys in an hour are one signal"


def test_compare_reports_unknown_not_zero(desk) -> None:
    tmp, _ = desk
    report = compare_sources(tmp)
    for horizon in ("5m", "15m", "30m", "60m"):
        assert report[horizon]["moneyflow"]["n"] == 0
        assert report[horizon]["moneyflow"]["mean"] is None
    assert "unknown" in report["note"] or "same shadow marks" in report["note"]
