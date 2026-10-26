from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil

import pytest

from scripts.dashboard import build_html, build_state
from scripts import dashboard as dashboard_module


@pytest.fixture
def empty_runtime(tmp_path, monkeypatch):
    """Isolate every dashboard path so "no ledger" assertions cannot depend on the stack.

    Without this the suite only passes while the containers are stopped, which is the
    wrong way round: a running stack must not turn a passing suite red.
    """
    real_templates = dashboard_module.ROOT / "templates"
    shutil.copytree(real_templates, tmp_path / "templates")
    (tmp_path / "user_data" / "marks").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dashboard_module, "ROOT", tmp_path)
    monkeypatch.setattr(dashboard_module, "MARKS_DIR", tmp_path / "user_data" / "marks")
    monkeypatch.setattr(dashboard_module, "SNAPSHOT_JOURNAL", tmp_path / "data" / "equity_snapshots.jsonl")
    return tmp_path


def test_dashboard_uses_clear_light_trading_console_layout(empty_runtime):
    page = build_html()
    assert "PaperTrade Agent" in page
    assert "Balance History" in page
    assert "Agent Activity Log" in page
    assert "A/B Experiment: Baseline vs Agent-Assisted" in page
    assert '<option value="ledger">账本状态</option>' in page
    assert "Market Radar" in page and "Risk Guard" in page and "Trade Journal" in page
    assert "Fast Experiment" in page and "15m" in page
    assert "NO REAL ORDERS" in page
    assert "含浮动盈亏" in page
    assert "权益快照" in page
    assert "https://" not in page
    assert "<script src=" not in page
    assert "<link rel=\"stylesheet\" href=\"http" not in page


def test_state_uses_no_fabricated_account_values_when_ledgers_are_missing(empty_runtime):
    state = build_state(datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc))
    assert state["paper_only"] is True
    assert state["has_live_data"] is False
    for account in state["accounts"].values():
        assert account["has_ledger"] is False
        assert account["realized_usdt"] is None
        assert account["equity_usdt"] is None
        assert account["equity_complete"] is False
        assert account["closed_count"] is None
        assert account["open_count"] is None
        assert account["equity_history"] == []
        assert account["recent_trades"] == []
    for signal in state["signals"].values():
        assert signal["valid"] is False
        assert signal["action"] is None


def test_embedded_state_is_valid_json_and_has_no_external_runtime_dependencies(empty_runtime):
    page = build_html()
    prefix = '<script id="state-data" type="application/json">'
    payload = page.split(prefix, 1)[1].split("</script>", 1)[0]
    state = json.loads(payload)
    assert state["paper_only"] is True
    assert state["starting_cash_usdt"] == 1000
    assert state["starting_cash_total_usdt"] == 3000
    assert state["equity_total_usdt"] is None
    assert 'id="kpi-pnl">--<' in page
    assert 'id="kpi-win">--<' in page


def test_dashboard_uses_marked_equity_and_sampled_history(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path
    from scripts import dashboard

    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "MARKS_DIR", tmp_path / "user_data" / "marks")
    monkeypatch.setattr(dashboard, "SNAPSHOT_JOURNAL", tmp_path / "data" / "equity_snapshots.jsonl")
    (tmp_path / "user_data").mkdir()
    (tmp_path / "runtime" / "baseline").mkdir(parents=True)
    (tmp_path / "runtime" / "hybrid").mkdir(parents=True)
    (tmp_path / "runtime" / "fast").mkdir(parents=True)
    now = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    for kind, pnl in (("baseline", 2.0), ("hybrid", 3.0), ("fast", 1.0)):
        (tmp_path / "user_data" / f"config.{kind}.json").write_text(
            json.dumps({"dry_run_wallet": 1000}), encoding="utf-8"
        )
        db = tmp_path / "runtime" / kind / f"{kind}.dryrun.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trades (is_open INTEGER, close_profit_abs REAL, close_date TEXT, pair TEXT)")
        conn.execute("INSERT INTO trades VALUES (0, ?, '2026-10-23 23:00:00', 'BTC/USDT')", (pnl,))
        conn.commit()
        conn.close()
    journal = tmp_path / "data" / "equity_snapshots.jsonl"
    journal.parent.mkdir()
    journal.write_text("\n".join(json.dumps({
        "schema_version": 1,
        "kind": kind,
        "sampled_at_ms": now_ms,
        "complete": True,
        "equity_usdt": 1000 + pnl,
        "realized_usdt": pnl,
        "unrealized_usdt": 0.0,
    }) for kind, pnl in (("baseline", 2.0), ("hybrid", 3.0), ("fast", 1.0))) + "\n", encoding="utf-8")

    state = dashboard.build_state(now)
    assert state["equity_complete"] is True
    assert state["equity_total_usdt"] == 3006.0
    assert state["equity_total_change_usdt"] == 6.0
    assert state["accounts"]["baseline"]["equity_history"][0]["equity"] == 1002.0
    assert state["accounts"]["hybrid"]["equity_history"][0]["equity"] == 1003.0
    assert state["accounts"]["fast"]["equity_history"][0]["equity"] == 1001.0


