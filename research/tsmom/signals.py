"""TSMOM 信号生成器。

分工是刻意的：这里只把「过去 N 天涨还是跌」翻译成持仓方向，不计算任何
成本、成交、收益。所有损益都由 strategy-falsifier 引擎独立推演。信号生成
器和审计器分开，审计才有意义——自己写信号又自己算收益，等于自己给自己
判卷。

时点语义（与引擎契约逐条对齐，错一根 K 线就是前视偏差）：

  * bar i 收盘时可见的全部信息决定 position[i]；
  * 引擎把 entries[i] 当作「bar i 收盘时可知」，成交在 bar i+1 开盘；
  * exits[i] 在 bar i 的收盘价平掉当时的旧仓。

所以一次翻仓 = 旧仓在 i 收盘平 + 新仓在 i+1 开盘建。中间隔的那一夜跳空
是真实成本，不是建模漏洞，而且它系统性地不利于策略——方向是对的，宁可
低估毛收益，也不要用一个对自己有利的假设去凑正期望。

信号只依赖 closes[0..i]，结构上不可能前视：position[i] 的计算里没有任何
下标大于 i 的项。
"""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SIGNAL_DIR = Path(__file__).resolve().parent / "signals"


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def read_candles(path: Path) -> list[dict]:
    """读 t,o,h,l,c,v。只做解析，不做任何清洗——清洗会掩盖数据问题。"""
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows.append({
                "t": int(r["t"]),
                "o": float(r["o"]),
                "h": float(r["h"]),
                "l": float(r["l"]),
                "c": float(r["c"]),
                "v": float(r.get("v") or 0),
            })
    rows.sort(key=lambda x: x["t"])
    return rows


# --------------------------------------------------------------------------
# 三种持仓方向的定义。共同点：position[i] 只用 <= i 的数据。
# --------------------------------------------------------------------------

def positions_tsmom(closes: list[float], n: int) -> list[int]:
    """经典时间序列动量：过去 N 天累计收益的符号。

    这是 Moskowitz-Ooi-Pedersen 那个最朴素的定义，只在单个标的上看自己
    的过去，不做跨品种排序——截面动量（按 N 日收益在币之间排名做多前几名
    做空后几名）是另一个实验，仓库里 #2 已经做过并证伪了。
    """
    pos = [0] * len(closes)
    for i in range(n, len(closes)):
        pos[i] = _sign(closes[i] / closes[i - n] - 1.0)
    return pos


def positions_riskadj(closes: list[float], n: int, thr: float) -> list[int]:
    """风险调整动量：过去 N 天日均收益的 t 统计量超过阈值才持仓。

    与上面不是同一个东西。经典 TSMOM 只看「涨没涨」，这里问的是「涨得够
    不够确定」——同样的涨幅，波动大的标的 t 值更低，于是不进仓。阈值把弱
    信号过滤成空仓，这是唯一能在等权引擎里表达的「波动率调整」形式。

    必须说清楚：真正的波动率目标（仓位大小 = 目标波动/已实现波动）改的是
    每笔的权重，而引擎每笔等权，所以**测不了**。能测的是方向层面的过滤。
    """
    rets = [0.0] * len(closes)
    for i in range(1, len(closes)):
        rets[i] = closes[i] / closes[i - 1] - 1.0
    pos = [0] * len(closes)
    for i in range(n, len(closes)):
        w = rets[i - n + 1: i + 1]
        mu = statistics.fmean(w)
        sd = statistics.stdev(w) if len(w) > 1 else 0.0
        if sd <= 0:
            pos[i] = 0
            continue
        t = mu / sd * math.sqrt(len(w))
        pos[i] = 1 if t > thr else (-1 if t < -thr else 0)
    return pos


def positions_regime(closes: list[float], n: int, ma: int) -> list[int]:
    """动态动量：动量给方向，长均线给许可。

    多头要在长期均线上方才开，空头要在下方才开——这是「市场状态识别」最
    便宜也最不容易过拟合的版本，只有一个参数。复杂的状态分类器（隐马尔可
    夫、聚类、多因子打分）参数一多，在 3000 根日线上必然过拟合，那是给自
    己造证据，不是做研究。
    """
    pos = [0] * len(closes)
    for i in range(max(n, ma), len(closes)):
        mom = _sign(closes[i] / closes[i - n] - 1.0)
        if mom == 0:
            pos[i] = 0
            continue
        m = statistics.fmean(closes[i - ma + 1: i + 1])
        if mom > 0 and closes[i] > m:
            pos[i] = 1
        elif mom < 0 and closes[i] < m:
            pos[i] = -1
        else:
            pos[i] = 0
    return pos


# --------------------------------------------------------------------------
# 持仓序列 -> 引擎要的 entries / exits
# --------------------------------------------------------------------------

