#!/usr/bin/env python
"""DeepSeek 真实快照冒烟：N 个历史决策点，逐个调用 provider。

用法：
    export DEEPSEEK_API_KEY=...
    python scripts/deepseek_snapshot_smoke.py --rounds 20

只读 Binance 公开 K 线；输出方向分布、延迟、错误与每分钟调用成本估算。
研究用，不产生交易。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.decision import DeepSeekProvider, MarketSnapshot  # noqa: E402
from research.intel.news_llm import LLMConfig  # noqa: E402
from research.market.state import analyze  # noqa: E402
from research.runtime.live import BinanceKlineSource  # noqa: E402
from research.runtime.sources import CandlesSource  # noqa: E402

WARMUP = 210


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--limit", type=int, default=300)
    args = ap.parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("DEEPSEEK_API_KEY 未设置", file=sys.stderr)
        return 2

    live = BinanceKlineSource(limit=args.limit, timeout_s=20, retries=1)
    views = live.candles(int(time.time() * 1000))
    base = views.get("1h") or []
    if len(base) < WARMUP + args.rounds + 2:
        print(f"1h K 线不足: {len(base)}", file=sys.stderr)
        return 1
    source = CandlesSource(base)
    provider = DeepSeekProvider(LLMConfig(api_key=key))
    out = []
    t0 = time.time()
    for i in range(args.rounds):
        as_of = base[WARMUP + i].close_ms + 31_000
        state = analyze("BTC/USDT", source.candles(as_of), as_of)
        d = provider.decide(MarketSnapshot(as_of, "BTC/USDT", state))
        out.append({"as_of_ms": as_of, "trend": state.trend, **d.to_dict()})
        print(f"{i + 1:02d} {state.trend:10s} -> {d.choice:5s} {d.status:12s} "
              f"{d.latency_ms:5d}ms {d.rationale[:60]}")
    elapsed = time.time() - t0
    summary = {
        "rounds": len(out),
        "elapsed_s": round(elapsed, 1),
        "choices": dict(Counter(x["choice"] for x in out)),
        "statuses": dict(Counter(x["status"] for x in out)),
        "avg_latency_ms": round(sum(x["latency_ms"] for x in out) / max(1, len(out)), 1),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["statuses"].get("OK") else 1


if __name__ == "__main__":
    raise SystemExit(main())