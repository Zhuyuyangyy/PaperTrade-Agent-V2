import json
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.validate import plateau
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
for tf in ("1h",):
    r = plateau(bs.bars, tf=tf)
    cells = r["cells"]
    stops = sorted({c["stop"] for c in cells}, key=float)
    trails = sorted({c["trail"] for c in cells}, key=float)
    print(f"=== {tf} 研究集净收益（行=stop, 列=trail, start=1.0）===")
    print("        " + "".join(f"{t:>10s}" for t in trails))
    for s in stops:
        row = {c["trail"]: c for c in cells if c["stop"] == s}
        print(f"stop {s:>4s} " + "".join(f"{row[t]['research_net']:>10s}" for t in trails))
    print(f"正收益 {r['positives']}/{r['total']} = {r['positive_share_pct']}%  判定 {r['verdict']}")
    print(f"最优 {r['best']}\n邻居 {r['positive_neighbours']}/{r['neighbours']}\n")
    Path(f"reports/entryE3_plateau_{tf}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
