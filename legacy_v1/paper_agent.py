#!/usr/bin/env python3
"""PaperTrade-Agent V1: public market data -> rule signal -> risk -> local fake ledger.

This program has no exchange credentials, no private trading API and no live-order path.
Only uses public Binance market-data GET requests (or locally-generated demo candles).
Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
from pathlib import Path
import random
import sqlite3
import sys
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Candle:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_ms: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not cfg["symbols"] or len(set(cfg["symbols"])) != len(cfg["symbols"]):
        raise ValueError("Must supply nonempty unique symbols")
    if not all(x.isupper() and x.isalnum() for x in cfg["symbols"]):
        raise ValueError("Invalid symbol")
    for k in ("initial_cash_usdt", "min_order_usdt"):
        if not 0 < float(cfg[k]) < 10**12:
            raise ValueError(f"Invalid {k}")
    for k in ("max_fraction_per_symbol", "max_total_exposure", "stop_loss_pct", "daily_loss_cutoff"):
        if not 0 < float(cfg[k]) <= 1:
            raise ValueError(f"Invalid {k}")
    if cfg["max_fraction_per_symbol"] > cfg["max_total_exposure"]:
        raise ValueError("Single-symbol cap cannot exceed total cap")
    if len(cfg["symbols"]) * cfg["max_fraction_per_symbol"] < cfg["max_total_exposure"]:
        # Harmless: the configured total cap may not be reachable with these pairs.
        pass
    for k in ("fee_bps", "slippage_bps"):
        if not 0 <= cfg[k] <= 1000:
            raise ValueError(f"Invalid {k}")
    if not 60 <= cfg["poll_seconds"] <= 86400:
        raise ValueError("poll_seconds must be 60..86400")
    if not 55 <= cfg["lookback_candles"] <= 1000:
        raise ValueError("lookback_candles must be 55..1000")
    if cfg["interval"] not in ("15m", "30m", "1h", "4h", "1d"):
        raise ValueError("Unsupported interval")
    if cfg["data_url"] != "https://data-api.binance.vision":
        raise ValueError("V1 data_url only allows the vetted public market data endpoint")
    return cfg


def http_json(path: str, params: dict[str, Any], base_url: str) -> Any:
    # Deliberately read-only. Market-data host, only GET for two public endpoints.
    if path not in ("/api/v3/klines", "/api/v3/ticker/price"):
        raise ValueError("Not an allowed public endpoint")
    url = f"{base_url}{path}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "PaperTrade-Agent/1.0 educational"}, method="GET")
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def market_snapshot(symbol: str, cfg: dict[str, Any]) -> tuple[list[Candle], float]:
    arr = http_json(
        "/api/v3/klines",
        {"symbol": symbol, "interval": cfg["interval"], "limit": cfg["lookback_candles"]},
        cfg["data_url"],
    )
    now_ms = int(utc_now().timestamp() * 1000)
    candles = [
        Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]),
               float(x[4]), float(x[5]), int(x[6]))
        for x in arr if int(x[6]) < now_ms
    ]
    if len(candles) < 51:
        raise RuntimeError(f"{symbol}: not enough CLOSED candles ({len(candles)} / 51)")
    last_tick = http_json("/api/v3/ticker/price", {"symbol": symbol}, cfg["data_url"])
    price = float(last_tick["price"])
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError("Bad market price")
    if candles[-1].close_ms > now_ms - 1000 * 60 * 60 * 24:
        return candles, price
    raise RuntimeError(f"{symbol}: last complete candle is over 24 hours old")


def decide(candles: list[Candle]) -> tuple[str, str]:
    """Deterministic baseline; no forward-looking candles, no LLM trading authority."""
    if len(candles) < 51:
        return "HOLD", "insufficient_closed_candles"
    closes = [x.close for x in candles]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    last_close = closes[-1]
    if sma20 > sma50 and last_close > sma20:
        return "BUY", f"sma20={sma20:.4f}>sma50={sma50:.4f};close>sma20"
    if sma20 < sma50 or last_close < sma20:
        return "SELL", f"sma20={sma20:.4f},sma50={sma50:.4f},close={last_close:.4f}"
    return "HOLD", "neutral_trend"


class Ledger:
    def __init__(self, db_path: Path, initial_cash: float):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY, qty REAL NOT NULL, entry_price REAL NOT NULL,
                entry_total REAL NOT NULL, opened_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, qty REAL NOT NULL, mark_price REAL NOT NULL,
                fill_price REAL NOT NULL, fee_usdt REAL NOT NULL,
                realized_pnl REAL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                symbol TEXT NOT NULL, candle_ms INTEGER NOT NULL,
                proposal TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, cash REAL NOT NULL,
                positions_value REAL NOT NULL, total REAL NOT NULL
            );
        """)
        if self.get_state("cash") is None:
            self.set_state("cash", str(initial_cash))
            self.set_state("initial_cash", str(initial_cash))
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @property
    def cash(self) -> float:
        return float(self.get_state("cash") or 0)

    def positions(self) -> dict[str, sqlite3.Row]:
        return {x["symbol"]: x for x in self.conn.execute("SELECT * FROM positions")}

    def equity(self, marks: dict[str, float]) -> tuple[float, float, float]:
        pos = self.positions()
        missing = sorted(set(pos) - set(marks))
        if missing:
            raise ValueError(f"Missing fresh marks for open positions: {missing}")
        holdings = sum(p["qty"] * marks[s] for s, p in pos.items())
        return self.cash, holdings, self.cash + holdings

    def day_baseline(self, when: datetime, current_total: float) -> float:
        prefix = when.strftime("%Y-%m-%d") + "%"
        row = self.conn.execute(
            "SELECT total FROM equity WHERE ts LIKE ? ORDER BY id LIMIT 1", (prefix,)
        ).fetchone()
        return float(row["total"]) if row else current_total

    def record_decision(self, when: datetime, symbol: str, candle_ms: int,
                        proposal: str, outcome: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO decisions(ts,symbol,candle_ms,proposal,outcome,reason) VALUES (?,?,?,?,?,?)",
            (iso(when), symbol, candle_ms, proposal, outcome, reason),
        )

    def buy(self, when: datetime, symbol: str, mark: float,
            notional: float, cfg: dict[str, Any], reason: str) -> None:
        assert symbol not in self.positions(), "Pyramiding disabled"
        fill = mark * (1 + cfg["slippage_bps"] / 10000)
        fee = notional * cfg["fee_bps"] / 10000
        total = notional + fee
        if total > self.cash + 1e-8:
            raise ValueError("Insufficient paper balance")
        qty = notional / fill
        self.conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?)",
            (symbol, qty, fill, total, iso(when)),
        )
        self.set_state("cash", str(self.cash - total))
        self.conn.execute(
            "INSERT INTO trades(ts,symbol,side,qty,mark_price,fill_price,fee_usdt,realized_pnl,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (iso(when), symbol, "BUY", qty, mark, fill, fee, None, reason),
        )

    def sell(self, when: datetime, symbol: str, mark: float,
             cfg: dict[str, Any], reason: str) -> None:
        p = self.positions()[symbol]
        fill = mark * (1 - cfg["slippage_bps"] / 10000)
        gross = p["qty"] * fill
        fee = gross * cfg["fee_bps"] / 10000
        proceeds = gross - fee
        realized_pnl = proceeds - p["entry_total"]
        self.set_state("cash", str(self.cash + proceeds))
        self.conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self.conn.execute(
            "INSERT INTO trades(ts,symbol,side,qty,mark_price,fill_price,fee_usdt,realized_pnl,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (iso(when), symbol, "SELL", p["qty"], mark, fill, fee, realized_pnl, reason),
        )

    def snapshot(self, when: datetime, marks: dict[str, float]) -> None:
        cash, holdings, total = self.equity(marks)
        self.conn.execute(
            "INSERT INTO equity(ts,cash,positions_value,total) VALUES (?,?,?,?)",
            (iso(when), cash, holdings, total),
        )


