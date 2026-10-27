"""Selectivity audit: does each gate separate winners from losers?

Every filter in the desk is a claim -- "candidates that pass me earn more than
the ones that don't". This module tests those claims on the recorded stream
without re-running anything: same candidates, same shadow prices, one horizon
at a time, every cohort printed (including the ones that show nothing).

It is a diagnostic, not a tuner. The cohorts are pinned in COHORTS below and
were fixed before the numbers were read; adding a cohort after seeing the
report is fishing, and the whole point is to stop doing that.

Two rules that keep the numbers honest:
  * entry price is the first shadow mark at or after the scan, and only if that
    mark is within MIN_ENTRY_LAG_S of the scan -- anything later is a price we
    could not have paid.
  * forward price is the mark nearest the horizon, but never the same round as
    the entry (MIN_FORWARD_S), and never outside TOLERANCE of the horizon.
    A horizon that no mark covers is unknown, not zero.
"""
from __future__ import annotations

import json
import random
import shutil
import sqlite3
import statistics
import tempfile
from pathlib import Path
from typing import Callable, Iterable

HORIZONS = (5, 15, 60)
TOLERANCE = 0.40          # a mark this far from the horizon still counts
MIN_ENTRY_LAG_S = 600     # buying later than this is not a fill, it is a wish
MIN_FORWARD_S = 120       # never the same round as the entry
BOOTSTRAP = 2000
SEED = 42
EXCURSION_THRESHOLDS = (0.05, 0.10, 0.15, 0.30, 0.60)


def _ratio(row: sqlite3.Row, num: str, den: str) -> float | None:
    try:
        d = float(row[den] or 0.0)
        n = float(row[num] or 0.0)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return n / d


# Fixed before looking at the data. Each entry is one falsifiable claim a live
# gate already makes; the report is allowed to say "no lift" for any of them.
COHORTS: tuple[tuple[str, str, Callable[[dict], bool]], ...] = (
    ("score>0", "RISK 通过否决与分数线", lambda c: (c.get("score") or 0) > 0),
    ("eligible", "eligible 标记为真", lambda c: bool(c.get("eligible"))),
    ("llm≥60", "JUDGE 判定 60 分以上", lambda c: (c.get("llm_score") or -1) >= 60),
    ("llm<60", "JUDGE 判定低于 60（对照）", lambda c: c.get("llm_score") is not None and c["llm_score"] < 60),
    ("holders≥1000", "持有人数 ≥ 1000", lambda c: (c.get("holder_count") or 0) >= 1000),
    ("holders<200", "持有人数 < 200（对照）", lambda c: (c.get("holder_count") or 0) < 200),
    ("top10≤20%", "前十大持仓 ≤ 20%", lambda c: c.get("top_holders_pct") is not None and c["top_holders_pct"] <= 20),
    ("top10>30%", "前十大持仓 > 30%（对照）", lambda c: (c.get("top_holders_pct") or 0) > 30),
    ("liq/fdv≥5%", "流动性/FDV ≥ 5%", lambda c: (_ratio(c, "liquidity_usd", "fdv_usd") or 0) >= 0.05),
    ("liq/fdv<2%", "流动性/FDV < 2%（对照）", lambda c: (_ratio(c, "liquidity_usd", "fdv_usd") or 0) < 0.02),
    ("vol1h≥$50k", "1 小时成交额 ≥ $50k", lambda c: (c.get("vol_1h") or 0) >= 50_000),
    ("vol1h<$10k", "1 小时成交额 < $10k（对照）", lambda c: (c.get("vol_1h") or 0) < 10_000),
    ("chg5m>0", "5 分钟动量为正", lambda c: (c.get("chg_5m") or 0) > 0),
    ("chg5m<0", "5 分钟动量为负（对照）", lambda c: (c.get("chg_5m") or 0) < 0),
)


def _config_cost(root: Path) -> float:
    path = root / "user_data" / "config.memehunter.json"
    bps = 120.0
    if path.is_file():
        try:
            bps = float(json.loads(path.read_text(encoding="utf-8")).get("round_trip_cost_bps", bps))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return bps / 10_000.0


def _load(root: Path, db_name: str = "memehunter.sqlite") -> tuple[sqlite3.Connection, Path]:
    """Copy the live ledger first: a replay must not race the desk, and WAL
    databases refuse a read-only URI open anyway."""
    src = root / "runtime" / "memehunter" / db_name
    tmp_dir = Path(tempfile.mkdtemp(prefix="selectivity_"))
    tmp_db = tmp_dir / db_name
    shutil.copy(src, tmp_db)
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    return conn, tmp_dir


