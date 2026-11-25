"""BTC/ETH/SOL 永续历史回补（只读公开数据，不碰交易接口）。

设计见 research/v4/data_backfill_plan.md。硬约束：
- 418/429 立即停，读 retry-after，绝不重试/换IP/切端点绕过
- 断点续传：每批追加落盘 partial jsonl，中断后从最后时间戳续
- 只走 fapi（vision 无 funding，不可替代）
- 限频：每批 sleep >= 0.6s
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://fapi.binance.com"
OUT = Path("data/v3_market")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
UA = {"User-Agent": "OpenAlpha research backfill"}
SLEEP_S = 0.65
RETRY_NETWORK = 1          # 仅网络超时/5xx 允许 1 次；418/429 绝不重试


class Banned(Exception):
    """418/429 —— 立即停手，绝不重试。"""


def fingerprint(rows: list[dict], meta: dict) -> str:
    """数据指纹：rows + meta（排除 sha256 自身），canonical JSON 取 sha256。

    复算：
        doc = json.load(open(...)); print(fingerprint(doc['rows'], doc['meta']))
    必须与 doc['meta']['sha256'] 相等。
    """
    payload = {"rows": rows, "meta": {k: v for k, v in meta.items() if k != "sha256"}}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _get(url: str) -> list:
    """单点 GET；418/429 抛 Banned，网络错误最多重试 1 次。"""
    req = urllib.request.Request(url, headers=UA)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(RETRY_NETWORK + 1):
        try:
            with opener.open(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                body = e.read().decode("utf-8", "replace")[:300]
                retry_after = e.headers.get("retry-after", "未给出")
                raise Banned(f"HTTP {e.code} retry-after={retry_after} body={body}") from None
            if 500 <= e.code < 600 and attempt < RETRY_NETWORK:
                time.sleep(2.0)
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < RETRY_NETWORK:
                time.sleep(2.0)
                continue
            raise RuntimeError(f"network: {e}") from None
    raise RuntimeError("unreachable")


def _last_ts(path: Path, key: str) -> int:
    """从 partial jsonl 读最后一条时间戳；不存在返回 0。"""
    if not path.is_file():
        return 0
    last = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    last = max(last, int(json.loads(line)[key]))
                except (ValueError, KeyError, TypeError):
                    continue
    return last


def backfill_klines(symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> tuple[int, int]:
    """返回 (新增行数, 请求次数)。每批追加落盘。"""
    p = OUT / symbol / "bars_1h.partial.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = max(start_ms, _last_ts(p, "t") + 3_600_000)  # 下一根开盘，防重复
    added, reqs = 0, 0
    while cur < end_ms:
        url = (f"{BASE}/fapi/v1/klines?symbol={symbol}&interval=1h"
               f"&startTime={cur}&limit={limit}")
        rows = _get(url)
        reqs += 1
        if not isinstance(rows, list) or not rows:
            break
        with p.open("a", encoding="utf-8") as f:
            for r in rows:
                rec = {"t": int(r[0]), "o": r[1], "h": r[2], "l": r[3],
                       "c": r[4], "v": r[5], "timeframe_ms": 3_600_000}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                added += 1
        cur = int(rows[-1][0]) + 3_600_000
        print(f"  [kline] {symbol} +{len(rows)} → "
              f"{datetime.fromtimestamp(cur/1000, timezone.utc).strftime('%Y-%m-%d %H:%M')}", flush=True)
        time.sleep(SLEEP_S)
        if len(rows) < limit:
            break
    return added, reqs


def backfill_funding(symbol: str, start_ms: int, end_ms: int, limit: int = 500) -> tuple[int, int]:
    p = OUT / symbol / "funding.partial.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = max(start_ms, _last_ts(p, "t") + 1)
    added, reqs = 0, 0
    while cur < end_ms:
        url = (f"{BASE}/fapi/v1/fundingRate?symbol={symbol}"
               f"&startTime={cur}&limit={limit}")
        rows = _get(url)
        reqs += 1
        if not isinstance(rows, list) or not rows:
            break
        with p.open("a", encoding="utf-8") as f:
            for r in rows:
                rec = {"t": int(r["fundingTime"]), "rate": r["fundingRate"],
                       "mark_price": r.get("markPrice")}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                added += 1
        cur = int(rows[-1]["fundingTime"]) + 1
        print(f"  [fund ] {symbol} +{len(rows)} → "
              f"{datetime.fromtimestamp(cur/1000, timezone.utc).strftime('%Y-%m-%d %H:%M')}", flush=True)
        time.sleep(SLEEP_S)
        if len(rows) < limit:
            break
    return added, reqs


def finalize(symbol: str, years: int) -> dict:
    """partial → 正式 json（去重/排序/缺口校验/指纹）。"""
    # K线时间戳严格 1h 网格；funding 结算时刻有 ±几秒的毫秒偏移，
    # 所以用容差判定（|delta-step| > 5min 才算缺口），避免把毫秒抖动误报成缺口。
    specs = (("bars_1h.json", "bars_1h.partial.jsonl", 3_600_000, 0),
             ("funding.json", "funding.partial.jsonl", 28_800_000, 300_000))
    summary = {"symbol": symbol}
    for final_name, partial_name, step, tol in specs:
        src = OUT / symbol / partial_name
        if not src.is_file():
            summary[final_name] = "MISSING"
            continue
        seen = {}
        with src.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    seen[int(r["t"])] = r
        rows = [seen[k] for k in sorted(seen)]
        gaps = []
        for a, b in zip(rows, rows[1:]):
            d = int(b["t"]) - int(a["t"])
            if abs(d - step) > tol:
                gaps.append({"from": a["t"], "to": b["t"], "delta_ms": d})
        doc = {"rows": rows,
               "meta": {"symbol": symbol, "source": "fapi.binance.com",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "years": years, "n": len(rows),
                        "first_ms": rows[0]["t"] if rows else None,
                        "last_ms": rows[-1]["t"] if rows else None,
                        "gaps": gaps[:200], "n_gaps": len(gaps)}}
        # 指纹口径（自明）：对 {"rows": rows, "meta": meta_不含sha256} 的
        # canonical JSON 取 sha256。复算无需猜"sha256 是置空还是删除"。
        doc["meta"]["sha256"] = fingerprint(rows, doc["meta"])
        dst = OUT / symbol / final_name
        dst.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        summary[final_name] = {"n": len(rows), "gaps": len(gaps),
                               "first": doc["meta"]["first_ms"], "last": doc["meta"]["last_ms"],
                               "sha256": doc["meta"]["sha256"][:16]}
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--kinds", nargs="+", default=["bars", "funding"],
                    choices=["bars", "funding"])
    ap.add_argument("--finalize-only", action="store_true")
    args = ap.parse_args()

    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = int((datetime.now(timezone.utc) - timedelta(days=365.25 * args.years)).timestamp() * 1000)
    print(f"窗口 {datetime.fromtimestamp(start/1000, timezone.utc):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(end/1000, timezone.utc):%Y-%m-%d} | "
          f"symbols={args.symbols} kinds={args.kinds}", flush=True)

    if not args.finalize_only:
        try:
            for sym in args.symbols:
                if "bars" in args.kinds:
                    a, r = backfill_klines(sym, start, end)
                    print(f"[完成] {sym} klines +{a} 行 / {r} 次请求", flush=True)
                if "funding" in args.kinds:
                    a, r = backfill_funding(sym, start, end)
                    print(f"[完成] {sym} funding +{a} 行 / {r} 次请求", flush=True)
        except Banned as e:
            print(f"\n[418/429 立即停手] {e}\n绝不重试、不换IP、不切端点。partial 已落盘，可稍后续跑。", flush=True)
            return 3

    print("\n=== 校验并生成正式缓存 ===", flush=True)
    for sym in args.symbols:
        print(json.dumps(finalize(sym, args.years), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