def step(ledger: Ledger, market: dict[str, tuple[list[Candle], float]],
         when: datetime, cfg: dict[str, Any]) -> list[str]:
    """Only paper transactions; sell (including stop-loss) before considering buys."""
    marks = {s: price for s, (_, price) in market.items()}
    cash, holdings, total = ledger.equity(marks)
    baseline = ledger.day_baseline(when, total)
    notes = []
    for symbol, (candles, price) in market.items():
        closed_ms = candles[-1].close_ms
        key = f"last_processed_{symbol}"
        last_processed = ledger.get_state(key)
        if last_processed is not None and closed_ms <= int(last_processed):
            # Still check stops, but reject repeated OR older candles. No repeated strategy trades.
            if symbol in ledger.positions():
                p = ledger.positions()[symbol]
                if price <= p["entry_price"] * (1 - cfg["stop_loss_pct"]):
                    ledger.sell(when, symbol, price, cfg, "STOP_LOSS_intrabar")
                    notes.append(f"{symbol} STOP_LOSS sell @ {price:.4f}")
            continue
        proposal, reason = decide(candles)
        p = ledger.positions().get(symbol)
        stopped = p is not None and price <= p["entry_price"] * (1 - cfg["stop_loss_pct"])
        if p and (proposal == "SELL" or stopped):
            why = "STOP_LOSS" if stopped else "TREND_EXIT"
            ledger.sell(when, symbol, price, cfg, why + ":" + reason)
            outcome = "PAPER_SELL"
            notes.append(f"{symbol} {outcome} @ {price:.4f}")
        elif p:
            outcome = "HOLD_POSITION"
        elif proposal == "BUY":
            _, holdings_now, equity_now = ledger.equity(marks)
            # Recompute circuit-breaker after any sales earlier in this same cycle.
            halted_now = (baseline - equity_now) / baseline >= cfg["daily_loss_cutoff"]
            if halted_now:
                outcome = "BLOCKED_DAILY_LOSS"
            else:
                allowable_total = cfg["max_total_exposure"] * equity_now - holdings_now
                desired = cfg["max_fraction_per_symbol"] * equity_now
                # No borrowed money; leave room for fee in available cash.
                capacity = ledger.cash / (1 + cfg["fee_bps"] / 10000)
                notional = min(desired, allowable_total, capacity)
                if notional >= cfg["min_order_usdt"]:
                    ledger.buy(when, symbol, price, notional, cfg, "TREND_ENTRY:" + reason)
                    outcome = "PAPER_BUY"
                    notes.append(f"{symbol} {outcome} @ {price:.4f} notional={notional:.2f}")
                else:
                    outcome = "BLOCKED_SIZE_OR_EXPOSURE"
        else:
            outcome = "NO_ACTION"
        ledger.record_decision(when, symbol, closed_ms, proposal, outcome, reason)
        ledger.set_state(key, str(closed_ms))
    ledger.snapshot(when, marks)
    ledger.conn.commit()
    return notes or ["No virtual trades this cycle"]


