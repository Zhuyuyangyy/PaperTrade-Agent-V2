"""实验 E2 命令行：入场结构对照（零 API）。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data import load_bars
from .structure import default_variants, run_variant


def main() -> int:
    ap = argparse.ArgumentParser(prog="research.experiment.structure_main")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cache = Path(a.cache) if a.cache else Path("data/v3_entryE/bars_5m_cache.json")
    bs = load_bars(days=a.days, cache=cache)
    bars = bs.bars
    rows, all_trades = [], []
    for v in default_variants():
        r = run_variant(bars, v)
        all_trades.extend(r.pop("trades"))
        rows.append(r)
        print(f"{r['variant']:12s} n={r['all'].get('n',0):4d} "
              f"net={r['all'].get('net','-'):>12s} win={r['all'].get('win_pct','-'):>6s}% "
              f"oos={r['oos'].get('net','-'):>12s}", flush=True)
    out = {"bars": len(bars), "quality": bs.quality.to_dict(), "variants": rows}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    tp = Path(a.out).with_suffix(".trades.jsonl")
    with tp.open("w", encoding="utf-8") as f:
        for t in all_trades:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"written -> {a.out} ({len(all_trades)} trades)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
