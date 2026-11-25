"""把 data/v3_market 的缓存导出成 verify 引擎吃的 CSV（可复算，不提交大文件）。

用法：
  PYTHONPATH=. python research/v4/export_candles_csv.py --symbol BTCUSDT --tf 1h
  PYTHONPATH=. python research/v4/export_candles_csv.py --symbol BTCUSDT --kind funding

输出到 research/v4/diagnostic/{sym}_{tf}_3y.csv 与 {sym}_funding_3y.csv，
这两个文件被 .gitignore 排除（可从 data/v3_market 重建）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SRC = Path("data/v3_market")
OUT = Path("research/v4/diagnostic")


def export_bars(sym: str, tf: str) -> Path:
    src = SRC / sym / "bars_1h.json"
    rows = json.loads(src.read_text(encoding="utf-8"))["rows"]
    if tf == "1h":
        out_rows = [(r["t"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows]
    elif tf == "4h":
        out_rows = []
        for i in range(0, len(rows) - 3, 4):
            ch = rows[i:i + 4]
            out_rows.append((ch[0]["t"], ch[0]["o"],
                             max(x["h"] for x in ch), min(x["l"] for x in ch),
                             ch[-1]["c"], str(sum(float(x["v"]) for x in ch))))
    else:
        raise SystemExit(f"仅支持 1h/4h，得到 {tf}")
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{sym.lower()}_{tf}_3y.csv"
    with p.open("w", encoding="utf-8") as f:
        f.write("t,o,h,l,c,v\n")
        for r in out_rows:
            f.write(",".join(str(x) for x in r) + "\n")
    print(f"{p} rows={len(out_rows)}")
    return p


def export_funding(sym: str) -> Path:
    src = SRC / sym / "funding.json"
    rows = json.loads(src.read_text(encoding="utf-8"))["rows"]
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{sym.lower()}_funding_3y.csv"
    with p.open("w", encoding="utf-8") as f:
        f.write("t,rate\n")
        for r in rows:
            f.write(f"{r['t']},{r['rate']}\n")
    print(f"{p} rows={len(rows)}")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="1h", choices=("1h", "4h"))
    ap.add_argument("--kind", default="bars", choices=("bars", "funding", "both"))
    args = ap.parse_args()
    if args.kind in ("bars", "both"):
        export_bars(args.symbol, args.tf)
    if args.kind in ("funding", "both"):
        export_funding(args.symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
