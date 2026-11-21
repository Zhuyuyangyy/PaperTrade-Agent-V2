"""``python -m research.runtime``：demo / run / live / replay / report / shadow / dashboard。"""
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
from .metrics import summarize_books
from .sources import CandlesSource
from .shadow import load_shadow_signals, report as shadow_report
from .round import run_round

D = Decimal
WARMUP_HOURS = 210


def _views(base) -> dict:
    return {name: resample(base, ms)[0] for name, ms in
            (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS))}


def _specs(verified: list[str]):
    return tuple(
        replace(s, verification="VERIFIED", verified_by="DEMO-ONLY")
        if s.strategy_id in verified else s
        for s in DEFAULT_SPECS
    )


def _books(root: Path, funding_bps: Decimal) -> tuple[PaperBroker, PaperBroker]:
    return (
        PaperBroker(root / "verified.sqlite", starting_cash=D("1000"),
                    funding_bps_per_8h=funding_bps),
        PaperBroker(root / "shadow.sqlite", starting_cash=D("1000"),
                    funding_bps_per_8h=funding_bps, shadow_mode="shadow_only"),
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
            if risk:
                print(f"  risk: {risk.decision} {risk.codes} — {risk.reason}")
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
    verified, shadow = _books(root, D("0"))
    loop = RunLoop(root, verified, RiskLimits(), source, specs=_specs(args.verified),
                   shadow_broker=shadow,
                   config=LoopConfig(interval_ms=args.interval_ms, max_rounds=args.max_rounds))
    summary = loop.run(start, until)
    for res in loop.results:
        print(f"[{res.state.as_of_ms}] {res.selection.decision}"
              f" risk={res.risk.decision if res.risk else 'NONE'}"
              f" fill={'yes' if res.fill else 'no'}"
              f" shadow_fill={'yes' if res.log.get('shadow_fill') else 'no'}")
    print(f"\nsummary: {json.dumps(summary, ensure_ascii=False)}")
    print(json.dumps(summarize_books(root), ensure_ascii=False, indent=2))
    print(f"root: {root}")
    verified.close()
    shadow.close()
    return 0


def _run_live(args) -> int:
    """真实数据 → RunLoop：验证盘 + 实验盘双账本，心跳/熔断/幂等/日志一条路径。"""
    root = Path(args.root) if args.root else Path("data/openalpha_live")
    root.mkdir(parents=True, exist_ok=True)
    source = BinanceKlineSource(symbol=args.symbol, limit=args.limit,
                                timeout_s=args.timeout_s, retries=1)
    verified, shadow = _books(root, D(str(args.funding_bps)))
    loop = RunLoop(
        root, verified, RiskLimits(), source, shadow_broker=shadow,
        log_path=root / "data" / "decisions.jsonl",
        config=LoopConfig(interval_ms=args.interval_ms,
                          decision_period_ms=args.interval_ms,
                          max_rounds=args.rounds),
    )
    sleep_s = args.sleep_s if args.sleep_s is not None else max(1.0, args.interval_ms / 1000)
    for i in range(args.rounds):
        as_of = int(time.time() * 1000)
        res = loop.step(as_of)
        if res is None:
            print(f"[{as_of}] skipped: 同一根 K 线已决策过（key={loop._round_key(as_of)}）")
        else:
            intent = res.selection.intent
            print(f"[{as_of}] state={res.state.trend}/{res.state.volatility}/"
                  f"{res.state.momentum}/{res.state.risk} conf={res.state.confidence}")
            print(f"  decision={res.selection.decision}"
                  + (f" {intent.strategy_id} shadow={intent.shadow}" if intent else ""))
            if res.risk:
                print(f"  risk={res.risk.decision} {res.risk.codes} — {res.risk.reason}")
            print(f"  fill={res.fill.to_dict() if res.fill else 'none'}")
            print(f"  shadow_fill={res.log.get('shadow_fill') or 'none'}")
            print(f"  equity={res.equity} shadow_equity={res.log.get('shadow_equity')}")
        print(f"  source.stats={source.stats()} circuit={loop.circuit.to_dict()}")
        if i + 1 < args.rounds:
            time.sleep(sleep_s)
    print("\nbook summary:")
    print(json.dumps(summarize_books(root), ensure_ascii=False, indent=2))
    hb = read_heartbeat(loop.heartbeat_path)
    print(f"heartbeat: {hb.to_dict() if hb else None}")
    print(f"root={root}")
    verified.close()
    shadow.close()
    return 0


def _run_replay(args) -> int:
    """真实历史 1h K 线 → 双账本跑 N 轮（默认 72），用于 72h 实验盘回放。"""
    root = Path(args.root) if args.root else Path(tempfile.mkdtemp(prefix="openalpha_replay_"))
    root.mkdir(parents=True, exist_ok=True)
    live = BinanceKlineSource(symbol=args.symbol, limit=args.limit,
                              timeout_s=args.timeout_s, retries=1)
    try:
        views = live.candles(int(time.time() * 1000))
    except Exception as exc:  # noqa: BLE001
        print(f"拉取历史 K 线失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    base = views.get("1h") or []
    if len(base) < WARMUP_HOURS + args.rounds + 2:
        print(f"1h K 线不足：{len(base)} 根，需要 >= {WARMUP_HOURS + args.rounds + 2}", file=sys.stderr)
        return 1
    source = CandlesSource(base)
    start = base[WARMUP_HOURS].close_ms + 31_000
    until = start + (args.rounds - 1) * TF_1H_MS
    verified, shadow = _books(root, D(str(args.funding_bps)))
    loop = RunLoop(root, verified, RiskLimits(), source, shadow_broker=shadow,
                   log_path=root / "data" / "decisions.jsonl",
                   config=LoopConfig(interval_ms=TF_1H_MS,
                                     decision_period_ms=TF_1H_MS,
                                     max_rounds=args.rounds))
    summary = loop.run(start, until)
    print(f"replay: {summary}")
    print(json.dumps(summarize_books(root), ensure_ascii=False, indent=2))
    print(f"root={root}")
    verified.close()
    shadow.close()
    return 0


def _run_report(args) -> int:
    print(json.dumps(summarize_books(Path(args.root)), ensure_ascii=False, indent=2))
    return 0


def _run_shadow(args) -> int:
    root = Path(args.root)
    signals = load_shadow_signals(root / "data" / "decisions.jsonl")
    if not signals:
        print("没有影子信号（先跑 live）")
        return 0
    source = BinanceKlineSource(symbol=args.symbol, limit=args.limit,
                                timeout_s=args.timeout_s, retries=1)
    try:
        candles = source.candles(int(time.time() * 1000)).get("1h", [])
    except Exception as exc:  # noqa: BLE001 - 影子评估失败只需如实报告
        print(f"拉取 K 线失败，无法评估影子信号: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(shadow_report(signals, candles, tuple(args.horizons)),
                     ensure_ascii=False, indent=2))
    return 0


def _run_dashboard(args) -> int:
    state = build_state(Path(args.root), args.now_ms)
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

    p_live = sub.add_parser("live", help="真实 Binance K 线 → RunLoop（双账本、心跳、熔断、幂等）")
    p_live.add_argument("--root", default=None)
    p_live.add_argument("--symbol", default="BTCUSDT")
    p_live.add_argument("--rounds", type=int, default=1)
    p_live.add_argument("--interval-ms", type=int, default=TF_1H_MS)
    p_live.add_argument("--sleep-s", type=float, default=None)
    p_live.add_argument("--limit", type=int, default=300)
    p_live.add_argument("--timeout-s", type=float, default=15.0)
    p_live.add_argument("--funding-bps", type=float, default=1.0,
                        help="资金费（bps / 8h，默认 1bp = 0.01%%）；正数多头支付")

    p_replay = sub.add_parser("replay", help="真实历史 1h K 线 → 双账本跑 N 轮（72h 回放）")
    p_replay.add_argument("--root", default=None)
    p_replay.add_argument("--symbol", default="BTCUSDT")
    p_replay.add_argument("--rounds", type=int, default=72)
    p_replay.add_argument("--limit", type=int, default=500)
    p_replay.add_argument("--timeout-s", type=float, default=20.0)
    p_replay.add_argument("--funding-bps", type=float, default=1.0)

    p_report = sub.add_parser("report", help="双账本统计（现金/成交/胜率/回撤）")
    p_report.add_argument("--root", required=True)

    p_shadow = sub.add_parser("shadow", help="影子信号之后的 +1h/4h/24h 表现（保守止损优先）")
    p_shadow.add_argument("--root", required=True)
    p_shadow.add_argument("--symbol", default="BTCUSDT")
    p_shadow.add_argument("--limit", type=int, default=500)
    p_shadow.add_argument("--timeout-s", type=float, default=15.0)
    p_shadow.add_argument("--horizons", nargs="*", type=int, default=[1, 4, 24])

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
    if args.cmd == "replay":
        return _run_replay(args)
    if args.cmd == "report":
        return _run_report(args)
    if args.cmd == "shadow":
        return _run_shadow(args)
    return _run_dashboard(args)


if __name__ == "__main__":
    raise SystemExit(main())