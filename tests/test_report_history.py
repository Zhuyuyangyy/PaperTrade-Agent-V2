from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from scripts.report import (
    MAX_MARK_AGE_MS,
    read_equity_history,
    starting_cash_for,
    summarize,
)

NOW = int(datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def create_trade_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades ("
        "is_open INTEGER, close_profit_abs REAL, close_date TEXT, pair TEXT, "
        "open_rate REAL, amount REAL, fee_open REAL, fee_close REAL, "
        "is_short INTEGER, realized_profit REAL)"
    )
    conn.executemany("INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def write_mark(mark_dir, pair="BTC/USDT", price=101000.0, observed_at_ms=NOW - 1000, source="ccxt-public:kraken"):
    mark_dir.mkdir(parents=True, exist_ok=True)
    path = mark_dir / f"{pair.replace('/', '_')}.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "pair": pair,
        "source": source,
        "price": price,
        "observed_at_ms": observed_at_ms,
        "exchange_timestamp_ms": observed_at_ms,
    }), encoding="utf-8")


def write_config(path, starting_cash=1200):
    path.write_text(json.dumps({"dry_run_wallet": starting_cash}), encoding="utf-8")


def test_report_marks_open_position_and_reads_starting_cash_from_config(tmp_path):
    db = tmp_path / "dryrun.sqlite"
    create_trade_db(db, [
        (0, 12.5, "2026-10-22 10:00:00", "BTC/USDT", 100000, 0.01, 0.001, 0.001, 0, 0),
        (0, -3.0, "2026-10-22 11:00:00", "ETH/USDT", 3000, 0.1, 0.001, 0.001, 0, 0),
        (1, None, None, "BTC/USDT", 100000, 0.01, 0.001, 0.001, 0, 0),
        (0, 5.0, "2026-10-22 12:00:00", "BTC/USDT", 100000, 0.01, 0.001, 0.001, 0, 0),
    ])
    config = tmp_path / "config.json"
    write_config(config, 1200)
    marks = tmp_path / "marks"
    write_mark(marks)

    result = summarize(db, config, marks, NOW)
    assert result is not None
    assert result["starting_cash_usdt"] == 1200.0
    assert result["starting_cash_source"] == "config"
    assert result["closed_count"] == 3
    assert result["open_count"] == 1
    assert result["realized_usdt"] == 14.5
    assert result["win_count"] == 2
    assert result["loss_count"] == 1
    assert result["win_rate"] == round(2 / 3, 4)
    expected_unrealized = (0.01 * 101000 * (1 - 0.001)) - (0.01 * 100000 * (1 + 0.001))
    assert result["unrealized_usdt"] == round(expected_unrealized, 6)
    assert result["equity_usdt"] == round(1200 + 14.5 + expected_unrealized, 6)
    assert result["equity_complete"] is True
    assert result["unpriced_open_count"] == 0
    assert result["recent_trades"][0]["pair"] == "BTC/USDT"
    assert result["recent_trades"][0]["pnl_usdt"] == 5.0
    assert "equity_history" not in result  # Historical curve must come from sampled snapshots.


def test_equity_is_unavailable_if_any_open_position_mark_is_stale(tmp_path):
    db = tmp_path / "dryrun.sqlite"
    create_trade_db(db, [
        (1, None, None, "BTC/USDT", 100000, 0.01, 0.001, 0.001, 0, 0),
    ])
    config = tmp_path / "config.json"
    write_config(config, 1000)
    marks = tmp_path / "marks"
    write_mark(marks, observed_at_ms=NOW - MAX_MARK_AGE_MS - 1)

    result = summarize(db, config, marks, NOW)
    assert result is not None
    assert result["equity_complete"] is False
    assert result["equity_usdt"] is None
    assert result["unrealized_usdt"] is None
    assert result["unpriced_open_count"] == 1


