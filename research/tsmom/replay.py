"""TSMOM 独立回放器：用来做引擎做不到的两类实验。

为什么需要它（不是重复造轮子）：

  strategy-falsifier 引擎有两个硬约束，恰好卡在 TSMOM 的核心假设上：
    1. 每笔等权，无法表达「仓位 = 目标波动 / 事前已实现波动」；
    2. 入场在信号 bar 的下一根开盘（正确），退出却在信号 bar 的当根收盘
       （与入场不对称，收盘价成交在实盘不可得）。

  所以本模块分两层：
    · replay_trades()  逐笔层，先用 engine_compat 模式**逐位复现**引擎的
      net_pct，证明成本/时点算法一致；再用 strict 模式把退出推迟到下一根
      开盘，量化「退出前视」到底值多少。
    · daily_replay()   逐日层，支持波动率目标仓位。这是 2×2 消融的地基。

所有函数只依赖 CSV 和显式参数，不调用引擎、不联网、不下单。

成本口径（与引擎 verify.py 的 _one_trade 对齐）：
  单边 fee_rate = 4bps，往返 8bps；
  资金费只在 8h 边界结算，多头付、空头收，按持仓占比分摊；
  强平：逐笔复利下空头单笔亏损可超过 100%，本模块按「保证金耗尽即强平」
  截断（见 liquidation 参数），引擎不做这件事。
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
FEE_RATE = 0.0004          # 单边 4bps，与 spec 一致
FUNDING_DIR = HERE / "funding"


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------

def read_candles(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows.append({k: (int(v) if k == "t" else float(v))
                         for k, v in r.items() if k in ("t", "o", "h", "l", "c", "v")})
    rows.sort(key=lambda x: x["t"])
    return rows


def read_funding(symbol: str) -> list[dict]:
    p = FUNDING_DIR / f"{symbol}.csv"
    if not p.is_file():
        return []
    with p.open(newline="", encoding="utf-8-sig") as fh:
        return [{"t": int(r["t"]), "rate": float(r["rate"])} for r in csv.DictReader(fh)]


def read_signal_csv(path: Path, side_field: str | None = None) -> dict[int, str]:
    """读 entries（t,side）或 exits（t,reason），返回 bar 时间戳 -> 标签。

    用时间戳而不是下标，是因为 walk-forward 切窗后下标会重排，时间戳不会。
    """
    out: dict[int, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out[int(r["t"])] = r.get(side_field or "reason", "")
    return out


# ---------------------------------------------------------------------------
# 逐笔层：先复现引擎，再改成交时点
# ---------------------------------------------------------------------------

def _funding_between(funding: list[dict], t0: int, t1: int, side: str) -> float:
    """复现引擎 _one_trade 的资金费算法：8h 边界按持仓覆盖比例分摊。"""
    paid = 0.0
    last = t0
    for f in funding:
        ft = f["t"]
        if last < ft <= t1:
            covered = min(ft, t1) - last
            span = max(t1 - last, 1)
            paid += f["rate"] * (covered / span) * (-1 if side == "long" else 1)
            last = ft
    return paid


def replay_trades(candles: list[dict], entries: dict[int, str], exits: dict[int, str],
                  *, mode: str = "engine_compat", fee_rate: float = FEE_RATE,
                  funding: list[dict] | None = None, liquidation: bool = False) -> list[dict]:
    """逐笔重放。mode:
      engine_compat — 入场 i+1 开盘、退出 i 收盘（= 引擎现状）
      strict        — 入场 i+1 开盘、退出 i+1 开盘（消除退出时点前视）

    exits 的时间戳是「信号确认时间」。strict 模式下真正的成交发生在它后面
    那根 K 线的开盘——即先拿到下一根开盘价，再平仓。
    """
    if mode not in ("engine_compat", "strict"):
        raise ValueError(mode)
    funding = funding or []
    idx = {c["t"]: i for i, c in enumerate(candles)}
    trades = []

    for sig_t, side in sorted(entries.items()):
        if side not in ("long", "short"):
            continue
        ei = idx.get(sig_t)
        if ei is None or ei + 1 >= len(candles):
            continue
        ji = ei + 1
        entry_price = candles[ji]["o"]
        if entry_price <= 0:
            continue

        # 找第一根「信号确认时间 >= 入场时间」的退出信号
        exit_i = None
        for t, reason in sorted(exits.items()):
            k = idx.get(t)
            if k is not None and k >= ji:
                exit_i, exit_reason = k, reason
                break
        if exit_i is None:
            continue

        if mode == "engine_compat":
            xi, exit_price = exit_i, candles[exit_i]["c"]
        else:
            xi = exit_i + 1
            if xi >= len(candles):
                continue
            exit_price = candles[xi]["o"]

        sign = 1.0 if side == "long" else -1.0
        gross = sign * (exit_price / entry_price - 1.0)
        fund = _funding_between(funding, candles[ji]["t"], candles[xi]["t"], side)
        fee = fee_rate * 2
        net = gross - fee + fund

        # 强平：空头亏光保证金（价格相对入场翻倍）时按 +100% 损失截断。
        # engine_compat 模式默认关闭，否则无法与引擎对齐。
        if liquidation and side == "short":
            adverse = exit_price / entry_price - 1.0
            if adverse >= 1.0:
                net = -1.0 - fee + fund

        trades.append({
            "side": side, "entry_i": ji, "exit_i": xi,
            "entry_t": candles[ji]["t"], "exit_t": candles[xi]["t"],
            "entry_price": entry_price, "exit_price": exit_price,
            # 引擎对 net_pct 做 round(...,6)。回放器必须做同样的舍入，
            # 否则逐位对照会在 1e-6 量级上假失败。
            "gross": gross, "fee": fee, "funding": fund, "net": round(net, 6),
            "reason": exit_reason, "bars": xi - ji,
        })
    return trades


# ---------------------------------------------------------------------------
# 逐日层：唯一能表达波动率目标仓位的口径
# ---------------------------------------------------------------------------

def realized_vol(closes: list[float], i: int, window: int = 20) -> float | None:
    """bar i 收盘时可知的事前波动率（只用 <= i 的收益）。年化。"""
    if i < window:
        return None
    rets = [closes[k] / closes[k - 1] - 1.0 for k in range(i - window + 1, i + 1)]
    sd = statistics.stdev(rets)
    return sd * (252 ** 0.5) if sd > 0 else None


def daily_replay(candles: list[dict], positions: list[int],
                 *, sizing: str = "fixed", target_vol: float = 0.20,
                 vol_window: int = 20, max_lev: float = 3.0,
                 fee_rate: float = FEE_RATE, funding: list[dict] | None = None,
                 min_vol: float = 0.01) -> dict:
    """逐日净值回放。信号在 bar i 收盘确认，仓位从 bar i+1 开盘生效。

    收益用 open-to-open：持有一段从 open_t 到 open_{t+1}，因此最后一个 bar
    没有下一天开盘价，必须丢弃——不能用收盘价补，那会引入与信号同刻的成交。

    sizing:
      fixed — 权重 = 1
      vol   — 权重 = target_vol / vol_{i-1}，截断到 [0, max_lev]
              这是 Moskowitz-Ooi-Pedersen 的「事前波动率调整仓位」。
    """
    if len(positions) != len(candles):
        raise ValueError("positions 与 candles 长度不一致")
    if sizing not in ("fixed", "vol"):
        raise ValueError(sizing)
    funding = funding or []
    rets: list[float] = []
    dates: list[int] = []
    weights: list[float] = []
    fee_costs: list[float] = []      # 换手手续费
    fund_costs: list[float] = []     # 资金费（正=付出）

    closes = [c["c"] for c in candles]
    prev_w = 0.0
    for t in range(len(candles) - 1):
        # bar t 收盘确认信号与波动，bar t+1 开盘建立仓位
        sig = positions[t]
        if sizing == "fixed":
            w = float(sig)
        else:
            v = realized_vol(closes, t, vol_window)
            if v is None or v < min_vol:
                w = 0.0
            else:
                w = sig * min(max_lev, target_vol / v)
        o0, o1 = candles[t + 1]["o"], candles[t + 2]["o"] if t + 2 < len(candles) else None
        if o1 is None or o0 <= 0:
            continue

        gross = w * (o1 / o0 - 1.0)
        fund = _funding_between(funding, candles[t + 1]["t"], candles[t + 2]["t"],
                                "long" if w > 0 else "short") if w != 0 else 0.0
        # 换手成本：仓位变化才付费，同向缩放也算换手
        fee = abs(w - prev_w) * fee_rate
        net = gross - fee + fund
        rets.append(net)
        dates.append(candles[t + 1]["t"])
        weights.append(w)
        fee_costs.append(fee)
        fund_costs.append(-fund)   # fund 是净收入，取负号得到「付出」
        prev_w = w

    eq, peak, max_dd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        max_dd = max(max_dd, 1 - eq / peak)
    n = len(rets)
    mu = statistics.fmean(rets) if n else 0.0
    sd = statistics.stdev(rets) if n > 1 else 0.0
    return {
        "n_days": n,
        "total_x": eq,
        "total_pct": (eq - 1) * 100,
        "ann_return_pct": ((eq ** (252 / n) - 1) * 100 if n > 0 else None),
        "ann_vol_pct": (sd * (252 ** 0.5) * 100 if sd else None),
        "sharpe": (mu / sd * (252 ** 0.5) if sd else None),
        "max_dd_pct": max_dd * 100,
        "avg_abs_weight": statistics.fmean([abs(w) for w in weights]) if n else 0.0,
        "turnover_fee_pct": sum(fee_costs) * 100,
        "funding_paid_pct": sum(fund_costs) * 100,
        "returns": rets,
        "weights": weights,
        "dates": dates,
    }


def paired_diff(a: dict, b: dict) -> dict:
    """两组日收益的配对差（a - b），按天对齐。"""
    n = min(len(a["returns"]), len(b["returns"]))
    if n == 0:
        return {"n": 0}
    d = [a["returns"][i] - b["returns"][i] for i in range(n)]
    mu = statistics.fmean(d)
    sd = statistics.stdev(d) if n > 1 else 0.0
    return {
        "n": n,
        "mean_daily_pct": mu * 100,
        "ann_pct": mu * 252 * 100,
        "t_stat": (mu / (sd / n ** 0.5) if sd else None),
        "series": d,
    }