def test_fast_heartbeat_and_marks_are_validated_for_live_activity(tmp_path, monkeypatch):
    import json as json_module
    from scripts import dashboard

    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "MARKS_DIR", tmp_path / "user_data" / "marks")
    monkeypatch.setattr(dashboard, "SNAPSHOT_JOURNAL", tmp_path / "data" / "equity_snapshots.jsonl")
    (tmp_path / "runtime" / "fast").mkdir(parents=True)
    (tmp_path / "user_data" / "marks").mkdir(parents=True)
    now = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)
    stamp = int(now.timestamp() * 1000)
    (tmp_path / "runtime" / "fast" / "fast_heartbeat.json").write_text(json_module.dumps({
        "schema_version": 1, "source": "PaperFast", "mode": "dry_run", "status": "running",
        "timeframe": "15m", "last_loop_at_ms": stamp - 5000,
    }), encoding="utf-8")
    (tmp_path / "user_data" / "marks" / "BTC_USDT.json").write_text(json_module.dumps({
        "schema_version": 1, "pair": "BTC/USDT", "source": "ccxt-public:kraken", "price": 81000.0,
        "observed_at_ms": stamp - 2000, "exchange_timestamp_ms": stamp - 2000,
    }), encoding="utf-8")
    state = dashboard.build_state(now)
    assert state["fast_status"]["valid"] is True
    assert state["fast_status"]["age_seconds"] == 5
    assert state["marks"]["BTC/USDT"]["price"] == 81000.0
    assert state["has_live_data"] is True
    stale = dashboard.fast_heartbeat_snapshot(stamp + 61_000)
    assert stale["valid"] is False
    assert stale["status"] == "心跳过期或未通过校验"


def test_account_activity_reports_last_fill_locks_and_next_candle_boundary(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path
    from scripts import dashboard

    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    now = datetime(2026, 10, 24, 0, 7, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    runtime = tmp_path / "runtime" / "baseline"
    runtime.mkdir(parents=True)
    db = runtime / "baseline.dryrun.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE orders (order_filled_date TEXT, ft_order_side TEXT, ft_pair TEXT)")
    conn.execute("CREATE TABLE pairlocks (pair TEXT, reason TEXT, lock_end_time TEXT, active INTEGER)")
    conn.execute("INSERT INTO orders VALUES (?, ?, ?)", ("2026-10-23 23:55:00", "buy", "BTC/USDT"))
    conn.execute("INSERT INTO pairlocks VALUES (?, ?, ?, ?)", ("ETH/USDT", "CooldownPeriod", "2026-10-24 00:30:00", 1))
    conn.execute("INSERT INTO pairlocks VALUES (?, ?, ?, ?)", ("BTC/USDT", "expired-lock", "2026-10-24 00:05:00", 1))
    conn.commit()
    conn.close()

    (runtime / "strategy_heartbeat.json").write_text(json.dumps({
        "schema_version": 1, "source": "PaperBaseline", "mode": "dry_run", "status": "running",
        "timeframe": "1h", "last_loop_at_ms": now_ms - 4000,
    }), encoding="utf-8")
    (runtime / "strategy_status_BTC_USDT.json").write_text(json.dumps({
        "schema_version": 1, "strategy": "PaperBaseline", "mode": "dry_run", "pair": "BTC/USDT",
        "timeframe": "1h", "action": "HOLD", "reason": "SMA20 尚未高于 SMA50",
        "candle_open_ms": now_ms - 3600000, "observed_at_ms": now_ms - 3000,
    }), encoding="utf-8")

    activity = dashboard.account_activity_snapshot("baseline", db, now_ms)
    assert activity["heartbeat"]["valid"] is True
    assert activity["heartbeat"]["age_seconds"] == 4
    assert activity["last_fill_pair"] == "BTC/USDT"
    assert activity["last_fill_side"] == "buy"
    assert activity["last_fill_age_seconds"] == 720
    assert activity["lock_data_available"] is True
    assert activity["lock_status"] == "1 个活跃锁"
    assert [lock["pair"] for lock in activity["active_locks"]] == ["ETH/USDT"]
    assert activity["next_evaluation_at_ms"] == int(datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert activity["seconds_to_next_evaluation"] == 3180
    assert activity["pair_statuses"]["BTC/USDT"]["valid"] is True
    assert activity["pair_statuses"]["BTC/USDT"]["action"] == "HOLD"


def test_fast_activity_uses_15m_boundary_and_unknown_lock_state_when_db_missing(tmp_path, monkeypatch):
    from scripts import dashboard

    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    now = datetime(2026, 10, 24, 0, 7, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    db = tmp_path / "runtime" / "fast" / "fast.dryrun.sqlite"
    activity = dashboard.account_activity_snapshot("fast", db, now_ms)
    assert activity["timeframe"] == "15m"
    assert activity["next_evaluation_at_ms"] == int(datetime(2026, 10, 24, 0, 15, tzinfo=timezone.utc).timestamp() * 1000)
    assert activity["seconds_to_next_evaluation"] == 480
    assert activity["last_fill_at_ms"] is None
    assert activity["last_fill_status"] == "账本不可读"
    assert activity["lock_data_available"] is False
    assert activity["lock_status"] == "账本不可读"


def test_dashboard_renders_trade_status_panel_without_fake_activity():
    page = build_html()
    assert "Why No Trade?" in page
    assert 'id="trade-status-grid"' in page
    assert "下次策略评估" in page
    assert "不预测信号" in page
    assert "strategy_status_BTC_USDT.json" not in page  # data paths stay server-side
    assert "BUY" not in page or "买入候选" in page  # no synthetic fills or P&L in UI text
