"""影子记录器的离线测试。

全部用合成数据，不依赖 shadow_data（11MB，gitignore）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "research" / "lite", ROOT / "research" / "live"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import funding_shadow as fs  # noqa: E402
import phase2b_signal as p2b  # noqa: E402

DAY = fs.DAY_MS


def _ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def synth(spec, start=_ms(2026, 8, 1), end=_ms(2026, 12, 31)):
    """价格恒定、funding 按币序排列：只用 funding 与成本，便于手算。"""
    bars_all, fund_all = {}, {}
    days = list(range(start, end + DAY, DAY))
    for i, sym in enumerate(spec["universe"]):
        bars_all[sym] = {
            d: {"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0,
                "qv": 1e9 - i * 1e6, "digest": f"{i:02x}{n:08x}"}
            for n, d in enumerate(days)}
        f_ts = [d + h * 3600_000 for d in days for h in (0, 8, 16)]
        rate = (i - 6) * 1e-4
        fund_all[sym] = (f_ts, [rate] * len(f_ts))
    return bars_all, fund_all


@pytest.fixture(scope="module")
def spec():
    return fs.load_config()["spec"]


# ---------------------------------------------------------------- 日历

def test_grid_starts_at_first_monday_after_start(spec):
    grid = fs.weekly_grid(spec, _ms(2026, 11, 25), _ms(2026, 12, 15))
    assert fs.al.utc(grid[0])[:10] == "2026-11-30"
    assert fs.al.utc(grid[1])[:10] == "2026-12-07"
    assert all((b - a) == 7 * DAY for a, b in zip(grid, grid[1:]))


def test_next_monday_is_strictly_future():
    now = _ms(2026, 11, 25) + 3 * 3600_000
    nxt = fs.next_monday_utc(now)
    assert nxt > now
    assert datetime.fromtimestamp(nxt / 1000, tz=timezone.utc).weekday() == 0
    assert fs.al.utc(nxt)[:10] == "2026-11-30"


# ---------------------------------------------------------------- 结算

def test_settle_prices_and_funding(spec):
    bars, fund = synth(spec, _ms(2026, 11, 1), _ms(2026, 11, 30))
    # 单一币，价格不变，funding 每 8h 0.0001 → 6 天 18 次 = 0.0018
    bars["BTCUSDT"][_ms(2026, 11, 30)]["o"] = 101.0
    gross, detail = fs.settle({"BTCUSDT": 1.0}, bars, fund,
                              _ms(2026, 11, 24), _ms(2026, 11, 30))
    # BTCUSDT 是下标 0，(0-6)*1e-4 = -6e-4/次
    # 18 次结算 × (-6e-4) = -0.0108，换成百分比是 -1.08
    assert detail["BTCUSDT"]["funding_pct"] == pytest.approx(-1.08, abs=1e-9)
    assert detail["BTCUSDT"]["price_ret_pct"] == pytest.approx(1.0, abs=1e-9)
    assert gross == pytest.approx(0.01 - (18 * -6e-4), abs=1e-12)


def test_turnover_counts_both_sides():
    assert fs.turnover({}, {"A": 1 / 6, "B": -1 / 6}) == pytest.approx(1 / 3)
    assert fs.turnover({"A": 1 / 6}, {"A": 1 / 6}) == 0.0
    assert fs.turnover({"A": 1 / 6}, {"B": 1 / 6}) == pytest.approx(1 / 3)


# ---------------------------------------------------------------- 一周

def _state(tmp: Path) -> Path:
    p = tmp / "state.json"
    p.write_text(json.dumps({
        "version": 1, "name": "test", "variant": "ungated",
        "nav": {k: 1.0 for k in ("6", "12", "18", "24")},
        "position": {}, "last_rebalance_day": None, "weeks": 0,
    }), encoding="utf-8")
    return p


def test_first_week_charges_entry_cost_only(spec, tmp_path, monkeypatch):
    bars, fund = synth(spec)
    monkeypatch.setattr(fs, "load_all", lambda s: (bars, fund))
    st_path = _state(tmp_path)
    rec = fs.run_once(refresh=False, now_ms=_ms(2026, 11, 30) + 3600_000,
                      state_path=st_path, ledger_path=tmp_path / "l.jsonl")

    assert rec["event"] == "rebalance"
    assert rec["decision_day"] == "2026-11-30"
    assert rec["settled_week"] is None
    assert rec["turnover"] == pytest.approx(1.0)
    assert len(rec["position"]) == 6
    # 期初空仓 → 只付入场成本，没有收益
    assert rec["nav"]["12"] == pytest.approx(1.0 - 0.0012, abs=1e-12)
    assert rec["nav"]["24"] == pytest.approx(1.0 - 0.0024, abs=1e-12)
    assert rec["nav"]["6"] == pytest.approx(1.0 - 0.0006, abs=1e-12)


def test_same_week_is_idempotent(spec, tmp_path, monkeypatch):
    bars, fund = synth(spec)
    monkeypatch.setattr(fs, "load_all", lambda s: (bars, fund))
    st_path = _state(tmp_path)
    led = tmp_path / "l.jsonl"
    now = _ms(2026, 11, 30) + 3600_000
    fs.run_once(refresh=False, now_ms=now, state_path=st_path, ledger_path=led)
    second = fs.run_once(refresh=False, now_ms=now + 3600_000,
                         state_path=st_path, ledger_path=led)
    assert second["event"] == "skip"
    assert json.loads(st_path.read_text(encoding="utf-8"))["weeks"] == 1


def test_second_week_settles_and_carries_nav(spec, tmp_path, monkeypatch):
    bars, fund = synth(spec)
    monkeypatch.setattr(fs, "load_all", lambda s: (bars, fund))
    st_path = _state(tmp_path)
    led = tmp_path / "l.jsonl"
    fs.run_once(refresh=False, now_ms=_ms(2026, 11, 30) + 3600_000,
                state_path=st_path, ledger_path=led)
    rec = fs.run_once(refresh=False, now_ms=_ms(2026, 12, 7) + 3600_000,
                      state_path=st_path, ledger_path=led)

    assert rec["event"] == "rebalance"
    assert rec["settled_week"]["from"] == "2026-11-30"
    assert rec["settled_week"]["to"] == "2026-12-07"
    assert rec["missed_weeks"] == 0
    gross = rec["settled_week"]["gross_pct"] / 100
    assert gross > 0                                    # 空高费率、多负费率，应赚钱
    expect = (1.0 - 0.0012) * (1 + gross) - rec["cost_pct"]["12"] / 100
    assert rec["nav"]["12"] == pytest.approx(expect, abs=1e-5)   # 落盘保留 6 位


def test_missing_week_is_recorded_not_hidden(spec, tmp_path, monkeypatch):
    bars, fund = synth(spec)
    monkeypatch.setattr(fs, "load_all", lambda s: (bars, fund))
    st_path = _state(tmp_path)
    led = tmp_path / "l.jsonl"
    fs.run_once(refresh=False, now_ms=_ms(2026, 11, 30) + 3600_000,
                state_path=st_path, ledger_path=led)
    rec = fs.run_once(refresh=False, now_ms=_ms(2026, 12, 14) + 3600_000,
                      state_path=st_path, ledger_path=led)
    assert rec["missed_weeks"] == 1


def test_record_carries_cost_policy_and_fingerprint(spec, tmp_path, monkeypatch):
    bars, fund = synth(spec)
    monkeypatch.setattr(fs, "load_all", lambda s: (bars, fund))
    st_path = _state(tmp_path)
    rec = fs.run_once(refresh=False, now_ms=_ms(2026, 11, 30) + 3600_000,
                      state_path=st_path, ledger_path=tmp_path / "l.jsonl")
    assert rec["cost_model"]["model_bps_per_side"] == 12.0
    assert rec["sensitivity_bps_per_side"] == [6.0, 12.0, 18.0, 24.0]
    assert len(rec["data"]["window_fingerprint"]) == 16
    assert len(rec["data"]["per_symbol"]) == len(spec["universe"])
