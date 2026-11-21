"""Sprint 6.1：provider 接入 RunLoop + point-in-time 数据集 + Jev 协议。"""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from research.decision import DatasetRow, RulesProvider, build_rows, purged_split
from research.decision.providers.jev import JevProvider
from research.market.contracts import Candle, TF_1H_MS
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.runtime.loop import LoopConfig, RunLoop
from research.runtime.sources import CandlesSource
from research.strategy.registry import DEFAULT_SPECS

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS
HOUR = TF_1H_MS


def _candles(n: int = 300) -> list[Candle]:
    out, price = [], 100.0
    for i in range(n):
        o = price
        price += 0.2
        out.append(Candle(BASE + i * TF_1H_MS, D(f"{o:.6f}"), D(f"{max(o, price) + 0.2:.6f}"),
                          D(f"{min(o, price) - 0.2:.6f}"), D(f"{price:.6f}"), D("1"), TF_1H_MS))
    return out


def _specs():
    return tuple(replace(s, verification="VERIFIED", verified_by="t")
                 for s in DEFAULT_SPECS if s.strategy_id == "trend_follow")


def test_runloop_writes_provider_log(tmp_path: Path) -> None:
    candles = _candles()
    broker = PaperBroker(tmp_path / "v.sqlite", starting_cash=D("1000"))
    shadow = PaperBroker(tmp_path / "s.sqlite", starting_cash=D("1000"),
                         shadow_mode="shadow_only")
    loop = RunLoop(tmp_path, broker, RiskLimits(), CandlesSource(candles),
                   specs=_specs(), shadow_broker=shadow,
                   providers=[RulesProvider()],
                   providers_log_path=tmp_path / "data" / "providers.jsonl",
                   config=LoopConfig(interval_ms=TF_1H_MS, decision_period_ms=TF_1H_MS,
                                     max_rounds=2))
    start = candles[265].close_ms
    loop.run(start, start + 5 * TF_1H_MS)
    log = tmp_path / "data" / "providers.jsonl"
    rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["decisions"][0]["provider"] == "rules"
    assert rows[0]["snapshot"]["trend"] == "TREND_UP"
    assert rows[0]["completed_at_ms"] > 0


def test_no_providers_means_no_log(tmp_path: Path) -> None:
    candles = _candles()
    broker = PaperBroker(tmp_path / "v.sqlite", starting_cash=D("1000"))
    loop = RunLoop(tmp_path, broker, RiskLimits(), CandlesSource(candles), specs=_specs(),
                   providers=[], config=LoopConfig(interval_ms=TF_1H_MS,
                                                   decision_period_ms=TF_1H_MS,
                                                   max_rounds=1))
    start = candles[265].close_ms
    loop.run(start, start + TF_1H_MS)
    assert not (tmp_path / "data" / "providers.jsonl").exists()


def test_dataset_labels_and_purge(tmp_path: Path) -> None:
    candles = _candles(10)      # 只有 10 小时：24h 标签必须未兑现
    log = tmp_path / "decisions.jsonl"
    entry = {
        "round_ts_ms": candles[2].close_ms,
        "state": {"trend": "TREND_UP", "momentum": "MOMENTUM_UP",
                  "features": {"adx": "30", "rsi": "62", "atr_pct": "0.01"}},
        "intel": None,
    }
    log.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    rows = build_rows(log, candles, (1, 24))
    assert len(rows) == 1
    base = float(candles[2].close)
    future = float(candles[3].close)
    assert abs(rows[0].labels["1h"] - (future / base - 1)) < 1e-9
    assert rows[0].labels["24h"] is None          # 未兑现 -> None，不是 0
    assert rows[0].features["trend_dir"] == 1.0

    many = [DatasetRow(BASE + i * HOUR, {}, {}) for i in range(10)]
    train, test = purged_split(many, train_ratio=0.7, embargo_hours=1)
    assert train and test
    assert train[-1].as_of_ms < test[0].as_of_ms
    assert train[-1].as_of_ms <= many[6].as_of_ms - HOUR


def test_jev_questions_protocol() -> None:
    def fake(url, key, body, timeout):
        assert body["questions"] and "state" in body
        return {"answers": {"trade_or_hold": {"answer": "long", "score": 0.7,
                                              "confidence": 0.6, "reason": "trend"}}}

    from research.decision import MarketSnapshot
    from research.market.contracts import DataQuality, MarketState

    state = MarketState("BTC/USDT", BASE, "TREND_UP", "NORMAL_VOL", "MOMENTUM_UP",
                        "RISK_NORMAL", D("0.7"), "t",
                        data_quality=DataQuality(timeframes_seen=("1h",)))
    d = JevProvider("https://jev", "k", fetch=fake).decide(
        MarketSnapshot(BASE, "BTC/USDT", state))
    assert d.status == "OK" and d.choice == "LONG" and d.confidence == D("0.6")


def test_jev_noul_is_hold_not_direction() -> None:
    def fake(url, key, body, timeout):
        return {"answers": [{"question": "trade_or_hold", "answer": "noul"}]}

    from research.decision import MarketSnapshot
    from research.market.contracts import DataQuality, MarketState

    state = MarketState("BTC/USDT", BASE, "RANGE", "NORMAL_VOL", "FLAT",
                        "RISK_NORMAL", D("0.4"), "t",
                        data_quality=DataQuality(timeframes_seen=("1h",)))
    d = JevProvider("https://jev", "k", fetch=fake).decide(
        MarketSnapshot(BASE, "BTC/USDT", state))
    assert d.status == "OK" and d.choice == "HOLD"