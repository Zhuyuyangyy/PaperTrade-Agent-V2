"""影子信号追踪测试（Sprint 4.1）：只读、保守、缺数据不猜。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from research.market.contracts import Candle, TF_1H_MS
from research.runtime.shadow import evaluate_signal, load_shadow_signals, report

D = Decimal
BASE = 1_795_000_000_000 // TF_1H_MS * TF_1H_MS


def _candle(i: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(BASE + i * TF_1H_MS, D(f"{o:.6f}"), D(f"{h:.6f}"), D(f"{l:.6f}"),
                  D(f"{c:.6f}"), D("1"), TF_1H_MS)


def _sig(side="LONG", entry="100", stop="95", tp="110", ts=None):
    from research.runtime.shadow import ShadowSignal

    return ShadowSignal("id1", ts if ts is not None else BASE, side, "trend_follow",
                        D(entry), D(stop), D(tp))


def _write_log(path: Path, signals: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for s in signals:
            fh.write(json.dumps({"selection": {"intent": s}}) + "\n")


def test_loads_only_shadow_intents(tmp_path: Path) -> None:
    log = tmp_path / "d.jsonl"
    _write_log(log, [
        {"intent_id": "a", "decision_ts_ms": BASE, "side": "LONG", "strategy_id": "x",
         "entry_ref_price": "100", "stop_price": "95", "take_profit_price": "110",
         "shadow": True},
        {"intent_id": "b", "decision_ts_ms": BASE, "side": "SHORT", "strategy_id": "x",
         "entry_ref_price": "100", "stop_price": "105", "take_profit_price": "90",
         "shadow": False},
    ])
    sigs = load_shadow_signals(log)
    assert len(sigs) == 1 and sigs[0].intent_id == "a"


def test_horizon_return_direction() -> None:
    candles = [_candle(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(30)]
    long = evaluate_signal(_sig("LONG", ts=BASE + TF_1H_MS), candles, (1, 4))
    short = evaluate_signal(_sig("SHORT", stop="120", tp="80", ts=BASE + TF_1H_MS), candles, (1,))
    assert D(long["horizons"]["1h"]["return_bps"]) > 0
    assert D(short["horizons"]["1h"]["return_bps"]) < 0
    assert long["horizons"]["1h"]["reason"] == "HORIZON"


def test_stop_is_conservative_and_wins_same_bar() -> None:
    # 第 2 小时同时触及 95 与 110：按止损
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 111, 94, 100)]
    out = evaluate_signal(_sig(ts=BASE + TF_1H_MS), candles, (4,))
    assert out["horizons"]["4h"]["reason"] == "STOP"
    assert D(out["horizons"]["4h"]["return_bps"]) < 0


def test_missing_horizon_is_none_not_guess() -> None:
    candles = [_candle(0, 100, 101, 99, 100)]
    out = evaluate_signal(_sig(ts=BASE + TF_1H_MS), candles, (24,))
    assert out["horizons"]["24h"] is None


def test_report_summary() -> None:
    candles = [_candle(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(30)]
    r = report([_sig(ts=BASE + TF_1H_MS)], candles, (1, 24))
    assert r["summary"]["n"] == 1
    assert r["summary"]["horizons"]["1h"]["win_rate"] == "1.000"
    assert r["summary"]["horizons"]["24h"]["n"] == 1
    assert D(r["summary"]["horizons"]["24h"]["mean_bps"]) > 0