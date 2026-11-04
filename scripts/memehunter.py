"""CLI for the MemeHunter E account (Solana meme desk).

    python scripts/memehunter.py scan            # scan, then open qualifying positions
    python scripts/memehunter.py scan --dry-run  # show the decision, open nothing
    python scripts/memehunter.py update          # mark to market, apply exits
    python scripts/memehunter.py status          # equity and open book
    python scripts/memehunter.py candidates      # latest scan, including rejects
    python scripts/memehunter.py loop            # stay alive: scan + mark forever
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import memehunter  # noqa: E402


def _line(token: "memehunter.Token", pts: float) -> str:
    ratio = (
        token.organic_buy_5m / token.organic_sell_5m
        if token.organic_sell_5m > 0
        else 99.0
    )
    return (
        f"{token.symbol:<12} 分{pts:>5.1f} | 有机{token.organic_score:>5.1f} | "
        f"流动性 {token.liquidity_usd:>10,.0f} | 持币 {token.holder_count:>6,} | "
        f"集中 {token.top_holders_pct:>4.1f}% | 5m {token.chg_5m:>+6.2f}% | "
        f"1h {token.chg_1h:>+6.2f}% | 有机买/卖 {ratio:>5.2f}"
    )


def _why(token: "memehunter.Token", pts: float) -> str:
    return (
        f"分{pts:.1f} 有机{token.organic_score:.1f}({token.organic_label}) "
        f"持币{token.holder_count} 集中{token.top_holders_pct:.1f}% "
        f"5m{token.chg_5m:+.2f}% 1h{token.chg_1h:+.2f}% "
        f"有机买{token.organic_buy_5m:.0f}/卖{token.organic_sell_5m:.0f} "
        f"净买家{token.net_buyers_5m} 流动性{token.liquidity_usd:.0f}"
    )


def _try_open(desk: "memehunter.Desk", ranked, limit: int) -> tuple[list, list]:
    """Open ranked candidates until the book is full or the list runs out.

    Returns (opened, skipped). Kept separate from cmd_scan because the loop
    needs the same behaviour without the per-token narration -- and because
    forgetting it in the loop is how the desk ends up scanning for hours and
    never trading.
    """
    opened, skipped = [], []
    for token, pts in ranked:
        if len(opened) >= limit:
            break
        out = desk.open_position(token, _why(token, pts))
        if out["opened"]:
            opened.append(out)
        else:
            skipped.append((token.symbol, out["reason"]))
    return opened, skipped


def cmd_scan(desk: memehunter.Desk, args) -> int:
    print("扫描 Jupiter（organic 1h / 5m / 24h + recent + 关键词搜索）…")
    result = desk.scan()
    print(f"\n扫到 {result['seen']} 个代币，通过全部硬门槛 {result['eligible']} 个")
    for err in result["errors"]:
        print(f"  ! {err}")

    ranked = result["ranked"]
    if not ranked:
        print("\n本轮没有任何代币通过质量门槛。")
        print("这不是故障：meme 币绝大多数在被发现时就已不合格（权限未撤销、筹码集中、")
        print("有机成交量不足）。宁可不交易，也不放宽门槛去凑成交。")
        return 0

    print(f"\n{'='*104}\n合格候选（按分数排序）\n{'='*104}")
    for token, pts in ranked[:10]:
        print(_line(token, pts))

    print(f"\n{'='*104}\n建仓（每仓 {desk.cfg['notional_usdt']:.0f} USDT，"
          f"最多 {desk.cfg['max_open_positions']} 仓，往返成本 "
          f"{desk.cfg['round_trip_cost_bps']:.0f} bps）\n{'='*104}")
    acted = 0
    for token, pts in ranked:
        if args.dry_run:
            ok, reason = desk.can_open(token, memehunter.now_ms())
            print(f"  [DRY] {_line(token, pts)}  -> {'可开仓' if ok else '跳过: ' + reason}")
            continue
        why = (
            f"分{pts:.1f} 有机{token.organic_score:.1f}({token.organic_label}) "
            f"持币{token.holder_count} 集中{token.top_holders_pct:.1f}% "
            f"5m{token.chg_5m:+.2f}% 1h{token.chg_1h:+.2f}% "
            f"有机买{token.organic_buy_5m:.0f}/卖{token.organic_sell_5m:.0f} "
            f"净买家{token.net_buyers_5m} 流动性{token.liquidity_usd:.0f}"
        )
        out = desk.open_position(token, why)
        if not out["opened"]:
            print(f"  {token.symbol:<12} 跳过: {out['reason']}")
            continue
        acted += 1
        print(
            f"  ✔ 建仓 #{out['id']} {token.symbol:<12} 参考价 {token.price_usd:.6g} "
            f"成交 {out['fill']:.6g}（滑点 {out['slippage_bps']:.1f}bps）"
            f" 成本 {out['cost_usd']:.3f} USDT  分 {pts:.1f}"
        )
        if acted >= int(desk.cfg["max_open_positions"]):
            break
    if not args.dry_run and acted == 0:
        print("  （本轮没有开新仓：可能满仓、冷却中，或候选全部被资金/仓位规则拒绝）")
    return 0


def cmd_update(desk: memehunter.Desk, args) -> int:
    result = desk.update()
    print(f"标记 {result['marked']} 个仓位")
    if result["stale"]:
        print(f"  无报价（保持旧标记，不计入完整权益）: {', '.join(result['stale'])}")
    if not result["closed"]:
        print("  本轮无平仓")
    for c in result["closed"]:
        print(
            f"  平仓 {c['symbol']:<12} {c['reason']:<14} "
            f"{c['pnl_pct']:>+7.2f}%  净 {c['pnl_usd']:>+9.4f} USDT"
        )
    return 0


def cmd_status(desk: memehunter.Desk, args) -> int:
    r = desk.report()
    print("== MemeHunter E 账户（Solana meme 虚拟盘）==")
    print(f"账本     : {desk.db_path}")
    print(f"现金     : {r['cash_usd']:,.2f} USDT")
    print(f"持仓市值 : {r['positions_usd']:,.2f} USDT")
    print(f"总权益   : {r['equity_usd']:,.2f} / {r['starting_usd']:,.2f} USDT"
          f"   ({r['pnl_usd'] / r['starting_usd'] * 100:+.2f}%)")
    line = f"已实现   : {r['realized_usd']:+,.2f} USDT  |  已平 {r['closed_count']} 笔"
    if r["win_rate"] is not None:
        line += f"  胜率 {r['win_rate']:.0f}%"
    print(line)
    if r["open"]:
        print(f"\n{'标的':<13}{'入场价':>14}{'现价':>14}{'浮动':>9}{'市值':>9}{'持有':>7}{'分':>6}")
        print("-" * 74)
        for p in r["open"]:
            chg = f"{p['change_pct']:+.1f}%" if p["change_pct"] is not None else "无报价"
            print(
                f"{p['symbol'][:12]:<13}{p['entry']:>14.6g}{p['mark']:>14.6g}"
                f"{chg:>9}{p['value_usd']:>9.1f}{p['age_min']:>6.0f}m{p['score']:>6.1f}"
            )
    else:
        print("\n当前无持仓")
    if r["closed"]:
        print("\n最近平仓:")
        for c in r["closed"]:
            print(f"  {c['symbol'][:12]:<13}{c['reason']:<14}{c['pnl_pct']:>+7.2f}%"
                  f"{c['pnl_usd']:>+10.4f} USDT")
    return 0


def cmd_candidates(desk: memehunter.Desk, args) -> int:
    rows = list(
        desk.conn.execute(
            "SELECT * FROM candidates WHERE scan_id=(SELECT MAX(id) FROM scans) "
            "ORDER BY eligible DESC, score DESC LIMIT 40"
        )
    )
    if not rows:
        print("还没有扫描记录，先跑 scan")
        return 1
    print(f"{'标的':<13}{'分':>6}{'合格':>5}  {'拒绝原因':<26}{'有机':>6}{'持币':>8}{'流动性':>11}")
    print("-" * 80)
    for r in rows:
        print(
            f"{r['symbol'][:12]:<13}{r['score']:>6.1f}{'是' if r['eligible'] else '否':>5}  "
            f"{(r['reject_reason'] or '-'):<24}{r['organic_score']:>6.1f}"
            f"{r['holder_count']:>8,}{r['liquidity_usd']:>11,.0f}"
        )
    return 0


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid is still running.

    os.kill(pid, 0) is not usable on Windows: it raises for every pid, alive or
    not. The Win32 pair used here is the portable way to ask the kernel.
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def cmd_loop(desk: memehunter.Desk, args) -> int:
    """Stay alive. Mark first, then scan.

    The desk used to run from a shell `for` loop that stopped after 30 cycles,
    so every time the terminal closed the book froze mid-position: five
    positions sat at a six-day-old mark and the dashboard kept rendering them.
    This is the same loop with an owner: a pid file, a heartbeat the dashboard
    can read, and a stop condition that survives a bad week.
    """
    interval = max(45, int(args.interval))
    root = ROOT / "runtime" / "memehunter"
    root.mkdir(parents=True, exist_ok=True)
    pid_file = root / ".loop.pid"
    beat_file = root / "loop.heartbeat.json"
    halt_file = root / "halt.json"

    if pid_file.exists():
        try:
            old = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            print(f"已有一个循环在跑（pid {old}）。先停掉它，再启动新的。")
            return 1
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    cycle = 0
    halted = halt_file.exists()
    print(f"循环启动 pid={os.getpid()} 间隔={interval}s 首次扫描…", flush=True)
    try:
        while True:
            cycle += 1
            started = time.time()
            lines = []
            # ---- 1. mark and exit first: an unmarked position is a blind one
            try:
                upd = desk.update()
                parts = [f"标记 {upd['marked']}"]
                if upd["stale"]:
                    parts.append(f"无报价 {len(upd['stale'])}")
                for c in upd["closed"]:
                    parts.append(f"平 {c['symbol']} {c['reason']} {c['pnl_pct']:+.1f}%")
                lines.append(" · ".join(parts))
            except Exception as exc:                       # noqa: BLE001
                lines.append(f"update 失败: {type(exc).__name__}: {exc}")

            # ---- 2. stop condition
            h = desk.health(since_ms=int(desk.cfg.get("health_since_ms") or 0))
            if h["halt"] and not halted:
                halted = True
                halt_file.write_text(
                    json.dumps({**h, "at": memehunter.now_ms()}, ensure_ascii=False),
                    encoding="utf-8",
                )
                lines.append("已熔断：" + h["note"])
            elif not h["halt"] and halted:
                halted = False
                halt_file.unlink(missing_ok=True)
                lines.append("熔断解除，恢复开仓")

            # ---- 3. always scan: it records the candidate snapshot the replay
            #      engine needs. Only open when the desk is allowed to trade.
            try:
                res = desk.scan()
                line = f"扫 {res['seen']} 合格 {res['eligible']}"
                if res["errors"]:
                    line += f" | {res['errors'][0]}"
                if halted or args.mark_only:
                    line += "（只记录，不开仓）"
                else:
                    room = int(desk.cfg["max_open_positions"]) - len(desk.open_positions())
                    opened, skipped = _try_open(desk, res["ranked"], room)
                    if opened:
                        line += " 开仓 " + "、".join(o["symbol"] for o in opened)
                    elif room <= 0:
                        line += "（满仓）"
                    elif res["eligible"]:
                        line += f"（{skipped[0][1] if skipped else '候选已持有'}）"
                lines.append(line)
            except Exception as exc:                       # noqa: BLE001
                lines.append(f"scan 失败: {type(exc).__name__}: {exc}")

            beat = {
                "pid": os.getpid(), "cycle": cycle, "interval_s": interval,
                "at_ms": memehunter.now_ms(), "halted": halted,
                "equity_usd": round(desk.report()["equity_usd"], 2),
                "line": " | ".join(lines),
            }
            beat_file.write_text(json.dumps(beat, ensure_ascii=False), encoding="utf-8")
            print(f"[{cycle}] " + " | ".join(lines), flush=True)

            sleep_s = interval - (time.time() - started)
            if sleep_s > 0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在收尾…", flush=True)
        try:
            desk.update()
        except Exception:                                  # noqa: BLE001
            pass
        return 0
    finally:
        pid_file.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["scan", "update", "status", "candidates", "loop"])
    ap.add_argument("--dry-run", action="store_true", help="scan only, open nothing")
    ap.add_argument("--interval", type=int, default=180,
                    help="loop: seconds between cycles (default 180)")
    ap.add_argument("--mark-only", action="store_true",
                    help="loop: mark and exit, never open")
    args = ap.parse_args()

    cfg = None
    cfg_path = ROOT / "user_data" / "config.memehunter.json"
    if cfg_path.is_file():
        cfg = {
            k: v for k, v in json.loads(cfg_path.read_text(encoding="utf-8")).items()
            if not k.startswith("_")
        }
    desk = memehunter.Desk(ROOT, cfg)
    return {
        "scan": cmd_scan,
        "update": cmd_update,
        "status": cmd_status,
        "candidates": cmd_candidates,
        "loop": cmd_loop,
    }[args.action](desk, args)


if __name__ == "__main__":
    sys.exit(main())
