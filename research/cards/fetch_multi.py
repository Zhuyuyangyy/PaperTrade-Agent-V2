"""多周期 K 线采集：BTC/ETH/SOL × 5m/15m/1h/4h。

只取已收盘的 K 线。未收盘的最后一根必须丢弃——用未完成的 K 线做结构识别
等于偷看未来：那根 K 线的高点可能还会被刷新，pivot 还没定型。

每根 K 线记 7 个原始字段 + 2 个衍生字段：
  taker_buy_volume  主动买入成交量（币安原始字段）
  trades           成交笔数（用于判断数据质量，不用作信号）

对 S13 CVD 来说 taker_buy_volume 是必需的；没有它就只能跳过该卡，不能编。
"""

from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://api.binance.com/api/v3/klines"
OUT = Path(__file__).resolve().parent / "data"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVALS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
START_MS = 1_598_918_400_000        # 2020-09-01 00:00 UTC

FIELDS = ["t", "o", "h", "l", "c", "v", "taker_buy_v", "trades"]


def fetch_page(symbol: str, interval: str, cursor: int) -> list[list]:
    url = f"{BASE}?symbol={symbol}&interval={interval}&startTime={cursor}&limit=1000"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == 3:
                raise RuntimeError(f"{symbol} {interval} cursor={cursor}: {e}") from e
            time.sleep(1.5 * (attempt + 1))
    return []


def fetch_all(symbol: str, interval: str) -> list[list]:
    step = INTERVALS[interval]
    out: list[list] = []
    cursor = START_MS
    while True:
        batch = fetch_page(symbol, interval, cursor)
        if not batch:
            break
        out.extend(batch)
        nxt = int(batch[-1][0]) + step
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < 1000:
            break
        time.sleep(0.25)
    return out


def to_csv(symbol: str, interval: str, rows: list[list]) -> Path:
    """落盘。丢弃未收盘的最后一根。"""
    now_ms = int(time.time() * 1000)
    complete = [r for r in rows if int(r[6]) < now_ms]

    p = OUT / f"{symbol}_{interval}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="\n", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(FIELDS)
        for r in complete:
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[9], r[8]])
    return p


def audit(path: Path, interval: str) -> dict:
    """空洞与重复检查。空洞会被下游误判成「一直持仓」，必须报出来。"""
    step = INTERVALS[interval]
    with path.open(newline="", encoding="utf-8") as fh:
        ts = [int(r["t"]) for r in csv.DictReader(fh)]
    if not ts:
        return {"n": 0}
    gaps = [i for i in range(1, len(ts)) if ts[i] - ts[i - 1] != step]
    return {"n": len(ts), "gaps": len(gaps),
            "first": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts[0] / 1000)),
            "last": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts[-1] / 1000))}


def main(argv: list[str] | None = None) -> None:
    """只拉指定周期：`python fetch_multi.py 1h 4h`。5m 太慢，必须单独后台跑。"""
    import sys
    only = (argv if argv is not None else sys.argv[1:]) or list(INTERVALS)
    meta_path = OUT / "_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    for sym in SYMBOLS:
        for itv in [i for i in INTERVALS if i in only]:
            rows = fetch_all(sym, itv)
            p = to_csv(sym, itv, rows)
            a = audit(p, itv)
            meta[f"{sym}_{itv}"] = a
            print(f"{sym:<9}{itv:<5} {a['n']:>7} 根  "
                  f"{a['first']} → {a['last']}  空洞 {a['gaps']}  "
                  f"{p.stat().st_size/1024/1024:.1f}MB", flush=True)

    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    bad = {k: v["gaps"] for k, v in meta.items() if v.get("gaps")}
    print("\n有空洞的:", bad if bad else "无")


if __name__ == "__main__":
    main()