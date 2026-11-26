"""M1 三件新工具的离线回归。

不打网络。钉住四类容易静默出错的口径：
1. **方向语义**——现货上涨时 up token 的 ask 该升、down token 该降（写反过一次）；
2. **churn 过滤**——同价位重复推送不能计入"下一次更新"，否则 t1 恒为 0；
3. **结算统计量**——5m 市场按 TWAP 均值判，不是末价（这是整轮实验的前提）；
4. **气象解析**——T 组优先、SPECI 排除、双单位换算（官方四个算例）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.polymarket import flat5m_probe as f5  # noqa: E402
from research.polymarket import twap_efficiency as te  # noqa: E402
from research.polymarket import weather_probe as wp  # noqa: E402


# ---------------------------------------------------------------- flat5m

def test_window_ts_is_300s_aligned():
    assert f5.window_ts(1795622400.0) == 1795622400
    assert f5.window_ts(1795622401.0) == 1795622400
    assert f5.window_ts(1795622699.9) == 1795622400
    assert f5.window_ts(1795622700.0) == 1795622700


def test_ladder_ask_depth_walks_levels():
    lb = f5.Ladder()
    lb.asks = {0.50: 100.0, 0.51: 100.0, 0.52: 100.0}
    # $200 在 0.50 能买 100 份（$50），剩下 $150 在 0.51 买 294.1 份
    shares, cost, top, lv = lb.ask_depth(200.0)
    assert shares > 100.0
    assert cost <= 200.0 + 1e-6
    assert lv >= 2
    assert top is not None and top >= 0.51


def test_ladder_ask_depth_respects_max_ticks():
    lb = f5.Ladder()
    lb.asks = {0.50: 10.0, 0.55: 1000.0}
    # 只允许吃 2 ticks：0.55 距 best 5 ticks，不该被吃到
    shares, cost, top, lv = lb.ask_depth(1000.0, max_ticks=2)
    assert lv == 1
    assert abs(shares - 10.0) < 1e-9
    assert top == 0.50


def test_ladder_apply_change_removes_on_zero_size():
    lb = f5.Ladder()
    lb.apply_change(0.42, 0.0, "SELL", 1, 1)
    assert 0.42 not in lb.asks
    lb.apply_change(0.42, 50.0, "SELL", 2, 2)
    assert lb.asks[0.42] == 50.0
    lb.apply_change(0.42, 0.0, "SELL", 3, 3)
    assert 0.42 not in lb.asks


def test_book_snapshot_replaces_both_sides():
    lb = f5.Ladder()
    lb.asks = {0.99: 1.0}
    lb.bids = {0.01: 1.0}
    msg = {"asks": [{"price": "0.60", "size": "10"}],
           "bids": [{"price": "0.40", "size": "20"}]}
    lb.apply_book(msg, 100, 100)
    assert lb.asks == {0.60: 10.0}
    assert lb.bids == {0.40: 20.0}


def mk_probe():
    p = f5.Probe(jump_bps=5.0, ref_ms=5000, horizon_ms=30000, cooldown_ms=1000)
    p.add_window({"ts": 1795622400, "slug": "s", "title": "t",
                  "condition_id": "c", "up": "UP", "down": "DOWN",
                  "accepting": True, "closed": False,
                  "start": 1795622400, "end": 1795622700, "volume": 0.0})
    return p


def test_touch_dedupes_same_price_updates():
    """同价位重复推送必须被丢掉——预检里 9514 条只有 4 个唯一价。"""
    p = mk_probe()
    lb = f5.Ladder()
    lb.asks = {0.51: 100.0}
    lb.bids = {0.50: 100.0}
    p._touch("UP", 1000, lb)
    lb.ts = 2000
    p._touch("UP", 2000, lb)          # 价格没变 → 不该新增
    assert len(p.updates["UP"]) == 1
    lb.asks = {0.53: 100.0}
    p._touch("UP", 3000, lb)
    assert len(p.updates["UP"]) == 2


def test_reference_price_needs_enough_history():
    """历史不够时宁可不触发，避免拿错误基准造出假事件。"""
    p = f5.Probe(5.0, 5000, 30000, 1000)
    p.spot.append((1000, 100.0))
    assert p._ref_price(2000, 100.0) is None       # 1000 比 (2000-5000) 还新
    p.spot.append((9000, 101.0))
    assert p._ref_price(10000, 101.0) == 100.0


# ---------------------------------------------------------------- twap

def test_norm_cdf_symmetric():
    assert abs(te.norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(te.norm_cdf(1.0) + te.norm_cdf(-1.0) - 1.0) < 1e-12


def test_fair_prob_converges_to_certainty_at_window_end():
    # 窗口结束：TWAP 已实现且高于开盘 → 必 UP
    assert te.fair_prob(101.0, 100.0, 300, 8.0) == 1.0
    assert te.fair_prob(99.0, 100.0, 300, 8.0) == 0.0


def test_fair_prob_is_centered_when_twap_equals_open():
    # 已实现 TWAP 恰等于开盘、窗口刚开始 → 约 50%，容差来自剩余段正偏
    p = te.fair_prob(100.0, 100.0, 60, 8.0)
    assert 0.45 <= p <= 0.55


def test_fair_prob_monotone_in_twap():
    ps = [te.fair_prob(100.0 + d, 100.0, 120, 8.0) for d in (-0.5, -0.1, 0.0, 0.1, 0.5)]
    assert ps == sorted(ps)


def test_fair_prob_small_move_is_ignored_late_in_window():
    """这条是本轮最关键的数学事实：窗口后期 50bps 对 TWAP 影响 < 1 tick。"""
    # 剩余 60s、TWAP 需追 0.02（≈2bps）才能反超 —— 概率应接近但仍小于 1
    p = te.fair_prob(99.98, 100.0, 240, 8.0)
    assert 0.0 < p < 1.0


# ---------------------------------------------------------------- weather

def test_parse_temp_prefers_t_group():
    raw = "METAR KLGA 251451Z 04014KT 10SM FEW045 BKN250 04/M02 A3074 RMK AO2 SLP407 T00441022"
    tc, src = wp.parse_temp_c(raw)
    assert tc == pytest.approx(4.4)
    assert src == "T-group"


def test_parse_temp_negative_t_group():
    tc, src = wp.parse_temp_c("METAR XXXX 251451Z T10171022")
    assert tc == pytest.approx(-1.7)
    assert src == "T-group"


def test_parse_temp_falls_back_to_body():
    tc, src = wp.parse_temp_c("METAR EGLL 251450Z 18008KT 9999 18/12 Q1015")
    assert tc == pytest.approx(18.0)
    assert src == "body"


def test_parse_temp_body_negative():
    tc, src = wp.parse_temp_c("METAR XXXX 251450Z M11/M17")
    assert tc == pytest.approx(-11.0)
    assert src == "body"


def test_parse_temp_missing_returns_none():
    tc, src = wp.parse_temp_c("METAR XXXX 251450Z 00000KT CAVOK")
    assert tc is None and src is None


def test_f_round_half_up_matches_official_examples():
    """官方描述给的四个算例，必须逐个对上。"""
    assert wp.f_round(wp.c_to_f(18.3)) == 65
    assert wp.f_round(wp.c_to_f(17.8)) == 64
    assert wp.f_round(wp.c_to_f(19.0)) == 66
    assert wp.f_round(wp.c_to_f(-22.5)) == -8


def test_parse_obs_extracts_ddhhmm():
    assert wp.parse_obs("METAR KLGA 251451Z 04014KT") == (25, 14, 51)
    assert wp.parse_obs("no timestamp") is None


def test_event_brackets_parses_fahrenheit_ranges():
    ev = {"markets": [{"question": "Will the highest temperature in NYC be 39°F or below on November 25?",
                       "clobTokenIds": '["a","b"]', "description": "states its range in whole degrees Fahrenheit"},
                      {"question": "Will the highest temperature in NYC be between 44-45°F on November 25?",
                       "clobTokenIds": '["c","d"]', "description": "states its range in whole degrees Fahrenheit"},
                      {"question": "Will the highest temperature in NYC be 58°F or higher on November 25?",
                       "clobTokenIds": '["e","f"]', "description": "states its range in whole degrees Fahrenheit"}]}
    rows, unit = wp.event_brackets(ev)
    assert unit == "F"
    assert len(rows) == 3
    assert rows[0]["lo"] == -999 and rows[0]["hi"] == 39
    assert rows[-1]["lo"] == 58 and rows[-1]["hi"] == 999


def test_event_brackets_detects_celsius_by_declaration_sentence():
    """°F 市场的描述里也会出现 '°C'（换算举例），只查符号会误判。"""
    ev = {"markets": [{"question": "Will the highest temperature in Tokyo be 14°C on November 26?",
                       "clobTokenIds": '["a","b"]',
                       "description": "Each market in this event states its range in whole degrees "
                                      "Celsius. 18.3°C is 64.94°F and counts as 65°F."}]}
    rows, unit = wp.event_brackets(ev)
    assert unit == "C"
    assert len(rows) == 1
    assert rows[0]["lo"] == rows[0]["hi"] == 14


def test_event_brackets_single_value_bracket():
    ev = {"markets": [{"question": "Will the highest temperature in Tokyo be 10°C on November 26?",
                       "clobTokenIds": '["a","b"]',
                       "description": "states its range in whole degrees Celsius"}]}
    rows, unit = wp.event_brackets(ev)
    assert unit == "C"
    assert rows[0]["lo"] == 10 and rows[0]["hi"] == 10


def test_probe_icao_from_resolution_source():
    ev = {"markets": [{"description": "…",
                       "resolutionSource": "https://aviationweather.gov/api/data/metar?ids=KLGA&format=raw"}]}
    assert wp.probe_icao(ev) == "KLGA"


def test_probe_icao_from_parentheses():
    ev = {"markets": [{"description": "reported at Paris-Le Bourget Airport (LFPB) on 25 Nov"}]}
    assert wp.probe_icao(ev) == "LFPB"


def test_probe_icao_returns_none_when_absent():
    ev = {"markets": [{"description": "recorded by the Hong Kong Observatory in degrees Celsius"}]}
    assert wp.probe_icao(ev) is None


def test_day_high_only_triggers_on_new_record():
    """首报不触发，未刷新新高不触发，刷新才触发——否则会把每个整点都当事件。"""
    cities = [("KLGA", {"title": "t", "markets": []},
               [{"lo": -999, "hi": 39, "yes": "Y1", "no": "N1"},
                {"lo": 40, "hi": 41, "yes": "Y2", "no": "N2"},
                {"lo": 48, "hi": 49, "yes": "Y3", "no": "N3"}], "F")]
    w = wp.WeatherWatch(cities, poll_s=1.0)
    w._check_breach("KLGA", 7.0, 1000, (25, 14, 51))     # 首报 7.0°C ≈ 45°F
    assert w.day_high["KLGA"][0] == 7.0
    assert w.events == []                                 # 首报不触发
    w._check_breach("KLGA", 6.0, 2000, (25, 15, 51))     # 更低，不算新高
    assert w.day_high["KLGA"][0] == 7.0
    assert w.events == []
    w._check_breach("KLGA", 9.5, 3000, (25, 16, 51))     # 9.5°C ≈ 49°F → 刷新
    assert w.day_high["KLGA"][0] == 9.5
    assert len(w.events) == 1
    dead = w.events[0]["dead"]
    # 49°F 对应 hi=39 与 40-41 两档归零，48-49 档不归零
    assert "Y1" in dead and "Y2" in dead and "Y3" not in dead


def test_day_high_resets_across_observation_days():
    cities = [("KLGA", {"title": "t", "markets": []},
               [{"lo": -999, "hi": 39, "yes": "Y1", "no": "N1"}], "F")]
    w = wp.WeatherWatch(cities, poll_s=1.0)
    w._check_breach("KLGA", 20.0, 1000, (25, 20, 51))
    assert w.day_high["KLGA"][0] == 20.0
    # 换日：必须重置，否则昨天的高温会把今天全部档位误判归零
    w._check_breach("KLGA", 2.0, 2000, (26, 0, 51))
    assert w.day_high["KLGA"][0] == 2.0
    assert w.events == []


def test_depth_usd_sums_levels():
    assert wp._depth_usd([(0.5, 100.0), (0.51, 50.0)]) == pytest.approx(75.5)
    assert wp._depth_usd(None) == 0.0
    assert wp._depth_usd([(None, 10.0)]) == 0.0

# ------------------------------------------------- weather_replay 的口径保护

def test_batch_limit_prevents_silent_truncation():
    """批量查询超过 5 站会被截断到 400 条，单站记录随之丢失。

    实测：12 站合并时 KLGA 从 48 条掉到 22 条。截断会**低估**当日最高温，
    把仍有竞争力的档位误判成"物理确定归零"——这是会凭空造出假 edge 的错误，
    而且完全静默。所以 MAX_BATCH 必须钉死在 5。
    """
    from research.polymarket import weather_replay as wr
    assert wr.MAX_BATCH == 5
    # 12 站必须切成 3 批，而不是一次丢进去
    calls = []
    orig = wr.W.http_text

    def fake(url, timeout=0):
        calls.append(url)
        return "[]", 0

    wr.W.http_text = fake
    try:
        wr.fetch_metars(["A1", "A2", "A3", "A4", "A5", "A6", "A7",
                         "A8", "A9", "B1", "B2", "B3"])
    finally:
        wr.W.http_text = orig
    assert len(calls) == 3
    for u in calls:
        n = u.split("ids=")[1].split("&")[0].count(",") + 1
        assert n <= wr.MAX_BATCH


def test_price_at_is_strictly_causal():
    """盘口价必须 <= 观测时刻才可用，否则前视泄露。

    这是上一轮 twap_efficiency 栽过的坑：取了"未来"的价格，
    凭空造出 18.5pp 的假 edge。
    """
    from research.polymarket import weather_replay as wr
    hist = [(100, 0.10), (200, 0.20), (300, 0.30)]
    assert wr.price_at(hist, 250) == (0.20, 200)
    assert wr.price_at(hist, 200) == (0.20, 200)
    assert wr.price_at(hist, 99) == (None, None)
    # 恰好等于截止时刻的点也算（<=，不是 <）
    assert wr.price_at(hist, 300) == (0.30, 300)


def test_scan_new_highs_ignores_first_observation():
    """首份观测只建立基准，不算刷新新高。

    否则每天开盘第一份报文都会触发一次"全档归零"，
    把整天本来正常的档位全部误杀。
    """
    import datetime as dt
    from research.polymarket import weather_replay as wr
    t0 = dt.datetime(2026, 11, 24, 0, 51)
    seq = [(t0, 20.0, None, ""),
           (t0 + dt.timedelta(hours=1), 19.0, None, ""),   # 降温，不触发
           (t0 + dt.timedelta(hours=2), 20.0, None, ""),   # 回到基准，不算新高
           (t0 + dt.timedelta(hours=3), 21.5, None, "")]   # 真新高
    events = wr.scan_new_highs(seq)
    assert len(events) == 1
    idx, prev_c, new_c, prev_f, new_f = events[0]
    assert idx == 3 and prev_c == 20.0 and new_c == 21.5
    # 21.5°C = 70.7°F → 71°F；20°C = 68°F
    assert prev_f == 68 and new_f == 71


def test_routine_temps_drops_speci_but_keeps_unlabeled():
    """细则：SPECI 开头的特殊观测不计入；无 METAR/SPECI 标签的算 routine。"""
    import datetime as dt
    from zoneinfo import ZoneInfo
    from research.polymarket import weather_replay as wr
    tz = ZoneInfo("America/New_York")
    base = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)
    rows = [
        {"obsTime": int(base.timestamp()), "rawOb": "METAR KLGA 241200Z 10/05",
         "temp": 10.0},
        {"obsTime": int(base.timestamp()) + 600,
         "rawOb": "SPECI KLGA 241210Z 30/20", "temp": 30.0},   # 必须丢弃
        {"obsTime": int(base.timestamp()) + 1200, "rawOb": "KLGA 241220Z 11/06",
         "temp": 11.0},                                        # 无标签算 routine
    ]
    seq, dropped = wr.routine_temps(rows, tz)
    assert dropped == 1
    assert len(seq) == 2
    assert max(x[1] for x in seq) == 11.0    # 30 度的 SPECI 不能污染日高


# --------------------------------------------- calibration_probe 的口径保护

def test_outcome_label_rejects_void_and_5050():
    """作废/50-50 结算的市场会污染标签，必须剔除。

    outcomePrices 只接受严格的 ["0","1"] 或 ["1","0"]。
    若某天出现 ["0.5","0.5"]（作废退回）或 ["0","0"]（异常），
    当成 y=0 或 y=1 会把校准曲线整体拉偏。
    """
    from research.polymarket import calibration_probe as C
    assert C.outcome_label({"outcomePrices": '["1", "0"]'}) == 1
    assert C.outcome_label({"outcomePrices": '["0", "1"]'}) == 0
    assert C.outcome_label({"outcomePrices": '["0.5", "0.5"]'}) is None
    assert C.outcome_label({"outcomePrices": '["0", "0"]'}) is None
    assert C.outcome_label({"outcomePrices": None}) is None
    assert C.outcome_label({"outcomePrices": '["0.9995", "0.0005"]'}) == 1


def test_price_history_chunks_to_respect_span_limit():
    """prices-history 跨度 >14 天直接 400，不是返回空。

    20 天跨度会 HTTP 400，很容易被误读成"该市场无历史数据"而静默丢弃。
    所以必须分段拉，且每段 <= SPAN_CHUNK_D。
    """
    from research.polymarket import calibration_probe as C
    assert C.SPAN_CHUNK_D <= C.MAX_SPAN_D
    spans = []
    orig = C.ap.http_json

    def fake(url, timeout=0):
        seg = url.split("startTs=")[1].split("&")[0]
        end = url.split("endTs=")[1].split("&")[0]
        spans.append(int(end) - int(seg))
        return {"history": []}

    C.ap.http_json = fake
    try:
        # 跨 30 天，必须切成多段
        C.price_history("TOK", 0, 30 * 86400, fidelity=60)
    finally:
        C.ap.http_json = orig
    assert len(spans) >= 4
    assert all(s <= C.SPAN_CHUNK_D * 86400 for s in spans)
    assert sum(spans) >= 30 * 86400


def test_price_at_respects_causality_in_calibration():
    """校准取样同样必须严格因果——用未来价格会把准确率刷到 100%。"""
    from research.polymarket import calibration_probe as C
    hist = [(1000, 0.2), (2000, 0.8), (3000, 0.9)]
    assert C.price_at(hist, 1500) == 0.2
    assert C.price_at(hist, 2000) == 0.8
    assert C.price_at(hist, 500) is None


def test_fee_rate_falls_back_when_schedule_missing():
    """读不到 feeSchedule 时必须用保守默认值，不能当成 0 费率。

    把费率当 0 会让"净收益"系统性偏高，是典型的假 edge 来源。
    """
    from research.polymarket import calibration_probe as C
    assert C.fee_rate_of({"feeSchedule": {"rate": 0.01}}) == 0.01
    assert C.fee_rate_of({}) == C.DEFAULT_FEE_RATE
    assert C.DEFAULT_FEE_RATE > 0
