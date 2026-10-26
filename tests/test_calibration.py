"""JUDGE calibration: bucketed forward returns, no future reads, no fake n."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.replay.calibration import (  # noqa: E402
    _spearman,
    format_calibration,
    judge_calibration,
)
from research.replay.labels import ensure_schema  # noqa: E402

T0 = 1_700_000_000_000
MIN = 60_000


@pytest.fixture()
def conn():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE candidates (id INTEGER PRIMARY KEY, scan_id INTEGER, scanned_at_ms INTEGER,
            mint TEXT, symbol TEXT, price_usd REAL, liquidity_usd REAL, fdv_usd REAL,
            organic_score REAL, organic_label TEXT, holder_count INTEGER, top_holders_pct REAL,
            chg_5m REAL, chg_1h REAL, chg_24h REAL, organic_buy_5m REAL, organic_sell_5m REAL,
            net_buyers_5m INTEGER, vol_1h REAL, score REAL, eligible INTEGER, reject_reason TEXT);
        CREATE TABLE llm_judgments (id INTEGER PRIMARY KEY, candidate_id INTEGER, symbol TEXT,
            model TEXT, prompt_hash TEXT, response_json TEXT, score INTEGER, flags TEXT,
            reason TEXT, latency_ms INTEGER, ok INTEGER, judged_at_ms INTEGER);
        """
    )
    ensure_schema(db)
    return db


def _candidate(cid: int, symbol: str, price: float, at_ms: int) -> None:
    pass


def test_spearman_is_one_for_a_perfect_ranking() -> None:
    assert _spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0


def test_spearman_is_minus_one_for_an_inverted_ranking() -> None:
    assert _spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0


def test_spearman_needs_at_least_three_points() -> None:
    assert _spearman([1, 2], [3, 4]) is None


def test_high_scores_should_show_higher_returns(conn: sqlite3.Connection) -> None:
    # Six candidates: the three the model liked are up, the three it disliked are down.
    for i in range(6):
        high = i < 3
        conn.execute(
            "INSERT INTO candidates (id, scan_id, scanned_at_ms, mint, symbol, price_usd,"
            " liquidity_usd, fdv_usd, organic_score, organic_label, holder_count, top_holders_pct,"
            " chg_5m, chg_1h, chg_24h, organic_buy_5m, organic_sell_5m, net_buyers_5m, vol_1h,"
            " score, eligible, reject_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, i + 1, T0, f"m{i}", f"SYM{i}", 1.0, 1e5, 1e6, 60.0, "good", 5000, 10.0,
             0, 0, 0, 1, 1, 10, 1e5, 60.0, 1, None),
        )
        conn.execute(
            "INSERT INTO candidate_marks (candidate_id, marked_at_ms, price_usd, source)"
            " VALUES (?,?,?, 'test')",
            (i + 1, T0 + 62 * MIN, 1.10 if high else 0.90),
        )
        conn.execute(
            "INSERT INTO llm_judgments (candidate_id, symbol, model, prompt_hash, response_json,"
            " score, flags, reason, latency_ms, ok, judged_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (i + 1, f"SYM{i}", "m", "h", "{}", 75 if high else 25, "[]", "r", 100, 1, T0),
        )
    conn.commit()
    report = judge_calibration(conn, 60)
    assert report["samples"] == 6
    assert report["spearman"] == 1.0
    bands = {b["band"]: b for b in report["bands"]}
    assert bands["60-80"]["mean_return_pct"] > bands["20-40"]["mean_return_pct"]
    assert bands["60-80"]["hit_rate_pct"] == 100.0
    assert "有区分度" in report["verdict"]


def test_a_judgement_without_a_forward_price_is_not_counted(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO candidates (id, scan_id, scanned_at_ms, mint, symbol, price_usd,"
        " liquidity_usd, fdv_usd, organic_score, organic_label, holder_count, top_holders_pct,"
        " chg_5m, chg_1h, chg_24h, organic_buy_5m, organic_sell_5m, net_buyers_5m, vol_1h,"
        " score, eligible, reject_reason) VALUES (1,1,?, 'm','S',1.0,1e5,1e6,60,'good',5000,10,"
        "0,0,0,1,1,10,1e5,60,1,NULL)",
        (T0,),
    )
    conn.execute(
        "INSERT INTO llm_judgments (candidate_id, symbol, model, prompt_hash, response_json,"
        " score, flags, reason, latency_ms, ok, judged_at_ms) VALUES (1,'S','m','h','{}',80,'[]','r',100,1,?)",
        (T0,),
    )
    conn.commit()
    report = judge_calibration(conn, 60)
    assert report["samples"] == 0
    assert "样本不足" in report["verdict"]


def test_failed_judgements_are_excluded(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO llm_judgments (candidate_id, symbol, model, prompt_hash, response_json,"
        " score, flags, reason, latency_ms, ok, judged_at_ms) VALUES (1,'S','m','h','{}',90,'[]','r',100,0,?)",
        (T0,),
    )
    conn.commit()
    assert judge_calibration(conn, 60)["samples"] == 0


def test_report_says_so_when_there_is_nothing_to_say(conn: sqlite3.Connection) -> None:
    text = format_calibration(judge_calibration(conn, 60))
    assert "n=0" in text and "样本不足" in text
