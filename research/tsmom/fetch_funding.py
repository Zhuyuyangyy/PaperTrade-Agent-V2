"""拉取资金费历史（8 小时一条），落成引擎要的 t,rate。

为什么必须做这件事：TSMOM 是长期持仓策略，多头在正资金费环境里持续付费。
BTC 历史上资金费均值约 +0.01%/8h，一年 1095 次，等于每年约 11% 的持有成本。
不扣这一项的「净收益」不是净收益，是把成本藏起来了。
"""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "funding"
BASE = "https://fapi.binance.com/fapi/v1/fundingRate"
DAY_MS = 86_400_000
STEP_MS = 8 * 3600 * 1000           # 资金费每 8 小时结算一次

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
           "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT"]


# 资金费历史不早于这个时间：币安永续合约 2019-09 上线。不带 startTime 时
# 端点只返回最近 500 条（166 天），所以必须显式从最早时间开始翻页，否则会
# 拿到一段「看起来完整」的近半年数据，用它回测九年等于把成本偷偷抹掉。
EARLIEST_MS = 1_567_000_000_000        # 2019-08-27，略早于首条记录


def fetch(symbol: str, start_ms: int = EARLIEST_MS) -> list[dict]:
    """按 1000 条一页往后翻。limit 上限 1000（= 333 天）。"""
    out: list[dict] = []
    cursor = start_ms
    while True:
        url = f"{BASE}?symbol={symbol}&startTime={cursor}&limit=1000"
        with urllib.request.urlopen(url, timeout=30) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        out.extend(batch)
        nxt = int(batch[-1]["fundingTime"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
        # 拉满了说明可能还有下一页；没拉满就是到头了
        if len(batch) < 1000:
            break
        time.sleep(0.25)
    return out


def write(symbol: str, rows: list[dict]) -> tuple[Path, dict]:
    """落盘 t,rate。只保留正数时间戳，去重（分页边界会重一天）。"""
    OUT.mkdir(parents=True, exist_ok=True)
    seen: set[int] = set()
    clean: list[tuple[int, float]] = []
    for r in rows:
        t = int(r["fundingTime"])
        if t in seen:
            continue
        seen.add(t)
        clean.append((t, float(r["fundingRate"])))
    clean.sort()

    now_ms = int(time.time() * 1000)
    clean = [(t, v) for t, v in clean if t < now_ms]      # 未结算的丢掉

    path = OUT / f"{symbol}.csv"
    # 行尾固定 LF：这份文件会被 sha256 记账
    with path.open("w", newline="\n", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["t", "rate"])
        for t, v in clean:
            w.writerow([t, f"{v:.10f}"])

    rates = [v for _, v in clean]
    meta = {
        "n": len(rates),
        "sum_pct": sum(rates) * 100,
        "mean_bps": (sum(rates) / len(rates) * 10000) if rates else 0.0,
        "positive_pct": (sum(1 for v in rates if v > 0) / len(rates) * 100) if rates else 0.0,
        "first": _fmt(clean[0][0]) if clean else None,
        "last": _fmt(clean[-1][0]) if clean else None,
    }
    return path, meta


def _fmt(ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    results = {}
    for sym in SYMBOLS:
        try:
            rows = fetch(sym)
            path, meta = write(sym, rows)
            results[sym] = meta
            print(f"{sym:<10} {meta['n']:>6} 条  {meta['first']} → {meta['last']}  "
                  f"均值 {meta['mean_bps']:+.3f}bps  累计 {meta['sum_pct']:+.2f}%  "
                  f"正费率占比 {meta['positive_pct']:.1f}%", flush=True)
        except Exception as exc:                                       # noqa: BLE001
            print(f"{sym:<10} 失败: {type(exc).__name__}: {exc}", flush=True)
            results[sym] = {"error": str(exc)[:200]}
        time.sleep(0.3)

    (OUT / "_meta.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ok = sum(1 for v in results.values() if "error" not in v)
    print(f"\n成功 {ok}/{len(SYMBOLS)}")


if __name__ == "__main__":
    main()