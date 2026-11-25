"""WebSocket 探测器的状态机回归。

全部离线：构造 book / price_change 消息喂进 State，不打网络。
重点钉三件事：
  1. 只有每条腿都刷新过才算 confirmed（单腿刷新是异步推送的拼接假象）
  2. 时长按交易所时间戳算，不是本地墙钟
  3. 净价差必须扣 fee = feeRate × p × (1−p)，两条腿各一次
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.polymarket import ws_probe as ws  # noqa: E402


def mk_state():
    st = ws.State()
    st.markets = {"c1": {"y": "TY", "n": "TN", "q": "will it", "liq": 1.0}}
    st.token_index = {"TY": [("bin", "c1")], "TN": [("bin", "c1")]}
    return st


def book(tok, bid, ask, ts, size=100.0):
    return {"event_type": "book", "asset_id": tok, "timestamp": str(ts),
            "bids": [{"price": str(bid), "size": str(size)}],
            "asks": [{"price": str(ask), "size": str(size)}]}


def pc(tok, bid, ask, ts, size=100.0):
    return {"event_type": "price_change", "timestamp": str(ts),
            "price_changes": [{"asset_id": tok, "price": str(ask), "size": str(size),
                               "side": "SELL", "best_bid": str(bid), "best_ask": str(ask)}]}


def test_no_evaluation_until_both_legs_quoted():
    st = mk_state()
    st.on_msg(book("TY", 0.49, 0.50, 1000))
    assert not st.open and not st.closed          # 只有一条腿，不能判
    st.on_msg(book("TN", 0.48, 0.49, 1000))
    assert "bin:c1" in st.open                    # sum=0.99 < 1，开 episode


def test_single_leg_refresh_does_not_confirm():
    """A 腿刷新造成的 sum<1 是拼接假象，必须两条腿都刷新过才算真机会。"""
    st = mk_state()
    st.on_msg(book("TY", 0.50, 0.51, 1000))
    st.on_msg(book("TN", 0.49, 0.50, 1000))       # sum=1.01，不触发
    assert not st.open
    st.on_msg(pc("TY", 0.48, 0.49, 2000))         # 单腿下调 → sum=0.99
    ep = st.open["bin:c1"]
    assert ep["confirmed"] is None
    st.on_msg(pc("TY", 0.48, 0.49, 2500))         # 同一条腿再刷，仍不算
    assert st.open["bin:c1"]["confirmed"] is None
    st.on_msg(pc("TN", 0.49, 0.50, 3000))         # 另一条腿也刷新 → 确认
    assert st.open["bin:c1"]["confirmed"] == 3000


def test_episode_duration_uses_exchange_timestamps():
    st = mk_state()
    st.on_msg(book("TY", 0.48, 0.49, 1000))
    st.on_msg(book("TN", 0.47, 0.48, 1000))
    st.on_msg(pc("TN", 0.49, 0.50, 1900))         # sum 回到 0.99→0.99? 见下
    st.on_msg(pc("TN", 0.51, 0.52, 2500))         # sum=0.49+0.52=1.01 → 结束
    ep = [e for e in st.closed if e["key"] == "bin:c1"][-1]
    assert ep["start"] == 1000
    assert ep["dur_ms"] == 1500


def test_net_edge_subtracts_fee_on_both_legs():
    """sum=0.99 在 p≈0.5 时 fee≈50bps，净价差只有 +50bps；sum=0.999 是 -40bps。"""
    st = mk_state()
    st.on_msg(book("TY", 0.49, 0.50, 1000))
    st.on_msg(book("TN", 0.48, 0.49, 1000))
    ep = st.open["bin:c1"]
    fee = ws.FEE_RATE * 0.50 * 0.50 + ws.FEE_RATE * 0.49 * 0.51
    assert ep["max_net"] == pytest.approx(1 - 0.99 - fee, abs=1e-9)

    st2 = mk_state()
    st2.on_msg(book("TY", 0.499, 0.500, 1000))
    st2.on_msg(book("TN", 0.498, 0.499, 1000))    # sum=0.999
    assert st2.open["bin:c1"]["max_net"] < 0       # 一个 tick 不够付费


def test_stale_quote_closes_episode_at_last_valid_moment():
    """一条腿超过 10s 不更新时，episode 必须按最后一次有效判定的时刻封口，
    否则它会永远挂在 open 里，汇总时算不出时长。"""
    st = mk_state()
    st.on_msg(book("TY", 0.49, 0.50, 1000))
    st.on_msg(book("TN", 0.48, 0.49, 1000))      # sum=0.99，开
    st.on_msg(pc("TY", 0.49, 0.50, 3000))        # 仍然 0.99，last_ok=3000
    assert st.open["bin:c1"]["last_ok"] == 3000
    st.on_msg(pc("TN", 0.52, 0.53, 1000 + ws.FRESH_MS + 10_000))   # TY 已陈旧
    ep = st.closed[-1]
    assert ep["close_reason"] == "stale"
    assert ep["dur_ms"] == 2000                   # 3000 - 1000，不是到 21000


def test_neg_risk_needs_every_leg_fresh():
    st = ws.State()
    st.neg_events = {"e1": {"legs": ["A", "B", "C"], "title": "winner"}}
    st.token_index = {"A": [("neg", "e1")], "B": [("neg", "e1")], "C": [("neg", "e1")]}
    st.on_msg(book("A", 0.32, 0.33, 1000))
    st.on_msg(book("B", 0.32, 0.33, 1000))
    assert not st.open                            # 缺 C
    st.on_msg(book("C", 0.32, 0.33, 1000))        # sum=0.99
    assert "neg:e1" in st.open
    assert st.open["neg:e1"]["n_legs"] == 3
