"""V2 引擎测试：三账本、5m 决策、tick 止损、早退、双模型缺省。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from research.decision import Decision
from research.runtime.v2_engine import BOOKS, V2Engine
from research.risk.engine import RiskLimits

D = Decimal
BASE = (1_795_000_000 // 300) * 300
NOW = (BASE + 320 * 300) * 1000


class FakeFeed:
    def __init__(self, price: float = 100.0):
        self.price_value = price
        self.bars = self._bars()

    def _bars(self):
        out, p = [], 100.0
        for i in range(320):
            o = p
            p += 0.05
            out.append({"time": BASE + i * 300, "open": o, "high": p + 0.2,
                        "low": o - 0.2, "close": p, "volume": 1.0})
        return out

    STEPS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

    def series(self, tf: str, limit: int = 500):
        step = self.STEPS.get(tf, 300)
        n = 320 if tf == "5m" else 500
        end_s = (NOW // 1000 // step) * step
        start = end_s - (n - 1) * step
        out, p = [], 100.0
        for i in range(n):
            o = p
            p += 0.05
            out.append({"time": start + i * step, "open": o, "high": p + 0.2,
                        "low": o - 0.2, "close": p, "volume": 1.0})
        return out[-limit:]

    def latest(self, tf: str = "5m"):
        return self.bars[-1]

    def price(self):
        return self.price_value

    def status(self):
        return {"mode": "test"}


class LongProvider:
    name = "deepseek"
    version = "t"

    def available(self) -> bool:
        return True

    def decide(self, snapshot) -> Decision:
        return Decision("deepseek", "t", "OK", "LONG", 0.8, D("0.7"), "fake long", ("e",))


def _engine(tmp_path: Path, deepseek=None, second=None) -> V2Engine:
    eng = V2Engine(tmp_path / "v2", FakeFeed(), RiskLimits(min_volatility_budget_bps=D("0"),
                                                           fee_bps=D("0"),
                                                           slippage_bps=D("0")),
                   deepseek=deepseek, second=second)
    eng.load_history = lambda: None      # 测试不拉真实网络
    return eng


def test_three_books_are_isolated(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=LongProvider())
    e.on_5m_close(NOW)
    for name in BOOKS:
        assert (tmp_path / "v2" / f"{name}.sqlite").is_file()
    assert e.books["rules_5m"]._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] >= 0
    assert e.books["deepseek_5m"]._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert e.books["dual_5m"]._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0  # 第二模型缺失 -> 暂停


def test_prefilter_blocks_when_cost_too_high(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=LongProvider())
    e.limits = RiskLimits(min_volatility_budget_bps=D("1000"), fee_bps=D("50"),
                          slippage_bps=D("50"))
    e.on_5m_close(NOW)
    log = (tmp_path / "v2" / "deepseek_5m_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log[-1])["decision"]["rationale"] == "COST_TOO_HIGH"


def test_tick_stop_closes_position(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=LongProvider())
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    stop = Decimal(pos["stop_price"])
    closed = e.on_tick(float(stop) - 1.0, NOW + 1000)
    assert any(c.reason == "STOP_LOSS" for c in closed)


def test_early_exit_after_two_bars(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=LongProvider())
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    entry = Decimal(pos["entry_price"])
    # 价格下跌但未触止损：两根 5m 后应 EARLY_EXIT
    e.feed.price_value = float(entry) - 0.01
    e.feed.bars[-1]["close"] = float(entry) - 0.01
    e.on_5m_close(NOW + 2 * 300_000)
    closed = e.books["deepseek_5m"]._db.execute(
        "SELECT exit_reason FROM positions WHERE status='closed'").fetchall()
    assert any(r["exit_reason"] == "EARLY_EXIT" for r in closed)


def test_dual_without_second_marks_unavailable(tmp_path: Path) -> None:
    e = _engine(tmp_path, deepseek=LongProvider(), second=None)
    e.on_5m_close(NOW)
    log = (tmp_path / "v2" / "dual_5m_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(log[-1])
    assert row["decision"]["choice"] == "HOLD"
    assert "dual_paused" in row["decision"]["rationale"]

def test_history_refreshes_across_nine_hours(tmp_path: Path) -> None:
    """模拟时钟：9 小时内每 5 分钟刷新一次历史，避免越跑越旧。"""
    e = _engine(tmp_path, deepseek=LongProvider())
    calls = []
    e.load_history = lambda: calls.append(1)
    for minute in range(0, 9 * 60, 5):
        e.on_5m_close(NOW + minute * 60_000)
    assert len(calls) >= 100, f"refreshed {len(calls)} times in 9h"
    assert e._history_ts == NOW + (9 * 60 - 5) * 60_000
