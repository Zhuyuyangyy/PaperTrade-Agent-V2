"""批量回测 runner：30 张卡 × 多币 × 多周期。

设计要点：

**1. 参数网格先固定，跑完不许扩。**
每张卡的参数组合在 `GRID` 里写死。跑完看到某个参数赚钱就把网格扩一圈，
等于在做多重检验筛选——48 个配置里随机也会有几个 p<0.05。本 runner 会
输出**配置总数**，供后续 Bonferroni 校正。

**2. DATA_BLOCKED 是产出的一部分，不是失败。**
S25（OI）、S26（清算）、S30（新闻）没有数据源，必须显式记录为
`DATA_BLOCKED`，不能跳过、也不能用价格序列凑。

**3. 全部结果都落盘，包括亏损的。**
只报告赚钱的策略是自欺欺人。`results.json` 保存每一个配置的完整统计。

用法：
  python runner.py 4h            # 跑 4h
  python runner.py 1h --cards S20 S01
  python runner.py 4h --symbols BTCUSDT
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from itertools import product
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import engine as E                     # noqa: E402
import signals_cards as C              # noqa: E402
import signals_more as M               # noqa: E402
from signals_cards import Cache        # noqa: E402

DATA = HERE / "data"
OUT = HERE / "_out"

BAR_HOURS = {"5m": 5 / 60, "15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}

# 数据可用性（按已采集的字段判断，不按愿望）
AVAILABLE_DATA = {"ohlcv", "taker_buy_volume"}

# VWAP 窗口按周期自适应：约等于一天的 bar 数。
# 4h 上一天只有 6 根，用 24 根是「六天」——这是能跑通的最小合理值。
VWAP_BARS = {"15m": 96, "1h": 24, "4h": 42}


# ---------------------------------------------------------------------------
# 卡 → (工厂函数, 参数网格, 默认 max_hold_bars)
# ---------------------------------------------------------------------------

def grid(fn, keys, values):
    """把 (键, 值列表) 展开成参数组合列表。"""
    combos = []
    for vals in product(*values):
        combos.append(dict(zip(keys, vals)))
    return combos


CARDS = {
    # ---------------- SMC / ICT ----------------
    "S01": (lambda c, p: C.s01_bos_continuation(c, **p),
            grid(None, ["confirm_bars", "retest_max_bars"],
                 [[2, 3], [20]]), 60),
    "S02": (lambda c, p: C.s02_choch_reversal(c, **p),
            grid(None, ["retest_max_bars"], [[20]]), 60),
    "S03": (lambda c, p: C.s03_fvg_retest(c, **p),
            grid(None, ["entry_at", "retest_max_bars"],
                 [["edge", "mid"], [40]]), 60),
    "S04": (lambda c, p: M.s04_ifvg(c, **p),
            grid(None, ["retest_max_bars"], [[40]]), 60),
    "S05": (lambda c, p: C.s05_ob_retest(c, **p),
            grid(None, ["retest_max_bars"], [[40]]), 60),
    "S06": (lambda c, p: M.s06_breaker(c, **p),
            grid(None, ["retest_max_bars"], [[40]]), 60),
    "S07": (lambda c, p: C.s07_sweep_mss(c, **p),
            grid(None, ["require_mss", "mss_window"],
                 [[True, False], [5]]), 60),
    "S08": (lambda c, p: C.s08_ote(c, **p),
            grid(None, ["fib_lo", "fib_hi"], [[0.62], [0.79]]), 60),
    "S09": (lambda c, p: M.s09_silver_bullet(c, **p),
            grid(None, ["touch_atr"], [[0.1]]), 40),

    # ---------------- Wyckoff ----------------
    "S10": (lambda c, p: M.s10_spring(c, **p),
            grid(None, ["require_second_test"], [[True, False]]), 80),
    "S11": (lambda c, p: M.s11_utad(c, **p),
            grid(None, ["require_second_test"], [[True, False]]), 80),
    "S12": (lambda c, p: M.s12_sos_lps(c, **p),
            grid(None, ["vol_z"], [[1.0, 1.5]]), 60),
    "S13": (lambda c, p: M.s13_sow_lpsy(c, **p),
            grid(None, ["vol_z"], [[1.0, 1.5]]), 60),

    # ---------------- PriceAction ----------------
    "S14": (lambda c, p: M.s14_breakout_retest(c, **p),
            grid(None, ["channel", "retest_bars"], [[20], [10]]), 60),
    "S15": (lambda c, p: M.s15_false_breakout(c, **p),
            grid(None, ["lookback"], [[20]]), 60),
    "S16": (lambda c, p: M.s16_sr_rejection(c, **p),
            grid(None, ["touch_atr"], [[0.2]]), 60),
    "S17": (lambda c, p: M.s17_pin_bar(c, **p),
            grid(None, ["wick_body_ratio"], [[2.0, 3.0]]), 60),
    "S18": (lambda c, p: M.s18_engulfing(c, **p),
            grid(None, ["min_body_ratio"], [[1.0, 1.5]]), 60),
    "S19": (lambda c, p: M.s19_inside_bar(c, **p),
            grid(None, ["max_wait"], [[8]]), 60),

    # ---------------- Trend ----------------
    "S20": (lambda c, p: C.s20_donchian(c, **p),
            grid(None, ["channel", "atr_mult"], [[20, 55], [2.0]]), 200),
    "S21": (lambda c, p: M.s21_squeeze(c, **p),
            grid(None, ["squeeze_pct"], [[0.2, 0.3]]), 100),
    "S22": (lambda c, p: M.s22_trend_pullback(c, **p),
            grid(None, ["ma_fast", "ma_slow"], [[20], [50]]), 60),
    "S23": (lambda c, p: M.s23_momentum(c, **p),
            grid(None, ["body_atr"], [[1.0, 1.5]]), 5),

    # ---------------- OrderFlow ----------------
    "S24": (lambda c, p: M.s24_cvd_divergence(c, **p),
            grid(None, ["window"], [[30]]), 60),
    "S25": (None, [], 60),      # 需 OI → DATA_BLOCKED
    "S26": (None, [], 60),      # 需清算 → DATA_BLOCKED
    "S27": (lambda c, p: M.s27_vsa(c, **p),
            grid(None, ["vol_zscore"], [[1.5, 2.0]]), 60),

    # ---------------- Other ----------------
    "S28": (lambda c, p: M.s28_vwap(c, **p),
            None, 40),          # 参数依赖周期，在 run_card 里注入
    "S29": (lambda c, p: M.s29_turtle_soup(c, **p),
            grid(None, ["lookback"], [[20]]), 60),
    "S30": (None, [], 60),      # 需新闻 → DATA_BLOCKED
}

BLOCKED_REASON = {
    "S25": "缺历史 open_interest（币安 REST 只留近期，无免费完整归档）",
    "S26": "缺历史强平数据（需第三方归档）",
    "S30": "缺带可靠时间戳的历史新闻事件",
}

# 卡与周期的兼容性。S09 Silver Bullet 定义在**小时窗口**（10/11/14–15/18–19
# UTC），而 4h K 线的 UTC 小时只能是 0/4/8/12/16/20，两者交集为空——
# 这不是「没信号」，是**这张卡在这个周期上无定义**。必须区分开来，
# 否则会把「周期选错」误读成「策略无效」。
TF_INCOMPATIBLE = {
    ("S09", "4h"): "Silver Bullet 是小时窗口策略，4h K 线不落在 10/11/14/15/18/19 UTC",
    ("S28", "1d"): "日内 VWAP 在日线上无定义",
}


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load(path: Path) -> list[dict]:
    """读 CSV。taker_buy_v / trades 存在就带上，不存在就不带（S24 会据此 blocked）。"""
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        has_taker = "taker_buy_v" in (rd.fieldnames or [])
        for r in rd:
            d = {"t": int(r["t"]), "o": float(r["o"]), "h": float(r["h"]),
                 "l": float(r["l"]), "c": float(r["c"]), "v": float(r["v"])}
            if has_taker and r.get("taker_buy_v"):
                d["taker_buy_v"] = float(r["taker_buy_v"])
            rows.append(d)
    return rows


def data_files(symbols: list[str] | None, timeframe: str) -> list[Path]:
    out = []
    for p in sorted(DATA.glob(f"*_{timeframe}.csv")):
        sym = p.stem.rsplit("_", 1)[0]
        if symbols and sym not in symbols:
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def resolve_grid(card: str, tf: str) -> list[dict]:
    """取该卡在该周期下的参数组合。

    S28 的 `vwap_bars` 依赖周期，不能在 CARDS 里写死（4h 用 24 根等于六天）。
    这里按 `VWAP_BARS` 注入，其余卡直通。
    """
    _, combos, _ = CARDS[card]
    if card == "S28":
        n = VWAP_BARS.get(tf)
        if n is None:
            return []
        return [{"dev_atr": d, "vwap_bars": n} for d in (1.5, 2.5)]
    return combos


def run_one(card: str, candles: list[dict], symbol: str, tf: str,
            params: dict, max_hold: int,
            cache: "Cache | None" = None) -> dict:
    """跑一个配置。

    `cache` 可传入复用的实例：`Cache` 只装**与参数无关**的结构索引
    （pivot / FVG / OB / sweep / range），构造参数 `left=5, right=5,
    min_gap_atr=0.5` 全固定。15m 上单次构建要 16 秒，132 个配置各建一次
    就是 107 分钟——其中 106 分钟在重复算同一件事。

    **但复用 Cache 有前提，不能靠「看起来没问题」**：
      1. 信号函数绝不可写 cache（`signals_*.py` 里没有任何 `cache.x =`
         赋值，这一点由 `tests/test_cards.py` 间接守住）。
      2. 每个配置仍必须重建**信号函数**——它有 `live`/`seen`/`armed`
         状态，跨轮复用会让第二轮信号流完全变样。
    """
    factory, _, _ = CARDS[card]
    if factory is None:
        return {"card": card, "symbol": symbol, "timeframe": tf,
                "params": params, "status": "DATA_BLOCKED",
                "reason": BLOCKED_REASON.get(card, ""), "error": ""}
    if (card, tf) in TF_INCOMPATIBLE:
        return {"card": card, "symbol": symbol, "timeframe": tf,
                "params": params, "status": "NOT_APPLICABLE",
                "reason": TF_INCOMPATIBLE[(card, tf)], "error": ""}

    t0 = time.time()
    # 没传就自建（单卡调试时方便），传了就复用
    cache = cache if cache is not None else Cache(candles, left=5, right=5)
    # **每次都必须新建信号函数**，这一步不能省
    make = factory(cache, params)
    if isinstance(make, tuple):      # s24 返回 (fn, reason)
        fn, err = make
        if fn is None:
            return {"card": card, "symbol": symbol, "timeframe": tf,
                    "params": params, "status": "DATA_BLOCKED",
                    "reason": err, "error": ""}
    else:
        fn = make

    try:
        res = E.run(candles, card=card, symbol=symbol, timeframe=tf,
                    params=params, signal_fn=fn, bar_hours=BAR_HOURS[tf],
                    max_hold_bars=max_hold)
    except AssertionError as e:
        return {"card": card, "symbol": symbol, "timeframe": tf,
                "params": params, "status": "EXEC_ERROR", "reason": str(e),
                "error": str(e)}

    d = res.summary()
    d["status"] = "OK" if res.n_trades > 0 else "NO_TRADES"
    d["elapsed_s"] = round(time.time() - t0, 1)
    d["n_bars"] = len(candles)
    return d


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("timeframe", nargs="?", default="4h")
    ap.add_argument("--cards", nargs="*", default=None)
    ap.add_argument("--symbols", nargs="*", default=None)
    args = ap.parse_args(argv)

    tf = args.timeframe
    files = data_files(args.symbols, tf)
    if not files:
        print(f"没有 {tf} 数据")
        return 1

    cards = args.cards or list(CARDS)
    OUT.mkdir(exist_ok=True)

    print(f"周期 {tf}，{len(files)} 个币，{len(cards)} 张卡")
    print(f"{'card':<5}{'symbol':<9}{'params':<26}{'n':>5}{'win%':>6}"
          f"{'netExp':>8}{'ann%':>8}{'shrp':>7}{'mdd%':>7}{'PF':>6}{'status':>10}")
    print("-" * 100)

    results = []
    t_start = time.time()
    for p in files:
        sym = p.stem.rsplit("_", 1)[0]
        candles = load(p)
        # **一个币只建一次 Cache**。它装的是与参数无关的结构索引，
        # 132 个配置各建一次等于把同一件事算 132 遍。
        t_cache = time.time()
        cache = Cache(candles, left=5, right=5)
        print(f"  [{sym}] {len(candles)} 根，Cache 构建 {time.time()-t_cache:.1f}s")
        for card in cards:
            factory, combos, mh = CARDS[card]
            if factory is None:
                r = run_one(card, candles, sym, tf, {}, mh)
                results.append(r)
                print(f"{card:<5}{sym:<9}{'-':<26}{0:>5}{0:>6.1f}{0:>8.2f}"
                      f"{0:>8.2f}{0:>7.2f}{0:>7.1f}{0:>6.2f}{r['status']:>10}")
                continue
            for prm in resolve_grid(card, tf):
                r = run_one(card, candles, sym, tf, prm, mh, cache=cache)
                results.append(r)
                ps = ",".join(f"{k}={v}" for k, v in prm.items())[:25]
                if r["status"] in ("OK", "NO_TRADES"):
                    print(f"{card:<5}{sym:<9}{ps:<26}{r['n_trades']:>5}"
                          f"{r['win_rate']:>6.1f}{r['net_expectancy']:>8.2f}"
                          f"{r['ann_return_pct']:>8.2f}{r['sharpe']:>7.2f}"
                          f"{r['max_dd_pct']:>7.1f}{r['profit_factor']:>6.2f}"
                          f"{r['status']:>10}")
                else:
                    print(f"{card:<5}{sym:<9}{ps:<26}{'-':>5}{'-':>6}{'-':>8}"
                          f"{'-':>8}{'-':>7}{'-':>7}{'-':>6}{r['status']:>10}"
                          f"  {r.get('reason','')[:30]}")

    elapsed = time.time() - t_start
    n_cfg = len(results)
    n_ok = sum(1 for r in results if r["status"] == "OK")
    n_blocked = sum(1 for r in results if r["status"] == "DATA_BLOCKED")
    n_na = sum(1 for r in results if r["status"] == "NOT_APPLICABLE")
    n_no = sum(1 for r in results if r["status"] == "NO_TRADES")
    n_pos = sum(1 for r in results
                if r["status"] == "OK" and r.get("net_expectancy", 0) > 0)

    print("-" * 100)
    print(f"共 {n_cfg} 个配置，耗时 {elapsed:.0f}s")
    print(f"  有成交 {n_ok} / 无成交 {n_no} / 数据缺失 {n_blocked} / 周期不适用 {n_na}")
    print(f"  正期望 {n_pos} 个（{n_pos/max(n_ok,1)*100:.1f}% of 有成交）")
    print(f"  **多重检验提醒**：{n_cfg} 个配置里纯随机期望约 "
          f"{0.05*n_cfg:.1f} 个 p<0.05，Bonferroni 阈值应取 0.05/{n_cfg}="
          f"{0.05/max(n_cfg,1):.2e}")

    out = OUT / f"cards_{tf}.json"
    out.write_text(json.dumps({"timeframe": tf, "n_configs": n_cfg,
                               "elapsed_s": round(elapsed, 1),
                               "results": results}, ensure_ascii=False,
                              indent=1) + "\n", encoding="utf-8")
    print(f"  写入 {out}  ({out.stat().st_size/1024:.0f}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())