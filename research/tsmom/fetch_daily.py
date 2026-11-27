"""从币安公开 API 拉日线，落成引擎能吃的 CSV（t,o,h,l,c,v）。

为什么用币安而不是仓库里那 3 年 1h 数据：
  1. 1h 只有 BTC，单币做不了截面分散，也测不出"多币组合能不能压波动"；
  2. 3 年日线只有 1095 个点，TSMOM 的持有期动辄 30 日，样本量根本不够；
  3. 日线单币 8 年 = 2900 点，主流币 8~10 个，拼起来才有统计意义。

数据口径（写进报告，不许含糊）：
  - 币安现货 K 线，UTC 日线，未做任何复权（加密无分红，无需复权）
  - 只取已收盘的 K 线（去掉最后一根未完成日）
  - 全程只读公开端点，不碰账户、不下单

用法：
    python research/tsmom/fetch_daily.py                # 拉默认币种
    python research/tsmom/fetch_daily.py --check        # 只体检已有文件
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

BASE = "https://api.binance.com/api/v3/klines"
DAY_MS = 86_400_000

# 选择标准：币安现货 USDT 对里，2020 年前就上市、且至今仍在交易的主流币。
# 刻意包含不同板块（L1 / 交易所 / 支付 / meme），避免"全是同涨同跌的 beta"。
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
    "SOLUSDT", "DOGEUSDT", "LTCUSDT", "LINKUSDT", "DOTUSDT",
    "TRXUSDT", "EOSUSDT", "XLMUSDT", "ETCUSDT", "BCHUSDT",
]


def fetch(symbol: str, start_ms: int = 0) -> list[list]:
    """按 1000 根一页往回翻，直到拿全历史。"""
    out: list[list] = []
    cursor = start_ms
    while True:
        url = f"{BASE}?symbol={symbol}&interval=1d&limit=1000&startTime={cursor}"
        req = urllib.request.Request(url, headers={"User-Agent": "research/tsmom"})
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.loads(r.read().decode())
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + DAY_MS
        time.sleep(0.25)          # 公开端点限速很宽松，但这个礼貌还是要讲
    return out


def to_csv(symbol: str, rows: list[list]) -> Path:
    """落盘。最后一根若未收盘则丢弃——用未完成的 K 线回测等于偷看未来。

    行尾固定 LF：`newline=""` 会让 csv 模块用平台默认行尾，Windows 上写出
    CRLF、Linux 上写出 LF，于是同一份数据在两台机器上字节不同。这个坑在
    docs/falsifier 上踩过一次（本地 231755B / 线上 227031B，差 4724 字节正好
    4724 行），不再踩第二次。
    """
    now_ms = int(time.time() * 1000)
    complete = [r for r in rows if int(r[6]) < now_ms]      # r[6] = closeTime

    p = DATA / f"{symbol}.csv"
    with open(p, "w", newline="\n", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["t", "o", "h", "l", "c", "v"])
        for r in complete:
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5]])
    return p


def report(path: Path) -> None:
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"  {path.stem:10s} 空文件")
        return
    first, last = rows[0]["t"], rows[-1]["t"]
    t0 = time.strftime("%Y-%m-%d", time.gmtime(int(first) / 1000))
    t1 = time.strftime("%Y-%m-%d", time.gmtime(int(last) / 1000))
    # 数据完整性：相邻日期必须严格差 1 天，否则中间有空洞
    gaps = 0
    for a, b in zip(rows, rows[1:]):
        if int(b["t"]) - int(a["t"]) != DAY_MS:
            gaps += 1
    print(f"  {path.stem:10s} {len(rows):5d} 根  {t0} → {t1}  空洞 {gaps} 处")


def main(argv: list[str]) -> int:
    DATA.mkdir(parents=True, exist_ok=True)

    if "--check" in argv:
        print("数据体检：")
        total = 0
        for s in SYMBOLS:
            p = DATA / f"{s}.csv"
            if p.is_file():
                report(p)
                with open(p, encoding="utf-8") as f:
                    total += max(0, sum(1 for _ in f) - 1)
        print(f"\n合计 {total} 根日线")
        return 0

    ok = 0
    for s in SYMBOLS:
        try:
            rows = fetch(s)
            p = to_csv(s, rows)
            print(f"  {s:10s} 拉到 {len(rows)} 根 → {p.name}")
            ok += 1
            time.sleep(0.3)
        except Exception as e:
            print(f"  {s:10s} 失败：{type(e).__name__}: {e}")

    print(f"\n成功 {ok}/{len(SYMBOLS)}")
    print("\n体检：")
    for s in SYMBOLS:
        p = DATA / f"{s}.csv"
        if p.is_file():
            report(p)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))