"""CLI: prefill market data, then audit the candidates against their controls.

    python -m research.audit prefill            # download what the runs need
    python -m research.audit run [--strategy X] # measure one or all
    python -m research.audit list               # the candidates and their sources
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .binance import Market
from .runner import default_candidates, format_book, run

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "runtime" / "audit" / "market.sqlite"
THREE_YEARS_MS = int(3.35 * 365 * 86400 * 1000)


def _cmd_prefill(args) -> int:
    market = Market(CACHE)
    since = int((time.time() * 1000) - THREE_YEARS_MS)
    # the regime gate needs BTC on the same venue TrendRider trades
    try:
        btc = market.klines("BTC/USDT:USDT", "1h", since, venue="futures")
        print(f"  {'(regime gate)':<20} {'BTC/USDT:USDT':<16} 1h   {len(btc):>7} 根", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  BTC regime gate 拉取失败: {exc}", flush=True)
    for strat in default_candidates():
        for pair in strat.pairs:
            interval = strat.timeframe
            # 15m over 3.3 years is 115k bars per pair; a year of it is plenty
            # for a carry strategy whose alpha is the 8h funding print.
            pair_since = since if interval == "1h" else int((time.time() * 1000) - 365 * 86400 * 1000)
            try:
                rows = market.klines(pair, interval, pair_since,
                                     venue=getattr(strat, "venue", "spot"))
                print(f"  {strat.name:<20} {pair:<16} {interval:<4} {len(rows):>7} 根", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  {strat.name:<20} {pair:<16} 失败 {type(exc).__name__}: {exc}", flush=True)
            if strat.name == "FundingRateEnhanced":
                try:
                    rates = market.funding(pair, pair_since)
                    print(f"    {'':<20} {pair:<16} 费率 {len(rates):>7} 条", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"    funding 失败: {exc}", flush=True)
    market.close()
    return 0


def _cmd_list(args) -> int:
    for strat in default_candidates():
        print(f"{strat.name}")
        print(f"  来源   {strat.source}")
        print(f"  周期   {strat.timeframe} · {len(strat.pairs)} 个交易对")
        print(f"  假设   {strat.hypothesis}")
        print(f"  证伪   {strat.falsify}")
        print()
    return 0


def _cmd_run(args) -> int:
    market = Market(CACHE)
    since = int((time.time() * 1000) - THREE_YEARS_MS)
    wanted = [s for s in default_candidates()
              if not args.strategy or args.strategy.lower() in s.name.lower()]
    for worst in (True, False):
        for strat in wanted:
            print(f"\n=== {strat.name} · {'最坏' if worst else '最好'} bar 解析 ===", flush=True)
            book = run(market, strat, since_ms=since, worst_case=worst)
            print(format_book(book, worst), flush=True)
    market.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit external strategies with our instruments")
    parser.add_argument("command", choices=["prefill", "run", "list"], nargs="?", default="run")
    parser.add_argument("--strategy", default="")
    args = parser.parse_args(argv)
    if args.command == "prefill":
        return _cmd_prefill(args)
    if args.command == "list":
        return _cmd_list(args)
    return _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
