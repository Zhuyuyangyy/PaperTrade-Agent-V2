"""The exit loop exists because a stop-loss needs a price, not a pipeline.

Run 839's lesson was that a broken step is invisible; this one is that a
*slow* step is invisible too. Marks ran inside the six-agent round, so the
-12% stop was only checked every 4-5 minutes -- long enough for a token to
gap from -8% to -21% between checks. Seven stop fills landed between -12.2%
and -21%; that gap is the single biggest measured leak in the book.

These tests pin the two properties that fix it: the ledger can be written by
two processes at once (WAL), and the stop is honoured at the price it was
actually seen at, not the one the next round happens to catch.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.memehunter as mh  # noqa: E402
from research.pipeline.agents import AgentResult, ExitAgent  # noqa: E402
from research.pipeline.orchestrator import Pipeline, exit_round, run_exit_loop  # noqa: E402

STOP_CFG = {"mode": "dry_run", "entry": {"stop_loss_pct": -12.0}}


@pytest.fixture()
def desk(tmp_path: Path):
    (tmp_path / "user_data").mkdir(parents=True)
    (tmp_path / "user_data" / "config.pipeline.json").write_text(
        json.dumps(STOP_CFG), encoding="utf-8"
    )
    return mh.Desk(
        tmp_path,
        {"starting_cash_usdt": 1000.0, "stop_loss_pct": -12.0, "take_profit_pct": 55.0,
         "timeout_minutes": 180, "trailing_giveback_pct": 30.0},
    )


def _open(d: mh.Desk, symbol: str, price: float, age_min: int = 10) -> None:
    d.conn.execute(
        "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd,"
        " entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd,"
        " entry_reason, status, mark_price, peak_price)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
        (mh.now_ms() - age_min * 60_000, f"mint-{symbol}", symbol, 50.0, 100.0,
         price, price, 100.0, 35.0, 0.6, "test", price, price),
    )
    d.conn.commit()


def test_connect_switches_the_ledger_to_wal(tmp_path: Path):
    conn = mh.connect(tmp_path / "wal.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10_000
    finally:
        conn.close()


def test_a_gap_past_the_stop_fills_at_the_price_it_was_seen_at(desk: mh.Desk, monkeypatch):
    # -15% when the stop is -12%: the fill is -15%, the price this pass
    # observed. Before the exit loop the next check was 4-5 minutes away, and
    # the same gap filled at -21%.
    _open(desk, "GAPPED", 1.0)
    monkeypatch.setattr(mh.Desk, "quote", lambda self, mints: {mints[0]: 0.85})
    result = desk.update()
    closed = result["closed"]
    assert len(closed) == 1
    assert closed[0]["reason"] == "STOP_LOSS"
    # -15% gross, minus the 1.2% round trip = -16.2% booked. The point is the
    # fill price: the stop fires on the price this pass observed, not on some
    # rounded "about -12%" fiction, and not on the price 4-5 minutes later.
    assert closed[0]["pnl_pct"] == pytest.approx(-16.2, abs=0.1)


def test_a_position_inside_the_band_is_marked_not_closed(desk: mh.Desk, monkeypatch):
    _open(desk, "CALM", 1.0)
    monkeypatch.setattr(mh.Desk, "quote", lambda self, mints: {mints[0]: 0.995})
    result = desk.update()
    assert result["closed"] == []
    assert result["marked"] == 1


def test_a_pass_leaves_no_write_lock_behind(tmp_path: Path, monkeypatch):
    """The regression this whole change is about.

    `_sample_equity` used to write without committing, so the exit loop's
    connection held the WAL write lock for the whole minute between passes and
    every entry round in between died on "database is locked". A second
    connection with a one-second timeout is the honest test of that.
    """
    (tmp_path / "user_data").mkdir(parents=True)
    (tmp_path / "user_data" / "config.pipeline.json").write_text(
        json.dumps(STOP_CFG), encoding="utf-8"
    )
    desk = mh.Desk(tmp_path, {"starting_cash_usdt": 1000.0, "stop_loss_pct": -12.0})
    _open(desk, "CALM", 1.0)
    monkeypatch.setattr(mh.Desk, "quote", lambda self, mints: {mints[0]: 0.995})
    desk.update()

    other = sqlite3.connect(str(desk.db_path), timeout=1.0)
    try:
        other.execute("PRAGMA busy_timeout=1000")
        other.execute(
            "INSERT INTO scans (scanned_at_ms, tokens_seen, tokens_eligible, errors)"
            " VALUES (1, 0, 0, '[]')"
        )
        other.commit()
    finally:
        other.close()


def test_exit_round_runs_the_exit_agent_not_the_desk_directly(tmp_path: Path, monkeypatch):
    """The orchestrator has no money powers; it decides how often EXIT runs."""
    (tmp_path / "user_data").mkdir(parents=True)
    (tmp_path / "user_data" / "config.pipeline.json").write_text(
        json.dumps(STOP_CFG), encoding="utf-8"
    )
    pipe = Pipeline(tmp_path)
    try:
        seen: list[str] = []

        def fake_run(self, ctx):
            seen.append("EXIT")
            return AgentResult(agent="EXIT", ok=True, summary="marked 1",
                                  detail={"result": {"marked": 1, "closed": [], "stale": []}})

        monkeypatch.setattr("research.pipeline.agents.ExitAgent.run", fake_run)
        assert exit_round(pipe) == {"marked": 1, "closed": [], "stale": []}
        assert seen == ["EXIT"]
    finally:
        pipe.close()


def test_exit_round_surfaces_a_failed_pass_instead_of_raising(tmp_path: Path, monkeypatch):
    (tmp_path / "user_data").mkdir(parents=True)
    (tmp_path / "user_data" / "config.pipeline.json").write_text(
        json.dumps(STOP_CFG), encoding="utf-8"
    )
    pipe = Pipeline(tmp_path)
    try:
        def failed(self, ctx):
            # What ExitAgent.run actually returns when _timed swallowed an
            # exception: a failed verdict carrying the real error.
            return AgentResult(agent="EXIT", ok=False, summary="平仓失败：HTTPError",
                               detail={"failed": True, "error": "HTTPError: 502"})

        monkeypatch.setattr("research.pipeline.agents.ExitAgent.run", failed)
        result = exit_round(pipe)
        assert result["closed"] == []
        assert result["error"] == "HTTPError: 502"
    finally:
        pipe.close()


def test_exit_loop_halts_the_moment_a_stop_file_exists(tmp_path: Path):
    (tmp_path / "user_data").mkdir(parents=True)
    (tmp_path / "user_data" / "config.pipeline.json").write_text(
        json.dumps(STOP_CFG), encoding="utf-8"
    )
    (tmp_path / "STOP").write_text("halt", encoding="utf-8")
    assert run_exit_loop(tmp_path, interval=1) == 0


def test_exit_loop_does_one_pass_then_sleeps(tmp_path: Path, monkeypatch):
    (tmp_path / "user_data").mkdir(parents=True)
    (tmp_path / "user_data" / "config.pipeline.json").write_text(
        json.dumps(STOP_CFG), encoding="utf-8"
    )
    monkeypatch.setattr(mh.Desk, "quote", lambda self, mints: {})
    slept: list[int] = []

    def fake_sleep(seconds: int):
        slept.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        run_exit_loop(tmp_path, interval=7)
    assert slept == [7]