def to_entries_exits(candles: list[dict], pos: list[int]) -> tuple[list[dict], list[dict]]:
    """把持仓序列拆成引擎的入场表与退出表。

    每一次持仓变化都同时产生一条退出（平旧仓）和一条入场（建新仓，若新仓
    不为空）。两者标在同一根 K 线上，靠引擎自己的时序语义错开：退出吃 i 的
    收盘价，入场吃 i+1 的开盘价。不需要额外错位，错位反而会重复扣成本。

    末尾未平的仓补一条退出信号，让它按最后一根收盘价结账。不补的话引擎会
    把它算成 open_signal（未了结），最后一笔盈利或亏损就被丢掉了，等于给
    策略挑了个有利的结局。
    """
    entries: list[dict] = []
    exits: list[dict] = []
    prev = 0
    for i, p in enumerate(pos):
        if p == prev:
            continue
        exits.append({"t": candles[i]["t"], "reason": "flip"})
        if p != 0:
            entries.append({"t": candles[i]["t"], "side": "long" if p > 0 else "short"})
        prev = p
    if pos and pos[-1] != 0:
        exits.append({"t": candles[-1]["t"], "reason": "end"})
    return entries, exits


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 行尾固定 LF：这份 CSV 会被 sha256 记账，换行被改写会让指纹失真
    with path.open("w", newline="\n", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def build(symbol: str, variant: str, lookback: int, thr: float = 1.0, ma: int = 200,
          offset_start: str | None = None, offset_end: str | None = None) -> dict:
    """生成一个 (symbol, variant, lookback) 的信号对，返回元数据。

    offset_start / offset_end 用于把窗口截到所有币的共同区间。不做这一步的话
    跨币比较是假的：BTC 有 2017 年至今，SOL 只有 2020 年之后，两者的「年化
    收益」根本不是同一段历史。
    """
    candles = read_candles(DATA_DIR / f"{symbol}.csv")
    if offset_start:
        candles = [c for c in candles if _fmt(c["t"]) >= offset_start]
    if offset_end:
        candles = [c for c in candles if _fmt(c["t"]) <= offset_end]
    closes = [c["c"] for c in candles]

    if variant == "tsmom":
        pos = positions_tsmom(closes, lookback)
    elif variant == "tsmom_long":
        # 只做多：多头仓位来自动量，空头一律空仓。用于给随机对照组一个干净
        # 的镜像方向——引擎的对照组只镜像主方向，多空混合的策略拿不到一个
        # 真正公平的选择性检验。
        pos = [max(0, p) for p in positions_tsmom(closes, lookback)]
    elif variant == "tsmom_short":
        pos = [min(0, p) for p in positions_tsmom(closes, lookback)]
    elif variant == "riskadj":
        pos = positions_riskadj(closes, lookback, thr)
    elif variant == "regime":
        pos = positions_regime(closes, lookback, ma)
    else:
        raise ValueError(f"未知 variant: {variant}")

    entries, exits = to_entries_exits(candles, pos)
    tag = f"{symbol}_{variant}_{lookback}"
    if variant == "riskadj":
        tag += f"_thr{thr}"
    if variant == "regime":
        tag += f"_ma{ma}"

    e_path = SIGNAL_DIR / f"{tag}_entries.csv"
    x_path = SIGNAL_DIR / f"{tag}_exits.csv"
    write_csv(e_path, entries, ["t", "side"])
    write_csv(x_path, exits, ["t", "reason"])

    in_mkt = sum(1 for p in pos if p != 0)
    return {
        "tag": tag,
        "symbol": symbol,
        "variant": variant,
        "lookback": lookback,
        "n_bars": len(candles),
        "first_bar": _fmt(candles[0]["t"]) if candles else None,
        "last_bar": _fmt(candles[-1]["t"]) if candles else None,
        "n_entries": len(entries),
        "n_exits": len(exits),
        "n_flips": len(exits),
        "long_entries": sum(1 for e in entries if e["side"] == "long"),
        "short_entries": sum(1 for e in entries if e["side"] == "short"),
        "in_market_pct": (in_mkt / len(pos) * 100) if pos else 0.0,
        "entries_path": str(e_path.relative_to(Path(__file__).resolve().parent)),
        "exits_path": str(x_path.relative_to(Path(__file__).resolve().parent)),
    }


def _fmt(ms: int, with_time: bool = False) -> str:
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ms / 1000)
    return d.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="生成 TSMOM 信号 CSV")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--variant", default="tsmom",
                    choices=["tsmom", "tsmom_long", "tsmom_short", "riskadj", "regime"])
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--thr", type=float, default=1.0)
    ap.add_argument("--ma", type=int, default=200)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    a = ap.parse_args()

    print(json.dumps(build(a.symbol, a.variant, a.lookback, a.thr, a.ma, a.start, a.end),
                     ensure_ascii=False, indent=2))