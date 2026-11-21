"""实时行情聚合与权益口径测试。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research.paper.broker import PaperBroker
from research.runtime.metrics import summarize
from research.strategy.contracts import TradeIntent
from research.terminal.feed import BinanceFeed

D = Decimal
BASE = (1_795_000_000 // 3600) * 3600


def _feed(with_base: bool = False) -> BinanceFeed:
    f = BinanceFeed()
    f._bars = [{"time": BASE + i * 60, "open": 100 + i, "high": 101 + i,
                "low": 99 + i, "close": 100.5 + i, "volume": 1.0} for i in range(180)]
    if with_base:
        f._base = {"1h": [{"time": BASE - 2 * 3600, "open": 90, "high": 95, "low": 89,
                           "close": 94, "volume": 10},
                          {"time": BASE - 3600, "open": 94, "high": 99, "low": 93,
                           "close": 98, "volume": 10}]}
    return f


def test_1m_to_5m_and_1h_aggregation() -> None:
    f = _feed()
    m5 = f.series("5m")
    assert m5 and m5[0]["open"] == 100 and m5[0]["close"] == 104.5
    assert m5[0]["high"] == 105 and m5[0]["low"] == 99
    h1 = f.series("1h")
    assert h1[0]["open"] == 100 and h1[0]["high"] == 160 and h1[0]["low"] == 99


def test_base_history_merges_before_aggregated_bars() -> None:
    f = _feed(with_base=True)
    h1 = f.series("1h")
    assert h1[0]["time"] == BASE - 2 * 3600
    assert h1[-1]["time"] >= BASE


def test_equity_includes_margin_and_unrealized(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         fee_bps=D("0"), slippage_bps=D("0"), shadow_mode="shadow_only")
    broker.submit(TradeIntent("i", 1, "BTC/USDT", "LONG", "t", "v1",
                              D("100"), D("95"), D("110"), 3_600_000, D("0.5"),
                              "r", shadow=True), D("100"))
    s = summarize(broker, prices={"BTC/USDT": D("110")})
    assert D(s["cash"]) == D("900")
    assert D(s["margin_used"]) == D("100")
    assert D(s["unrealized_pnl"]) == D("10")
    assert D(s["equity"]) == D("1010")   # 不是 910