def test_equity_is_unavailable_if_one_of_multiple_open_positions_is_unmarked(tmp_path):
    db = tmp_path / "dryrun.sqlite"
    create_trade_db(db, [
        (1, None, None, "BTC/USDT", 100000, 0.01, 0.001, 0.001, 0, 0),
        (1, None, None, "ETH/USDT", 3000, 0.1, 0.001, 0.001, 0, 0),
    ])
    config = tmp_path / "config.json"
    write_config(config, 1000)
    marks = tmp_path / "marks"
    write_mark(marks, "BTC/USDT", 101000)

    result = summarize(db, config, marks, NOW)
    assert result is not None
    assert result["open_count"] == 2
    assert result["unpriced_open_count"] == 1
    assert result["equity_usdt"] is None


def test_no_open_positions_can_be_fully_valued_without_market_marks(tmp_path):
    db = tmp_path / "dryrun.sqlite"
    create_trade_db(db, [
        (0, 7.0, "2026-10-22 10:00:00", "BTC/USDT", 100, 1, 0.001, 0.001, 0, 0),
    ])
    config = tmp_path / "config.json"
    write_config(config, 1000)
    result = summarize(db, config, tmp_path / "no-marks", NOW)
    assert result is not None
    assert result["equity_complete"] is True
    assert result["unrealized_usdt"] == 0
    assert result["equity_usdt"] == 1007.0


def test_starting_cash_uses_explicit_fallback_only_when_config_unavailable(tmp_path):
    assert starting_cash_for(tmp_path / "missing.json") == (1000.0, "fallback")
    cfg = tmp_path / "bad.json"
    cfg.write_text('{"dry_run_wallet": "bad"}', encoding="utf-8")
    assert starting_cash_for(cfg) == (1000.0, "fallback")


def test_equity_history_only_accepts_complete_snapshots_for_requested_account(tmp_path):
    journal = tmp_path / "equity_snapshots.jsonl"
    rows = [
        {"schema_version": 1, "kind": "baseline", "sampled_at_ms": NOW, "complete": True, "equity_usdt": 1001.0},
        {"schema_version": 1, "kind": "hybrid", "sampled_at_ms": NOW + 1000, "complete": True, "equity_usdt": 1002.0},
        {"schema_version": 1, "kind": "baseline", "sampled_at_ms": NOW + 2000, "complete": False, "equity_usdt": None},
    ]
    journal.write_text("\n".join(json.dumps(x) for x in rows) + "\nnot-json\n", encoding="utf-8")
    result = read_equity_history(journal, "baseline")
    assert len(result) == 1
    assert result[0]["equity"] == 1001.0


def test_unknown_or_locked_style_schema_fails_closed(tmp_path):
    db = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades (id INTEGER, random_column TEXT)")
    conn.commit()
    conn.close()
    assert summarize(db, now_ms=NOW) is None


def test_mark_is_rejected_when_exchange_timestamp_is_stale_even_if_receipt_is_recent(tmp_path):
    marks = tmp_path / "marks"
    write_mark(marks, observed_at_ms=NOW - 1000)
    path = marks / "BTC_USDT.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["exchange_timestamp_ms"] = NOW - MAX_MARK_AGE_MS - 1000
    path.write_text(json.dumps(data), encoding="utf-8")
    db = tmp_path / "dryrun.sqlite"
    create_trade_db(db, [
        (1, None, None, "BTC/USDT", 100000, 0.01, 0.001, 0.001, 0, 0),
    ])
    config = tmp_path / "config.json"
    write_config(config, 1000)
    result = summarize(db, config, marks, NOW)
    assert result is not None
    assert result["equity_complete"] is False
    assert result["equity_usdt"] is None


def test_missing_open_trade_close_fee_uses_configured_fee_not_zero(tmp_path):
    db = tmp_path / "dryrun.sqlite"
    create_trade_db(db, [
        (1, None, None, "BTC/USDT", 100000, 0.01, 0.001, None, 0, 0),
    ])
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"dry_run_wallet": 1000, "fee": 0.002}), encoding="utf-8")
    marks = tmp_path / "marks"
    write_mark(marks, price=101000)
    result = summarize(db, config, marks, NOW)
    assert result is not None
    expected = 0.01 * 101000 * (1 - 0.002) - 0.01 * 100000 * (1 + 0.001)
    assert result["unrealized_usdt"] == round(expected, 6)
