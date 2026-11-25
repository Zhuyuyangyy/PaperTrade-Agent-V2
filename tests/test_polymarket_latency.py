"""延迟探测器的离线回归。

不打网络：直接构造 Binance aggTrade 与 Polymarket 推送，喂进 Recorder。
重点钉住触发口径——用固定 1 秒窗口几乎打不出事件（BTC 1 秒波动 σ≈0.9bps），
必须是"相对上次事件的累计变动"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.polymarket import latency_probe as lp  # noqa: E402


def trade(sym, px, ts, exch=None):
    return {"e": "aggTrade", "s": sym, "p": str(px), "T": exch or ts}


def mk_rec(jump_bps=5.0):
    r = lp.Recorder()
    r.jump_bps = jump_bps
    r.tokens = ["TY", "TN"]
    r.strikes = {"BTCUSDT": [(58000.0, "c1")]}
    return r


def test_parse_strike_handles_commas_and_decimals():
    assert lp.parse_strike("Will Bitcoin be above $58,000 on November 25?") == 58000
    assert lp.parse_strike("Will Ethereum be above $1,600.5?") == 1600.5
    assert lp.parse_strike("no number here") is None


def test_pct_is_nearest_rank():
    assert lp.pct([], 50) is None
    assert lp.pct([1, 2, 3, 4], 0) == 1
    assert lp.pct([1, 2, 3, 4], 90) == 4


def test_jump_fires_only_after_cumulative_move():
    """基准是上一次事件时的价格，不是固定 1 秒窗口。"""
    r = mk_rec(5.0)
    r.on_binance(trade("BTCUSDT", 58000, 1000), 1000)
    assert not r.events                       # 首笔只建基准
    r.on_binance(trade("BTCUSDT", 58015, 1100), 1100)   # +2.6bps，不够
    assert not r.events
    r.on_binance(trade("BTCUSDT", 58035, 1200), 1200)   # 累计 +6.0bps → 触发
    assert len(r.events) == 1
    assert r.events[0]["kind"] == "jump"
    assert r.events[0]["move_bps"] == pytest.approx(6.0, abs=0.1)


def test_jump_rebases_after_event():
    r = mk_rec(5.0)
    r.on_binance(trade("BTCUSDT", 58000, 1000), 1000)
    r.on_binance(trade("BTCUSDT", 58040, 1100), 1100)
    assert len(r.events) == 1
    r.on_binance(trade("BTCUSDT", 58045, 1200), 1200)   # 相对新基准仅 +0.9bps
    assert len(r.events) == 1


def test_jump_debounced_within_one_second():
    r = mk_rec(2.0)
    r.on_binance(trade("BTCUSDT", 58000, 1000), 1000)
    r.on_binance(trade("BTCUSDT", 58020, 1100), 1100)   # 触发
    r.on_binance(trade("BTCUSDT", 58040, 1300), 1300)   # 300ms 内，被去抖
    assert len(r.events) == 1


def test_strike_cross_emits_event():
    r = mk_rec(999.0)                          # 关闭跃变触发，只看穿越
    r.on_binance(trade("BTCUSDT", 57990, 1000), 1000)
    r.on_binance(trade("BTCUSDT", 58010, 1200), 1200)
    assert any(e["kind"] == "cross" for e in r.events)


def test_price_change_updates_quote_and_logs_update():
    r = mk_rec()
    msg = {"event_type": "price_change", "timestamp": "1700000000000",
           "price_changes": [{"asset_id": "TY", "best_bid": "0.60", "best_ask": "0.62"}]}
    r.on_poly(msg, 1700000000100)
    assert r.quote["TY"]["ask"] == 0.62
    assert r.updates[-1] == (1700000000100, "TY", 0.60, 0.62)


def test_book_takes_extremes_not_first_level():
    """CLOB 的 bids/asks 不保证有序，必须取极值。"""
    r = mk_rec()
    msg = {"event_type": "book", "asset_id": "TY", "timestamp": "1700000000000",
           "bids": [{"price": "0.40", "size": "10"}, {"price": "0.58", "size": "5"}],
           "asks": [{"price": "0.70", "size": "7"}, {"price": "0.62", "size": "3"}]}
    r.on_poly(msg, 1700000000000)
    assert r.quote["TY"]["bid"] == 0.58
    assert r.quote["TY"]["ask"] == 0.62
    assert r.quote["TY"]["ask_sz"] == 3
