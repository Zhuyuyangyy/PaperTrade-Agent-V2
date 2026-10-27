"""Is the LLM's opinion worth anything? Measure it instead of arguing.

For every recorded judgement we now have (or will have) a forward return from
the shadow marks. That makes the question answerable:

    do candidates the model scored 70-80 actually outperform those it scored
    20-40, after costs, on data it never saw?

Three numbers, no opinions:

* bucket table -- sample count, mean forward return, hit rate (return > 0) per
  score band. A calibrated model's hit rate should rise with the band.
* rank correlation -- Spearman between score and forward return. Near zero
  means the opinion carries no information; strongly positive means it does.
* Brier score -- treating "forward return > 0" as the outcome and score/100 as
  the predicted probability. Lower is better; 0.25 is a coin flip.

If the buckets are flat and the correlation is noise, JUDGE is decoration and
the honest move is to say so and drop it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .labels import candidate_returns

# Forward horizon the calibration is judged on, in minutes.
HORIZON_MINUTES = 60
# Score bands, half-open [low, high).
BANDS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]
MIN_BAND_SAMPLE = 3


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation without scipy. None when undefined."""
    n = len(xs)
    if n < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            shared = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = shared
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den_x = sum((a - mx) ** 2 for a in rx) ** 0.5
    den_y = sum((b - my) ** 2 for b in ry) ** 0.5
    if den_x <= 0 or den_y <= 0:
        return None
    return round(num / (den_x * den_y), 4)


def judge_calibration(conn: sqlite3.Connection, horizon_minutes: int = HORIZON_MINUTES) -> dict:
    """Bucket every judgement that has a forward return at the chosen horizon."""
    rows = conn.execute(
        "SELECT j.id, j.candidate_id, j.symbol, j.score, j.ok, j.judged_at_ms"
        " FROM llm_judgments j WHERE j.ok = 1 AND j.score IS NOT NULL"
        " ORDER BY j.judged_at_ms"
    ).fetchall()
    samples: list[dict] = []
    for row in rows:
        candidate_id = row[1]
        if candidate_id is None:
            continue
        returns = candidate_returns(conn, int(candidate_id))
        key = f"{horizon_minutes}m"
        if key not in returns:
            continue  # no forward price yet: unknown, never assumed
        samples.append({
            "judgment_id": row[0],
            "symbol": row[2],
            "score": float(row[3]),
            "fwd_return": float(returns[key]),
        })
    return _summarise(samples, horizon_minutes)