def print_report(ledger: Ledger, marks: dict[str, float] | None = None) -> None:
    row = ledger.conn.execute("SELECT * FROM equity ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        print("No records yet. Run 'demo' or 'once' first.")
        return
    current = row["total"]
    init = float(ledger.get_state("initial_cash") or 1000)
    realized = ledger.conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM trades WHERE side='SELL'").fetchone()[0]
    closed = ledger.conn.execute("SELECT COUNT(*) FROM trades WHERE side='SELL'").fetchone()[0]
    wins = ledger.conn.execute("SELECT COUNT(*) FROM trades WHERE side='SELL' AND realized_pnl>0").fetchone()[0]
    highs = ledger.conn.execute("SELECT total FROM equity ORDER BY id").fetchall()
    peak, maxdd = 0.0, 0.0
    for (v,) in highs:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak)
    print("\n===== PAPER ACCOUNT (NOT REAL FUNDS) =====")
    print(f"Time: {row['ts']} UTC | Equity: {current:,.2f} USDT | Cash: {row['cash']:,.2f}")
    print(f"P&L vs start: {current-init:+,.2f} USDT ({(current/init-1)*100:+.2f}%)")
    print(f"Realized P&L: {realized:+,.2f} USDT | Closed trades: {closed} | Win-rate: {(wins/closed*100 if closed else 0):.1f}%")
    print(f"Observed max drawdown: {maxdd*100:.2f}% (snapshot-based)")
    for s, p in ledger.positions().items():
        print(f"Open {s}: qty={p['qty']:.8f}, simulated entry={p['entry_price']:.3f}")
    for t in ledger.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 5"):
        print(f"#{t['id']} {t['ts']} {t['side']} {t['symbol']} fill={t['fill_price']:.4f} PnL={t['realized_pnl']}")


