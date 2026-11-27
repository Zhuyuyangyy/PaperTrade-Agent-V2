"""对照组公平性修正：把「无限期持有」的偏差修掉。

问题（已实测确认，不是猜测）：

  spec 里 exit.timeout_bars=100000 相当于「永不超时」，策略靠 exit_signals
  逐段平仓，平均持仓 14.7 根、中位 3 根。但引擎的随机对照组没有退出信号，
  它只吃同一个 timeout_bars——于是每个随机入场都被持有到数据结束（最长
  2277 根，约 6 年）。在涨了十几倍的币上，随机做空六年的单笔亏损能到
  -334%，而真实账户早就爆仓了。

  后果：对照组被结构性地做成必输的一方，
        SOLUSDT control mean = -9.93%/笔、worst = -334.7%/笔、
        median 却是 +1.48%、胜率 54.3%（中位数和胜率都为正，只有均值被
        极值拖死）。在这样的对照组面前，任何短持仓策略都会「通过选择性
        检验」——82.3% 的通过率是构造出来的，不是发现。

修正：对照组用一个与策略实际持仓期匹配的超时。取策略持仓段长度的中位数，
      让对照组在「同样典型持有期」下与策略比较。这才是「随机入场 vs 择时
      入场」的问题，而不是「短线 vs 无限期」。

仍然存在且无法在此消除的偏差（必须写进报告，不能藏）：
  * 引擎的对照组只镜像主方向。多空混合策略拿不到真正公平的对照，
    所以本修正只对 long 腿 / short 腿单独成立；
  * 随机入场的持仓期分布是「均匀 + 固定超时」，策略是「动量翻转」，
    形状不同。中位对齐只对齐了中心，没有对齐分布尾部。
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import signals as sg
from run import build_spec, digest, run_engine, OUT

START = "2020-09-01"
END = "2026-11-26"
FOLDS = 4


def hold_lengths(symbol: str, variant: str, lookback: int, thr: float = 1.0,
                 ma: int = 200) -> dict:
    """策略的持仓段长度分布。这是对照组超时取值的唯一依据。"""
    candles = sg.read_candles(HERE / "data" / f"{symbol}.csv")
    lo = dt.datetime.fromisoformat(START).timestamp() * 1000
    hi = dt.datetime.fromisoformat(END).timestamp() * 1000
    candles = [c for c in candles if lo <= c["t"] <= hi]
    closes = [c["c"] for c in candles]

    if variant in ("tsmom", "tsmom_long", "tsmom_short"):
        pos = sg.positions_tsmom(closes, lookback)
        pos = {"tsmom": pos,
               "tsmom_long": [max(0, p) for p in pos],
               "tsmom_short": [min(0, p) for p in pos]}[variant]
    elif variant == "riskadj":
        pos = sg.positions_riskadj(closes, lookback, thr)
    elif variant == "regime":
        pos = sg.positions_regime(closes, lookback, ma)
    else:
        raise ValueError(variant)

    runs, cur = [], 0
    for i, p in enumerate(pos):
        if i > 0 and p != pos[i - 1]:
            if cur:
                runs.append(cur)
            cur = 0
        if p != 0:
            cur += 1
    if cur:
        runs.append(cur)
    if not runs:
        return {"n_segments": 0, "median": None}
    return {
        "n_segments": len(runs),
        "median": int(statistics.median(runs)),
        "mean": round(statistics.fmean(runs), 1),
        "p90": sorted(runs)[int(len(runs) * 0.9)],
        "max": max(runs),
    }


def run_fair(symbol: str, variant: str, lookback: int, timeout: int, *,
             thr: float = 1.0, ma: int = 200) -> dict:
    """跑一个「对照组超时 = 策略中位持仓期」的版本。"""
    meta = sg.build(symbol, variant, lookback, thr=thr, ma=ma,
                    offset_start=START, offset_end=END)
    name = f"FAIR_{symbol}__{variant}__{lookback}__to{timeout}"
    build_spec(name, symbol,
               "../" + meta["entries_path"].replace("\\", "/"),
               "../" + meta["exits_path"].replace("\\", "/"),
               hypothesis=f"{symbol} {variant} 回看{lookback}，对照组超时对齐到中位持仓 {timeout} 根",
               timeout=timeout, folds=FOLDS, grid=False)
    d = run_engine(f"{name}.json", HERE / "data" / f"{symbol}.csv")
    return d


def main() -> None:
    plan = []
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "XRPUSDT"]:
        for variant in ("tsmom_long", "tsmom_short"):
            for lb in (10, 20, 30, 60):
                h = hold_lengths(sym, variant, lb)
                if not h["n_segments"]:
                    continue
                to = max(1, h["median"])
                plan.append((sym, variant, lb, to, h))

    print(f"计划 {len(plan)} 组\n")
    rows = []
    for sym, variant, lb, to, h in plan:
        d = run_fair(sym, variant, lb, to)
        g = digest(d, variant)
        g.update({"variant": variant, "lookback": lb, "fair_timeout": to,
                  "hold_median": h["median"], "hold_mean": h["mean"],
                  "hold_p90": h["p90"], "hold_max": h["max"],
                  "n_segments": h["n_segments"]})
        rows.append(g)
        print(f"{sym:<9}{variant:<12}{lb:>3} 超时={to:>3}  "
              f"策略 {g['net_sum_pct']:>9.1f}%  对照均 {g['control_diff_pct']:>8.2f}pp  "
              f"CI {g['control_ci']}  {g['selectivity']}", flush=True)

    out = OUT / "fair_control.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    sel = sum(1 for r in rows if r.get("control_ci") and not (r["control_ci"][0] <= 0 <= r["control_ci"][1]))
    print(f"\n修正后仍有选择性: {sel}/{len(rows)} = {sel/len(rows)*100:.1f}%")
    print(f"（修正前同口径为 82.3%，差额就是对照组无限期持有造成的偏差）")


if __name__ == "__main__":
    main()