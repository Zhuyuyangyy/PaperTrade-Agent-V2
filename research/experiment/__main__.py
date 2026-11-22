"""实验 M 命令行：配对历史事件回放。

    python -m research.experiment paired --root data/v3_paired \
        --cache data/v3_paired/bars_5m.json --max-events 20 --horizon 12

只拉一次历史并缓存；后续全部离线回放，不重复打 Binance REST。
"""
from __future__ import annotations

import argparse
import json
import time
from decimal import Decimal as D
from pathlib import Path

from research.market.contracts import Candle
from research.runtime.live import BinanceKlineSource

from .paired import generate_events, run_event


def _load_bars(cache: Path, limit: int) -> tuple[list[Candle], bool]:
    if cache.exists():
        rows = json.loads(cache.read_text(encoding="utf-8"))
        bars = [Candle(int(r["open_ms"]), D(str(r["open"])), D(str(r["high"])),
                       D(str(r["low"])), D(str(r["close"])), D(str(r["volume"])),
                       int(r["timeframe_ms"])) for r in rows]
        return bars, True
    # 只拉一次 5m，不用 BinanceKlineSource 的四周期批量请求——那正是 418 的来源。
    import urllib.error
    import urllib.request

    url = f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw = None
    for attempt in range(3):
        try:
            with op.open(req, timeout=20) as resp:
                raw = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (418, 429) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    rows = raw or []
    bars = [Candle(int(r[0]), D(str(r[1])), D(str(r[2])), D(str(r[3])),
                   D(str(r[4])), D(str(r[5])), 300_000) for r in rows]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([{
        "open_ms": c.open_ms, "open": str(c.open), "high": str(c.high),
        "low": str(c.low), "close": str(c.close), "volume": str(c.volume),
        "timeframe_ms": c.timeframe_ms} for c in bars], ensure_ascii=False), encoding="utf-8")
    return bars, False


def _paired(args) -> int:
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    bars, cached = _load_bars(Path(args.cache), args.limit)
    events = generate_events(bars, notional=D(str(args.notional)),
                             max_events=args.max_events)
    ai = None
    if args.ai == "deepseek":
        from research.decision.providers.position_ai import DeepSeekPositionManager
        from research.execution.binance_demo import load_demo_env
        from research.intel.news_llm import LLMConfig
        import os
        load_demo_env(root)
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            ai = DeepSeekPositionManager(LLMConfig(api_key=key))
    out_path = root / "paired_results.jsonl"
    totals: dict[str, D] = {g: D("0") for g in "ABCD"}
    with out_path.open("a", encoding="utf-8") as fh:
        for ev in events:
            idx = next((i for i, c in enumerate(bars) if c.open_ms + c.timeframe_ms == ev.as_of_ms), -1)
            future = bars[idx + 1:] if idx >= 0 else []
            for r in run_event(ev, future, root, ai_manager=ai,
                               horizon=args.horizon, ai_only_when_success=True):
                totals[r.group] += r.pnl
                fh.write(json.dumps({"event": ev.to_dict(), "result": r.to_dict()},
                                    ensure_ascii=False) + "\n")
    print(json.dumps({
        "root": str(root), "bars": len(bars), "cache_hit": cached,
        "events": len(events), "ai_manager": bool(ai),
        "totals": {k: str(v) for k, v in totals.items()},
        "out": str(out_path),
    }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="research.experiment")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("paired", help="配对历史事件回放")
    p.add_argument("--root", required=True)
    p.add_argument("--cache", required=True, help="5m K 线缓存 JSON（只写一次）")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--max-events", type=int, default=20)
    p.add_argument("--horizon", type=int, default=12)
    p.add_argument("--notional", type=float, default=100)
    p.add_argument("--ai", choices=("none", "deepseek"), default="none")
    a = ap.parse_args()
    return _paired(a)


if __name__ == "__main__":
    raise SystemExit(main())
