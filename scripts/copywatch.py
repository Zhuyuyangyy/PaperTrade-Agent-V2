"""Inspect and curate the CopyWatch D account ledger.

Read-only by default. The only mutating commands are ``promote``/``demote``, which only
edit the local watch list: they cannot place an order or touch real funds.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.copywatch import store  # noqa: E402

DB_PATH = ROOT / "runtime" / "copywatch" / "copywatch.sqlite"


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "--"
    return f"{value:,.{digits}f}"


def _clock(ms: int | None) -> str:
    if not ms:
        return "--"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M:%S UTC")


def cmd_status(args) -> int:
    db = Path(args.db)
    if not db.exists():
        print(f"D 账本尚不存在：{db}")
        print("先运行：python -m research.copywatch.agent once")
        return 1
    conn = store.connect(db)
    store.ensure_schema(conn)
    wallets = store.wallet_stats(conn)
    closed = store.closed_stats(conn)
    latest = store.latest_equity(conn)
    open_rows = store.open_positions(conn)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    counts = store.decision_counts(conn, 0)
    swaps = store.swap_count(conn, 0)
    print("== CopyWatch D 账户 ==")
    print(f"账本文件      : {db}")
    print(f"观察钱包      : {wallets['observed']}   跟踪钱包: {wallets['tracked']}")
    print(f"链上成交事件  : {swaps['swaps']}（买入 {swaps['buys']}，进入研判 {swaps['actionable']}）")
    print(f"研判记录      : {counts['decisions']}（模拟成交 {counts['paper']}，跳过 {counts['skipped']}）")
    print(f"已平仓        : {closed['closed_count']} 笔 · 胜 {closed['win_count']} / 负 {closed['loss_count']}"
          f" · 已实现 {closed['realized_pnl_usd']:+,.2f} USDT")
    if latest is not None:
        age = (now_ms - int(latest["ts_ms"])) / 1000
        print(f"最新权益样本  : {_fmt(latest['equity_usd'])} USDT · 现金 {_fmt(latest['cash_usd'])}"
              f" · 持仓净值 {_fmt(latest['positions_usd'])} · {age:.0f}s 前"
              f" · {'完整' if latest['complete'] else '不完整（缺新鲜标记）'}")
    print(f"未平仓        : {len(open_rows)}")
    for row in open_rows:
        print(f"  #{row['id']} {row['symbol']} {row['network']} 开仓 {_clock(row['opened_at_ms'])}"
              f" 成本 {_fmt(row['entry_cost_usd'])} mark {_fmt(row['mark_price'], 4)}")
    breakdown = store.skip_breakdown(conn, 0)
    if breakdown:
        print("跳过原因分布  : " + " · ".join(f"{k}={v}" for k, v in breakdown.items()))
    conn.close()
    return 0


def cmd_wallets(args) -> int:
    conn = store.connect(Path(args.db))
    store.ensure_schema(conn)
    rows = conn.execute(
        "SELECT address, network, swaps, notional_usd, tracked, note FROM wallets"
        " ORDER BY notional_usd DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("观察列表中还没有钱包。")
    for row in rows:
        flag = "跟踪" if row["tracked"] else "观察"
        print(f"{flag} {row['address']} [{row['network']}] {row['swaps']:>4} 笔"
              f" ${row['notional_usd']:>12,.0f} {row['note'] or ''}")
    conn.close()
    return 0


def cmd_events(args) -> int:
    conn = store.connect(Path(args.db))
    store.ensure_schema(conn)
    for item in store.recent_events(conn, args.limit):
        print(f"{_clock(item['ts_ms'])} [{item['tag']:>7}] {item['title']}  ·  {item['detail']}")
    conn.close()
    return 0


def _set_tracked(args, tracked: bool) -> int:
    conn = store.connect(Path(args.db))
    store.ensure_schema(conn)
    address = args.address.lower()
    row = conn.execute(
        "SELECT 1 FROM wallets WHERE address = ? AND network = ?", (address, args.network)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO wallets(address, network, first_seen_ms, last_seen_ms, note)"
            " VALUES(?,?,?,?,?)",
            (address, args.network, 0, 0, "pinned" if tracked else "demoted"),
        )
    conn.execute(
        "UPDATE wallets SET tracked = ?, note = ? WHERE address = ? AND network = ?",
        (1 if tracked else 0, "pinned" if tracked else "demoted", address, args.network),
    )
    conn.commit()
    conn.close()
    print(f"{'纳入' if tracked else '移出'}跟踪列表：{address} [{args.network}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CopyWatch D account inspector")
    parser.add_argument("--db", default=str(DB_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    wallets = sub.add_parser("wallets")
    wallets.add_argument("--limit", type=int, default=20)
    events = sub.add_parser("events")
    events.add_argument("--limit", type=int, default=30)
    for name in ("promote", "demote"):
        item = sub.add_parser(name)
        item.add_argument("address")
        item.add_argument("--network", default="base")
    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "wallets":
        return cmd_wallets(args)
    if args.command == "events":
        return cmd_events(args)
    if args.command == "promote":
        return _set_tracked(args, True)
    return _set_tracked(args, False)


if __name__ == "__main__":
    sys.exit(main())
