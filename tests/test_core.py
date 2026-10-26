from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from research.core import (
    ClosedCandle, HOUR_MS, build_decision, checked_closed_candles,
    load_ta_bias, write_decisions,
)
from research.worker import fake_closed_rows, run_demo
from user_data.strategies.signal_gate import approved_from_file, validate_buy_signal

NOW = int(datetime(2026, 10, 23, 22, 5, tzinfo=timezone.utc).timestamp() * 1000)


def valid_decision(now=NOW):
    bars = checked_closed_candles(fake_closed_rows(now), now)
    return build_decision("BTC/USDT", bars, now)


def test_synthetic_rising_data_produces_buy_rule_vote():
    d = valid_decision()
    assert d["action"] == "BUY"
    assert d["votes"]["trend"] == "BUY"
    assert d["votes"]["momentum"] == "BUY"
    assert d["risk_veto"] is False


def test_exact_candle_signal_gate_and_replay_rejection():
    d = valid_decision()
    candle = d["candle_open_ms"]
    assert validate_buy_signal(d, "BTC/USDT", candle, NOW) == (True, "approved")
    assert validate_buy_signal(d, "ETH/USDT", candle, NOW)[0] is False
    assert validate_buy_signal(d, "BTC/USDT", candle - HOUR_MS, NOW)[0] is False
    assert validate_buy_signal(d, "BTC/USDT", candle, NOW + 2 * HOUR_MS)[0] is False
    assert validate_buy_signal(d, "BTC/USDT", candle, candle + 10)[0] is False


@pytest.mark.parametrize("patch", [
    {"schema_version": 2},
    {"timeframe": "5m"},
    {"source": "SYNTHETIC-DEMO-NOT-TRADEABLE"},
    {"observed_at_ms": NOW + 7000},
    {"observed_at_ms": NOW - 2 * HOUR_MS},
    {"expires_at_ms": NOW + 3 * HOUR_MS},
    {"action": "HOLD"},
    {"risk_veto": True},
    {"votes": {"trend": "HOLD", "momentum": "BUY"}},
    {"candle_open_ms": True},
    {"indicators": {"a": float("nan")}},
])
def test_malformed_signal_blocks(patch):
    d = valid_decision()
    candle = d["candle_open_ms"]
    d.update(patch)
    assert validate_buy_signal(d, "BTC/USDT", candle, NOW)[0] is False


def test_unclosed_future_candle_is_not_consumed():
    rows = fake_closed_rows(NOW)
    current = NOW // HOUR_MS * HOUR_MS
    rows.append([current, 90, 95, 85, 92, 200])
    accepted = checked_closed_candles(rows, NOW)
    assert accepted[-1].open_ms == current - HOUR_MS


def test_missing_or_corrupt_recent_bars_rejected():
    rows = fake_closed_rows(NOW)
    rows[-3][0] += 360_000
    with pytest.raises(ValueError):
        checked_closed_candles(rows, NOW)
    rows = fake_closed_rows(NOW)
    rows[-1][4] = float("nan")
    with pytest.raises(ValueError):
        checked_closed_candles(rows, NOW)


def test_high_volatility_veto():
    bars = checked_closed_candles(fake_closed_rows(NOW), NOW)
    jagged = []
    for i, bar in enumerate(bars):
        price = (100.0 if i % 2 == 0 else 114.0)
        jagged.append(ClosedCandle(bar.open_ms, price, price + 2,
                                   price - 2, price, 100))
    d = build_decision("BTC/USDT", jagged, NOW)
    assert d["risk_veto"] is True
    assert d["action"] == "HOLD"


def test_optional_ta_bias_blocks_without_exact_recent_buy(tmp_path):
    candles = checked_closed_candles(fake_closed_rows(NOW), NOW)
    d = build_decision("BTC/USDT", candles, NOW, ta_policy="require_buy", ta_root=tmp_path)
    assert d["action"] == "HOLD"
    path = tmp_path / "BTC_USDT.json"
    path.write_text(json.dumps({"pair": "BTC/USDT", "source": "TradingAgents",
                                "action": "BUY", "analysis_date": "2026-10-23", "observed_at_ms": NOW - 1000}))
    assert load_ta_bias(tmp_path, "BTC/USDT", NOW) == "BUY"
    d = build_decision("BTC/USDT", candles, NOW, ta_policy="require_buy", ta_root=tmp_path)
    assert d["action"] == "BUY"
    path.write_text(json.dumps({"pair": "BTC/USDT", "source": "TradingAgents",
                                "action": "BUY", "analysis_date": "2026-10-23", "observed_at_ms": NOW - 25 * HOUR_MS}))
    assert load_ta_bias(tmp_path, "BTC/USDT", NOW) is None


def test_atomic_file_and_identical_bar_no_duplicate_log(tmp_path):
    d = valid_decision()
    outdir, log = tmp_path / "signals", tmp_path / "decisions.jsonl"
    assert write_decisions(outdir, [d], log) == 1
    assert write_decisions(outdir, [d], log) == 0
    assert len(log.read_text().splitlines()) == 1
    assert approved_from_file(outdir, "BTC/USDT", d["candle_open_ms"], NOW)[0] is True
    with pytest.raises(ValueError):
        write_decisions(outdir, [{**d, "source": "SYNTHETIC"}], log)


def test_demo_never_writes_tradeable_signal(tmp_path):
    demos = run_demo(tmp_path / "synthetic")
    assert all(x["source"] != "ccxt-public:kraken" for x in demos)
    d = demos[0]
    assert approved_from_file(tmp_path / "synthetic", d["pair"], d["candle_open_ms"], d["observed_at_ms"])[0] is False
