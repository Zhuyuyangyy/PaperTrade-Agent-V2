from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import types

import pandas as pd
import pytest

# Stub only imported strategy interfaces; real Freqtrade image is not present here.
ft = types.ModuleType("freqtrade")
strategies = types.ModuleType("freqtrade.strategy")
strategies.IStrategy = object
persistence = types.ModuleType("freqtrade.persistence")
class Trade:
    items = []
    @classmethod
    def get_trades_proxy(cls, **kwargs):
        return cls.items
persistence.Trade = Trade
sys.modules["freqtrade"] = ft
sys.modules["freqtrade.strategy"] = strategies
sys.modules["freqtrade.persistence"] = persistence

from PaperBaseline import PaperBaseline
from PaperHybrid import PaperHybrid
from PaperFast import PaperFast
from research.core import build_decision, checked_closed_candles, atomic_write_json
from research.worker import fake_closed_rows


def make_df(now_ms, rising=True):
    rows = fake_closed_rows(now_ms)
    if not rising:
        rows = [[t, 150-i*0.4, 151-i*0.4, 149-i*0.4, 150-i*0.4, vol]
                for i, (t, _, _, _, _, vol) in enumerate(rows)]
    return pd.DataFrame([{"date": pd.to_datetime(t, unit="ms", utc=True),
                          "open": op, "high": h, "low": low, "close": c, "volume": vol}
                         for t, op, h, low, c, vol in rows])


def new_strategy(cls, tmp_path):
    s = cls()
    s.config = {"dry_run": True, "dry_run_wallet": 1000, "trading_mode": "spot", "max_open_trades": 2,
                "stake_amount": 100, "exchange": {"name": "kraken", "pair_whitelist": ["BTC/USDT", "ETH/USDT"], "key": "", "secret": "", "password": ""},
                "api_server": {"enabled": False}, "force_entry_enable": False,
                "runmode": "dry_run"}
    s.wallets = types.SimpleNamespace(get_total_stake_amount=lambda: 1000)
    s.SIGNAL_DIR = tmp_path
    return s


def test_baseline_buys_but_hybrid_never_enters_without_current_agent(tmp_path):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = make_df(now_ms)
    s0, s1 = new_strategy(PaperBaseline, tmp_path), new_strategy(PaperHybrid, tmp_path)
    base = s0.populate_entry_trend(s0.populate_indicators(df, {}), {"pair": "BTC/USDT"})
    hybrid = s1.populate_entry_trend(s1.populate_indicators(df, {}), {"pair": "BTC/USDT"})
    assert base["enter_long"].iloc[-1] == 1
    assert hybrid["enter_long"].sum() == 0


def test_hybrid_approves_only_last_matching_candle(tmp_path):
    # Live clocks: the hybrid validates signal freshness against its own
    # datetime.now(), so the signal must be written with a real recent time.
    # The one exception is the closed-candle check, anchored 60s back -- the
    # same convention run_demo uses -- because inside the first 30s after an
    # hour boundary an hour-aligned feed has no candle that is both settled
    # and fresh (the dead zone, pinned by the test below).
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = make_df(now_ms)
    safe_ms = now_ms - 60_000
    bars = checked_closed_candles(fake_closed_rows(safe_ms), safe_ms)
    d = build_decision("BTC/USDT", bars, safe_ms)
    atomic_write_json(tmp_path / "BTC_USDT.json", d)
    s = new_strategy(PaperHybrid, tmp_path)
    assert s.process_only_new_candles is False
    analyzed = s.populate_indicators(df, {"pair": "BTC/USDT"})
    result = s.populate_entry_trend(analyzed, {"pair": "BTC/USDT"})
    assert result["enter_long"].iloc[-1] == 1
    assert result["enter_long"].iloc[:-1].sum() == 0
    s.dp = types.SimpleNamespace(get_analyzed_dataframe=lambda **kwargs: (result, None))
    now = datetime.now(timezone.utc)
    assert s.confirm_trade_entry("BTC/USDT", "market", 1, 100, "gtc", now, "agent", "long") is True
    (tmp_path / "BTC_USDT.json").unlink()
    assert s.confirm_trade_entry("BTC/USDT", "market", 1, 100, "gtc", now, "agent", "long") is False


