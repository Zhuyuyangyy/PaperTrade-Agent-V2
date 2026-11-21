#!/usr/bin/env python
"""终端实时证据采集：每 30 秒记录价格、权益、feed 状态，默认 10 分钟。

用法：
    python scripts/terminal_evidence.py --minutes 10 --out data/terminal_evidence.jsonl
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--interval-s", type=float, default=30.0)
    ap.add_argument("--out", default="data/terminal_evidence.jsonl")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{args.base}/api/state", timeout=8) as r:
                    s = json.load(r)
                with urllib.request.urlopen(f"{args.base}/api/klines?tf=1m", timeout=8) as r:
                    k = json.load(r)
                row = {
                    "ts_ms": int(time.time() * 1000),
                    "price": k[-1]["close"],
                    "kline_time": k[-1]["time"],
                    "feed": s.get("feed"),
                    "equity": {name: (s["books"][name] or {}).get("equity")
                               for name in ("shadow", "deepseek", "verified")},
                    "cash": {name: (s["books"][name] or {}).get("cash")
                             for name in ("shadow", "deepseek", "verified")},
                    "unrealized": {name: (s["books"][name] or {}).get("unrealized_pnl")
                                   for name in ("shadow", "deepseek", "verified")},
                }
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                n += 1
            except Exception as exc:  # noqa: BLE001
                fh.write(json.dumps({"ts_ms": int(time.time() * 1000),
                                     "error": f"{type(exc).__name__}: {exc}"}) + "\n")
            time.sleep(args.interval_s)
    print(f"wrote {n} samples -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())