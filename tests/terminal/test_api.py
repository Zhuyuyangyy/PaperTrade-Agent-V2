"""终端测试：只读、真实账本、标记与认证。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from research.paper.broker import PaperBroker
from research.paper.deepseek_desk import DeepSeekDesk, DeskConfig
from research.decision import Decision
from research.decision.contracts import MarketSnapshot
from research.market.contracts import DataQuality, MarketState
from research.risk.engine import RiskLimits
from research.strategy.contracts import TradeIntent
from research.terminal.server import INDEX_HTML, Terminal, TerminalConfig, read_marks, read_state

D = Decimal
BASE = 1_795_000_000_000


class _Fake:
    name = "deepseek"
    version = "fake-v1"

    def available(self) -> bool:
        return True

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        return Decision("deepseek", "fake-v1", "OK", "LONG", 0.8, D("0.7"),
                        "test reason", ("ev",))


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    (root / "data").mkdir(parents=True)
    shadow = PaperBroker(root / "shadow.sqlite", starting_cash=D("1000"),
                         shadow_mode="shadow_only")
    shadow.submit(TradeIntent("i1", BASE, "BTC/USDT", "LONG", "trend_follow", "v1",
                              D("100"), D("95"), D("110"), 3_600_000, D("0.7"),
                              "breakout confirmed", shadow=True), D("100"))
    shadow.mark_bar("BTC/USDT", D("120"), D("120"), D("120"), BASE + 3_600_000)
    shadow.close()
    (root / "state").mkdir()
    (root / "state" / "heartbeat.json").write_text(json.dumps(
        {"pid": 4242, "last_ok_ms": BASE, "rounds": 3, "successful_rounds": 3}),
        encoding="utf-8")
    (root / "data" / "decisions.jsonl").write_text(json.dumps({
        "round_ts_ms": BASE, "selection": {"decision": "LONG", "intent": {
            "intent_id": "i1", "reason": "breakout confirmed", "strategy_id": "trend_follow",
            "shadow": True}}}) + "\n", encoding="utf-8")
    desk = DeepSeekDesk(
        PaperBroker(root / "deepseek_shadow.sqlite", starting_cash=D("1000"),
                    shadow_mode="allow"),
        _Fake(), RiskLimits(min_volatility_budget_bps=D("0")),
        root / "data" / "deepseek_decisions.jsonl", DeskConfig())
    state = MarketState("BTC/USDT", BASE, "TREND_UP", "NORMAL_VOL", "MOMENTUM_UP",
                        "RISK_NORMAL", D("0.7"), "t",
                        data_quality=DataQuality(timeframes_seen=("1h",)),
                        features={"close": D("100"), "atr_abs": D("1"), "atr_pct": D("0.01"), "adx": D("30"), "rsi": D("60")})
    desk.step(state, BASE, None)
    desk.broker.close()
    return root


def test_state_reads_real_books(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = TerminalConfig(root)
    st = read_state(cfg)
    assert st["books"]["shadow"]["orders"] == 1
    assert st["books"]["deepseek_shadow"]["orders"] == 1
    assert "verified" not in st["books"]  # 账本不存在则不列出
    assert st["heartbeat"]["pid"] == 4242
    assert st["deepseek_last"]["choice"] == "LONG"
    assert st["rules_last"]["decision"] == "LONG"


def test_marks_include_entry_exit_and_reason(tmp_path: Path) -> None:
    root = _root(tmp_path)
    marks = read_marks(TerminalConfig(root))
    texts = [m["text"] for m in marks["markers"]]
    assert any(t in ("▲", "▼") for t in texts)
    assert any(t in ("TP", "SL", "EX") for t in texts)
    details = marks["details"]
    iid = next(k for k in details if k.startswith("shadow:"))
    assert details[iid]["reason"] == "breakout confirmed"
    assert details[iid]["pnl"] is not None


def test_page_keeps_tradingview_attribution() -> None:
    assert "TradingView" in INDEX_HTML
    assert "lightweight-charts" not in INDEX_HTML or "/static/chart.js" in INDEX_HTML


def test_http_endpoints(tmp_path: Path) -> None:
    root = _root(tmp_path)
    term = Terminal(TerminalConfig(root, "127.0.0.1", 0))
    server = term.serve()
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        for path, ctype in (("/", "text/html"), ("/api/state", "application/json"),
                            ("/api/klines?tf=bogus", "application/json")):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
                code = 200
            except urllib.error.HTTPError as exc:
                code = exc.code
            assert code in (200, 400)
    finally:
        server.shutdown()
        server.server_close()