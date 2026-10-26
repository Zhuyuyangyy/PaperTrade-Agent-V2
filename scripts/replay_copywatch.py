"""Replay CopyWatch signals against the recorded swap tape.

Answers one question with data instead of waiting: if we had copied every
tracked wallet's buy and exited after N minutes, what was the expectation
after costs?

The tape in `swaps` is the only price source used, so a horizon is only
counted when the tape actually covers it. Horizons a signal cannot reach are
reported as `uncovered`, never silently filled with a stale price.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.copywatch import store  # noqa: E402

# Round-trip friction in basis points, fee only; gas is charged separately
# because it is absolute (0.02 USD on base) rather than proportional.
FEE_BPS_ROUND_TRIP = {"base": 20.0, "eth": 20.0}
GAS_USD_ROUND_TRIP = {"base": 0.04, "eth": 4.8}
DEFAULT_NOTIONAL = 100.0


def horizon_stats(rows, horizon_min, notional, min_usd):
    """Match each entry to the first print at or after entry + horizon."""
    outcomes = []
    uncovered = 0
    for entry in rows:
        target = entry["block_ts_ms"] + horizon_min * 60_000
        exit_row = None
        for candidate in entry["_tape_after"]:
            if candidate["block_ts_ms"] >= target:
                exit_row = candidate
                break
        if exit_row is None:
            uncovered += 1
            continue
        gross = (exit_row["price_usd"] / entry["price_usd"] - 1.0) * notional
        fee = notional * FEE_BPS_ROUND_TRIP.get(entry["network"], 20.0) / 10_000.0
        gas = GAS_USD_ROUND_TRIP.get(entry["network"], 4.8)
        net = gross - fee - gas
        outcomes.append(
            {
                "net": net,
                "gross": gross,
                "cost": fee + gas,
                "hold_ms": exit_row["block_ts_ms"] - entry["block_ts_ms"],
                "usd": entry["usd"],
            }
        )
    if not outcomes:
        return None
    nets = [o["net"] for o in outcomes]
    wins = sum(1 for n in nets if n > 0)
    return {
        "n": len(nets),
        "uncovered": uncovered,
        "avg_net": sum(nets) / len(nets),
        "avg_gross": sum(o["gross"] for o in outcomes) / len(outcomes),
        "avg_cost": sum(o["cost"] for o in outcomes) / len(outcomes),
        "win_rate": wins / len(nets),
        "total": sum(nets),
        "avg_hold_min": sum(o["hold_ms"] for o in outcomes) / len(outcomes) / 60_000,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="5,15,30,45,60")
    ap.add_argument("--min-usd", type=float, default=0.0, help="ignore signals below this size")
    ap.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)
    ap.add_argument("--all-wallets", action="store_true", help="use every wallet, not only tracked")
    args = ap.parse_args()

    db = ROOT / "runtime" / "copywatch" / "copywatch.sqlite"
    if not db.is_file():
        print(f"no ledger at {db}")
        return 1
    conn = store.connect(db)

    tracked = {r["address"] for r in conn.execute("SELECT address FROM wallets WHERE tracked=1")}
    tape = [
        dict(r)
        for r in conn.execute(
            "SELECT network, symbol, wallet, kind, usd, price_usd, block_ts_ms FROM swaps "
            "WHERE price_usd > 0 ORDER BY block_ts_ms"
        )
    ]

    print(f"tape       : {len(tape)} prints")
    span_h = (tape[-1]["block_ts_ms"] - tape[0]["block_ts_ms"]) / 3_600_000 if tape else 0
    print(f"window     : {span_h:.2f} h")

    by_symbol = {}
    for row in tape:
        by_symbol.setdefault((row["network"], row["symbol"]), []).append(row)

    signals = [
        row
        for row in tape
        if row["kind"] == "buy" and row["usd"] >= args.min_usd
        and (args.all_wallets or row["wallet"] in tracked)
    ]
    print(f"signals    : {len(signals)} buys" + ("" if args.all_wallets else " from tracked wallets"))
    if not signals:
        print("no signals to replay")
        return 1

    # Pre-attach the tape tail per signal so lookups stay O(n) overall.
    for sig in signals:
        pool = by_symbol.get((sig["network"], sig["symbol"]), [])
        sig["_tape_after"] = [r for r in pool if r["block_ts_ms"] >= sig["block_ts_ms"]]

    horizons = [int(h) for h in args.horizons.split(",")]
    print()
    header = f"{'hold':>6} {'n':>5} {'覆盖':>6} {'毛收益':>9} {'成本':>8} {'净收益':>9} {'胜率':>7} {'合计':>9}"
    print(header)
    print("-" * len(header))
    for h in horizons:
        stats = horizon_stats(signals, h, args.notional, args.min_usd)
        if not stats:
            print(f"{h:>5}m {'—':>5}  该窗口无覆盖")
            continue
        print(
            f"{h:>5}m {stats['n']:>5} {stats['n'] / len(signals):>5.0%} "
            f"{stats['avg_gross']:>+8.3f} {stats['avg_cost']:>8.3f} "
            f"{stats['avg_net']:>+8.3f} {stats['win_rate']:>6.0%} {stats['total']:>+8.2f}"
        )
    print()
    print(f"(每个信号 {args.notional:.0f} USDT 名义；成本 = 手续费 20bps 往返 + gas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
