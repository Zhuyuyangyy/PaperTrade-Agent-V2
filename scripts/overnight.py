#!/usr/bin/env python
"""8 小时无人值守编排器：监控 + 每 90 分钟研究循环 + 早晨报告。

不启动/不重启交易进程；交易进程由外部（V2 引擎、1h live）负责。
只读账本、只写报告，不改运行中的策略。

用法：
    python scripts/overnight.py --hours 8 --root data/openalpha_v2 \
        --out reports/overnight/2026-11-22
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.runtime.metrics import summarize_books  # noqa: E402

D = Decimal
HERE = ROOT / "scripts"


def _health(root: Path) -> dict:
    hb = root / "state" / "heartbeat_v2.json"
    out = {"ts_ms": int(time.time() * 1000), "heartbeat_age_ms": None, "books": {},
           "feed": None}
    if hb.is_file():
        try:
            data = json.loads(hb.read_text(encoding="utf-8"))
            out["heartbeat_age_ms"] = out["ts_ms"] - int(data.get("updated_at_ms", 0))
            out["books"] = data.get("books") or {}
            out["feed"] = data.get("feed")
        except ValueError:
            pass
    return out


def _trades(root: Path, out: Path) -> None:
    rows = []
    for path in sorted(root.glob("*.sqlite")):
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            q = ("SELECT o.intent_id, o.ts_ms, o.side, o.price, o.qty, o.strategy_id,"
                 " p.exit_ts, p.exit_price, p.exit_reason, p.pnl, p.status"
                 " FROM orders o LEFT JOIN positions p ON p.intent_id=o.intent_id"
                 " ORDER BY o.ts_ms")
            for r in c.execute(q):
                rows.append({"book": path.stem, **{k: r[k] for k in r.keys()}})
        finally:
            c.close()
    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["book", "intent_id", "ts_ms", "side", "price",
                                           "qty", "strategy_id", "exit_ts", "exit_price",
                                           "exit_reason", "pnl", "status"])
        w.writeheader()
        w.writerows(rows)


def _performance(root: Path, out: Path, prices=None) -> dict:
    books = summarize_books(root, prices=prices)
    with (out / "performance.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["book", "equity", "cash", "margin", "unrealized", "realized",
                    "win_rate", "max_drawdown", "orders", "closed", "open"])
        for name, b in books.items():
            if name == "root" or not b:
                continue
            w.writerow([name, b["equity"], b["cash"], b["margin_used"], b["unrealized_pnl"],
                        b["realized_pnl"], b["win_rate"], b["max_drawdown"],
                        b["orders"], b["closed"], b["open_positions"]])
    return books


def _last_loop(out: Path) -> dict | None:
    p = out / "research_loop.jsonl"
    if not p.is_file():
        return None
    lines = p.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _report(root: Path, out: Path, hours: float, started_ms: int) -> None:
    books = _performance(root, out)
    _trades(root, out)
    loop = _last_loop(out) or {}
    bt = (loop.get("backtest") or {})
    best = max(bt.items(), key=lambda kv: kv[1].get("net_bps", -1e9), default=(None, {}))
    health_rows = []
    hp = out / "health.csv"
    if hp.is_file():
        health_rows = list(csv.DictReader(hp.open(encoding="utf-8")))
    gaps = [int(r["heartbeat_age_ms"]) for r in health_rows
            if r.get("heartbeat_age_ms") and r["heartbeat_age_ms"].isdigit()]
    summary = {
        "hours_planned": hours,
        "elapsed_hours": round((int(time.time() * 1000) - started_ms) / 3_600_000, 2),
        "books": {k: {kk: v[kk] for kk in ("equity", "cash", "realized_pnl", "orders",
                                          "closed", "open_positions", "win_rate")}
                  for k, v in books.items() if k != "root" and v},
        "research_loops": len(hp.read_text().splitlines()) - 1 if hp.is_file() else 0,
        "last_loop": loop.get("ts"),
        "backtest": bt,
        "best_variant": best[0],
        "max_heartbeat_gap_ms": max(gaps) if gaps else None,
    }
    (out / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    (out / "summary.md").write_text(f"""# 夜间自主研究总结

