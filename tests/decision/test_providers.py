"""DecisionProvider 测试：规则/DeepSeek/Jev/线性模型，全部离线。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from research.decision import (
    DeepSeekProvider,
    JevProvider,
    LinearProvider,
    LogisticModel,
    MarketSnapshot,
    RulesProvider,
    consensus,
    flatten,
    run,
    train,
)
from research.intel.features import IntelFeatures
from research.intel.news_llm import LLMConfig
from research.market.contracts import DataQuality, MarketState

D = Decimal
NOW = 1_795_000_000_000


def _state(trend="TREND_UP", momentum="MOMENTUM_UP") -> MarketState:
    dq = DataQuality(timeframes_seen=("1h",))
    return MarketState("BTC/USDT", NOW, trend, "NORMAL_VOL", momentum, "RISK_NORMAL",
                       D("0.7"), "test", data_quality=dq,
                       features={"close": D("100"), "atr_abs": D("1"), "adx": D("30"),
                                 "rsi": D("62"), "atr_pct": D("0.012"), "volume_z": D("1.5"),
                                 "ema20": D("101"), "ema60": D("100.5"), "ema200": D("100")})


def _snapshot(intel=None) -> MarketSnapshot:
    return MarketSnapshot(NOW, "BTC/USDT", _state(), intel)


def test_rules_provider_matches_registry() -> None:
    d = RulesProvider().decide(_snapshot())
    assert d.status == "OK" and d.choice == "LONG" and d.score > 0
    assert d.evidence


def test_deepseek_unavailable_without_key() -> None:
    d = DeepSeekProvider(LLMConfig(api_key="")).decide(_snapshot())
    assert d.status == "UNAVAILABLE" and d.choice == "HOLD"


def test_deepseek_parses_typed_json() -> None:
    def fake(url, key, body, timeout):
        return {"choices": [{"message": {"content": '{"choice":"SHORT","score":-0.8,'
                                            '"confidence":0.7,"rationale":"ETF outflows",'
                                            '"evidence":["etf"]}'}}]}

    d = DeepSeekProvider(LLMConfig(api_key="k"), fetch=fake).decide(_snapshot())
    assert d.status == "OK" and d.choice == "SHORT" and d.confidence == D("0.7")


def test_deepseek_bad_json_degrades_to_error() -> None:
    def fake(url, key, body, timeout):
        return {"choices": [{"message": {"content": "{not json"}}]}

    d = DeepSeekProvider(LLMConfig(api_key="k"), fetch=fake).decide(_snapshot())
    assert d.status == "ERROR" and d.choice == "HOLD"


def test_jev_unavailable_without_endpoint() -> None:
    d = JevProvider().decide(_snapshot())
    assert d.status == "UNAVAILABLE" and d.choice == "HOLD"


def test_jev_parses_typed_output_and_noul() -> None:
    def fake(url, key, body, timeout):
        return {"output": {"choice": "LONG", "score": 0.6, "confidence": 0.55,
                           "rationale": "trend", "evidence": ["ema"]}}

    d = JevProvider("https://jev", "k", fetch=fake).decide(_snapshot())
    assert d.status == "OK" and d.choice == "LONG"

    def noul(url, key, body, timeout):
        return {"output": {"choice": "NOUL", "score": 0}}

    d2 = JevProvider("https://jev", "k", fetch=noul).decide(_snapshot())
    assert d2.status == "OK" and d2.choice == "HOLD"


def test_linear_requires_training_then_predicts() -> None:
    assert LinearProvider().decide(_snapshot()).status == "UNAVAILABLE"

    samples, labels = [], []
    for i in range(60):
        up = i % 2 == 0
        samples.append({"trend_dir": 1.0 if up else -1.0,
                        "momentum_dir": 1.0 if up else -1.0,
                        "adx_norm": 0.6, "rsi_norm": 0.3 if up else -0.3,
                        "atr_norm": 0.5, "news_bias": 0.0, "pred_bias": 0.0,
                        "wallet_bias": 0.0, "micro_bias": 0.0})
        labels.append(1 if up else 0)
    model = train(samples, labels, epochs=300)
    provider = LinearProvider(model)
    assert provider.decide(_snapshot()).choice == "LONG"
    down = MarketSnapshot(NOW, "BTC/USDT", _state(trend="TREND_DOWN", momentum="MOMENTUM_DOWN"))
    assert provider.decide(down).choice == "SHORT"


def test_linear_model_roundtrip(tmp_path: Path) -> None:
    m = LogisticModel({"trend_dir": 1.0}, 0.0, 40, NOW)
    p = tmp_path / "m.json"
    m.save(p)
    loaded = LogisticModel.load(p)
    assert loaded.weights == m.weights and loaded.samples == 40


def test_flatten_includes_intel_biases() -> None:
    intel = IntelFeatures(news={"bias": 0.4}, prediction={"bias": -0.2},
                          wallet={"bias": 0.1}, microstructure={"bias": 0.3})
    x = flatten(_snapshot(intel))
    assert x["news_bias"] == 0.4 and x["pred_bias"] == -0.2


def test_compare_logs_without_side_effects(tmp_path: Path) -> None:
    log = tmp_path / "providers.jsonl"
    decisions = run(_snapshot(), [RulesProvider(), JevProvider()], log_path=log)
    assert len(decisions) == 2
    assert log.read_text(encoding="utf-8").count("\n") == 1
    c = consensus(decisions)
    assert c["votes"]["LONG"] == 1 and c["votes"]["HOLD"] == 0
    assert any(d.status == "UNAVAILABLE" for d in decisions)