def test_hour_boundary_dead_zone_is_stale_not_silent():
    """The dead zone, pinned: within 30s after an hour boundary an hour-aligned
    feed has no candle that is both settled (30s rule) and fresh (1h window),
    so the staleness guard must fire loudly -- and the demo's one-minute-back
    anchor must not trip it. This test failed intermittently in CI before the
    guard became a dedicated exception and the clocks were pinned."""
    boundary = int(datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    live = boundary + 5_000
    with pytest.raises(ValueError, match="stale"):
        checked_closed_candles(fake_closed_rows(live), live)
    anchored = live - 60_000
    assert len(checked_closed_candles(fake_closed_rows(anchored), anchored)) >= 60


def test_hybrid_exit_is_independent_of_research_signal(tmp_path):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = make_df(now_ms, rising=False)
    s = new_strategy(PaperHybrid, tmp_path)
    analyzed = s.populate_indicators(df, {"pair": "BTC/USDT"})
    out = s.populate_exit_trend(analyzed, {"pair": "BTC/USDT"})
    assert out["exit_long"].iloc[-1] == 1


def test_live_mode_credentials_and_remote_api_fail_closed(tmp_path):
    s = new_strategy(PaperBaseline, tmp_path)
    s.bot_start()
    s.config["dry_run"] = False
    with pytest.raises(RuntimeError): s.bot_start()
    s.config["dry_run"] = True
    s.config["exchange"]["secret"] = "poison"
    with pytest.raises(RuntimeError): s.bot_start()
    s.config["exchange"]["secret"] = ""
    s.config["api_server"]["enabled"] = True
    with pytest.raises(RuntimeError): s.bot_start()


def test_hybrid_refuses_backtest_looking_at_current_file(tmp_path):
    s = new_strategy(PaperHybrid, tmp_path)
    s.config["runmode"] = "backtest"
    with pytest.raises(RuntimeError): s.bot_start()


def test_entry_position_size_and_daily_realized_cutoff(tmp_path):
    s = new_strategy(PaperBaseline, tmp_path)
    now = datetime.now(timezone.utc)
    assert s.custom_stake_amount("BTC/USDT", now, 100, 200, 10, 200, 1, None, "long") == 100
    s.wallets = types.SimpleNamespace(get_total_stake_amount=lambda: 500)
    assert s.custom_stake_amount("BTC/USDT", now, 100, 200, 10, 200, 1, None, "long") == 50
    Trade.items = [types.SimpleNamespace(close_date_utc=now, close_profit_abs=-25.0)]
    try:
        assert s.confirm_trade_entry("BTC/USDT", "market", 1, 100, "gtc", now, None, "long") is False
        assert s.daily_realized_loss_ok(now) is False
    finally:
        Trade.items = []


def test_adverse_slippage_hooks_apply_same_5bps_to_both_accounts(tmp_path):
    now = datetime.now(timezone.utc)
    for cls in (PaperBaseline, PaperHybrid):
        strategy = new_strategy(cls, tmp_path)
        entry = strategy.custom_entry_price("BTC/USDT", None, now, 100.0, None, "long")
        exit_price = strategy.custom_exit_price(
            "BTC/USDT", types.SimpleNamespace(is_short=False), now, 100.0, 0.0, "exit_signal"
        )
        assert entry == pytest.approx(100.05)
        assert exit_price == pytest.approx(99.95)
        assert strategy.SLIPPAGE_BPS == 5


def test_slippage_hooks_do_not_adjust_unapproved_pairs_or_short_entries(tmp_path):
    strategy = new_strategy(PaperBaseline, tmp_path)
    now = datetime.now(timezone.utc)
    assert strategy.custom_entry_price("DOGE/USDT", None, now, 100.0, None, "long") == 100.0
    assert strategy.custom_entry_price("BTC/USDT", None, now, 100.0, None, "short") == 100.0
    assert strategy.custom_exit_price(
        "DOGE/USDT", types.SimpleNamespace(is_short=False), now, 100.0, 0.0, None
    ) == 100.0
    assert strategy.custom_exit_price(
        "BTC/USDT", types.SimpleNamespace(is_short=True), now, 100.0, 0.0, None
    ) == 100.0


def test_all_three_lanes_use_limit_orders_so_custom_prices_reach_the_fill():
    # Freqtrade only treats the custom price as the ORDER limit price. A market order
    # fills at the market average, so a market entry silently drops the 5 bps adverse
    # entry price from P&L (verified against the orders table: ft_price = 1.0005x
    # market but average == market). Every leg must be a limit order.
    assert PaperBaseline.order_types["entry"] == "limit"
    assert PaperBaseline.order_types["exit"] == "limit"
    assert PaperBaseline.order_types["stoploss"] == "limit"
    assert PaperHybrid.order_types == PaperBaseline.order_types
    assert PaperFast.order_types == PaperBaseline.order_types


def test_fast_lane_uses_15m_ema_strategy_and_generates_a_trend_candidate(tmp_path):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = make_df(now_ms)
    strategy = new_strategy(PaperFast, tmp_path)
    strategy.config["max_open_trades"] = 2
    assert strategy.timeframe == "15m"
    assert strategy.stoploss == -0.02
    indicators = strategy.populate_indicators(df, {"pair": "BTC/USDT"})
    assert {"ema9", "ema21", "rsi14"}.issubset(indicators.columns)
    entry = strategy.populate_entry_trend(indicators, {"pair": "BTC/USDT"})
    assert entry["enter_long"].iloc[-1] == 1
    exit_frame = strategy.populate_exit_trend(indicators, {"pair": "BTC/USDT"})
    assert exit_frame["exit_long"].iloc[-1] == 0
    assert strategy.custom_entry_price("BTC/USDT", None, datetime.now(timezone.utc), 100, None, "long") == pytest.approx(100.05)


def test_fast_lane_writes_real_dry_run_heartbeat(tmp_path):
    import json
    now = datetime.now(timezone.utc)
    strategy = new_strategy(PaperFast, tmp_path)
    strategy.config["user_data_dir"] = str(tmp_path)
    strategy.config["runmode"] = "dry_run"
    strategy.bot_loop_start(now)
    payload = json.loads((tmp_path / "fast_heartbeat.json").read_text(encoding="utf-8"))
    assert payload["source"] == "PaperFast"
    assert payload["mode"] == "dry_run"
    assert payload["timeframe"] == "15m"
    assert abs(payload["last_loop_at_ms"] - int(now.timestamp() * 1000)) < 1000


def test_baseline_publishes_real_loop_heartbeat_and_strategy_observation(tmp_path):
    import json
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    strategy = new_strategy(PaperBaseline, tmp_path)
    strategy.config["user_data_dir"] = str(tmp_path)
    strategy.config["runmode"] = "dry_run"
    strategy.bot_loop_start(now)
    heartbeat = json.loads((tmp_path / "strategy_heartbeat.json").read_text(encoding="utf-8"))
    assert heartbeat["source"] == "PaperBaseline"
    assert heartbeat["status"] == "running"
    assert heartbeat["timeframe"] == "1h"

    df = make_df(now_ms)
    indicators = strategy.populate_indicators(df, {"pair": "BTC/USDT"})
    strategy.populate_entry_trend(indicators, {"pair": "BTC/USDT"})
    status = json.loads((tmp_path / "strategy_status_BTC_USDT.json").read_text(encoding="utf-8"))
    assert status["strategy"] == "PaperBaseline"
    assert status["pair"] == "BTC/USDT"
    assert status["action"] in ("BUY", "HOLD")
    assert status["mode"] == "dry_run"


def test_fast_heartbeat_also_publishes_generic_account_heartbeat(tmp_path):
    import json
    now = datetime.now(timezone.utc)
    strategy = new_strategy(PaperFast, tmp_path)
    strategy.config["user_data_dir"] = str(tmp_path)
    strategy.config["runmode"] = "dry_run"
    strategy.bot_loop_start(now)
    payload = json.loads((tmp_path / "strategy_heartbeat.json").read_text(encoding="utf-8"))
    assert payload["source"] == "PaperFast"
    assert payload["timeframe"] == "15m"
    assert payload["mode"] == "dry_run"


def test_hybrid_publishes_agent_veto_reason_when_research_signal_is_missing(tmp_path):
    import json
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    strategy = new_strategy(PaperHybrid, tmp_path)
    strategy.config["user_data_dir"] = str(tmp_path)
    strategy.config["runmode"] = "dry_run"
    df = make_df(now_ms)
    analyzed = strategy.populate_indicators(df, {"pair": "BTC/USDT"})
    strategy.populate_entry_trend(analyzed, {"pair": "BTC/USDT"})
    payload = json.loads((tmp_path / "strategy_status_BTC_USDT.json").read_text(encoding="utf-8"))
    assert payload["strategy"] == "PaperHybrid"
    assert payload["action"] == "HOLD"
    assert "Agent 信号未通过" in payload["reason"]


def test_fast_publishes_real_strategy_candidate_status(tmp_path):
    import json
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    strategy = new_strategy(PaperFast, tmp_path)
    strategy.config["user_data_dir"] = str(tmp_path)
    strategy.config["runmode"] = "dry_run"
    df = make_df(now_ms)
    analyzed = strategy.populate_indicators(df, {"pair": "BTC/USDT"})
    strategy.populate_entry_trend(analyzed, {"pair": "BTC/USDT"})
    payload = json.loads((tmp_path / "strategy_status_BTC_USDT.json").read_text(encoding="utf-8"))
    assert payload["strategy"] == "PaperFast"
    assert payload["timeframe"] == "15m"
    assert payload["action"] in ("BUY", "HOLD")
    assert payload["reason"]
