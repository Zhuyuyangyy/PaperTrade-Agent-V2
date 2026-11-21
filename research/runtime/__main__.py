"""``python -m research.runtime``：demo / run / dashboard。

demo      三种 regime 各跑一轮，打印完整决策链；
run       用合成数据按小时推进，跑 RunLoop（心跳、熔断、幂等、日志）；
dashboard 只读心跳/日志/账本，输出 JSON 或自包含 HTML。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from research.market.contracts import TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.market.demo import REGIMES, demo_as_of_ms, synthetic_1m
from research.market.klines import resample
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.strategy.registry import DEFAULT_SPECS

from .dashboard import build_state, render_html
from .heartbeat import read as read_heartbeat
from .live import BinanceKlineSource
from .loop import LoopConfig, RunLoop
from .sources import CandlesSource
from .round import run_round

D = Decimal
WARMUP_HOURS = 210          # 1h 需要 >=205 根已收盘 K 线


def _views(base) -> dict:
    return {name: resample(base, ms)[0] for name, ms in
            (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS))}


def _specs(verified: list[str]):
    return tuple(
        replace(s, verification="VERIFIED", verified_by="DEMO-ONLY")
        if s.strategy_id in verified else s
        for s in DEFAULT_SPECS
    )


def _run_demo(args) -> int:
    specs = _specs(args.verified)
    if args.verified:
        print(f"WARNING: --verified={args.verified} 仅用于演示（verified_by=DEMO-ONLY）；"
              f"正式运行不得通过命令行提升策略权限。", file=sys.stderr)
    as_of = demo_as_of_ms()
    with tempfile.TemporaryDirectory() as td:
        for regime in REGIMES:
            broker = PaperBroker(Path(td) / f"ledger_{regime}.sqlite", starting_cash=D("1000"))
            log_path = Path(td) / f"decisions_{regime}.jsonl"
            res = run_round("BTC/USDT", _views(synthetic_1m(regime)), as_of, broker,
                            RiskLimits(), log_path=log_path, specs=specs)
            sel, risk = res.selection, res.risk
            intent = sel.intent
            print(f"[{regime}] decision={sel.decision}"
                  + (f" strategy={intent.strategy_id} shadow={intent.shadow}" if intent else ""))
            print(f"  state: {res.state.trend}/{res.state.volatility}/{res.state.momentum}/"
                  f"{res.state.risk} conf={res.state.confidence}")
            if intent:
                print(f"  intent: {intent.side} entry={intent.entry_ref_price} "
                      f"stop={intent.stop_price} tp={intent.take_profit_price}")
            if risk:
                print(f"  risk: {risk.decision} {risk.codes} — {risk.reason}")
            else:
                print("  risk: (无意图，未进入风控)")
            print(f"  fill: {res.fill.to_dict() if res.fill else 'none'}")
            print(f"  hold: {sel.hold_reason or '-'}")
            broker.close()
    return 0


def _run_loop(args) -> int:
    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="openalpha_run_"))
    root.mkdir(parents=True, exist_ok=True)
    base = synthetic_1m(args.regime)
    source = CandlesSource(base)
    start = base[0].open_ms + WARMUP_HOURS * TF_1H_MS
    until = start + args.hours * TF_1H_MS
    broker = PaperBroker(root / "ledger.sqlite", starting_cash=D("1000"))
    loop = RunLoop(root, broker, RiskLimits(), source, specs=_specs(args.verified),
                   config=LoopConfig(interval_ms=args.interval_ms, max_rounds=args.max_rounds))
    summary = loop.run(start, until)
    for res in loop.results:
        intent = res.selection.intent
        print(f"[{res.state.as_of_ms}] {res.selection.decision}"
              + (f" {intent.strategy_id} shadow={intent.shadow}" if intent else "")
              + f" risk={res.risk.decision if res.risk else 'NONE'}"
              + f" fill={'yes' if res.fill else 'no'} equity={res.equity}")
    hb = read_heartbeat(loop.heartbeat_path)
    print(f"\nsummary: {json.dumps(summary, ensure_ascii=False)}")
    print(f"heartbeat: {hb.to_dict() if hb else None}")
    print(f"root: {root}")
    broker.close()
    return 0


def _run_live(args) -> int:
    """真实数据跑一轮（或 N 轮）：只读公开 K 线，模拟下单，默认影子。"""
    root = Path(args.root) if args.root else Path("data/openalpha_live")
    root.mkdir(parents=True, exist_ok=True)
    source = BinanceKlineSource(symbol=args.symbol, limit=args.limit,
                                timeout_s=args.timeout_s, retries=1)
    broker = PaperBroker(root / "ledger.sqlite", starting_cash=D("1000"))
    specs = _specs(args.verified)
    limits = RiskLimits()
    for i in range(args.rounds):
        as_of = int(time.time() * 1000)
        views = source.candles(as_of)
        res = run_round("BTC/USDT", views, as_of, broker, limits,
                        log_path=root / "data" / "decisions.jsonl", specs=specs)
        intent = res.selection.intent
        print(f"[{as_of}] state={res.state.trend}/{res.state.volatility}/"
              f"{res.state.momentum}/{res.state.risk} conf={res.state.confidence}")
        print(f"  decision={res.selection.decision}"
              + (f" {intent.strategy_id} shadow={intent.shadow}" if intent else ""))
        if res.risk:
            print(f"  risk={res.risk.decision} {res.risk.codes} — {res.risk.reason}")
        if res.fill:
            print(f"  fill={res.fill.to_dict()}")
        if res.selection.hold_reason:
            print(f"  hold={res.selection.hold_reason}")
        print(f"  equity={res.equity}  source.stats={source.stats()}")
        if i + 1 < args.rounds:
            time.sleep(args.sleep_s)
    broker.close()
    print(f"root={root}  ledger={root / 'ledger.sqlite'}")
    return 0


def _run_dashboard(args) -> int:
    root = Path(args.root)
    state = build_state(root, args.now_ms)
    if args.html:
        Path(args.html).write_text(render_html(state), encoding="utf-8")
        print(f"html -> {args.html}")
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="research.runtime", description="OpenAlpha 自主模拟运行时")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="三种 regime 的完整决策链")
    p_demo.add_argument("--verified", nargs="*", default=[], help="演示用：标记 VERIFIED（DEMO-ONLY）")

    p_run = sub.add_parser("run", help="按小时推进的运行循环（合成数据）")
    p_run.add_argument("--root", default=None)
    p_run.add_argument("--regime", choices=REGIMES, default="trend_up")
    p_run.add_argument("--hours", type=int, default=24)
    p_run.add_argument("--interval-ms", type=int, default=TF_1H_MS)
    p_run.add_argument("--max-rounds", type=int, default=None)
    p_run.add_argument("--verified", nargs="*", default=[])

    p_live = sub.add_parser("live", help="真实 Binance K 线跑一轮/N 轮（模拟，默认影子）")
    p_live.add_argument("--root", default=None)
    p_live.add_argument("--symbol", default="BTCUSDT")
    p_live.add_argument("--rounds", type=int, default=1)
    p_live.add_argument("--sleep-s", type=float, default=300.0)
    p_live.add_argument("--limit", type=int, default=300)
    p_live.add_argument("--timeout-s", type=float, default=15.0)
    p_live.add_argument("--verified", nargs="*", default=[])

    p_dash = sub.add_parser("dashboard", help="只读看板（JSON 或 HTML）")
    p_dash.add_argument("--root", required=True)
    p_dash.add_argument("--now-ms", type=int, default=0)
    p_dash.add_argument("--html", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "demo":
        return _run_demo(args)
    if args.cmd == "run":
        return _run_loop(args)
    if args.cmd == "live":
        return _run_live(args)
    return _run_dashboard(args)


if __name__ == "__main__":
    raise SystemExit(main())