def _marks(conn: sqlite3.Connection) -> dict[int, list[tuple[int, float]]]:
    out: dict[int, list[tuple[int, float]]] = {}
    for row in conn.execute(
        "SELECT candidate_id, marked_at_ms, price_usd FROM candidate_marks"
        " WHERE price_usd > 0 ORDER BY candidate_id, marked_at_ms"
    ):
        out.setdefault(int(row["candidate_id"]), []).append(
            (int(row["marked_at_ms"]), float(row["price_usd"]))
        )
    return out


def _entry_point(path: list[tuple[int, float]], scanned_ms: int):
    """First mark at or after the scan, and only if we could have paid it."""
    for t, p in path:
        if t >= scanned_ms:
            return (t, p) if t - scanned_ms <= MIN_ENTRY_LAG_S * 1000 else None
    return None


def _round_trip(path: list[tuple[int, float]], scanned_ms: int, horizon_min: int):
    """(entry, forward) prices for one candidate at one horizon, or None."""
    entry = _entry_point(path, scanned_ms)
    if entry is None:
        return None
    target = entry[0] + horizon_min * 60_000
    tol = horizon_min * 60_000 * TOLERANCE
    best = None
    for t, p in path:
        if t - entry[0] < MIN_FORWARD_S * 1000:
            continue
        if abs(t - target) <= tol and (best is None or abs(t - target) < abs(best[0] - target)):
            best = (t, p)
    if best is None:
        return None
    return entry[1], best[1]


