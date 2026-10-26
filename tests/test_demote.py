"""A wallet that loses twice in a row stops being copied.

These tests pin the exact rule the desk runs on: the streak is counted over the
wallet's closed paper fills, newest first; one win resets it; a single loss never
drops anyone; and a dropped wallet cannot be promoted again until the cooldown
expires.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.copywatch import agent as agent_mod  # noqa: E402
from research.copywatch import store  # noqa: E402

POLICY = {"min_swaps": 3, "min_notional_usd": 2000.0, "demote_after_losses": 2, "demote_cooldown_hours": 24}
WALLET = "0xabc1230000000000000000000000000000000001"
NETWORK = "base"


@pytest.fixture()
def conn():
    db = Path(tempfile.mkdtemp()) / "demote.sqlite"
    connection = store.connect(db)
    store.ensure_schema(connection)
    connection.execute(
        "INSERT INTO wallets(address, network, first_seen_ms, last_seen_ms, swaps, buys, sells,"
        " notional_usd, tracked, promoted_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (WALLET, NETWORK, 1, 1, 5, 5, 0, 50000.0, 1, 1),
    )
    connection.commit()
    yield connection
    connection.close()


def _close_trade(conn: sqlite3.Connection, symbol: str, pnl: float, exit_at: int) -> None:
    conn.execute(
        "INSERT INTO positions(event_id, network, pool, symbol, wallet, signal_class, fee_bps,"
        " gas_usd, opened_at_ms, entry_ref_price, entry_fill_price, qty, notional_usd, entry_fee_usd,"
        " entry_gas_usd, entry_cost_usd, signal_price_usd, drift_bps, latency_ms, status,"
        " exit_at_ms, exit_fill_price, exit_fee_usd, exit_gas_usd, exit_proceeds_usd, pnl_usd)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"ev-{symbol}-{exit_at}", NETWORK, "0xpool", symbol, WALLET, "test", 30.0, 1.0,
            1, 1.0, 1.0, 100.0, 100.0, 0.3, 1.0, 101.3, 1.0, 0.0, 10, "closed",
            exit_at, 1.0, 0.3, 1.0, 100.0 + pnl, pnl,
        ),
    )
    conn.commit()


def _position(symbol: str = "WETH") -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t(wallet TEXT, network TEXT, symbol TEXT, id INTEGER)"
    )
    conn.execute("INSERT INTO t VALUES(?,?,?,?)", (WALLET, NETWORK, symbol, 1))
    return conn.execute("SELECT * FROM t").fetchone()


def _agent() -> agent_mod.CopyWatchAgent:
    # Skip __init__ (it opens the real ledger); only the helper under test is used.
    return agent_mod.CopyWatchAgent.__new__(agent_mod.CopyWatchAgent)


def _tracked(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT tracked FROM wallets WHERE address=?", (WALLET,)).fetchone()
    return int(row[0])


def test_two_losses_in_a_row_drop_the_wallet(conn: sqlite3.Connection) -> None:
    _close_trade(conn, "WETH", -1.5, 1000)
    _close_trade(conn, "USDC", -2.0, 2000)
    summary: dict = {}
    _agent()._maybe_demote(conn, _position(), -2.0, 3000, POLICY, summary)
    assert _tracked(conn) == 0
    assert summary["demotions"] == [WALLET]


def test_one_win_inside_the_streak_keeps_the_wallet(conn: sqlite3.Connection) -> None:
    _close_trade(conn, "WETH", -1.5, 1000)
    _close_trade(conn, "USDC", +3.0, 2000)
    _close_trade(conn, "DAI", -0.5, 3000)
    _agent()._maybe_demote(conn, _position(), -0.5, 4000, POLICY, {})
    assert _tracked(conn) == 1


def test_a_single_loss_never_drops_anyone(conn: sqlite3.Connection) -> None:
    _close_trade(conn, "WETH", -1.5, 1000)
    _agent()._maybe_demote(conn, _position(), -1.5, 2000, POLICY, {})
    assert _tracked(conn) == 1


def test_streak_is_counted_newest_first(conn: sqlite3.Connection) -> None:
    # Oldest two are losses, but the newest two are a win then a loss.
    _close_trade(conn, "WETH", -1.0, 1000)
    _close_trade(conn, "USDC", -1.0, 2000)
    _close_trade(conn, "DAI", +5.0, 3000)
    _close_trade(conn, "SHIB", -0.2, 4000)
    _agent()._maybe_demote(conn, _position(), -0.2, 5000, POLICY, {})
    assert _tracked(conn) == 1


def test_demotion_records_the_reason_and_timestamp(conn: sqlite3.Connection) -> None:
    _close_trade(conn, "WETH", -1.5, 1000)
    _close_trade(conn, "USDC", -2.0, 2000)
    _agent()._maybe_demote(conn, _position(), -2.0, 3000, POLICY, {})
    row = conn.execute(
        "SELECT note, demoted_at_ms FROM wallets WHERE address=?", (WALLET,)
    ).fetchone()
    assert "连续 2 笔亏损" in row[0]
    assert row[1] == 3000


def test_demotion_can_be_disabled(conn: sqlite3.Connection) -> None:
    _close_trade(conn, "WETH", -1.5, 1000)
    _close_trade(conn, "USDC", -2.0, 2000)
    policy = dict(POLICY, demote_after_losses=0)
    _agent()._maybe_demote(conn, _position(), -2.0, 3000, policy, {})
    assert _tracked(conn) == 1


def test_dropped_wallet_is_not_promoted_again_during_cooldown(conn: sqlite3.Connection) -> None:
    _close_trade(conn, "WETH", -1.5, 1000)
    _close_trade(conn, "USDC", -2.0, 2000)
    _agent()._maybe_demote(conn, _position(), -2.0, 3000, POLICY, {})
    assert _tracked(conn) == 0
    # The promotion gate lives in poll_once; the rule it reads is the timestamp.
    demoted_at = conn.execute(
        "SELECT demoted_at_ms FROM wallets WHERE address=?", (WALLET,)
    ).fetchone()[0]
    cooldown_ms = int(POLICY["demote_cooldown_hours"]) * 3_600_000
    now_ms = 3000 + cooldown_ms - 1
    assert now_ms - int(demoted_at) < cooldown_ms  # still cooling
