"""实盘信号层与 dryrun 运行器的护栏测试。

全部离线：用合成日线，不依赖 research/lite/data 下的 23MB 行情快照
（那些文件被 gitignore，CI 上没有）。
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "research" / "lite", ROOT / "research" / "live"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import voltarget_runner as vr  # noqa: E402
import voltarget_signal as sv  # noqa: E402
from research.live.ladder import HALT_RULES, Ladder  # noqa: E402

BASE = 1_760_000_000_000 - (1_760_000_000_000 % sv.DAY_MS)


def synth_days(n: int = 80, vol: float = 0.03, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    days, px = [], 30_000.0
    for i in range(n):
        r = rng.gauss(0.0005, vol)
        o = px
        c = px * (1 + r)
        days.append({"day": BASE + i * sv.DAY_MS, "o": o, "h": max(o, c) * 1.002,
                     "l": min(o, c) * 0.998, "c": c, "hours": 24,
                     "digest": f"{i:016x}"})
        px = c
    return days


def fresh_state() -> dict:
    return {"version": 1, "nav": 1.0, "weight": 0.0, "pending": None,
            "last_settled_day": None, "last_decision_day": None,
            "halted": False, "halt_reason": "", "period": {"cum": 1.0, "days": 0},
            "decisions": 0, "settled": 0}


@pytest.fixture(scope="module")
def cfg():
    return sv.load_config()


# ---------------------------------------------------------------- 缓冲带

def test_buffer_holds_below_threshold_and_moves_above():
    assert sv.apply_buffer(0.55, 0.50, 0.10) == (0.50, False)   # 5pp < 10pp，不动
    assert sv.apply_buffer(0.61, 0.50, 0.10) == (0.61, True)    # 11pp > 10pp，调
    # 边界：正好等于缓冲带不调（严格大于才调）
    assert sv.apply_buffer(0.60, 0.50, 0.10) == (0.50, False)


# ---------------------------------------------------------------- 信号

def test_compute_matches_manual_sigma(cfg):
    days = synth_days()
    sig = sv.compute(days, cfg, prev_weight=0.0, now_ms=days[-1]["day"] + 3600_000)
    rets = [0.0] + [days[i]["c"] / days[i - 1]["c"] - 1 for i in range(1, len(days))]
    hist = rets[len(days) - 20:len(days)]
    import statistics, math
    expect = statistics.stdev(hist) * math.sqrt(365)
    assert sig["sigma20_ann"] == pytest.approx(expect, abs=1e-8)
    assert sig["raw_target"] == pytest.approx(min(0.30 / expect, 1.0), abs=1e-8)


def test_compute_targets_the_next_utc_day(cfg):
    days = synth_days()
    sig = sv.compute(days, cfg, 0.0, now_ms=days[-1]["day"] + sv.DAY_MS)
    assert sig["decision_for_day_ms"] == days[-1]["day"] + sv.DAY_MS
    assert sig["decision_for_day"] == sv.al.utc(days[-1]["day"] + sv.DAY_MS)[:10]


def test_weight_never_exceeds_one_and_never_negative(cfg):
    for vol in (0.001, 0.02, 0.20, 0.80):
        days = synth_days(n=90, vol=vol, seed=3)
        sig = sv.compute(days, cfg, 0.0, now_ms=days[-1]["day"] + sv.DAY_MS)
        assert 0.0 <= sig["raw_target"] <= 1.0


# ---------------------------------------------------------------- 对齐

def _reference_returns(days: list[dict], win: int = 20, target: float = 0.30,
                       buf: float = 0.10, cost_bps: float = 11.0):
    """独立的第二实现，逐项照 Phase 3 回测的语义重写一遍。"""
    rets = [0.0] + [days[i]["c"] / days[i - 1]["c"] - 1 for i in range(1, len(days))]
    import statistics, math
    out, w = [], 0.0
    for i in range(win + 1, len(days)):
        sig = statistics.stdev(rets[i - win:i]) * math.sqrt(365)
        raw = min(target / sig, 1.0) if sig > 0 else 0.0
        w_new = raw if abs(raw - w) > buf else w
        overnight = w * (days[i]["o"] / days[i - 1]["c"] - 1)
        intraday = w_new * (days[i]["c"] / days[i]["o"] - 1)
        cost = abs(w_new - w) * cost_bps / 10_000.0
        out.append((days[i]["day"], overnight + intraday - cost))
        w = w_new
    return out


def test_replay_matches_independent_reference(cfg):
    days = synth_days(n=120, vol=0.04)
    mine = sv.replay_returns(days, cfg)
    ref = _reference_returns(days)
    assert len(mine) == len(ref)
    for (a_t, a_r), (b_t, b_r) in zip(mine, ref):
        assert a_t == b_t
        assert abs(a_r - b_r) < 1e-12


# ---------------------------------------------------------------- 冻结

def test_gap_in_signal_window_freezes(cfg, tmp_path):
    days = synth_days()
    holed = [d for d in days if d["day"] != days[-5]["day"]]
    st = fresh_state()
    lad = vr.load_ladder(cfg, tmp_path / "ladder.json")
    out = vr.process(cfg, st, lad, holed, days[-1]["day"] + sv.DAY_MS,
                     offline=True, ledger_path=tmp_path / "ledger.jsonl")
    assert out["event"] == "halt"
    assert "缺口" in out["reason"]
    assert st["halted"] is True
    assert st["last_decision_day"] is None      # 冻结后不产出新决策


def test_stale_feed_freezes(cfg, tmp_path):
    days = synth_days()
    st = fresh_state()
    lad = vr.load_ladder(cfg, tmp_path / "ladder.json")
    out = vr.process(cfg, st, lad, days, days[-1]["day"] + 4 * sv.DAY_MS,
                     offline=True, ledger_path=tmp_path / "ledger.jsonl")
    assert out["event"] == "halt"
    assert "陈旧" in out["reason"]
    assert st["halted"] is True


def test_reconciliation_mismatch_freezes(cfg, tmp_path):
    days = synth_days()
    st = fresh_state()
    st["pending"] = {"day": days[-1]["day"] + sv.DAY_MS, "prev_weight": 0.0,
                     "new_weight": 0.5, "cost": 0.0, "closed_period": True}
    st["weight"] = 0.37       # 账本被手工改过
    lad = vr.load_ladder(cfg, tmp_path / "ladder.json")
    out = vr.process(cfg, st, lad, days, days[-1]["day"] + sv.DAY_MS,
                     offline=True, ledger_path=tmp_path / "ledger.jsonl")
    assert out["event"] == "halt"
    assert "对账不平" in out["reason"]
    assert st["halted"] is True


# ---------------------------------------------------------------- 幂等与账本

def test_same_decision_day_is_not_executed_twice(cfg, tmp_path):
    days = synth_days()
    st = fresh_state()
    lad = vr.load_ladder(cfg, tmp_path / "ladder.json")
    now = days[-1]["day"] + sv.DAY_MS
    first = vr.process(cfg, st, lad, days, now, offline=True,
                       ledger_path=tmp_path / "ledger.jsonl")
    assert first["event"] == "decision"
    second = vr.process(cfg, st, lad, days, now, offline=True,
                        ledger_path=tmp_path / "ledger.jsonl")
    assert second["event"] == "skip"
    assert st["decisions"] == 1


def test_settlement_updates_nav_and_closes_period(cfg, tmp_path):
    days = synth_days(n=90, vol=0.02)
    st = fresh_state()
    lad = vr.load_ladder(cfg, tmp_path / "ladder.json")
    led = tmp_path / "ledger.jsonl"

    # 第一天：决策（从空仓建仓，不构成"平仓"，不记假交易）
    now1 = days[-1]["day"] + sv.DAY_MS
    vr.process(cfg, st, lad, days, now1, offline=True, ledger_path=led)
    assert st["pending"]["day"] == days[-1]["day"] + sv.DAY_MS
    assert lad.stats()["n"] == 0

    # 第二天：多了新的一根日线，应结算前一笔
    days2 = synth_days(n=90, vol=0.02) + [{
        "day": days[-1]["day"] + sv.DAY_MS,
        "o": days[-1]["c"], "h": days[-1]["c"] * 1.01,
        "l": days[-1]["c"] * 0.99, "c": days[-1]["c"] * 1.005,
        "hours": 24, "digest": "ff" * 8}]
    out = vr.process(cfg, st, lad, days2, now1 + sv.DAY_MS, offline=True, ledger_path=led)
    assert out["settled"] is not None
    assert st["settled"] == 1
    assert st["nav"] != 1.0
    # 持仓期要么还开着（累计 1 天），要么因为换仓被结算成 1 笔
    assert st["period"]["days"] == 1 or lad.stats()["n"] == 1


def test_ladder_halt_rules_are_per_instance():
    """给日频策略放宽断流阈值，不能污染其它策略的默认值。"""
    default = Ladder(name="a")
    daily = Ladder(name="b", halt_rules={"max_consecutive_losses": 99,
                                         "max_data_gap_s": 172800,
                                         "reconciliation_tolerance": 0})
    assert default.halt_rules["max_data_gap_s"] == HALT_RULES["max_data_gap_s"]
    assert daily.halt_rules["max_data_gap_s"] == 172800
    assert daily.evaluate(data_age_s=3600).action == "hold"
    assert default.evaluate(data_age_s=3600).action == "halt"
