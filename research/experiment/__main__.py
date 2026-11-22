"""实验 M 命令行：独立 run_id 的配对历史事件回放。

    python -m research.experiment paired --root data/v3_paired_v2 \
        --run-id r20261122_01 --days 60 --horizon 72 --sample 40
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from decimal import Decimal as D
from pathlib import Path

from .data import load_bars
from .paired import generate_events, run_event, select_stratified


def _paired(args) -> int:
    run_dir = Path(args.root) / args.run_id
    if run_dir.exists():
        raise SystemExit(f"run_id 已存在，拒绝复用旧目录/SQLite/JSONL: {run_dir}")
    run_dir.mkdir(parents=True)
    if getattr(args, "env_from", None):
        import shutil
        shutil.copy(Path(args.env_from), run_dir / ".env")
    cache = run_dir / "bars_5m_cache.json"
    if getattr(args, "cache_from", None):
        import shutil
        shutil.copy(Path(args.cache_from), cache)
    barset = load_bars(days=args.days, cache=cache)
    bars = barset.bars
    if len(bars) < args.horizon + 100:
        raise SystemExit(f"行情不足：{len(bars)} 根，至少需要 {args.horizon + 100}")

    all_events = generate_events(bars, notional=D(str(args.notional)))
    chosen = select_stratified(all_events, n=args.sample)
    if not chosen:
        raise SystemExit("没有生成任何事件")

    ai = None
    if args.ai == "deepseek":
        from research.decision.providers.position_ai import DeepSeekPositionManager
        from research.execution.binance_demo import load_demo_env
        from research.intel.news_llm import LLMConfig
        load_demo_env(run_dir)
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            ai = DeepSeekPositionManager(LLMConfig(api_key=key))

    cfg = {
        "run_id": args.run_id, "symbol": "BTCUSDT", "days": args.days,
        "horizon_bars": args.horizon, "horizon_hours": args.horizon * 5 / 60,
        "notional": str(args.notional), "sample_n": args.sample,
        "event_rule": "fixed EMA20 cross, incremental ATR14, no lookahead",
        "selection": "regime × time-quartile stratified, PnL-blind",
        "news": "NEWS_UNAVAILABLE (no historical news feed)",
        "ai": args.ai, "bar_quality": barset.quality.to_dict(),
        "bars_sha256": hashlib.sha256(
            json.dumps([c.open_ms for c in bars]).encode()).hexdigest()[:16],
        "events_total": len(all_events),
        # 选中事件集的指纹：只有 fingerprint 相同的 run 才能跨 run 比较收益。
        "events_fingerprint": hashlib.sha256(
            json.dumps([(e.as_of_ms, e.side, e.regime) for e in chosen],
                       ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16],
    }
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "events.jsonl").write_text("".join(
        json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in all_events),
        encoding="utf-8")

    out_path = run_dir / "paired_results.jsonl"
    totals = {g: D("0") for g in "ABCD"}
    with out_path.open("x", encoding="utf-8") as fh:
        for ev in chosen:
            future = bars[ev.bar_index + 1:]
            for r in run_event(ev, future, run_dir, ai_manager=ai,
                               horizon=args.horizon):
                totals[r.group] += r.pnl
                fh.write(json.dumps({"event": ev.to_dict(), "result": r.to_dict()},
                                    ensure_ascii=False) + "\n")
    print(json.dumps({
        "run_dir": str(run_dir), "bars": len(bars), "cache_hit": barset.quality.cache_hit,
        "bar_quality": barset.quality.to_dict(), "events_total": len(all_events),
        "events_selected": len(chosen), "ai_manager": bool(ai),
        "net_pnl_totals": {k: str(v) for k, v in totals.items()},
    }, ensure_ascii=False, indent=2))
    return 0


def _analyze(args) -> int:
    from .analyze import analyze_run

    out = analyze_run(Path(args.run), top_n=args.top)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written -> {args.out}")
    else:
        print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="research.experiment")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("analyze", help="汇总配对回放（只读）")
    pa.add_argument("--run", required=True)
    pa.add_argument("--out", default=None)
    pa.add_argument("--top", type=int, default=12, help="控制台展示的统计字段数")
    p = sub.add_parser("paired", help="配对历史事件回放（独立 run_id）")
    p.add_argument("--root", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--horizon", type=int, default=72, help="固定观察窗口，48-96 根 5m")
    p.add_argument("--sample", type=int, default=40)
    p.add_argument("--notional", type=float, default=100)
    p.add_argument("--ai", choices=("none", "deepseek"), default="none")
    p.add_argument("--env-from", default=None, help="复制 .env 到新 run 目录（不打印内容）")
    p.add_argument("--cache-from", default=None, help="复用已有 bars_5m_cache.json，避免重复 REST")
    a = ap.parse_args()
    if a.cmd == "analyze":
        return _analyze(a)
    if not 48 <= a.horizon <= 96:
        raise SystemExit("--horizon 必须在 48-96 根 5m 之间（预先固定实验参数）")
    return _paired(a)


if __name__ == "__main__":
    raise SystemExit(main())
