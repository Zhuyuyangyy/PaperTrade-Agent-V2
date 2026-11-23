"""R0 随机基线：回答「随机入场在这套框架下到底亏多少」。

不引入任何新信号假设。三个对照：

  - **R0**：保留 E5 的信号时间，随机交易方向；
  - **R1**：从可交易时段随机生成入场，匹配交易笔数与多空比例；
  - **R2**：保留 E5 的信号时间，全部反向交易。

为什么必须先做这个：连续三个信号家族（均线 / 费率水平 / 费率变化率）
都没过置换检验，说明"找不到信号"可能不是信号的问题，而是这套
「1h 入场 + ATR 退出 + 18bps 往返」的框架本身就在漏钱。随机基线给出刻度：
知道随机能得多少分，才知道某个 p 值算不算好。

铁律：
  - 撮合、费用、滑点、资金费、ATR 止损、移动止损、单仓约束**全部复用**
    :mod:`research.experiment.funding_exp`，不复制粘贴，避免口径漂移；
  - 口径写入 :class:`R0Spec` 即冻结，不得因结果回头改；
  - 只读本地缓存，**禁止访问 fapi.binance.com**（418 封禁不可重试）。

多重检验：E5 的最优值是「96 组网格挑出来的」，所以随机对照必须**重跑整条
选择流程**——每次抽样都重新扫网格取最优，而不是拿已选出的参数去比。
只比固定参数会系统性低估 p 值。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass, field
from decimal import Decimal as D
from pathlib import Path

from research.market.contracts import Candle

from .funding import FundingSignal, funding_signals
from .funding_exp import (TF_MS, FundingParams, _atr14, _index_at, book_stats,
                          prepare_context, simulate_signal)

BPS = D("10000")

#: 成本敏感性情景（往返 bps）。18 = 当前实测主口径（(4+5)×2），其余为假设情景。
COST_SCENARIOS = (D("0"), D("8"), D("12"), D("18"), D("24"), D("30"))


# --------------------------------------------------------------------------
# 冻结口径
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class R0Spec:
    """一次冻结、全程复用。改任何字段都必须改 version 并重跑全部对照。"""

    version: str = "R0-v1"
    # 数据（全部来自本地缓存，2024-11-23 ~ 2026-11-23）
    interval: str = "1h"
    step_ms: int = 3_600_000
    days: int = 730
    # 信号
    lookback: int = 90
    min_history: int = 30
    e5_mode: str = "delta"
    e5_z: D = D("2.5")
    e5_key: str = "delta-z2.5-stop1.5-trail0.75-h24"
    # 切分（与 E5 一致：前 70% 研究集）
    research_frac: float = 0.7
    # 执行成本（与 E5 一致）
    notional: D = D("100")
    fee_bps: D = D("4")
    slippage_bps: D = D("5")
    round_trip_bps: D = D("18")
    # 随机
    seed: int = 20261123
    n_mc: int = 2000

    def to_dict(self) -> dict:
        out = {}
        for k, v in self.__dict__.items():
            out[k] = str(v) if isinstance(v, D) else v
        return out


def data_fingerprint(bars: list[Candle], funding: list[dict]) -> dict:
    """对数据内容取指纹（不是文件字节），保证结论可追溯到具体数据。"""
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b.open_ms}|{b.open}|{b.high}|{b.low}|{b.close}\n".encode())
    bars_sha = h.hexdigest()

    h2 = hashlib.sha256()
    for f in funding:
        h2.update(f"{int(f['funding_ms'])}|{f['rate']}\n".encode())
    fund_sha = h2.hexdigest()

    return {
        "bars": len(bars), "funding": len(funding),
        "first_open_ms": int(bars[0].open_ms), "last_close_ms": int(bars[-1].close_ms),
        "funding_first_ms": int(funding[0]["funding_ms"]),
        "funding_last_ms": int(funding[-1]["funding_ms"]),
        "bars_sha256": bars_sha, "funding_sha256": fund_sha,
    }


# --------------------------------------------------------------------------
# 预处理
# --------------------------------------------------------------------------
def atr_series(bars: list[Candle], period: int = 14) -> list[D]:
    """全序列 ATR，与 :func:`_atr14` 逐点等价，但用前缀和把 O(n·period) 降到 O(n)。

    R1 需要从全样本随机取入场点，逐点调用 _atr14 会重复算 14 次 TR；
    这里一次算完，且保留 Decimal 精度。
    """
    n = len(bars)
    out = [D("0")] * n
    if n <= period:
        return out
    tr = [D("0")] * n
    for k in range(1, n):
        c, pv = bars[k], bars[k - 1]
        tr[k] = max(c.high - c.low, abs(c.high - pv.close), abs(c.low - pv.close))
    pref = [D("0")] * (n + 1)
    for k in range(1, n):
        pref[k + 1] = pref[k] + tr[k]
    div = D(period)
    for i in range(period, n):
        out[i] = (pref[i + 1] - pref[i - period + 1]) / div
    return out


class Prepared:
    """一次预处理，多次复用：信号集、ATR、资金费数组。

    缓存是**性能优化不是口径改动**：:meth:`contexts` 与原
    ``run_funding_variant`` 的逐信号处理完全一致（同样调用 prepare_context）。
    """

    def __init__(self, bars: list[Candle], funding: list[dict], spec: R0Spec):
        self.bars = bars
        self.spec = spec
        self.f_times = [int(x["funding_ms"]) for x in funding]
        self.f_rates = [D(x["rate"]) for x in funding]
        self.cut = int(len(bars) * spec.research_frac)
        self.atr = atr_series(bars)
        self.tradable = [i for i in range(20, len(bars) - 1) if self.atr[i] > 0]
        self._sig: dict[tuple, list[FundingSignal]] = {}
        self._ctx: dict[tuple, list[dict]] = {}

    def signals(self, mode: str, z: D) -> list[FundingSignal]:
        key = (mode, str(z))
        if key not in self._sig:
            self._sig[key] = funding_signals(
                self._funding_raw(), self.bars, lookback=self.spec.lookback,
                z_threshold=z, min_history=self.spec.min_history, mode=mode)
        return self._sig[key]

    def _funding_raw(self) -> list[dict]:
        return [{"funding_ms": t, "rate": str(r)}
                for t, r in zip(self.f_times, self.f_rates)]

    def contexts(self, mode: str, z: D) -> list[dict]:
        """可交易信号上下文（已剔除历史不足 / ATR 无效），保持时间顺序。"""
        key = (mode, str(z))
        if key not in self._ctx:
            out = []
            for s in self.signals(mode, z):
                ctx = prepare_context(self.bars, s)
                if ctx is not None:
                    out.append(ctx)
            self._ctx[key] = out
        return self._ctx[key]

    def ctx_at(self, idx: int) -> dict:
        """为任意可交易 bar 构造上下文（R1 随机时机用）。

        ``z``/``rate`` 对随机时机没有意义，填 0 以保持字段结构一致。
        """
        return {"idx": int(idx), "atr": self.atr[idx], "default_side": "",
                "z": "0", "rate": "0",
                "entry_open_ms": int(self.bars[idx + 1].open_ms),
                "ref": self.bars[idx + 1].open}


# --------------------------------------------------------------------------
# 组合回测
# --------------------------------------------------------------------------
def run_book(prep: Prepared, p: FundingParams, contexts: list[dict],
             sides: list[str], *, single_position: bool = True,
             fee_bps: D | None = None,
             slippage_bps: D | None = None) -> dict:
    """按单仓资金账户跑一组合成交，返回逐笔 + 研究/样本外统计。

    ``attempted`` 与 ``filled`` 分开返回：单仓约束会挡掉部分信号，
    不同退出路径挡掉的数量不同——**不能假装各组实际成交完全相同**。
    """
    if len(contexts) != len(sides):
        raise ValueError(f"contexts/sides 长度不一致: {len(contexts)} vs {len(sides)}")
    trades: list[dict] = []
    busy_until = -1
    skipped = 0
    for ctx, side in zip(contexts, sides):
        idx = int(ctx["idx"])
        if single_position and idx + 1 <= busy_until:
            skipped += 1
            continue
        d = simulate_signal(prep.bars, prep.f_times, prep.f_rates, ctx, side, p,
                            fee_bps=fee_bps, slippage_bps=slippage_bps)
        trades.append(d)
        if single_position:
            busy_until = idx + 1 + int(int(d["holding_ms"]) // TF_MS)

    research = [x for x in trades if x["signal_bar"] < prep.cut]
    oos = [x for x in trades if x["signal_bar"] >= prep.cut]
    return {
        "attempted": len(contexts), "filled": len(trades),
        "skipped_overlap": skipped,
        "all": book_stats(trades),
        "research": book_stats(research),
        "oos": book_stats(oos),
        "trades": trades,
    }


def e5_params(spec: R0Spec, **over) -> FundingParams:
    """E5 真实最优配置（stop1.5 / trail0.75 / start1.0 / hold24 / delta / z2.5）。"""
    base = dict(z_threshold=spec.e5_z, lookback=spec.lookback, stop_atr=D("1.5"),
                trail_atr=D("0.75"), trail_start_atr=D("1.0"), hold_bars=24,
                flip=False, mode=spec.e5_mode,
                notional=spec.notional, fee_bps=spec.fee_bps,
                slippage_bps=spec.slippage_bps)
    base.update(over)
    return FundingParams(**base)


# --------------------------------------------------------------------------
# 统计工具
# --------------------------------------------------------------------------
def quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数。输入必须已排序。"""
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 区间——小 p 时比正态近似可靠（不会给出负下界）。"""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def _bootstrap_q_ci(vals: list[float], q: float, *, n_boot: int = 400,
                    seed: int = 0) -> tuple[float | None, float | None]:
    """分位数的 bootstrap 区间。样本 <20 时返回 (None, None)——
    不是 nan，因为 nan 会让 json.dumps 写出非法 JSON 且 nan != nan 会破坏可复现性断言。"""
    rng = random.Random(seed)
    n = len(vals)
    if n < 20:
        return (None, None)
    out = []
    for _ in range(n_boot):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        sample.sort()
        out.append(quantile(sample, q))
    out.sort()
    return (quantile(out, 0.025), quantile(out, 0.975))


def _ci_list(pair: tuple[float | None, float | None]) -> list | None:
    if pair[0] is None:
        return None
    return [round(pair[0], 4), round(pair[1], 4)]


def summarize(values: list[float], real: float) -> dict:
    """随机分布 vs 真实值：位置、尾部、真实百分位、经验 p、MC 误差。"""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return {"n": 0}
    mean = statistics.fmean(s)
    sd = statistics.pstdev(s) if n > 1 else 0.0
    better = sum(1 for v in s if v >= real)
    p = better / n
    lo, hi = wilson_ci(better, n)
    se = math.sqrt(p * (1 - p) / n) if n else float("nan")
    p5, p95 = quantile(s, 0.05), quantile(s, 0.95)
    b5 = _bootstrap_q_ci(s, 0.05)
    b95 = _bootstrap_q_ci(s, 0.95)
    return {
        "n": n,
        "mean": round(mean, 4), "sd": round(sd, 4),
        "median": round(statistics.median(s), 4),
        "p05": round(p5, 4), "p95": round(p95, 4),
        "p05_ci95": _ci_list(b5), "p95_ci95": _ci_list(b95),
        "min": round(s[0], 4), "max": round(s[-1], 4),
        "share_positive_pct": round(100 * sum(1 for v in s if v > 0) / n, 2),
        "real": round(real, 4),
        "real_percentile": round(100 * sum(1 for v in s if v < real) / n, 2),
        "n_better_or_equal": better,
        "p_value": round(p, 4),
        "p_wilson_ci95": [round(lo, 4), round(hi, 4)],
        "p_mc_se": round(se, 5),
        "p_mc_se_note": "se = sqrt(p(1-p)/n)；95% 区间见 p_wilson_ci95",
    }


# --------------------------------------------------------------------------
# R0：保留信号时间，随机方向
# --------------------------------------------------------------------------
def mc_r0(prep: Prepared, spec: R0Spec, *, n_mc: int | None = None,
          grid: list[FundingParams] | None = None,
          matched: bool = False, mode: str | None = None, z: D | None = None,
          seed: int | None = None, collect_per_signal: bool = False,
          progress=None) -> dict:
    """R0 Monte Carlo。

    ``grid`` 为 None 时只跑固定参数（E5 最优），得到纯方向随机的分布；
    给 grid 则每次抽样都**重跑完整参数选择**（研究集最优），
    这才是对「96 组挑出来」的正确零分布。

    ``matched=True`` 时方向按真实多空比例重排（控制方向分布，只随机谁多谁空），
    否则每个信号独立 50/50——两种都报，避免挑一个好看的。
    """
    n_mc = spec.n_mc if n_mc is None else n_mc
    mode = spec.e5_mode if mode is None else mode
    z = spec.e5_z if z is None else z
    seed = spec.seed if seed is None else seed
    ctxs = prep.contexts(mode, z)
    n_sig = len(ctxs)
    if n_sig == 0:
        return {"error": "无可用信号"}

    base = e5_params(spec)
    fixed = base if not grid else None

    # 真实多空比例：信号层（38 个）与成交层（34 笔）不同——单仓约束会挡单。
    # matched 口径在**信号层**重排方向，否则长度对不上、也违背"保留信号时间"的定义。
    real = run_book(prep, base, ctxs, [c["default_side"] for c in ctxs])
    n_long = sum(1 for x in real["trades"] if x["side"] == "LONG")
    n_short = real["filled"] - n_long
    n_long_sig = sum(1 for c in ctxs if c["default_side"] == "LONG")
    base_sides = (["LONG"] * n_long_sig + ["SHORT"] * (n_sig - n_long_sig)
                  if matched else None)

    recs: list[dict] = []
    per_sig: dict[int, list[float]] = {}
    for k in range(n_mc):
        rng = random.Random(seed + k)
        if base_sides is not None:
            sides = list(base_sides)
            rng.shuffle(sides)
        else:
            sides = [rng.choice(("LONG", "SHORT")) for _ in range(n_sig)]

        if fixed is not None:
            r = run_book(prep, fixed, ctxs, sides)
            if collect_per_signal:
                for t in r["trades"]:
                    per_sig.setdefault(int(t["signal_bar"]), []).append(
                        float(t["net_pnl"]))
            recs.append({"k": k, "best_key": fixed.key,
                         "research": float(r["research"].get("net", "0") or 0),
                         "oos": float(r["oos"].get("net", "0") or 0),
                         "all": float(r["all"].get("net", "0") or 0),
                         "n": r["filled"], "skipped": r["skipped_overlap"],
                         "win_pct": float(r["all"].get("win_pct", "0") or 0),
                         "max_dd_pct": float(r["all"].get("max_dd_pct", "0") or 0)})
        else:
            best = None
            for p in grid:
                r = run_book(prep, p, ctxs, sides)
                net = float(r["research"].get("net", "0") or 0)
                if best is None or net > best["research"]:
                    best = {"best_key": p.key, "research": net,
                            "oos": float(r["oos"].get("net", "0") or 0),
                            "all": float(r["all"].get("net", "0") or 0),
                            "n": r["filled"], "skipped": r["skipped_overlap"],
                            "win_pct": float(r["all"].get("win_pct", "0") or 0),
                            "max_dd_pct": float(r["all"].get("max_dd_pct", "0") or 0)}
            best["k"] = k
            recs.append(best)
        if progress is not None and (k + 1) % 50 == 0:
            progress(k + 1, n_mc)

    real_research = float(real["research"].get("net", "0") or 0)
    real_all = float(real["all"].get("net", "0") or 0)
    per_signal = []
    if collect_per_signal:
        real_by_idx = {int(t["signal_bar"]): t for t in real["trades"]}
        for idx in sorted(per_sig):
            vals = sorted(per_sig[idx])
            rt = real_by_idx.get(idx)
            rn = float(rt["net_pnl"]) if rt else None
            per_signal.append({
                "signal_bar": idx,
                "real_side": rt["side"] if rt else None,
                "real_net": round(rn, 4) if rn is not None else None,
                "n_draws": len(vals),
                "rand_median": round(statistics.median(vals), 4),
                "rand_mean": round(statistics.fmean(vals), 4),
                "rand_p05": round(quantile(vals, 0.05), 4),
                "rand_p95": round(quantile(vals, 0.95), 4),
                "real_percentile": round(100 * sum(1 for v in vals if v < rn) / len(vals), 2)
                if rn is not None else None,
            })
    return {
        "kind": "R0_matched" if matched else "R0_indep",
        "grid": "fixed_e5" if fixed is not None else f"reselect_{len(grid)}",
        "mode": mode, "z": str(z), "n_mc": n_mc, "seed": seed,
        "n_signals": n_sig,
        "real": {"research": real_research, "all": real_all,
                 "oos": float(real["oos"].get("net", "0") or 0),
                 "filled": real["filled"], "n_long": n_long, "n_short": n_short,
                 "signal_long": n_long_sig, "signal_short": n_sig - n_long_sig,
                 "win_pct": float(real["all"].get("win_pct", "0") or 0),
                 "max_dd_pct": float(real["all"].get("max_dd_pct", "0") or 0)},
        "research_net": summarize([r["research"] for r in recs], real_research),
        "all_net": summarize([r["all"] for r in recs], real_all),
        "win_pct": summarize([r["win_pct"] for r in recs],
                             float(real["all"].get("win_pct", "0") or 0)),
        "max_dd_pct": summarize([r["max_dd_pct"] for r in recs],
                                float(real["all"].get("max_dd_pct", "0") or 0)),
        "n_trades": summarize([float(r["n"]) for r in recs], float(real["filled"])),
        "best_key_freq": _freq(r["best_key"] for r in recs),
        "per_signal": per_signal,
        "records": recs,
    }


# --------------------------------------------------------------------------
# R0-full：完整流程对等对照（E5 的 96 组网格到底有多少是有效自由度）
# --------------------------------------------------------------------------
def mc_r0_signal_family(prep: Prepared, spec: R0Spec, *, n_mc: int | None = None,
                        mode_z_pairs: list[tuple[str, D]] | None = None,
                        exit_grid: list[FundingParams] | None = None,
                        seed: int | None = None,
                        collect_per_signal: bool = False, progress=None) -> dict:
    """R0 的完整流程版本：随机方向 + 多信号集 + 多退出参数，重新选最优。

    与 :func:`mc_r0` 的区别：这里信号集也参与选择，所以 mode/z 是**有效**自由度。
    方向按 signal idx 查表（同一个信号在所有网格点取同一个随机方向），
    否则网格之间的差异会混入"方向运气"，选出的最优就只是噪声。

    这回答了 E5 的核心质疑：它从 96 组里挑出 +7.8727，
    而"随机方向 + 同样的挑选流程"能挑出多少？
    """
    n_mc = spec.n_mc if n_mc is None else n_mc
    seed = spec.seed if seed is None else seed
    mode_z_pairs = mode_z_pairs or [("level", D("2.0")), ("level", D("2.5")),
                                   ("delta", D("2.0")), ("delta", D("2.5"))]
    exit_grid = exit_grid or [e5_params(spec, stop_atr=D(s), trail_atr=D(t),
                                        hold_bars=h)
                              for s in ("1.5", "2.0", "2.5")
                              for t in ("0.75", "1.0")
                              for h in (24, 72)]

    ctx_by_pair = {(m, str(z)): prep.contexts(m, z) for m, z in mode_z_pairs}
    all_idxs = sorted({int(c["idx"]) for ctxs in ctx_by_pair.values() for c in ctxs})
    if not all_idxs:
        return {"error": "无可用信号"}

    base = e5_params(spec)
    real_ctxs = prep.contexts(spec.e5_mode, spec.e5_z)
    real = run_book(prep, base, real_ctxs, [c["default_side"] for c in real_ctxs])

    recs: list[dict] = []
    per_sig: dict[int, list[float]] = {}
    for k in range(n_mc):
        rng = random.Random(seed + 7_000 + k)
        side_by_idx = {i: rng.choice(("LONG", "SHORT")) for i in all_idxs}
        best = None
        for (m, z), ctxs in ctx_by_pair.items():
            sides = [side_by_idx[int(c["idx"])] for c in ctxs]
            for p in exit_grid:
                pp = e5_params(spec, mode=m, z_threshold=z,
                               stop_atr=p.stop_atr, trail_atr=p.trail_atr,
                               hold_bars=p.hold_bars)
                r = run_book(prep, pp, ctxs, sides)
                net = float(r["research"].get("net", "0") or 0)
                if best is None or net > best["research"]:
                    best = {"best_key": pp.key, "research": net,
                            "oos": float(r["oos"].get("net", "0") or 0),
                            "all": float(r["all"].get("net", "0") or 0),
                            "n": r["filled"], "skipped": r["skipped_overlap"],
                            "win_pct": float(r["all"].get("win_pct", "0") or 0),
                            "max_dd_pct": float(r["all"].get("max_dd_pct", "0") or 0)}
                    if collect_per_signal:
                        best["_trades"] = [(int(t["signal_bar"]), float(t["net_pnl"]))
                                           for t in r["trades"]]
        if collect_per_signal:
            for idx, pnl in best.pop("_trades", []):
                per_sig.setdefault(idx, []).append(pnl)
        best["k"] = k
        recs.append(best)
        if progress is not None and (k + 1) % 50 == 0:
            progress(k + 1, n_mc)

    real_research = float(real["research"].get("net", "0") or 0)
    real_all = float(real["all"].get("net", "0") or 0)
    per_signal = []
    if collect_per_signal:
        real_by_idx = {int(t["signal_bar"]): t for t in real["trades"]}
        for idx in sorted(per_sig):
            vals = sorted(per_sig[idx])
            rt = real_by_idx.get(idx)
            rn = float(rt["net_pnl"]) if rt else None
            per_signal.append({
                "signal_bar": idx, "real_side": rt["side"] if rt else None,
                "real_net": round(rn, 4) if rn is not None else None,
                "n_draws": len(vals),
                "rand_median": round(statistics.median(vals), 4),
                "rand_p05": round(quantile(vals, 0.05), 4),
                "rand_p95": round(quantile(vals, 0.95), 4),
                "real_percentile": round(100 * sum(1 for v in vals if v < rn) / len(vals), 2)
                if rn is not None else None,
            })
    return {
        "kind": "R0_full_process", "n_mc": n_mc, "seed": seed,
        "grid_effective": len(ctx_by_pair) * len(exit_grid),
        "signal_sets": len(ctx_by_pair), "exit_configs": len(exit_grid),
        "real": {"research": real_research, "all": real_all,
                 "filled": real["filled"]},
        "research_net": summarize([r["research"] for r in recs], real_research),
        "all_net": summarize([r["all"] for r in recs], real_all),
        "win_pct": summarize([r["win_pct"] for r in recs],
                             float(real["all"].get("win_pct", "0") or 0)),
        "max_dd_pct": summarize([r["max_dd_pct"] for r in recs],
                                float(real["all"].get("max_dd_pct", "0") or 0)),
        "best_key_freq": _freq(r["best_key"] for r in recs),
        "per_signal": per_signal,
        "records": recs,
    }


# --------------------------------------------------------------------------
# R1：随机时机，匹配笔数与多空比
# --------------------------------------------------------------------------
def mc_r1(prep: Prepared, spec: R0Spec, *, n_mc: int | None = None,
          grid: list[FundingParams] | None = None,
          target_n: int | None = None, n_long: int | None = None,
          n_short: int | None = None, seed: int | None = None,
          max_retry: int = 60, progress=None) -> dict:
    """R1：从可交易时段无放回抽取入场点，匹配真实笔数与多空比。

    单仓约束保留（与 E5 同规则），因此抽样后可能被挡掉几笔；
    被挡就重抽，直到 ``filled == target_n``（最多 ``max_retry`` 次）。
    ``attempts`` 分布会一并报告——重抽次数多说明随机时机本身更容易撞仓。
    """
    n_mc = spec.n_mc if n_mc is None else n_mc
    seed = spec.seed if seed is None else seed
    base = e5_params(spec)

    # 以 E5 真实账本为匹配基准
    ctxs = prep.contexts(spec.e5_mode, spec.e5_z)
    real = run_book(prep, base, ctxs, [c["default_side"] for c in ctxs])
    if target_n is None:
        target_n = real["filled"]
    if n_long is None:
        n_long = sum(1 for x in real["trades"] if x["side"] == "LONG")
    if n_short is None:
        n_short = target_n - n_long

    pool = prep.tradable
    if target_n > len(pool):
        return {"error": f"可交易池不足: {len(pool)} < {target_n}"}

    recs: list[dict] = []
    for k in range(n_mc):
        rng = random.Random(seed + 10_000 + k)
        best = None
        attempts = 0
        for attempt in range(1, max_retry + 1):
            attempts = attempt
            idxs = sorted(rng.sample(pool, target_n))
            sides = ["LONG"] * n_long + ["SHORT"] * n_short
            rng.shuffle(sides)
            # 先做一次单仓可行性检查（用固定参数），确定这次抽样能成交几笔
            check = run_book(prep, base, [prep.ctx_at(i) for i in idxs], sides)
            if check["filled"] == target_n:
                break
        ctx_list = [prep.ctx_at(i) for i in idxs]
        if grid is None:
            r = check
            best = {"best_key": base.key,
                    "research": float(r["research"].get("net", "0") or 0),
                    "oos": float(r["oos"].get("net", "0") or 0),
                    "all": float(r["all"].get("net", "0") or 0),
                    "n": r["filled"], "skipped": r["skipped_overlap"],
                    "win_pct": float(r["all"].get("win_pct", "0") or 0),
                    "max_dd_pct": float(r["all"].get("max_dd_pct", "0") or 0)}
        else:
            for p in grid:
                r = run_book(prep, p, ctx_list, sides)
                net = float(r["research"].get("net", "0") or 0)
                if best is None or net > best["research"]:
                    best = {"best_key": p.key, "research": net,
                            "oos": float(r["oos"].get("net", "0") or 0),
                            "all": float(r["all"].get("net", "0") or 0),
                            "n": r["filled"], "skipped": r["skipped_overlap"],
                            "win_pct": float(r["all"].get("win_pct", "0") or 0),
                            "max_dd_pct": float(r["all"].get("max_dd_pct", "0") or 0)}
        best["k"] = k
        best["attempts"] = attempts
        recs.append(best)
        if progress is not None and (k + 1) % 50 == 0:
            progress(k + 1, n_mc)

    real_research = float(real["research"].get("net", "0") or 0)
    real_all = float(real["all"].get("net", "0") or 0)
    return {
        "kind": "R1_random_time",
        "grid": "fixed_e5" if grid is None else f"reselect_{len(grid)}",
        "n_mc": n_mc, "seed": seed,
        "target_n": target_n, "n_long": n_long, "n_short": n_short,
        "tradable_pool": len(pool),
        "real": {"research": real_research, "all": real_all,
                 "filled": real["filled"],
                 "win_pct": float(real["all"].get("win_pct", "0") or 0),
                 "max_dd_pct": float(real["all"].get("max_dd_pct", "0") or 0)},
        "research_net": summarize([r["research"] for r in recs], real_research),
        "all_net": summarize([r["all"] for r in recs], real_all),
        "win_pct": summarize([r["win_pct"] for r in recs],
                             float(real["all"].get("win_pct", "0") or 0)),
        "max_dd_pct": summarize([r["max_dd_pct"] for r in recs],
                                float(real["all"].get("max_dd_pct", "0") or 0)),
        "attempts": summarize([float(r["attempts"]) for r in recs], 1.0),
        "best_key_freq": _freq(r["best_key"] for r in recs),
        "records": recs,
    }


# --------------------------------------------------------------------------
# R2：全部反向（确定性，无需 MC）
# --------------------------------------------------------------------------
def reverse_test(prep: Prepared, spec: R0Spec,
                 grid: list[FundingParams] | None = None) -> dict:
    """R2：保留信号时间，把所有方向翻过来。

    这是确定性的——只有一个结果，不是分布。它能立刻回答
    「策略是不是反向更好」这个最常见也最容易被自我安慰的问题。
    """
    ctxs = prep.contexts(spec.e5_mode, spec.e5_z)
    flip = {"LONG": "SHORT", "SHORT": "LONG"}
    sides = [flip[c["default_side"]] for c in ctxs]
    base = e5_params(spec)
    r = run_book(prep, base, ctxs, sides)
    out = {"kind": "R2_reverse", "params": base.key,
           "research_net": r["research"].get("net", "0"),
           "oos_net": r["oos"].get("net", "0"),
           "all": r["all"], "research": r["research"], "oos": r["oos"],
           "filled": r["filled"], "trades": r["trades"]}
    if grid:
        best = None
        for p in grid:
            rr = run_book(prep, p, ctxs, sides)
            net = float(rr["research"].get("net", "0") or 0)
            if best is None or net > best["research_net"]:
                best = {"key": p.key, "research_net": net,
                        "oos_net": float(rr["oos"].get("net", "0") or 0),
                        "filled": rr["filled"]}
        out["reselect_best"] = best
    return out


# --------------------------------------------------------------------------
# 成本敏感性
# --------------------------------------------------------------------------
def scale_costs(spec: R0Spec, round_trip_bps: D) -> tuple[D, D]:
    """把目标往返成本等比拆成手续费与滑点。

    保持 E5 的 4:5 结构（手续费 8bps/往返、滑点 10bps/往返），
    只按比例缩放：18bps 时恰好退化为冻结口径，0bps 时全为零。
    这是**假设情景**，不是实测——只有 18bps 是实测值。
    """
    scale = D(round_trip_bps) / spec.round_trip_bps
    return spec.fee_bps * scale, spec.slippage_bps * scale


def cost_sensitivity(prep: Prepared, spec: R0Spec,
                     scenarios: tuple[D, ...] = COST_SCENARIOS) -> dict:
    """同一套冻结策略在 0/8/12/18/24/30 bps 往返成本下的表现。"""
    ctxs = prep.contexts(spec.e5_mode, spec.e5_z)
    sides = [c["default_side"] for c in ctxs]
    base = e5_params(spec)
    rows = []
    for rt in scenarios:
        fee, slip = scale_costs(spec, rt)
        p = e5_params(spec, fee_bps=fee, slippage_bps=slip)
        r = run_book(prep, p, ctxs, sides, fee_bps=fee, slippage_bps=slip)
        gross = sum((D(x["gross"]) for x in r["trades"]), D("0"))
        fees = sum((D(x["entry_fee"]) + D(x["exit_fee"]) for x in r["trades"]), D("0"))
        fund = sum((D(x["funding"]) for x in r["trades"]), D("0"))
        n = max(1, r["filled"])
        atr_bps = [float(x["atr_bps"]) for x in r["trades"]]
        rows.append({
            "round_trip_bps": str(rt),
            "fee_bps_per_side": str(fee.quantize(D("0.0001"))),
            "slippage_bps_per_side": str(slip.quantize(D("0.0001"))),
            "filled": r["filled"],
            "net": r["all"].get("net", "0"),
            "research_net": r["research"].get("net", "0"),
            "oos_net": r["oos"].get("net", "0"),
            "win_pct": r["all"].get("win_pct", "0"),
            "gross": str(gross.quantize(D("0.0001"))),
            "fees": str(fees.quantize(D("0.0001"))),
            "funding": str(fund.quantize(D("0.0001"))),
            "avg_gross_per_trade": str((gross / n).quantize(D("0.0001"))),
            "avg_cost_per_trade": str(((fees + fund) / n).quantize(D("0.0001"))),
            "breakeven_gross_needed": str(((fees + fund) / n).quantize(D("0.0001"))),
            "atr_bps_mean": round(statistics.fmean(atr_bps), 1) if atr_bps else 0.0,
            "cost_over_atr_pct": round(float(rt) / statistics.fmean(atr_bps) * 100, 1)
            if atr_bps and statistics.fmean(atr_bps) > 0 else None,
            "is_assumption": str(rt) != str(spec.round_trip_bps),
        })
    return {"scenarios": rows,
            "primary_round_trip_bps": str(spec.round_trip_bps),
            "note": "18bps 为实测主口径（4+5 单边 ×2）；其余为等比缩放假设情景。"}


# --------------------------------------------------------------------------
# 重叠度 / 风险调整收益
# --------------------------------------------------------------------------
def overlap_stats(trades: list[dict]) -> dict:
    """持仓重叠：同账户下若不加单仓约束，会有多少笔在时间上打架。"""
    spans = []
    for t in trades:
        entry = int(t.get("entry_ts_ms") or 0)
        exit_ = int(t.get("exit_ts_ms") or 0)
        if exit_ <= entry:
            exit_ = entry + int(t.get("holding_ms") or 0)
        spans.append((entry, exit_))
    spans.sort()
    n = len(spans)
    if n == 0:
        return {"n": 0}
    pairs = 0
    overlapping = 0
    max_concurrent = 0
    for i in range(n):
        conc = 1
        for j in range(n):
            if i == j:
                continue
            if spans[j][0] < spans[i][1] and spans[i][0] < spans[j][1]:
                if j > i:
                    pairs += 1
                    overlapping += 1
                conc += 1
        max_concurrent = max(max_concurrent, conc)
    total_pairs = n * (n - 1) // 2
    busy_ms = sum(b - a for a, b in spans)
    horizon = (spans[-1][1] - spans[0][0]) if n > 1 else 0
    return {
        "n": n,
        "overlap_pairs": overlapping,
        "total_pairs": total_pairs,
        "overlap_pct": round(100 * overlapping / total_pairs, 2) if total_pairs else 0.0,
        "max_concurrent": max_concurrent,
        "exposure_pct": round(100 * busy_ms / horizon, 2) if horizon > 0 else None,
        "note": "重叠对 = 两笔持仓时间区间相交；max_concurrent>1 说明单仓约束确实在挡单。",
    }


def risk_adjusted(trades: list[dict]) -> dict:
    """风险调整收益：Sharpe（按笔）、Sortino、Calmar、每笔收益/ATR。"""
    pnls = [float(t["net_pnl"]) for t in trades]
    n = len(pnls)
    if n < 2:
        return {"n": n}
    mean = statistics.fmean(pnls)
    sd = statistics.pstdev(pnls)
    downside = [x for x in pnls if x < 0]
    dsd = math.sqrt(sum(x * x for x in downside) / len(downside)) if downside else 0.0
    equity, peak, dd = 1000.0, 1000.0, 0.0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak if peak > 0 else 0.0)
    total = sum(pnls)
    return {
        "n": n, "total": round(total, 4), "mean": round(mean, 4), "sd": round(sd, 4),
        "sharpe_per_trade": round(mean / sd, 4) if sd > 0 else None,
        "sharpe_annualized": round(mean / sd * math.sqrt(n), 4) if sd > 0 else None,
        "sortino_per_trade": round(mean / dsd, 4) if dsd > 0 else None,
        "max_dd_pct": round(dd * 100, 4),
        "calmar": round(total / (dd * 1000) , 4) if dd > 0 else None,
        "note": "Sharpe 按笔口径，annualized 用 sqrt(笔数) 粗放年化，仅供参考。",
    }


# --------------------------------------------------------------------------
# 多重检验
# --------------------------------------------------------------------------
def multiple_testing(p_values: dict[str, float],
                     n_grid_searched: dict[str, int] | None = None) -> dict:
    """对已搜过的信号家族做校正。

    我们实际搜过三个家族（E2 均线结构、E4 费率水平、E5 费率变化率），
    每个家族内部又扫了参数网格。单个未校正 p 值不能算通过。

    这里给两种口径：
      - Bonferroni（最保守）：p × 家族数
      - BH-FDR：控制错误发现率
    网格内的多重比较已经由「每次置换重跑整条选择流程」隐含校正，
    但家族之间的搜索没有被校正——所以这里只按家族数校正。
    """
    fams = list(p_values.items())
    m = len(fams)
    adj = {}
    for name, p in fams:
        adj[name] = {
            "p_raw": round(p, 4),
            "bonferroni": round(min(1.0, p * m), 4),
            "grid_size": (n_grid_searched or {}).get(name),
        }
    # BH-FDR
    ordered = sorted(fams, key=lambda kv: kv[1])
    prev = 1.0
    for rank in range(m, 0, -1):
        name, p = ordered[rank - 1]
        val = min(prev, p * m / rank)
        prev = val
        adj[name]["bh_fdr"] = round(min(1.0, val), 4)
    return {
        "families": m,
        "detail": adj,
        "threshold": 0.05,
        "verdict_note": ("Bonferroni 后全部不显著" if all(
            v["bonferroni"] > 0.05 for v in adj.values()) else "存在校正后仍显著者"),
        "caveat": "这是事后校正，不是预注册；校正后不显著只能说明证据不足，"
                  "校正后显著也不等于可上线。",
    }


def _freq(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------
# 各周期成本压力
# --------------------------------------------------------------------------
def aggregate_bars(bars: list[Candle], factor: int) -> list[Candle]:
    """把 factor 根小周期合成一根大周期（OHLC 聚合，成交量求和）。

    只用于 ATR 量级比较，不参与撮合——撮合始终在原始周期上跑。
    起点必须对齐到目标周期边界，否则 Candle 的边界校验会拒绝；
    开头不足一个完整周期的部分直接丢弃。
    """
    if factor <= 1:
        return list(bars)
    src_ms = int(bars[0].timeframe_ms)
    target_ms = src_ms * factor
    start = 0
    while start < len(bars) and int(bars[start].open_ms) % target_ms != 0:
        start += 1
    out: list[Candle] = []
    for i in range(start, len(bars) - factor + 1, factor):
        chunk = bars[i:i + factor]
        out.append(Candle(
            int(chunk[0].open_ms),
            chunk[0].open,
            max(c.high for c in chunk),
            min(c.low for c in chunk),
            chunk[-1].close,
            sum((c.volume for c in chunk), D("0")),
            target_ms,
        ))
    return out


def atr_bps_stats(bars: list[Candle], *, period: int = 14,
                  sample: int = 5000) -> dict:
    """ATR 相对价格的 bps 分布：回答「一次往返成本吃掉多少波动空间」。

    对超长序列均匀降采样，避免 17k 点全算（Decimal 下没必要）。
    """
    if len(bars) <= period + 1:
        return {"n": 0}
    series = atr_series(bars, period)
    idxs = [i for i in range(period, len(bars)) if series[i] > 0 and bars[i].close > 0]
    if len(idxs) > sample:
        step = len(idxs) / sample
        idxs = [idxs[int(k * step)] for k in range(sample)]
    vals = sorted(float(series[i] / bars[i].close * BPS) for i in idxs)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals), "period": period,
        "median_bps": round(quantile(vals, 0.5), 1),
        "p25_bps": round(quantile(vals, 0.25), 1),
        "p75_bps": round(quantile(vals, 0.75), 1),
        "mean_bps": round(statistics.fmean(vals), 1),
    }


def cost_pressure(atr_bps: float, round_trip_bps: float) -> dict:
    """成本占 ATR 的比例，以及需要多少 ATR 的毛收益才够本。"""
    if atr_bps <= 0:
        return {"round_trip_bps": round_trip_bps, "atr_bps": atr_bps,
                "cost_over_atr_pct": None, "atr_needed_to_break_even": None}
    return {
        "round_trip_bps": round_trip_bps,
        "atr_bps": round(atr_bps, 1),
        "cost_over_atr_pct": round(round_trip_bps / atr_bps * 100, 1),
        "atr_needed_to_break_even": round(round_trip_bps / atr_bps, 3),
        "note": "比例 >100% 表示一次完整往返的成本大于该周期一根 K 线的平均波幅，"
                "意味着一根 K 线级别的优势在数学上不可能覆盖成本。",
    }


__all__ = [
    "R0Spec", "Prepared", "data_fingerprint", "atr_series", "run_book",
    "e5_params", "mc_r0", "mc_r0_signal_family", "mc_r1", "reverse_test",
    "cost_sensitivity",
    "scale_costs", "overlap_stats", "risk_adjusted", "multiple_testing",
    "summarize", "wilson_ci", "quantile", "COST_SCENARIOS", "BPS",
    "aggregate_bars", "atr_bps_stats", "cost_pressure",
]
