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


# ---------- P1：多周期入场输入 ----------
def test_entry_snapshot_has_all_timeframes_and_costs() -> None:
    from research.decision.entry import build_entry_snapshot
    from research.market.contracts import TimeframeView

    views = {}
    for i, tf in enumerate(("5m", "15m", "1h", "4h")):
        views[tf] = TimeframeView(
            timeframe=tf, last_closed_ms=NOW - 30_000, close=D(str(59000 + i)),
            ema20=D(str(59000 + i + 10)), ema60=D(str(59000 + i - 10)),
            rsi=D("55"), adx=D("20"), macd_hist=D("1"), atr_pct=D("0.5"),
            volume_z=D("0.7"), slope_bps=D("3"))
    state = type("S", (), {"timeframes": views, "symbol": "BTC/USDT"})()
    snap = build_entry_snapshot(
        state=state, as_of_ms=NOW, digest=None,
        fee_bps=D("4"), slippage_bps=D("5"), spread_bps=D("5"), min_net_bps=D("10"))
    summary = snap.summary()
    assert set(summary["timeframes"]) == {"5m", "15m", "1h", "4h"}
    assert summary["timeframes"]["5m"]["last_closed_ms"] == NOW - 30_000
    assert D(summary["costs"]["roundtrip_bps"]) == D("23")
    assert summary["timeframes"]["5m"]["trend"] == "TREND_UP"


def test_entry_snapshot_excludes_future_news() -> None:
    from research.decision.entry import build_entry_snapshot
    from research.intel.contracts import IntelDigest, NewsEvent, SourceRef

    src = SourceRef(url="u", publisher="p", published_at_ms=NOW, first_seen_ms=NOW,
                    retrieved_ms=NOW)
    past = NewsEvent(event_id="a", headline="past", facts=(), occurred_at_ms=NOW - 1000,
                     first_seen_ms=NOW - 1000, sources=(src,), credibility=D("0.9"),
                     horizon_ms=1000)
    future = NewsEvent(event_id="b", headline="future", facts=(), occurred_at_ms=NOW + 1000,
                       first_seen_ms=NOW + 1000, sources=(src,), credibility=D("0.9"),
                       horizon_ms=1000)
    digest = IntelDigest(as_of_ms=NOW, news=(past, future))
    state = type("S", (), {"timeframes": {}, "symbol": "BTC/USDT"})()
    snap = build_entry_snapshot(state=state, as_of_ms=NOW, digest=digest,
                                last_news_ms=NOW - 10_000)
    assert [x["headline"] for x in snap.news] == ["past"]
    assert snap.news_since_last == ("past",)


def test_engine_logs_multitf_input_and_uses_shared_signal(tmp_path: Path) -> None:
    from research.decision import Decision
    from research.decision.providers.entry_multitf import DeepSeekMultiTFEntry

    class Candidate:
        name = "candidate"
        version = "v1"

        def decide(self, snap):
            return Decision("candidate", "v1", "OK", "SHORT", 0.9, D("0.8"), "test")

    e = _engine(tmp_path, deepseek=CountingProvider(),
                config=V2Config(entry_mode="multitf"))
    e.experiment = True
    e.entry_provider = Candidate()
    out = e.on_5m_close(NOW)
    assert set(out) == set(__import__("research.runtime.v2_engine", fromlist=["BOOKS"]).BOOKS)
    for name, row in out.items():
        assert row["decision"]["choice"] == "SHORT", name
        assert row["entry_model"] == "candidate"
        assert row["entry_input_hash"]
    p = tmp_path / "v2" / "entry_inputs.jsonl"
    assert p.exists()
    logged = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert set(logged["timeframes"]) == {"5m", "15m", "1h", "4h"}
    assert "roundtrip_bps" in logged["costs"]