def _summarise(samples: list[dict], horizon_minutes: int) -> dict:
    if not samples:
        return {"horizon_minutes": horizon_minutes, "samples": 0, "bands": [],
                "spearman": None, "brier": None, "verdict": "样本不足：还没有带后续价格的研判"}

    bands = []
    for low, high in BANDS:
        bucket = [s for s in samples if low <= s["score"] < high]
        if not bucket:
            continue
        rets = [s["fwd_return"] for s in bucket]
        bands.append({
            "band": f"{low}-{high if high <= 100 else 100}",
            "n": len(bucket),
            "mean_return_pct": round(sum(rets) / len(rets), 3),
            "hit_rate_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100.0, 1),
            "worst_pct": round(min(rets), 3),
            "symbols": [s["symbol"] for s in bucket][:6],
            "enough": len(bucket) >= MIN_BAND_SAMPLE,
        })

    xs = [s["score"] for s in samples]
    ys = [s["fwd_return"] for s in samples]
    spearman = _spearman(xs, ys)

    # Brier against "did it go up": predicted p = score/100, outcome in {0,1}.
    brier = round(sum((s["score"] / 100.0 - (1.0 if s["fwd_return"] > 0 else 0.0)) ** 2
                     for s in samples) / len(samples), 4)

    # Left-tail test. JUDGE scores STRUCTURAL HEALTH (authority, holders,
    # liquidity), not "this will pump", so the honest question is not whether
    # high scores earn more -- it is whether low scores mark the disasters.
    # A useful health opinion shows up here even when the mean is flat.
    low_side = [s["fwd_return"] for s in samples if s["score"] < 60]
    high_side = [s["fwd_return"] for s in samples if s["score"] >= 60]
    left_tail = None
    if len(low_side) >= MIN_BAND_SAMPLE and len(high_side) >= MIN_BAND_SAMPLE:
        low_worst, high_worst = min(low_side), min(high_side)
        low_crashes = sum(1 for r in low_side if r <= -30.0)
        high_crashes = sum(1 for r in high_side if r <= -30.0)
        left_tail = {
            "low_n": len(low_side), "high_n": len(high_side),
            "low_worst_pct": round(low_worst, 3), "high_worst_pct": round(high_worst, 3),
            "low_crashes": low_crashes, "high_crashes": high_crashes,
            "protects": low_worst > high_worst and low_crashes <= high_crashes,
        }

    meaningful = [b for b in bands if b["enough"]]
    if len(meaningful) >= 2:
        best = max(meaningful, key=lambda b: b["mean_return_pct"])
        worst = min(meaningful, key=lambda b: b["mean_return_pct"])
        if spearman is not None and spearman > 0.15 and best["mean_return_pct"] > worst["mean_return_pct"]:
            verdict = f"JUDGE 有区分度：{best['band']} 分桶均收益 {best['mean_return_pct']:+.2f}% 优于 {worst['band']} 的 {worst['mean_return_pct']:+.2f}%（Spearman {spearman}）"
        elif spearman is not None and spearman < -0.15:
            verdict = f"JUDGE 反向：低分桶反而更好（Spearman {spearman}），需要检查 prompt 或字段口径"
        else:
            verdict = f"暂无法区分：分桶差异在噪声范围内（Spearman {spearman}，样本 {len(samples)}）"
        if left_tail is not None:
            tail_note = (
                f"防左尾检验：低分({left_tail['low_n']}笔)最差 {left_tail['low_worst_pct']:+.1f}% / "
                f"≥30% 暴跌 {left_tail['low_crashes']} 次；高分({left_tail['high_n']}笔)最差 "
                f"{left_tail['high_worst_pct']:+.1f}% / 暴跌 {left_tail['high_crashes']} 次 → "
                + ("低分确实标记更多风险，健康度口径成立" if left_tail["protects"] else "低分未标记更多风险，健康度口径存疑")
            )
            verdict += "\n  " + tail_note
    else:
        verdict = f"样本不足：{len(samples)} 个带后续收益的研判，分桶未达 {MIN_BAND_SAMPLE} 个/桶"

    return {
        "horizon_minutes": horizon_minutes,
        "samples": len(samples),
        "bands": bands,
        "spearman": spearman,
        "brier": brier,
        "brier_note": "0.25 = 抛硬币，越低越好；以『未来收益>0』为 outcome",
        "left_tail": left_tail,
        "verdict": verdict,
    }


def format_calibration(report: dict) -> str:
    lines = [f"JUDGE 校准（{report['horizon_minutes']} 分钟前向收益，n={report['samples']}）"]
    if not report["bands"]:
        lines.append("  " + report["verdict"])
        return "\n".join(lines)
    lines.append("  分数段   样本   均收益      命中率   标的")
    for band in report["bands"]:
        flag = "" if band["enough"] else "  (样本不足)"
        lines.append(
            f"  {band['band']:<8} {band['n']:>4}   {band['mean_return_pct']:>+7.2f}%   "
            f"{band['hit_rate_pct']:>5.1f}%   {', '.join(band['symbols'][:4])}{flag}"
        )
    lines.append(f"  Spearman 秩相关: {report['spearman']}  Brier: {report['brier']}（{report['brier_note']}）")
    lines.append("  " + report["verdict"])
    return "\n".join(lines)
