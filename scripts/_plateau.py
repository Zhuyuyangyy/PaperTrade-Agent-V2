import json
from pathlib import Path
from research.experiment.data import load_bars
from research.experiment.validate import plateau
bs = load_bars(days=60, cache=Path('data/v3_entryE/bars_5m_cache.json'))
r = plateau(bs.bars, tf="4h")
cells = r["cells"]
# 热力图：行=stop, 列=trail
stops = sorted({c["stop"] for c in cells}, key=float)
trails = sorted({c["trail"] for c in cells}, key=float)
print("4h 研究集净收益（行=stop ATR, 列=trail ATR, start=1.0）")
print("        " + "".join(f"{t:>10s}" for t in trails))
for s in stops:
    row = {c["trail"]: c for c in cells if c["stop"] == s}
    print(f"stop {s:>4s} " + "".join(f"{row[t]['research_net']:>10s}" for t in trails))
print()
print("同口径 样本外：")
print("        " + "".join(f"{t:>10s}" for t in trails))
for s in stops:
    row = {c["trail"]: c for c in cells if c["stop"] == s}
    print(f"stop {s:>4s} " + "".join(f"{row[t]['oos_net']:>10s}" for t in trails))
print()
print(f"正收益格 {r['positives']}/{r['total']} = {r['positive_share_pct']}%")
print(f"最优格 {r['best']}")
print(f"最优格同行同列邻居为正: {r['positive_neighbours']}/{r['neighbours']}")
print("判定:", r["verdict"])
Path("reports/entryE3_plateau.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