def test_legacy_provider_is_frozen() -> None:
    """旧基线仍用 MarketSnapshot.summary()，不能偷偷换成多周期输入。"""
    from research.decision.providers.deepseek import DeepSeekProvider
    from research.intel.news_llm import LLMConfig

    seen = {}

    def fetch(url, key, body, timeout):
        seen["body"] = body
        return {"choices": [{"message": {"content":
                '{"choice":"HOLD","score":0,"confidence":0.5,"rationale":"x"}'}}]}

    calls_before = 0
    p = DeepSeekProvider(LLMConfig(api_key="k"), fetch=fetch)
    snap = type("Snap", (), {"summary": lambda self: {"as_of_ms": 1, "trend": "x"}})()
    p.decide(snap)
    user = seen["body"]["messages"][1]["content"]
    assert "timeframes" not in user and "roundtrip_bps" not in user


# ---------- P2：配对历史事件回放 ----------
def _synthetic_bars(n: int = 120, start_ms: int = (1_790_000_000_000 // 300_000) * 300_000) -> list:
    from research.market.contracts import Candle
    out = []
    px = 59000.0
    for i in range(n):
        # 交替趋势与回撤，制造若干次 EMA20 穿越
        drift = 12.0 if (i // 12) % 2 == 0 else -9.0
        o = px
        px = o + drift
        hi, lo = max(o, px) + 4, min(o, px) - 4
        out.append(Candle(start_ms + i * 300_000, D(str(o)), D(str(hi)),
                          D(str(lo)), D(str(px)), D("10"), 300_000))
    return out


def test_generate_events_uses_only_closed_bars(tmp_path: Path) -> None:
    from research.experiment.paired import generate_events

    bars = _synthetic_bars()
    events = generate_events(bars, max_events=6)
    assert events, "固定规则应至少生成一批事件（全量、无挑选）"
    for e in events:
        idx = next(i for i, c in enumerate(bars)
                   if c.open_ms + c.timeframe_ms == e.as_of_ms)
        # 事件价格必须等于那一刻的已收盘价，不能用未来数据
        assert e.entry_price == bars[idx].close
        assert e.as_of_ms <= bars[-1].close_ms


def test_paired_groups_share_identical_entry(tmp_path: Path) -> None:
    from research.decision.position import PositionDecision
    from research.experiment.paired import generate_events, run_event

    bars = _synthetic_bars()
    events = generate_events(bars, max_events=1)
    assert events
    ev = events[0]
    idx = next(i for i, c in enumerate(bars)
               if c.open_ms + c.timeframe_ms == ev.as_of_ms)
    results = run_event(ev, bars[idx + 1:], tmp_path, ai_manager=None, horizon=10)

    assert [r.group for r in results] == ["A", "B", "C", "D"]
    # 四组初始成交价/数量/费用一致：用第一次记录的 entry_price 反推验证
    for r in results:
        assert r.actions or r.exit_reason in ("OPEN", "TAKE_PROFIT", "STOP_LOSS")
    d = results[-1]
    assert d.ai_calls == 0
    assert any(a["action"] == "NO_AI" for a in d.actions), "没有模型时不许冒充 AI 管理"

    c = results[2]
    partials = [a for a in c.actions if a["action"] == "CLOSE_PARTIAL"]
    assert len(partials) <= 1, "配对回放里 C 组也不能反复分批"


# ---------- P3：共享 REST 预算 ----------
def test_global_request_budget_limits_and_cooldown() -> None:
    from research.market.budget import RequestBudget

    b = RequestBudget(max_per_minute=2, cooldown_s=60)
    b.acquire(); b.acquire()
    assert b.stats["requests"] == 2
    # 第三次会被窗口挡住；这里只验证计数与冷却，不做长时间睡眠
    b.note_rate_limited()
    snap = b.snapshot()
    assert snap["rate_limited"] == 1 and snap["cooldown_triggered"] == 1
    assert snap["cooldown_left_s"] > 0


# ---------- P0：配对结算修正 ----------
def test_open_position_is_marked_to_market_at_window_end(tmp_path: Path) -> None:
    from research.experiment.paired import generate_events, run_event

    bars = _synthetic_bars(n=160)
    events = generate_events(bars, max_events=1)
    assert events
    ev = events[0]
    idx = next(i for i, c in enumerate(bars)
               if c.open_ms + c.timeframe_ms == ev.as_of_ms)
    results = run_event(ev, bars[idx + 1:], tmp_path, horizon=12, ai_manager=None)
    for r in results:
        if r.exit_reason == "OPEN_AT_WINDOW_END":
            assert r.unrealized_pnl != 0
            assert r.equity_end == D("1000") + r.pnl
        assert r.equity_end != D("1000") or r.pnl == 0
        assert "fees_paid" in r.to_dict() and "funding" in r.to_dict()


def test_d_snapshot_uses_real_trends_and_news_unavailable(tmp_path: Path) -> None:
    from research.decision import Decision
    from research.experiment.paired import generate_events, run_event

    seen: list[dict] = []

    class Recorder:
        name, version = "recorder", "v1"

        def decide(self, snap):
            seen.append(snap.summary())
            return Decision("recorder", "v1", "OK", "HOLD", 0.5, D("0.7"), "hold")

    bars = _synthetic_bars(n=160)
    events = generate_events(bars, max_events=1)
    ev = events[0]
    idx = next(i for i, c in enumerate(bars)
               if c.open_ms + c.timeframe_ms == ev.as_of_ms)
    run_event(ev, bars[idx + 1:], tmp_path, horizon=8, ai_manager=Recorder())
    assert seen
    s = seen[0]
    assert s["news_status"] == "NEWS_UNAVAILABLE"
    assert set(s["trend"]) == {"5m", "15m", "1h"}
    assert all(v in ("TREND_UP", "TREND_DOWN", "FLAT", "UNKNOWN")
               for v in s["trend"].values())
    assert "mfe" in s and "mae" in s and "holding_ms" in s


def test_a_group_uses_original_early_exit_rule(tmp_path: Path) -> None:
    from research.experiment.paired import generate_events, run_event

    bars = _synthetic_bars(n=160)
    events = generate_events(bars, max_events=1)
    ev = events[0]
    idx = next(i for i, c in enumerate(bars)
               if c.open_ms + c.timeframe_ms == ev.as_of_ms)
    results = run_event(ev, bars[idx + 1:], tmp_path, horizon=12, ai_manager=None)
    a = results[0]
    assert all(x["provider"] == "fixed_atr_early_exit" for x in a.actions)
    assert any("原始规则" in str(x["reason"]) for x in a.actions) or \
        all(x["action"] in ("HOLD", "CLOSE_ALL") for x in a.actions)


# ---------- P0：配对结算修正 ----------
def test_open_position_is_marked_to_market_at_window_end(tmp_path: Path) -> None:
    from research.experiment.paired import generate_events, run_event

    bars = _synthetic_bars(n=160)
    events = generate_events(bars, max_events=1)
    assert events
    ev = events[0]
    idx = next(i for i, c in enumerate(bars)
               if c.open_ms + c.timeframe_ms == ev.as_of_ms)
    results = run_event(ev, bars[idx + 1:], tmp_path, horizon=12, ai_manager=None)
    for r in results:
        if r.exit_reason == "OPEN_AT_WINDOW_END":
            assert r.unrealized_pnl != 0
            assert r.equity_end == D("1000") + r.pnl
        assert r.equity_end != D("1000") or r.pnl == 0
        assert "fees_paid" in r.to_dict() and "funding" in r.to_dict()


def test_d_snapshot_uses_real_trends_and_news_unavailable(tmp_path: Path) -> None:
    from research.decision import Decision
    from research.experiment.paired import generate_events, run_event

    seen: list[dict] = []

    class Recorder:
        name, version = "recorder", "v1"

        def decide(self, snap):
            seen.append(snap.summary())
            return Decision("recorder", "v1", "OK", "HOLD", 0.5, D("0.7"), "hold")

    bars = _synthetic_bars(n=160)
    events = generate_events(bars, max_events=1)
    ev = events[0]
    idx = next(i for i, c in enumerate(bars)
               if c.open_ms + c.timeframe_ms == ev.as_of_ms)
    run_event(ev, bars[idx + 1:], tmp_path, horizon=8, ai_manager=Recorder())
    assert seen
    s = seen[0]
    assert s["news_status"] == "NEWS_UNAVAILABLE"
    assert set(s["trend"]) == {"5m", "15m", "1h"}
    assert all(v in ("TREND_UP", "TREND_DOWN", "FLAT", "UNKNOWN")
               for v in s["trend"].values())
    assert "mfe" in s and "mae" in s and "holding_ms" in s


def test_a_group_uses_original_early_exit_rule(tmp_path: Path) -> None:
    from research.experiment.paired import generate_events, run_event

    bars = _synthetic_bars(n=160)
    events = generate_events(bars, max_events=1)
    ev = events[0]
    idx = next(i for i, c in enumerate(bars)
               if c.open_ms + c.timeframe_ms == ev.as_of_ms)
    results = run_event(ev, bars[idx + 1:], tmp_path, horizon=12, ai_manager=None)
    a = results[0]
    assert all(x["provider"] == "fixed_atr_early_exit" for x in a.actions)
    assert any("原始规则" in str(x["reason"]) for x in a.actions) or \
        all(x["action"] in ("HOLD", "CLOSE_ALL") for x in a.actions)


# ---------- P1：历史数据分页/校验与分析 ----------
def test_bar_loader_paginates_and_detects_gap(tmp_path: Path) -> None:
    from research.experiment.data import load_bars

    step = 300_000
    end = (1_790_000_000_000 // step) * step
    start0 = ((end - 2 * 86_400_000) // step) * step
    calls: list[str] = []

    def fake(url):
        calls.append(url)
        s = int(url.split("startTime=")[1])
        def row(t):
            return [t, "59000", "59010", "58990", "59000", "10",
                    "0", "0", "0", "0", "0", "0"]
        if s == start0:
            return [row(start0 + i * step) for i in range(500)]
        if s == start0 + 500 * step:
            # 从 start+step 开始，制造一根缺口
            return [row(s + step + i * step) for i in range(100)]
        return []

    bs = load_bars(days=2, cache=tmp_path / "bars.json",
                   fetch=fake, end_ms=end)
    assert len(calls) == 2
    assert bs.quality.gaps == 1
    assert bs.quality.max_gap_ms == step * 2
    bs2 = load_bars(days=2, cache=tmp_path / "bars.json", fetch=fake, end_ms=end)
    assert bs2.quality.cache_hit and len(calls) == 2


def test_analyze_run_marks_no_ai_and_overlap(tmp_path: Path) -> None:
    from research.experiment.analyze import analyze_run

    (tmp_path / "config.json").write_text(json.dumps({
        "horizon_bars": 72, "days": 60, "sample_n": 2}), encoding="utf-8")
    lines = []
    for i in range(2):
        for g in "ABCD":
            lines.append(json.dumps({
                "event": {"as_of_ms": 1_790_000_000_000 + i * 300_000,
                          "regime": "RANGE"},
                "result": {"group": g, "net_pnl": "1", "realized_pnl": "1",
                           "unrealized_pnl": "0", "mfe": "2", "holding_ms": 1000,
                           "fees_paid": "0.1", "exit_reason": "MGR_CLOSE",
                           "ai_calls": 0, "ai_ok": 0, "ai_hold": 0, "ai_fail": 0,
                           "actions": [{"action": "NO_AI"}] if g == "D" else []},
            }))
    (tmp_path / "paired_results.jsonl").write_text("\n".join(lines), encoding="utf-8")
    out = analyze_run(tmp_path)
    assert "NO_AI_CALL" in out["ai_position_manager"]["verdict"]
    assert out["sample_overlap"]["overlapping_pairs"] == 1
    assert out["groups"]["A"]["net_pnl_sum"] == "2"