- 计划运行：{hours:.1f} 小时；实际：{summary['elapsed_hours']} 小时
- 数据库：`{root}`
- 三本 5m 账本 + 1h 基线 + 终端

## 账户

| 账本 | equity | 已实现 | 订单 | 平仓 | 持仓 | 胜率 |
|---|---|---|---|---|---|---|
""" + "\n".join(f"| {k} | {v['equity']} | {v['realized_pnl']} | {v['orders']} | "
                f"{v['closed']} | {v['open_positions']} | {v['win_rate']} |"
                for k, v in summary["books"].items())
    + f"""

## 回测（最近一次研究循环）

| 变体 | n | 净 bps/笔 | PF | 胜率 | 回撤 bps |
|---|---|---|---|---|---|
""" + "\n".join(f"| {k} | {v.get('n')} | {v.get('net_bps')} | {v.get('pf')} | "
                f"{v.get('win')} | {v.get('dd_bps')} |" for k, v in bt.items())
    + """

**注意**：回测仅用历史 1h 数据、固定成本假设；8 小时的实时样本不足以证明任何策略长期有效。
""", encoding="utf-8")

    (out / "best_candidate.md").write_text(f"""# 最有希望的候选（基于最近一次回测）

- 变体：**{best[0]}**
- 指标：{json.dumps(best[1], ensure_ascii=False)}
- 样本：{best[1].get('n')} 笔；< 100 笔时结论一律 INCONCLUSIVE
- 局限：单段历史数据、未做 purged walk-forward 的多次重复检验、未含资金费与盘口冲击
- 处理：不提升 VERIFIED、不热更新运行策略；仅记录为下一轮候选
""", encoding="utf-8")

    (out / "system_health.md").write_text(f"""# 系统健康

- 健康采样：{len(health_rows)} 次
- 最大心跳间隔：{summary['max_heartbeat_gap_ms']} ms
- 最后研究循环：{summary['last_loop']}
- 账本：{', '.join(k for k in summary['books'])}
""", encoding="utf-8")

    (out / "next_steps.md").write_text("""# 下一步（三个方向）

1. 用 ≥ 300 个 5m 决策点重跑 ADX 过滤与 2 根认错，做 purged walk-forward；
2. Polymarket 独立概率盘：YES/NO 双盘口 + 扣费 Edge + 独立账本；
3. 第二模型裁决：配置凭据后对比 dual 与 deepseek 的扣费净收益。
""", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--root", default="data/openalpha_v2")
    ap.add_argument("--out", required=True)
    ap.add_argument("--every-min", type=float, default=90.0)
    args = ap.parse_args()
    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = int(time.time() * 1000)
    deadline = time.time() + args.hours * 3600
    last_loop = 0.0

    hp = out / "health.csv"
    new = not hp.exists()
    with hp.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["ts_ms", "heartbeat_age_ms", "feed_mode",
                                           "books_json"])
        if new:
            w.writeheader()
        while time.time() < deadline:
            h = _health(root)
            w.writerow({"ts_ms": h["ts_ms"], "heartbeat_age_ms": h["heartbeat_age_ms"],
                        "feed_mode": (h.get("feed") or {}).get("mode"),
                        "books_json": json.dumps(h["books"], ensure_ascii=False)})
            fh.flush()
            if time.time() - last_loop >= args.every_min * 60 or last_loop == 0:
                try:
                    subprocess.run([sys.executable, str(HERE / "overnight_research.py"),
                                    "--root", str(root), "--out", str(out),
                                    "--tag", time.strftime("%H%M")],
                                   timeout=1200, check=False)
                except Exception as exc:  # noqa: BLE001
                    with (out / "errors.log").open("a", encoding="utf-8") as ef:
                        ef.write(f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}\n")
                last_loop = time.time()
                _report(root, out, args.hours, started)
            time.sleep(60)
    _report(root, out, args.hours, started)
    print(f"overnight done -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())