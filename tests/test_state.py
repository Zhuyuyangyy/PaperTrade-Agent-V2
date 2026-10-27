"""The office feed: every book listed, every agent's last move readable."""
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
from scripts.state import AGENT_ORDER, build_state  # noqa: E402


@pytest.fixture()
def house(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime" / "memehunter"
    runtime.mkdir(parents=True)
    conn = mh.connect(runtime / "memehunter.sqlite")
    conn.executescript(
        "CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_ms INTEGER, finished_ms INTEGER, version INTEGER, ok INTEGER);"
        "CREATE TABLE pipeline_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,"
        " seq INTEGER, agent TEXT, ok INTEGER, summary TEXT, produced INTEGER,"
        " rejected INTEGER, elapsed_ms INTEGER, detail TEXT, decided_at_ms INTEGER);"
    )
    conn.execute("INSERT INTO pipeline_runs VALUES (1, 1000, 2000, 1, 1)")
    for seq, agent in enumerate(AGENT_ORDER, start=1):
        conn.execute(
            "INSERT INTO pipeline_steps (run_id, seq, agent, ok, summary, elapsed_ms, decided_at_ms)"
            " VALUES (1,?,?,1,?,100,2000)", (seq, agent, f"{agent} 做了一些事"),
        )
    conn.execute(
        "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd,"
        " entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd,"
        " entry_reason, status, exit_at_ms, exit_reason, exit_fill_price, exit_cost_usd,"
        " pnl_usd, pnl_pct) VALUES (1,'m','AAA',60.0,100.0,1.0,1.0,100.0,35.0,0.6,"
        " 'test','closed',2,'TIMEOUT',1.05,0.6,3.8,3.8)"
    )
    conn.execute("INSERT INTO equity VALUES (2, 900, 103, 1003.0, 1)")
    conn.commit()
    conn.close()
    # A second book with no pipeline tables at all, like the control group.
    mh.connect(runtime / "counterpart.sqlite").close()
    return tmp_path


def test_every_book_is_listed_even_without_a_pipeline(house: Path):
    state = build_state(house)
    labels = {w["label"] for w in state["wallets"]}
    assert labels == {"E 现役", "C 对照组"}


def test_the_feed_carries_each_agents_last_move(house: Path):
    state = build_state(house)
    live = next(w for w in state["wallets"] if w["label"] == "E 现役")
    assert live["run_id"] == 1
    assert set(live["steps"]) == set(AGENT_ORDER)
    assert live["steps"]["FOMO"]["summary"] == "FOMO 做了一些事"
    assert live["steps"]["FOMO"]["ok"] is True


def test_a_book_with_no_pipeline_is_not_an_error(house: Path):
    state = build_state(house)
    control = next(w for w in state["wallets"] if w["label"] == "C 对照组")
    assert control["run_id"] is None
    assert control["steps"] == {}


def test_state_is_json_serialisable(house: Path):
    json.dumps(build_state(house), ensure_ascii=False)