def synthetic_candles(symbol: str, count: int = 360) -> list[Candle]:
    # Educational fake series only. DO NOT PRESENT DEMO P&L AS A REAL BACKTEST.
    rng = random.Random(16 if symbol == "BTCUSDT" else 29)
    price = 30000.0 if symbol == "BTCUSDT" else 2000.0
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = []
    for i in range(count):
        mu = 0.0015 if (i // 80) % 2 == 0 else -0.0017
        vol = rng.gauss(0, 0.006)
        opened = price
        price = max(0.01, price * (1 + mu + vol))
        top = max(price, opened) * 1.001
        bottom = min(price, opened) * 0.999
        opened_ms = int((start + timedelta(hours=i)).timestamp() * 1000)
        out.append(Candle(opened_ms, opened, top, bottom, price, 1,
                          opened_ms + 3600000 - 1))
    return out


def run_demo(cfg: dict[str, Any], db_path: Path) -> None:
    # Execute on the NEXT bar opening after using the previous bar closing signal.
    # Simulation differs from actual real-time fills; fees and slippage included.
    if db_path.exists():
        db_path.unlink()
    series = {s: synthetic_candles(s) for s in cfg["symbols"]}
    ledger = Ledger(db_path, cfg["initial_cash_usdt"])
    try:
        print("Offline SYNTHETIC demo (not historical market performance)")
        for i in range(60, min(map(len, series.values())) - 1):
            market = {s: (candles[:i+1], candles[i+1].open) for s, candles in series.items()}
            when = datetime.fromtimestamp((series[cfg["symbols"][0]][i+1].open_ms) / 1000, tz=timezone.utc)
            step(ledger, market, when, cfg)
        print_report(ledger)
    finally:
        ledger.close()


def render_dashboard(db_path: Path, output_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    points = list(conn.execute("SELECT ts,total,cash FROM equity ORDER BY id"))
    if not points:
        raise ValueError("No snapshots; run 'demo' or 'once' first")
    trades = list(conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 80"))
    cash, latest = points[-1]["cash"], points[-1]["total"]
    first = float(conn.execute("SELECT value FROM state WHERE key='initial_cash'").fetchone()[0])
    vals = [float(x["total"]) for x in points]
    lo, hi = min(vals), max(vals)
    delta = (hi-lo) or 1
    coords = []
    width, height = 820, 190
    for i, x in enumerate(vals):
        xp = 15 + i / max(1,len(vals)-1) * width
        yp = 20 + height - (x - lo) / delta * height
        coords.append(f"{xp:.2f},{yp:.2f}")
    polyline = " ".join(coords)
    rows = "\n".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{:.4f}</td><td>{}</td><td>{}</td></tr>".format(
            escape(t["ts"]), escape(t["symbol"]), escape(t["side"]),
            t["fill_price"], "—" if t["realized_pnl"] is None else f"{t['realized_pnl']:+.2f}",
            escape(t["reason"]),
        ) for t in trades
    )
    pnl = latest - first
    html = f"""<!doctype html><html lang="zh"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PaperTrade Agent V1 — 虚拟盘</title>
<style>body{{background:#09101c;color:#e9f1ff;font-family:system-ui,-apple-system,sans-serif;margin:0;padding:24px}}
main{{max-width:1060px;margin:auto}}.muted{{color:#94a3b8}}.tag{{font-size:12px;color:#9ae6b4;border:1px solid #246c5c;border-radius:30px;padding:6px 10px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:28px 0}}.card{{background:#101c2e;border:1px solid #253955;border-radius:18px;padding:20px}}h1{{font-size:32px}}h2{{font-size:19px}}.num{{font-size:26px;font-weight:700;margin-top:10px}}.chart{{max-width:100%;height:auto}}table{{width:100%;border-collapse:collapse;min-width:690px}}td,th{{border-bottom:1px solid #25334b;padding:12px;text-align:left;font-size:13px}}.scroll{{overflow:auto}}.warn{{color:#ffc97a}}</style>
<main><p class="muted">LOCAL SIMULATION / READ-ONLY DASHBOARD</p><h1>PaperTrade Agent <span class="tag">SIMULATED ONLY</span></h1>
<p class="warn">这是完全虚拟的实验账本。走势图基于软件记录的快照，不代表真实成交或未来收益。</p>
<div class="cards"><div class="card"><div class="muted">净资产 / USDT</div><div class="num">{latest:,.2f}</div></div>
<div class="card"><div class="muted">累计盈亏 / USDT</div><div class="num">{pnl:+,.2f}</div></div>
<div class="card"><div class="muted">虚拟现金 / USDT</div><div class="num">{cash:,.2f}</div></div>
<div class="card"><div class="muted">模拟成交笔数</div><div class="num">{len(trades)}</div></div></div>
<div class="card"><h2>净资产轨迹</h2><svg viewBox="0 0 850 240" class="chart" role="img" aria-label="模拟资金曲线"><line x1="15" y1="210" x2="835" y2="210" stroke="#45566e"/><polyline fill="none" stroke="#4de0b0" stroke-width="2.6" points="{polyline}"/></svg><div class="muted">最高 {hi:.2f} · 最低 {lo:.2f} · 共 {len(points)} 次快照</div></div>
<div class="card" style="margin-top:18px"><h2>虚拟成交日志</h2><div class="scroll"><table><thead><tr><th>UTC 时间</th><th>交易对</th><th>方向</th><th>模拟成交价</th><th>已实现盈亏 USDT</th><th>执行依据</th></tr></thead><tbody>{rows}</tbody></table></div></div>
<p class="muted">生成时间 {escape(iso(utc_now()))}。本页面完全本地静态文件，无交易所连接或下单能力。</p></main></html>"""
    output_path.write_text(html, encoding="utf-8")
    conn.close()
    print(f"Dashboard: {output_path.resolve()}")


def export_trades(db_path: Path, path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM trades ORDER BY id"))
    with path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out)
        fields = ["id","ts","symbol","side","qty","mark_price","fill_price","fee_usdt","realized_pnl","reason"]
        writer.writerow(fields)
        for row in rows:
            writer.writerow([row[k] for k in fields])
    print(f"Exported {len(rows)} virtual trades to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="NO LIVE TRADING: educational paper Agent")
    parser.add_argument("command", choices=["demo", "once", "loop", "report", "dashboard", "export"])
    parser.add_argument("--config", default=str(BASE / "settings.json"))
    args = parser.parse_args()
    cfg = load_config(Path(args.config))
    local_db = BASE / cfg["database"]
    demo_db = BASE / "data" / "synthetic_demo.sqlite3"
    if args.command == "demo":
        run_demo(cfg, demo_db)
        render_dashboard(demo_db, BASE / "data" / "demo_dashboard.html")
        return
    if args.command in ("once", "loop"):
        ledger = Ledger(local_db, cfg["initial_cash_usdt"])
        try:
            while True:
                try:
                    # Fetch all pairs before changing the ledger. No partial-market decisions.
                    market = {s: market_snapshot(s, cfg) for s in cfg["symbols"]}
                    now = utc_now()
                    for note in step(ledger, market, now, cfg):
                        print(f"[{iso(now)}] {note}")
                    print_report(ledger)
                except Exception as exc:
                    ledger.conn.rollback()
                    print(f"[{iso(utc_now())}] Data/decision cycle failed; no real money at risk: {exc}", file=sys.stderr)
                    if args.command == "once":
                        raise SystemExit(2) from exc
                if args.command == "once":
                    break
                time.sleep(cfg["poll_seconds"])
        except KeyboardInterrupt:
            print("Stopped; paper positions remain in local SQLite ledger.")
        finally:
            ledger.close()
        return
    db = local_db
    if not db.exists():
        print("No live-market virtual ledger yet. Run 'once' first. For an offline test run 'demo'.")
        raise SystemExit(1)
    if args.command == "dashboard":
        render_dashboard(db, BASE / "data" / "live_dashboard.html")
    elif args.command == "export":
        export_trades(db, BASE / "data" / "paper_trades.csv")
    elif args.command == "report":
        ledger = Ledger(db, cfg["initial_cash_usdt"])
        try:
            print_report(ledger)
        finally:
            ledger.close()


if __name__ == "__main__":
    main()
