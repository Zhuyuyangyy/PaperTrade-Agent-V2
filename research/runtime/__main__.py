"""``python -m research.runtime demo``：完整自主决策演示（只读、模拟、离线）。

对 market demo 的三种 regime 各跑一轮：
MarketState → StrategyRegistry → TradeIntent → RiskDecision → PaperBroker → DecisionLog

默认所有策略都是 ``UNTESTED``（只产生影子意图，不成交）；
``--verified trend_follow`` 会把指定策略标为 VERIFIED，演示成交路径。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from research.market.contracts import TF_15M_MS, TF_1H_MS, TF_4H_MS, TF_5M_MS
from research.market.demo import REGIMES, demo_as_of_ms, synthetic_1m
from research.market.klines import resample
from research.paper.broker import PaperBroker
from research.risk.engine import RiskLimits
from research.runtime.round import run_round
from research.strategy.registry import DEFAULT_SPECS

D = Decimal


def _views(base) -> dict:
    return {name: resample(base, ms)[0] for name, ms in
            (("5m", TF_5M_MS), ("15m", TF_15M_MS), ("1h", TF_1H_MS), ("4h", TF_4H_MS))}


def _specs(verified: list[str]):
    """演示专用：把指定策略标成 VERIFIED。

    正式运行入口不允许这样做——``VERIFIED`` 只能由独立验证报告授予
    （``StrategySpec.verified_by`` 必填，且构造时校验）。
    """
    return tuple(
        replace(s, verification="VERIFIED", verified_by="DEMO-ONLY")
        if s.strategy_id in verified else s
        for s in DEFAULT_SPECS
    )


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(prog="research.runtime", description="OpenAlpha 自主模拟决策（离线）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--verified", nargs="*", default=[], help="标记为 VERIFIED 的策略 id（会真的进模拟账本）")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd != "demo":
        return 2

    specs = _specs(args.verified)
    if args.verified:
        print(f"WARNING: --verified={args.verified} 仅用于演示（verified_by=DEMO-ONLY）；"
              f"正式运行不得通过命令行提升策略权限。", file=sys.stderr)
    limits = RiskLimits()
    as_of = demo_as_of_ms()
    with tempfile.TemporaryDirectory() as td:
        summaries = []
        for regime in REGIMES:
            # 每个 regime 用独立账本：三种 regime 的价格序列互不相干，混在一个账本里
            # 会在下一轮 mark 时拿另一个 regime 的价格去平前一个 regime 的仓。
            broker = PaperBroker(Path(td) / f"ledger_{regime}.sqlite", starting_cash=D("1000"))
            log_path = Path(td) / f"decisions_{regime}.jsonl"
            res = run_round("BTC/USDT", _views(synthetic_1m(regime)), as_of, broker,
                            limits, log_path=log_path, specs=specs)
            summaries.append((regime, broker.restore()))
            if args.json:
                print(json.dumps({"regime": regime, **res.log}, ensure_ascii=False, indent=2))
            else:
                sel, risk = res.selection, res.risk
                intent = sel.intent
                print(f"[{regime}] decision={sel.decision}"
                      + (f" strategy={intent.strategy_id} shadow={intent.shadow}" if intent else ""))
                print(f"  state: {res.state.trend}/{res.state.volatility}/{res.state.momentum}/"
                      f"{res.state.risk} conf={res.state.confidence}")
                if intent:
                    print(f"  intent: {intent.side} entry={intent.entry_ref_price} "
                          f"stop={intent.stop_price} tp={intent.take_profit_price}")
                    print(f"  why: {intent.reason}")
                if risk:
                    print(f"  risk: {risk.decision} {risk.codes} — {risk.reason}")
                else:
                    print("  risk: (无意图，未进入风控)")
                print(f"  fill: {res.fill.to_dict() if res.fill else 'none'}")
                print(f"  hold: {sel.hold_reason or '-'}")
                print(f"  equity: {res.equity}")
            broker.close()
        if not args.json:
            print("\nledgers:")
            for regime, summary in summaries:
                print(f"  {regime}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())