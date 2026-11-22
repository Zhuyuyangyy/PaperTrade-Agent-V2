"""V3 验收审计修复的回归测试：只测行为，不测排行榜。"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path

from research.decision.position import PositionSnapshot, rules_position_action
from research.runtime.v2_engine import V2Config

from .test_v2_engine import NOW, CountingProvider, _engine


def _snap(mark="59000", entry="59100", stop="60000") -> PositionSnapshot:
    return PositionSnapshot(
        as_of_ms=1_795_000_000_000, symbol="BTC/USDT", side="SHORT", qty=D("0.0016"),
        entry_price=D(entry), mark_price=D(mark), notional=D("94"),
        unrealized_net=D("1"), stop_price=D(stop), data_as_of_ms=1_795_000_000_000,
    )


# ---------- P0-1：C 组只分批一次 ----------
def test_rules_partial_only_once_then_trails() -> None:
    s = _snap(mark="58000")          # 1.22R 浮盈
    assert rules_position_action(s).action == "CLOSE_PARTIAL"
    nxt = rules_position_action(s, stage="partial_done")
    assert nxt.action == "MOVE_STOP", nxt.action


def test_partial_stage_persists_and_is_restored(tmp_path: Path) -> None:
    from research.paper.broker import PaperBroker
    from research.strategy.contracts import TradeIntent

    intent = TradeIntent(
        "stage1", NOW, "BTC/USDT", "SHORT", "s", "v1", D("100"), D("102"), D("95"),
        3_600_000, D("0.7"), "test", shadow=True)
    b = PaperBroker(tmp_path / "b.sqlite", shadow_mode="allow")
    f = b.submit(intent, D("100"))
    pos = b.open_positions()[0]
    assert b.set_mgmt_stage(int(pos["id"]), "partial_done") is True
    b2 = PaperBroker(tmp_path / "b.sqlite", shadow_mode="allow")
    assert b2.open_positions()[0]["mgmt_stage"] == "partial_done"


def test_engine_writes_stage_after_partial(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider(),
                config=V2Config(position_manager="rules"))
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    r = abs(D(pos["entry_price"]) - D(pos["stop_price"]))
    sign = D("1") if pos["side"] == "LONG" else D("-1")
    e.feed.price_value = float(D(pos["entry_price"]) + sign * r * D("1.3"))
    out = e.on_5m_close(NOW + 300_000)
    m = ((out.get("deepseek_5m") or {}).get("entry") or {}).get("managed") or {}
    assert m.get("action") == "CLOSE_PARTIAL"
    assert m.get("stage_set") is True
    left = e.books["deepseek_5m"].open_positions()[0]
    assert left["mgmt_stage"] == "partial_done"


# ---------- P0-2/3：报表统一核算 ----------
def test_report_counts_partial_once_and_managed_close(tmp_path: Path) -> None:
    from scripts.experiment_report import book_stats

    root = tmp_path
    db = root / "deepseek_5m.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE positions (id INTEGER PRIMARY KEY, pnl TEXT, exit_reason TEXT,
                                qty TEXT, entry_price TEXT, status TEXT);
        CREATE TABLE position_fills (pnl TEXT, qty TEXT);
        CREATE TABLE orders (notional TEXT);
    """)
    con.execute("INSERT INTO positions VALUES (1,'2','CLOSE_ALL','0.001','59000','closed')")
    con.execute("INSERT INTO position_fills VALUES ('1','0.0005')")
    con.execute("INSERT INTO orders VALUES ('100')")
    con.commit()
    con.close()

    logs = root / "deepseek_5m_decisions.jsonl"
    logs.write_text(json.dumps({"entry": {"fill": {"notional": "100"}, "managed": {
        "action": "CLOSE_ALL", "executed": True, "ai_status": "OK"}}},
        ensure_ascii=False) + "\n", encoding="utf-8")

    r = book_stats(root, "deepseek_5m")
    assert D(r["realized_closed"]) == D("2")
    assert D(r["realized_partial"]) == D("1")
    assert D(r["realized_net"]) == D("3"), "部分平仓不能算两遍"
    assert r["managed_close_all"] == 1
    assert r["closed_trades_db"] == 1 and r["partial_fills"] == 1
    assert r["ai_status"].get("OK") == 1