def _stats(vals: list[float], cost: float) -> dict:
    if not vals:
        return {"n": 0}
    nets = sorted(v - cost for v in vals)
    n = len(nets)
    mean = sum(nets) / n
    trimmed = nets[int(n * 0.05): n - int(n * 0.05)] or nets
    gains = sum(v for v in nets if v > 0)
    losses = -sum(v for v in nets if v < 0)
    # How much of the whole result rides on the best 1%: memecoin PnL is a
    # power law, and a cohort whose edge lives entirely in its tail is a
    # different bet from one that is uniformly positive.
    top1 = max(1, n // 100)
    tail = sum(nets[-top1:]) / sum(nets) if sum(nets) > 0 else None
    return {
        "n": n,
        "mean_pct": mean * 100,
        "median_pct": (nets[n // 2] if n % 2 else (nets[n // 2 - 1] + nets[n // 2]) / 2) * 100,
        "trimmed_pct": (sum(trimmed) / len(trimmed)) * 100,
        "win_pct": sum(1 for v in nets if v > 0) / n * 100,
        "profit_factor": (gains / losses) if losses > 0 else None,
        "tail_share": tail,
        "nets": nets,
    }


def _lift_ci(vals: list[float], base: list[float], cost: float) -> tuple[float | None, float | None]:
    """95% bootstrap CI for (cohort mean - all mean). Overlapping zero means
    the gate has not shown anything yet, whatever the point estimate says."""
    if len(vals) < 5 or len(base) < 5:
        return None, None
    rng = random.Random(SEED)
    diffs = []
    # rng.choices + fmean keeps 2000 resamples in C; the per-element version
    # of this loop takes minutes per cohort on a 2000-sample base.
    fmean = statistics.fmean
    for _ in range(BOOTSTRAP):
        a = fmean(rng.choices(vals, k=len(vals)))
        b = fmean(rng.choices(base, k=len(base)))
        diffs.append(a - b)
    diffs.sort()
    lo = diffs[int(len(diffs) * 0.025)]
    hi = diffs[int(len(diffs) * 0.975)]
    return lo * 100, hi * 100


def audit(root: Path, horizons: Iterable[int] = HORIZONS, db_name: str = "memehunter.sqlite") -> dict:
    conn, tmp_dir = _load(root, db_name)
    try:
        cost = _config_cost(root)
        marks = _marks(conn)
        rows = []
        for row in conn.execute(
            "SELECT id, symbol, scanned_at_ms, price_usd, score, llm_score, eligible,"
            " holder_count, top_holders_pct, liquidity_usd, fdv_usd, vol_1h, organic_score,"
            " chg_5m, reject_reason FROM candidates WHERE id IN"
            " (SELECT DISTINCT candidate_id FROM candidate_marks)"
        ):
            d = dict(row)
            d["path"] = marks.get(int(d["id"]), [])
            rows.append(d)
        span = conn.execute("SELECT MIN(scanned_at_ms), MAX(scanned_at_ms) FROM candidates").fetchone()
    finally:
        conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    import datetime as _dt

    out = {
        "cost_pct": cost * 100,
        "candidates_with_marks": len(rows),
        "window": [
            _dt.datetime.fromtimestamp(span[0] / 1000).strftime("%Y-%m-%d %H:%M") if span[0] else None,
            _dt.datetime.fromtimestamp(span[1] / 1000).strftime("%Y-%m-%d %H:%M") if span[1] else None,
        ],
        "horizons": {},
        "notes": [
            "净收益已扣 round_trip_cost_bps（往返）。",
            "均值受少数暴涨币主导：同时看中位数、5% 截尾均值与尾部占比。",
            "lift 是相对 ALL 的均值差，95% bootstrap 区间；跨 0 即视为尚未证实。",
            "同时检验 14 个队列 × 3 个期限 = 42 个对比，单个显著是正常的，不是发现。",
        ],
    }
    for h in horizons:
        pairs = []  # (candidate dict, raw return)
        for d in rows:
            rt = _round_trip(d["path"], int(d["scanned_at_ms"]), int(h))
            if rt is None:
                continue
            entry, fwd = rt
            if entry <= 0:
                continue
            pairs.append((d, fwd / entry - 1.0))
        base_vals = [r for _, r in pairs]
        base = _stats(base_vals, cost)
        cohorts = []
        for name, claim, test in COHORTS:
            vals = [r for d, r in pairs if test(d)]
            if not vals:
                cohorts.append({"name": name, "claim": claim, "n": 0})
                continue
            st = _stats(vals, cost)
            lo, hi = _lift_ci(vals, base_vals, cost)
            st.update(
                {
                    "name": name,
                    "claim": claim,
                    "lift_pct": st["mean_pct"] - base["mean_pct"],
                    "lift_ci": [lo, hi],
                }
            )
            cohorts.append(st)
        out["horizons"][f"{h}m"] = {"all": base, "cohorts": cohorts, "n_all": len(pairs)}
    return out


def excursions(root: Path, horizon_min: int = 60, db_name: str = "memehunter.sqlite") -> dict:
    """How far prices actually travel inside a hold window.

    The take-profit and stop-loss are only reachable if the distribution gets
    there. This measures the best and worst price seen in the window, so a
    barrier can be checked against the moves that exist instead of the moves
    the desk hopes for. Same entry rule as the audit (no retroactive fills).
    """
    conn, tmp_dir = _load(root, db_name)
    try:
        marks = _marks(conn)
        rows = [
            (int(r["id"]), int(r["scanned_at_ms"]))
            for r in conn.execute(
                "SELECT id, scanned_at_ms FROM candidates WHERE id IN"
                " (SELECT DISTINCT candidate_id FROM candidate_marks)"
            )
        ]
    finally:
        conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    ups: list[float] = []
    downs: list[float] = []
    for cid, scanned in rows:
        entry = _entry_point(marks.get(cid, []), scanned)
        if entry is None:
            continue
        t0, p0 = entry
        if p0 <= 0:
            continue
        window = [p for t, p in marks.get(cid, []) if t0 < t <= t0 + horizon_min * 60_000]
        if not window:
            continue
        ups.append(max(window) / p0 - 1.0)
        downs.append(min(window) / p0 - 1.0)
    n = len(ups)
    out = {"horizon_min": horizon_min, "n": n, "thresholds": {}}
    if not n:
        return out
    ups_s, downs_s = sorted(ups), sorted(downs)
    for thr in EXCURSION_THRESHOLDS:
        out["thresholds"][thr] = {
            "up_pct": sum(1 for x in ups if x >= thr) / n * 100,
            "down_pct": sum(1 for x in downs if x <= -thr) / n * 100,
        }
    out["median_up_pct"] = ups_s[n // 2] * 100
    out["p90_up_pct"] = ups_s[min(n - 1, int(n * 0.9))] * 100
    out["median_down_pct"] = downs_s[n // 2] * 100
    out["p10_down_pct"] = downs_s[max(0, int(n * 0.1))] * 100
    return out


def format_excursions(rep: dict) -> str:
    if not rep.get("n"):
        return f"{rep['horizon_min']}m 窗口：无样本"
    lines = [
        f"{rep['horizon_min']}m 持有的实际波动 · n={rep['n']} · "
        f"最大有利中位 {rep['median_up_pct']:+.1f}% / p90 {rep['p90_up_pct']:+.1f}% · "
        f"最大不利中位 {rep['median_down_pct']:+.1f}% / p10 {rep['p10_down_pct']:+.1f}%"
    ]
    lines.append(f"  {'幅度':>6}{'摸到涨的概率':>14}{'摸到跌的概率':>14}")
    for thr, v in rep["thresholds"].items():
        lines.append(f"  {thr*100:>5.0f}%{v['up_pct']:>13.1f}%{v['down_pct']:>13.1f}%")
    return "\n".join(lines)


def decomposition(root: Path, db_name: str = "memehunter.sqlite") -> dict:
    """Where the money actually went: gross move versus the toll.

    The desk books one pnl per trade; this splits it into what the price did
    and what the round trip cost, grouped by exit reason. A book that loses
    money while its gross is flat is not picking badly -- it is paying to
    stand still.
    """
    conn, tmp_dir = _load(root, db_name)
    try:
        rows = []
        for row in conn.execute(
            "SELECT symbol, exit_reason, qty, entry_fill_price, exit_fill_price,"
            " notional_usd, entry_cost_usd, exit_cost_usd, pnl_usd"
            " FROM positions WHERE status='closed'"
        ):
            d = dict(row)
            qty = float(d["qty"] or 0.0)
            entry = float(d["entry_fill_price"] or 0.0)
            exitp = float(d["exit_fill_price"] or 0.0)
            d["gross"] = qty * (exitp - entry)
            d["toll"] = float(d["entry_cost_usd"] or 0.0) + float(d["exit_cost_usd"] or 0.0)
            d["net"] = float(d["pnl_usd"] or 0.0)
            rows.append(d)
    finally:
        conn.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    by: dict[str, dict] = {}
    for d in rows:
        b = by.setdefault(str(d.get("exit_reason") or "?"), {"n": 0, "gross": 0.0, "toll": 0.0, "net": 0.0, "wins": 0})
        b["n"] += 1
        b["gross"] += d["gross"]
        b["toll"] += d["toll"]
        b["net"] += d["net"]
        b["wins"] += 1 if d["net"] > 0 else 0
    total = {
        "n": len(rows),
        "gross": sum(d["gross"] for d in rows),
        "toll": sum(d["toll"] for d in rows),
        "net": sum(d["net"] for d in rows),
        "wins": sum(1 for d in rows if d["net"] > 0),
    }
    return {"by_reason": by, "total": total}


def format_decomposition(rep: dict) -> str:
    total = rep["total"]
    if not total["n"]:
        return "已平仓账单：无成交"
    lines = [f"已平仓账单分解 · {total['n']} 笔 · 毛利 {total['gross']:+.2f} · 成本 {-total['toll']:+.2f} · 净 {total['net']:+.2f}"]
    lines.append(f"  {'退出原因':<14}{'笔数':>5}{'毛利$':>10}{'成本$':>10}{'净$':>10}{'盈利笔':>7}")
    for reason, b in sorted(rep["by_reason"].items(), key=lambda kv: -kv[1]["net"]):
        lines.append(
            f"  {reason:<14}{b['n']:>5}{b['gross']:>+10.2f}{-b['toll']:>+10.2f}{b['net']:>+10.2f}{b['wins']:>7}"
        )
    lines.append(
        "  毛利 - 成本 = 净；毛利接近 0 而净为负，说明亏损来自来回的手续费，而不是看错方向。"
    )
    return "\n".join(lines)


def format_audit(result: dict) -> str:
    lines = []
    span = result.get("window") or [None, None]
    lines.append(
        f"选择性审计 · 成本 {result['cost_pct']:.2f}% 往返 · 候选 {result['candidates_with_marks']} 个"
        f" · 窗口 {span[0]} → {span[1]}"
    )
    for hkey, block in result["horizons"].items():
        base = block["all"]
        lines.append("")
        if not base.get("n"):
            lines.append(f"[{hkey}] 无可回放样本")
            continue
        lines.append(
            f"[{hkey}] ALL n={base['n']}  均值 {base['mean_pct']:+.2f}%  中位 {base['median_pct']:+.2f}%"
            f"  截尾 {base['trimmed_pct']:+.2f}%  胜率 {base['win_pct']:.0f}%"
            + (f"  尾部占比 {base['tail_share']*100:.0f}%" if base.get("tail_share") is not None else "")
        )
        lines.append(f"      {'队列':<12} {'n':>5} {'均值':>8} {'中位':>8} {'胜率':>6} {'lift':>8}  {'95% 区间':>16}")
        for c in block["cohorts"]:
            if not c.get("n"):
                lines.append(f"      {c['name']:<12} {0:>5}       —")
                continue
            ci = c.get("lift_ci") or [None, None]
            ci_txt = (
                f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci[0] is not None and ci[1] is not None else "—"
            )
            mark = " *" if ci[0] is not None and (ci[0] > 0 or ci[1] < 0) else "  "
            lines.append(
                f"      {c['name']:<12} {c['n']:>5} {c['mean_pct']:>+7.2f}% {c['median_pct']:>+7.2f}%"
                f" {c['win_pct']:>5.0f}% {c['lift_pct']:>+7.2f}%  {ci_txt:>16}{mark}"
            )
    lines.append("")
    for note in result.get("notes", []):
        lines.append("  · " + note)
    lines.append("  * = 区间不跨 0（42 个对比里出一个是常态）")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    print(format_audit(audit(root)))
