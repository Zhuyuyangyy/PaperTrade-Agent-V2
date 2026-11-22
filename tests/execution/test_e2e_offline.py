"""端到端离线验证：引擎 → 适配器 → 假交易所，全链路零网络。

证明三件事：
  1. V2 决策产生意图后，镜像路径能走完 开仓 → 保护单 → 平仓；
  2. 条件单一律走 algo 通道（不碰 /fapi/v1/order），且带 reduceOnly；
  3. 没有凭据时一个订单都不发。
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from research.execution.adapter import DemoExecutionAdapter, SymbolFilters
from research.runtime.v2_engine import V2Engine
from research.risk.engine import RiskLimits

from tests.execution.test_adapter import EXCHANGE_INFO, FakeClient  # noqa: F401 - 复用替身

D = Decimal
BASE = (1_795_000_000 // 300) * 300
NOW = (BASE + 320 * 300) * 1000


class Feed:
    STEPS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

    def __init__(self, price: float = 100.0):
        self.price_value = price

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

    def base_series(self, tf: str):
        return self.series(tf)

    def latest(self, tf: str = "5m"):
        return self.series(tf, 2)[-1]

    def price(self):
        return self.price_value

    def status(self):
        return {"mode": "e2e"}


class AlwaysLong:
    name, version = "deepseek", "e2e"

    def available(self) -> bool:
        return True

    def decide(self, snapshot):
        from research.decision import Decision
        return Decision("deepseek", "e2e", "OK", "LONG", 0.9, D("0.8"), "e2e long", ("x",))


def _wired(tmp_path: Path, client=None) -> tuple[V2Engine, DemoExecutionAdapter]:
    client = client or FakeClient()
    adapter = DemoExecutionAdapter(client, symbol="BTCUSDT", root=tmp_path / "ex",
                                   poll_s=0.0, poll_tries=1)
    adapter.filters = SymbolFilters.from_exchange_info(EXCHANGE_INFO, "BTCUSDT")
    engine = V2Engine(tmp_path / "v2", Feed(),
                      RiskLimits(min_volatility_budget_bps=D("0"), fee_bps=D("0"),
                                 slippage_bps=D("0")),
                      deepseek=AlwaysLong(), executor=adapter, demo_book="deepseek_5m")
    engine.load_history = lambda as_of_ms=None: True
    return engine, adapter


def test_full_round_trip_open_and_close(tmp_path: Path) -> None:
    engine, adapter = _wired(tmp_path)
    engine.on_5m_close(NOW)

    # 本地账本有一笔，交易所镜像完成（开仓 + 止损 + 止盈）
    assert len(engine.books["deepseek_5m"].open_positions()) == 1
    plain = adapter.client.plain
    algo = adapter.client.algo
    assert len(plain) == 1 and plain[0]["type"] == "MARKET"
    assert {a["type"] for a in algo} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert all(a["reduceOnly"] is True for a in algo)
    # 止损方向必须与持仓相反
    assert plain[0]["side"] == "BUY"
    assert {a["side"] for a in algo} == {"SELL"}

    # 价格打穿止损 → 本地平仓 + 交易所撤条件单 + 市价反向平仓
    pos = engine.books["deepseek_5m"].open_positions()[0]
    engine.on_tick(float(Decimal(pos["stop_price"])) - 1.0, NOW + 1000)
    assert not engine.books["deepseek_5m"].open_positions()
    assert len(adapter.client.cancels) == 2
    assert adapter.client.plain[-1]["reduceOnly"] is True
    assert adapter.client.plain[-1]["side"] == "SELL"


def test_stop_market_never_uses_legacy_endpoint_in_e2e(tmp_path: Path) -> None:
    """-4120 的直接回归：全链路里条件单只能出现在 algo 通道。"""
    engine, adapter = _wired(tmp_path)
    engine.on_5m_close(NOW)
    assert all(a["type"] != "MARKET" for a in adapter.client.algo)
    assert all(p["type"] == "MARKET" for p in adapter.client.plain)


def test_no_credentials_means_zero_exchange_orders(tmp_path: Path) -> None:
    class NoCreds(FakeClient):
        def available(self):
            return False

    client = NoCreds()
    engine, adapter = _wired(tmp_path, client)
    engine.on_5m_close(NOW)
    assert engine.books["deepseek_5m"].open_positions(), "本地虚拟盘照常跑"
    assert not client.plain and not client.algo, "没凭据不许发任何订单"
    assert adapter.errors or any("DEMO_UNAVAILABLE" in e for e in engine.errors)


def test_decisions_log_records_intel_and_bar(tmp_path: Path) -> None:
    engine, _ = _wired(tmp_path)
    engine.on_5m_close(NOW)
    row = json.loads((tmp_path / "v2" / "deepseek_5m_decisions.jsonl")
                     .read_text(encoding="utf-8").splitlines()[-1])
    assert "closed_bar_ms" in row and row["closed_bar_ms"] <= NOW
    assert row["intel"]["status"] == "DISABLED"
    assert row["history_refresh"]["ok"] is True