# ---------- P1-4/5：快照与 AI 标注 ----------
def test_snapshot_has_real_trend_mfe_mae_and_news(tmp_path: Path) -> None:
    from research.intel.contracts import IntelDigest, NewsEvent, SourceRef

    e = _engine(tmp_path, deepseek=CountingProvider(),
                config=V2Config(position_manager="rules"))
    e.on_5m_close(NOW)
    src = SourceRef(url="https://example.test", publisher="test",
                    published_at_ms=NOW - 10_000, first_seen_ms=NOW - 10_000,
                    retrieved_ms=NOW)
    news = NewsEvent(event_id="n1", headline="BTC ETF outflow", facts=(),
                     occurred_at_ms=NOW - 10_000, first_seen_ms=NOW - 10_000,
                     sources=(src,), credibility=D("0.8"), horizon_ms=3_600_000)
    digest = IntelDigest(as_of_ms=NOW, news=(news,), markets=(), wallets=(), warnings=())
    e.position_ai = None
    pos = e.books["deepseek_5m"].open_positions()[0]
    r = abs(D(pos["entry_price"]) - D(pos["stop_price"]))
    sign = D("1") if pos["side"] == "LONG" else D("-1")
    e.feed.price_value = float(D(pos["entry_price"]) + sign * r * D("1.5"))
    out = e.on_5m_close(NOW + 300_000, bar_open_ms=None)
    # 直接用 digest 注入：on_5m_close 的 intel_fn 是 fixture 的，这里单独验 helper
    delta = e._news_delta(digest, NOW, NOW - 60_000)
    assert delta == ("BTC ETF outflow",)
    m = ((out.get("deepseek_5m") or {}).get("entry") or {}).get("managed") or {}
    snap = m.get("snapshot") or {}
    trend = snap.get("trend") or {}
    assert trend.get("1h") in ("TREND_UP", "TREND_DOWN", "FLAT", "UNKNOWN")
    assert snap.get("mfe") and snap.get("mae")
    assert snap.get("entry_reason") is not None
    assert snap.get("risk_budget_left") is not None


def test_ai_status_recorded_and_fallback_marked(tmp_path: Path) -> None:
    from research.decision.position import PositionDecision

    class OkAI:
        name = "stub_ai"

        def decide(self, snap):
            return PositionDecision("stub_ai", "v1", "HOLD", "keep",
                                    D("0.7"), data_as_of_ms=snap.data_as_of_ms)

    for provider, expected in ((OkAI(), "OK"), (None, "NO_PROVIDER")):
        e = _engine(tmp_path / expected, deepseek=CountingProvider(),
                    config=V2Config(position_manager="off", cooldown_bars=0))
        # 只让一本账启用 AI，否则其它账也会消费同一个 stub
        e.experiment = True
        e.book_configs = {"deepseek_5m": V2Config(position_manager="ai", cooldown_bars=0)}
        e.position_ai = provider
        e.on_5m_close(NOW)
        pos = e.books["deepseek_5m"].open_positions()[0]
        r = abs(D(pos["entry_price"]) - D(pos["stop_price"]))
        sign = D("1") if pos["side"] == "LONG" else D("-1")
        e.feed.price_value = float(D(pos["entry_price"]) + sign * r * D("0.05"))
        out = e.on_5m_close(NOW + 300_000)
        m = ((out.get("deepseek_5m") or {}).get("entry") or {}).get("managed") or {}
        assert m.get("ai_status") == expected, m
        assert m.get("ai_fallback") is (expected != "OK")
        assert e.position_ai_stats["calls"] == (1 if provider else 0)


# ---------- P1-6：数据质量 ----------
def test_stale_bar_is_not_reprocessed(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.on_5m_close(NOW)
    before = e.data_stats["stale_skips"]
    e.feed.seeded_until_ms = NOW               # 行情冻结
    a = e.step_once(NOW + 25 * 60_000)
    mid = e.data_stats["stale_skips"]
    b = e.step_once(NOW + 25 * 60_000 + 1000)  # 同一根 K 线，下一秒
    assert e.data_stats["stale_skips"] == mid == before + 1, "同一根 K 线只应跳过一次"
    assert b in ({}, None), "第二次轮询不应重新产生决策"


def test_418_triggers_rest_cooldown(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=CountingProvider())
    e.load_history = lambda as_of_ms=None: e.errors.append(
        "history: SourceError: HTTP Error 418: I'm a teapot") or False
    e._history_ts = 0
    e._rest_cooldown_until = 0
    r1 = e._refresh_history(NOW)
    assert r1["ok"] is False and r1["rate_limited"] is True
    assert e.data_stats["rate_limited"] == 1
    assert e._rest_cooldown_until > NOW
    r2 = e._refresh_history(NOW + 1000)
    assert r2["attempted"] is False and "rest_cooldown_left_ms" in r2
    assert e.data_stats["refresh_attempts"] == 1
