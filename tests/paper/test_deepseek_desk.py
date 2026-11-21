"""DeepSeek 独立盘测试：HOLD 不交易、风控必经、与 Rules 同成本。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from research.decision import Decision
from research.decision.contracts import MarketSnapshot
from research.market.contracts import Candle, DataQuality, MarketState, TF_1H_MS
from research.paper.broker import PaperBroker
from research.paper.deepseek_desk import DeepSeekDesk, DeskConfig
from research.risk.engine import RiskLimits

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


class FakeProvider:
    name = "deepseek"
    version = "fake-v1"

    def __init__(self, choice="HOLD", conf=D("0.7")):
        self.choice = choice
        self.conf = conf

    def available(self) -> bool:
        return True

    def decide(self, snapshot: MarketSnapshot) -> Decision:
        return Decision("deepseek", self.version, "OK", self.choice, 0.8, self.conf,
                        "fake rationale", ("evidence",))


def _state(stale: bool = False) -> MarketState:
    dq = DataQuality(timeframes_seen=("1h",), critical_stale=stale)
    return MarketState("BTC/USDT", BASE, "TREND_UP", "NORMAL_VOL", "MOMENTUM_UP",
                       "RISK_NORMAL", D("0.7"), "t", data_quality=dq,
                       features={"close": D("100"), "atr_abs": D("1"), "adx": D("30"),
                                 "rsi": D("60"), "atr_pct": D("0.01")})


def _bar(i: int = 0) -> Candle:
    return Candle(BASE + i * TF_1H_MS, D("100"), D("101"), D("99"), D("100"),
                  D("1"), TF_1H_MS)


def _desk(tmp_path: Path, provider, limits=None) -> DeepSeekDesk:
    broker = PaperBroker(tmp_path / "ds.sqlite", starting_cash=D("1000"),
                         fee_bps=D("5"), slippage_bps=D("5"),
                         funding_bps_per_8h=D("1"), shadow_mode="allow")
    return DeepSeekDesk(broker, provider, limits or RiskLimits(
        min_volatility_budget_bps=D("0")), tmp_path / "ds.jsonl",
        DeskConfig())


def test_hold_does_not_trade(tmp_path: Path) -> None:
    desk = _desk(tmp_path, FakeProvider("HOLD"))
    entry = desk.step(_state(), BASE, _bar())
    assert entry["fill"] is None
    assert desk.broker.open_positions() == []
    assert len((tmp_path / "ds.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_long_goes_through_risk_and_opens(tmp_path: Path) -> None:
    desk = _desk(tmp_path, FakeProvider("LONG"))
    entry = desk.step(_state(), BASE, _bar())
    assert entry["risk"]["decision"] == "ALLOW"
    assert entry["fill"]["side"] == "LONG"
    pos = desk.broker.open_positions()[0]
    assert Decimal(pos["stop_price"]) < D("100") < Decimal(pos["tp_price"])
    assert pos["strategy_id"] == "deepseek"


def test_stale_data_blocks_entry(tmp_path: Path) -> None:
    desk = _desk(tmp_path, FakeProvider("LONG"))
    entry = desk.step(_state(stale=True), BASE, _bar())
    assert entry["risk"]["decision"] != "ALLOW"
    assert entry["fill"] is None
    assert desk.broker.open_positions() == []


def test_same_snapshot_is_idempotent(tmp_path: Path) -> None:
    desk = _desk(tmp_path, FakeProvider("LONG"))
    desk.step(_state(), BASE, _bar())
    desk.step(_state(), BASE + 1000, _bar())
    assert len(desk.broker.open_positions()) <= 1
    assert desk.broker._db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_costs_match_rules_book(tmp_path: Path) -> None:
    desk = _desk(tmp_path, FakeProvider("LONG"))
    desk.step(_state(), BASE, _bar())
    rules = PaperBroker(tmp_path / "rules.sqlite", starting_cash=D("1000"),
                        fee_bps=D("5"), slippage_bps=D("5"), funding_bps_per_8h=D("1"),
                        shadow_mode="shadow_only")
    from research.strategy.contracts import TradeIntent

    intent = TradeIntent("r1", BASE, "BTC/USDT", "LONG", "trend_follow", "v1",
                         D("100"), D("95"), D("110"), TF_1H_MS, D("0.7"), "r", shadow=True)
    rules.submit(intent, D("100"))
    ds_order = desk.broker._db.execute("SELECT fee, price FROM orders").fetchone()
    r_order = rules._db.execute("SELECT fee, price FROM orders").fetchone()
    assert ds_order["fee"] == r_order["fee"]
    assert abs(Decimal(ds_order["price"]) - Decimal(r_order["price"])) <= D("0.01")