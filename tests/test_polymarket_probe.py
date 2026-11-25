"""Polymarket 探测器的数学与陷阱回归。

全部离线，构造 CLOB 格式的盘口字典，不打网络。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.polymarket import arb_probe as ap  # noqa: E402


def book(bids, asks):
    return {"bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}


# ---------------------------------------------------------------- 盘口排序

def test_bids_are_returned_best_first_even_though_api_sends_ascending():
    """CLOB 实测 bids 升序（bids[0] 最差）。取 levels[0] 会把最差价当买一。"""
    bk = book(bids=[(0.01, 100), (0.02, 100), (0.30, 50)], asks=[(0.98, 10), (0.40, 5)])
    bids, asks = ap.sides(bk)
    assert bids[0][0] == 0.30      # 最优买价是 max
    assert asks[0][0] == 0.40      # 最优卖价是 min
    assert [p for p, _ in bids] == [0.30, 0.02, 0.01]
    assert [p for p, _ in asks] == [0.40, 0.98]


def test_zero_size_levels_are_dropped():
    bk = book(bids=[(0.30, 0)], asks=[(0.40, 0), (0.41, 5)])
    _b, a = ap.sides(bk)
    assert a == [(0.41, 5.0)]


# ---------------------------------------------------------------- 吃单

def test_walk_buy_crosses_levels_and_reports_partial_fill():
    levels = [(0.40, 10), (0.50, 10)]
    cost, fee, filled = ap.walk_buy(levels, 15, 0.0)
    assert filled == pytest.approx(15)
    assert cost == pytest.approx(10 * 0.40 + 5 * 0.50)   # 4.0 + 2.5
    assert fee == 0.0


def test_walk_buy_stops_at_available_depth():
    cost, _fee, filled = ap.walk_buy([(0.40, 3)], 10, 0.0)
    assert filled == pytest.approx(3)
    assert cost == pytest.approx(1.2)


def test_fee_formula_matches_documented_shape():
    """fee = C × feeRate × p × (1-p)，在 p=0.5 处最大。"""
    _c, fee_50, _ = ap.walk_buy([(0.5, 100)], 100, 0.05)
    _c, fee_90, _ = ap.walk_buy([(0.9, 100)], 100, 0.05)
    assert fee_50 == pytest.approx(100 * 0.05 * 0.5 * 0.5)
    assert fee_90 == pytest.approx(100 * 0.05 * 0.9 * 0.1)
    assert fee_50 > fee_90


# ---------------------------------------------------------------- 等份数求解

def test_complete_set_profit_at_zero_fee():
    # 两腿 0.40 + 0.55 = 0.95，每合成一份赚 0.05
    legs = [[(0.40, 10_000)], [(0.55, 10_000)]]
    r = ap.solve_shares(legs, capital=950.0, fee_rate=0.0)
    assert r["shares"] == pytest.approx(1000, rel=1e-6)
    assert r["cost"] == pytest.approx(950.0, rel=1e-6)
    assert r["profit"] == pytest.approx(50.0, rel=1e-6)


def test_depth_is_the_shortest_leg():
    legs = [[(0.40, 500)], [(0.55, 20)]]
    r = ap.solve_shares(legs, capital=10_000.0, fee_rate=0.0)
    assert r["shares"] == pytest.approx(20)
    assert r["max_deployable"] == pytest.approx(20 * 0.95)


def test_fee_can_flip_a_thin_edge_negative():
    legs = [[(0.49, 10_000)], [(0.49, 10_000)]]     # 0.98，毛利 2%
    free = ap.solve_shares(legs, capital=980.0, fee_rate=0.0)
    charged = ap.solve_shares(legs, capital=980.0, fee_rate=0.05)
    # 每份手续费 = 0.05*0.49*0.51*2 ≈ 0.025，2 分钱毛利被吃掉
    assert free["profit"] > 0
    assert charged["profit"] < 0


# ---------------------------------------------------------------- 完备性

def _neg_event(legs):
    return {"event_id": "e1", "title": "t", "legs": [
        {"market_id": str(i), "yes_token": f"y{i}"} for i in range(legs)]}


def test_incomplete_set_is_flagged_and_gets_no_net_numbers():
    # 4 腿，ask 之和 0.116 —— 明显缺腿（真实概率质量不够 1）
    books = {f"y{i}": book(bids=[(0.02, 100)], asks=[(0.029, 100)]) for i in range(4)}
    row = ap.eval_neg(_neg_event(4), books, deep=True)
    assert row["sum_mid"] < 0.97
    assert row["complete"] is False
    assert "f0.00_c100" not in row          # 不完整就不算净利，防止被误读


def test_complete_set_gets_depth_and_net_numbers():
    books = {f"y{i}": book(bids=[(0.24, 1000)], asks=[(0.245, 1000)]) for i in range(4)}
    row = ap.eval_neg(_neg_event(4), books, deep=True)
    assert row["complete"] is True
    assert row["sum_ask"] == pytest.approx(0.98)
    assert row["gross_edge"] == pytest.approx(0.02)
    assert row["f0.00_c100"]["net_bps"] > 0
    assert row["f0.05_c100"]["net_bps"] < 0


def test_missing_leg_returns_none_instead_of_guessing():
    books = {f"y{i}": book(bids=[(0.24, 100)], asks=[(0.245, 100)]) for i in range(3)}
    assert ap.eval_neg(_neg_event(4), books, deep=True) is None


# ---------------------------------------------------------------- 二元

def test_binary_arb_needs_both_legs_below_one():
    m = {"condition_id": "c1", "question": "q", "yes_token": "y", "no_token": "n",
         "liquidity": 1000.0}
    good = {"y": book(bids=[(0.39, 1000)], asks=[(0.40, 1000)]),
            "n": book(bids=[(0.54, 1000)], asks=[(0.55, 1000)])}
    row = ap.eval_binary(m, good, deep=True)
    assert row["sum_ask"] == pytest.approx(0.95)
    assert row["f0.00_c100"]["net_bps"] > 0

    bad = {"y": book(bids=[(0.49, 1000)], asks=[(0.50, 1000)]),
           "n": book(bids=[(0.49, 1000)], asks=[(0.51, 1000)])}
    row2 = ap.eval_binary(m, bad, deep=True)
    assert row2["sum_ask"] == pytest.approx(1.01)
    assert row2["gross_edge"] < 0


# ---------------------------------------------------------------- 尾盘

def test_tail_needs_a_future_end_and_a_price_in_band():
    m = {"condition_id": "t1", "question": "q", "yes_token": "y", "no_token": "n",
         "liquidity": 1000.0, "end_date": "2026-11-25T20:00:00Z"}
    books = {"y": book(bids=[(0.95, 500)], asks=[(0.96, 500)])}
    now = int(__import__("datetime").datetime(2026, 11, 25, 12, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    row = ap.eval_tail(m, books, now, max_hours=48)
    assert row is not None
    assert row["ask"] == 0.96
    assert row["hours_left"] == 8.0
    assert row["depth_usd"] == pytest.approx(480.0)
    # 已经到期的市场不报
    assert ap.eval_tail(m, books, now + 24 * 3600 * 1000, max_hours=48) is None


def test_tail_breakeven_includes_fee():
    m = {"condition_id": "t2", "question": "q", "yes_token": "y", "no_token": "n",
         "liquidity": 1000.0, "end_date": "2026-11-25T20:00:00Z"}
    books = {"y": book(bids=[(0.949, 500)], asks=[(0.95, 500)])}
    now = int(__import__("datetime").datetime(2026, 11, 25, 12, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    row = ap.eval_tail(m, books, now, max_hours=48)
    # 保本胜率必须高于报价（手续费 + 滑点）
    assert row["breakeven_win_rate"] > 0.95
