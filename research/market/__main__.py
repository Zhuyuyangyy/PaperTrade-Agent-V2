"""``python -m research.market``：demo / analyze（只读、离线、确定性）。

demo   三种合成 regime（趋势/震荡/高波动）→ MarketState，用来演示"有证据的判断"；
analyze  读 1m CSV → 聚合 5m/15m/1h/4h → MarketState（只吃已收盘 K 线）。
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from .contracts import Candle, TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS, TIMEFRAMES
from .demo import REGIMES, demo_as_of_ms, synthetic_1m
from .klines import load_csv, resample
from .state import analyze


def _stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _views_from_1m(base: list[Candle]) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = {}
    for name, ms in (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS)):
        candles, _ = resample(base, ms)
        if candles:
            out[name] = candles
    return out


def _run_demo(as_json: bool) -> int:
    as_of = demo_as_of_ms()
    for regime in REGIMES:
        state = analyze("BTC/USDT", _views_from_1m(synthetic_1m(regime)), as_of)
        if as_json:
            print(json.dumps({"regime": regime, **state.to_dict()}, ensure_ascii=False, indent=2))
        else:
            print(f"[{regime}] {state.describe()}")
            print(f"  evidence={len(state.evidence)} contradictions={len(state.contradictions)} "
                  f"gaps={len(state.data_quality.gaps)} tf={list(state.timeframes)}")
            for item in state.invalidations:
                print(f"  失效条件: {item}")
    return 0


def _run_analyze(csv_path: Path, as_of_ms: int | None, settle_ms: int, as_json: bool) -> int:
    base = load_csv(csv_path, 60_000)
    if not base.candles:
        print("没有可用 K 线（检查 t 是否为毫秒且对齐整分钟）", file=sys.stderr)
        return 2
    as_of = as_of_ms if as_of_ms is not None else base.candles[-1].close_ms + 1_000
    state = analyze("BTC/USDT", _views_from_1m(base.candles), as_of, settle_ms=settle_ms)
    if as_json:
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(state.describe())
        for ev in state.evidence:
            print(f"  证据: {ev.code} [{ev.timeframe}] {ev.threshold} value={ev.value}")
        for c in state.contradictions:
            print(f"  冲突: {c.code} — {c.a} vs {c.b}")
        for item in state.invalidations:
            print(f"  失效条件: {item}")
        dq = state.data_quality
        print(f"  数据: tf={list(state.timeframes)} gaps={len(dq.gaps)} age={dq.age_ms}ms stale={dq.stale}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _stdout_utf8()
    parser = argparse.ArgumentParser(prog="research.market", description="OpenAlpha L2 市场理解层（只读）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="三种合成 regime 的状态输出")
    p_demo.add_argument("--json", action="store_true")

    p_an = sub.add_parser("analyze", help="从 1m CSV 聚合多周期并输出 MarketState")
    p_an.add_argument("--csv", required=True, type=Path)
    p_an.add_argument("--as-of", type=int, default=None, help="决策时刻（UTC 毫秒）")
    p_an.add_argument("--settle-ms", type=int, default=30_000, help="K 线收盘宽限（默认 30s）")
    p_an.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "demo":
        return _run_demo(args.json)
    return _run_analyze(args.csv, args.as_of, args.settle_ms, args.json)


if __name__ == "__main__":
    raise SystemExit(main())