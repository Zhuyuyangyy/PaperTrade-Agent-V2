"""Public CCXT OHLCV -> risk-first Agent decisions; no exchange credentials, no orders."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sys
import time

from research.core import (
    HOUR_MS, PAIR_MAP, StaleFeedError, atomic_write_json, build_decision,
    checked_closed_candles, write_decisions,
)

ROOT = Path(__file__).resolve().parents[1]


def utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def open_public_exchange():
    import ccxt  # Optional: demo and unit tests do not require installation.
    exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 15_000})
    exchange.load_markets()
    return exchange


def fetch_public(ex, pair: str, n: int = 160) -> list[list[float]]:
    if ex.id != "kraken" or pair not in PAIR_MAP or pair not in ex.markets:
        raise RuntimeError("Only approved public Kraken pairs are permitted")
    return ex.fetch_ohlcv(pair, "1h", limit=n)


def fetch_public_mark(ex, pair: str) -> dict:
    """Fetch a current public ticker mark, stamped with local receipt time."""
    if ex.id != "kraken" or pair not in PAIR_MAP or pair not in ex.markets:
        raise RuntimeError("Only approved public Kraken pairs are permitted")
    ticker = ex.fetch_ticker(pair)
    price = ticker.get("last") if isinstance(ticker, dict) else None
    if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(price) or price <= 0:
        raise ValueError("Ticker has no valid last price")
    exchange_ts = ticker.get("timestamp")
    if type(exchange_ts) is not int or exchange_ts < 0:
        exchange_ts = None
    received_ms = utc_ms()
    if exchange_ts is not None:
        if exchange_ts > received_ms + 30_000:
            raise ValueError("Ticker timestamp is implausibly far in the future")
        if received_ms - exchange_ts > 5 * 60 * 1000:
            raise ValueError("Ticker timestamp is stale")
    return {
        "schema_version": 1,
        "pair": pair,
        "source": "ccxt-public:kraken",
        "price": float(price),
        "observed_at_ms": received_ms,
        "exchange_timestamp_ms": exchange_ts,
    }


def run_once(outdir: Path, logpath: Path, ta_policy: str, ta_root: Path,
             marks_out: Path | None = None) -> list[dict]:
    now = utc_ms()
    # Collect and validate all pairs before writing any signal.
    snapshots = {}
    ex = open_public_exchange()
    try:
        for pair in PAIR_MAP:
            rows = fetch_public(ex, pair)
            snapshots[pair] = checked_closed_candles(rows, now)
    except StaleFeedError as exc:
        # Hour-boundary dead zone (first ~30s of an hour): the newest 1h
        # candle has not settled its 30s, the previous one is already stale.
        # That is a "not yet", not a failure -- skip the tick and let the
        # next one run with fresh candles. A genuinely broken feed still
        # raises (invalid OHLCV / gaps are plain ValueErrors).
        print(f"skip tick: {exc}", flush=True)
        return []
    decisions = [build_decision(pair, snapshots[pair], now, ta_policy, ta_root) for pair in PAIR_MAP]
    count = write_decisions(outdir, decisions, logpath)
    if marks_out is not None:
        # Ticker marks are independent of signal publication: a failed mark must not
        # invent a price, and must not prevent a valid closed-candle decision from being written.
        for pair in PAIR_MAP:
            try:
                mark = fetch_public_mark(ex, pair)
                atomic_write_json(marks_out / (pair.replace("/", "_") + ".json"), mark)
            except Exception as exc:
                print(f"mark unavailable for {pair}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    print(f"{datetime.now(timezone.utc).isoformat()} closed-candle decisions={len(decisions)} updated={count}", flush=True)
    for d in decisions:
        print(f"  {d['pair']} {d['action']} votes={d['votes']} veto={d['risk_veto']}", flush=True)
    return decisions


def fake_closed_rows(now_ms: int, factor: float = 1.0, n: int = 120) -> list[list[float]]:
    """Deterministic FAKE feed for software tests, never used as genuine market data."""
    current_hour = now_ms // HOUR_MS * HOUR_MS
    rows = []
    for i in range(n):
        # 120 completely closed candles ending one minute before now.
        t = current_hour - (n - i) * HOUR_MS
        p = factor * (50.0 + i * 0.4)
        rows.append([t, p - 0.2 * factor, p + factor, p - factor, p, 100.0])
    return rows


def run_demo(outdir: Path) -> list[dict]:
    # Anchor one minute back. The synthetic feed is hour-aligned, and inside
    # the first 30s after an hour boundary no candle is both settled (30s
    # rule) and fresh (1h staleness window) -- the demo would crash exactly
    # there. One minute back is also what this feed's docstring already
    # claims: "120 completely closed candles ending one minute before now".
    now = utc_ms() - 60_000
    # DEMO outputs to a distinct directory; does not write to trade consumption path.
    decisions = [
        build_decision(pair, checked_closed_candles(fake_closed_rows(now, factor=1.0 + i), now), now)
        for i, pair in enumerate(PAIR_MAP)
    ]
    for d in decisions:
        d["source"] = "SYNTHETIC-DEMO-NOT-TRADEABLE"
    for d in decisions:
        atomic_write_json(outdir / (d["pair"].replace("/", "_") + ".json"), d)
        print(f"SYNTHETIC ONLY: {d['pair']} {d['action']}, source={d['source']}")
    return decisions


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-only, public-market research worker")
    parser.add_argument("command", choices=["demo", "once", "loop"])
    parser.add_argument("--out", type=Path, default=ROOT / "user_data" / "signals")
    parser.add_argument("--marks-out", type=Path, default=ROOT / "user_data" / "marks")
    parser.add_argument("--demo-out", type=Path, default=ROOT / "data" / "synthetic_signals")
    parser.add_argument("--log", type=Path, default=ROOT / "data" / "decisions.jsonl")
    parser.add_argument("--ta-root", type=Path, default=ROOT / "data" / "ta_bias")
    parser.add_argument("--ta-policy", choices=["off", "require_buy"], default="off")
    parser.add_argument("--poll-seconds", type=int, default=120)
    args = parser.parse_args()
    if args.command == "demo":
        run_demo(args.demo_out)
        return 0
    if args.poll_seconds < 60:
        parser.error("poll-seconds must be >= 60")
    if args.command == "once":
        run_once(args.out, args.log, args.ta_policy, args.ta_root, args.marks_out)
        return 0
    print("PAPER RESEARCH LOOP: public OHLCV only. No execution permissions.", flush=True)
    try:
        while True:
            try:
                run_once(args.out, args.log, args.ta_policy, args.ta_root, args.marks_out)
            except Exception as ex:
                print(f"research failed closed: {type(ex).__name__}: {ex}", file=sys.stderr, flush=True)
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("research loop stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
