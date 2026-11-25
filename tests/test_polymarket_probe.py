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


# ---------------------------------------------------------------- 校准数学

def test_timestamps_from_api_are_seconds_and_must_be_normalised():
    """prices-history 的 t 是秒。用毫秒比较会让所有窗口判空且不报错。"""
    assert ap._norm_ts(1730905202) == 1730905202000
    assert ap._norm_ts(1730905202000) == 1730905202000   # 已是毫秒则不重复放大


def test_wilson_lb_matches_reference():
    """8/10 的 Wilson 95% 下界 = 0.4902（标准值）。"""
    assert ap.wilson_lb(8, 10) == pytest.approx(0.4902, abs=1e-4)


def test_wilson_lb_punishes_small_perfect_samples():
    """满分小样本的下界远低于报价——这是 0.95+ 过不了门槛的数学原因。"""
    assert ap.wilson_lb(10, 10) < 0.75
    assert ap.wilson_lb(30, 30) < 0.90
    assert ap.wilson_lb(400, 400) > 0.985
    assert ap.wilson_lb(0, 20) == 0.0
    assert ap.wilson_lb(0, 0) is None


def test_sample_at_rejects_stale_quotes():
    """滞后超过容差必须判无数据，不能拿 5 小时前的价格冒充 6 小时前。"""
    base = 1_700_000_000_000
    path = [(base - 10 * 3_600_000, 0.5), (base - 9 * 3_600_000, 0.6)]
    got = ap.sample_at(path, base, tol_h=3.0)
    assert got is None                    # 最近点也滞后 9 小时
    got = ap.sample_at(path, base, tol_h=12.0)
    assert got[0] == 0.6 and got[1] == pytest.approx(9.0)


def test_sample_at_takes_last_quote_not_first():
    base = 1_700_000_000_000
    path = [(base - 2 * 3_600_000, 0.40), (base - 1 * 3_600_000, 0.45)]
    assert ap.sample_at(path, base, tol_h=3.0)[0] == 0.45


def test_first_cross_marks_window_start_as_censored():
    """下标 0 说明 14 天窗口起点就已在区间内，真实首触不可知，不能当首次。"""
    base = 1_700_000_000_000
    path = [(base, 0.97), (base + 3_600_000, 0.98)]
    ts, p, idx = ap.first_cross(path, 0.95)
    assert idx == 0 and p == 0.97
    path2 = [(base, 0.50), (base + 3_600_000, 0.96)]
    ts2, p2, idx2 = ap.first_cross(path2, 0.95)
    assert idx2 == 1 and p2 == 0.96


def test_first_cross_skips_prices_above_ceiling():
    """0.999 以上是结算边界，不该当作"首次触到 0.95"。"""
    base = 1_700_000_000_000
    path = [(base, 0.999), (base + 3_600_000, 0.96)]
    ts, p, idx = ap.first_cross(path, 0.95)
    assert p == 0.96


def test_first_cross_low_is_the_mirror_direction():
    base = 1_700_000_000_000
    path = [(base, 0.50), (base + 3_600_000, 0.04)]
    ts, p, idx = ap.first_cross_low(path, 0.05)
    assert p == 0.04 and idx == 1


def test_net_edge_applies_official_fee_formula():
    """fee = feeRate × p × (1-p) 每股；0.96 买、100% 兑现、费 1% → 412.67bps。"""
    got = ap.net_edge_bps(1.0, 0.96, fee_rate=0.01, slip_bps=0.0)
    assert got == pytest.approx(412.67, abs=0.02)
    # 滑点按名义扣：35bps 再吃掉 35bps
    assert ap.net_edge_bps(1.0, 0.96, 0.01, 35.0) == pytest.approx(377.67, abs=0.02)


def test_net_edge_at_the_top_of_the_band_is_razor_thin():
    """0.99 买入即使全兑现，扣 5% 费 + 35bps 滑点后也只剩 61bps；
    同样条件下 0.96 买有 351bps —— 边际随价格急剧衰减。"""
    top = ap.net_edge_bps(1.0, 0.99, fee_rate=0.05, slip_bps=35.0)
    mid = ap.net_edge_bps(1.0, 0.96, fee_rate=0.05, slip_bps=35.0)
    assert top == pytest.approx(61.01, abs=0.05)
    assert mid == pytest.approx(361.67, abs=0.05)
    assert mid > 4 * top


def test_classify_uses_event_tags_over_question_text():
    assert ap.classify(["Sports", "NFL"], "Will the Chiefs win?") == "sports"
    assert ap.classify(["Crypto"], "Bitcoin above 100k?") == "crypto"
    assert ap.classify(["Politics"], "Who wins the Senate?") == "politics"
    assert ap.classify([], "Will it rain in Seattle tomorrow?") == "other"


def test_bucket_table_flags_only_when_lower_bound_beats_cost():
    rows = [{"p": 0.96, "yes": True, "vol": 1e6} for _ in range(9)]
    rows.append({"p": 0.96, "yes": False, "vol": 1e6})     # 10 中 9 胜，下界 0.596
    t = ap.bucket_table(rows, ((0.95, 0.98),), fee_rate=0.01, slip_bps=35.0)
    assert t[0]["n"] == 10 and t[0]["win_rate"] == pytest.approx(0.9)
    assert t[0]["lb_net_bps"] < 0                          # 小样本不可能过关
