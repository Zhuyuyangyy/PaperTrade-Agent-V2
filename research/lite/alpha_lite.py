#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alpha_lite.py — 零依赖 / 零 API Key 的 15m 策略验证管线（单文件）。

设计目标：先用最小成本回答"这个策略到底行不行"，再决定要不要工程化。

四条硬约束都做进了代码结构，不靠使用者自觉：

1. 数据来源只用 Binance 公开行情端点（/api/v3/klines），不签名、不带 key、
   不读 .env、不 import 项目内任何模块。整个文件只依赖 Python 标准库。
2. 只接受**已收盘**的 K 线：
     - 拉取时：close_time < 服务器时间 serverTime
     - 回测时：close_time < 本次运行开始时刻（再挡一次，防人手改数据文件）
   任何一根不满足直接报错退出，不静默丢弃。
3. 信号在 bar[i] 收盘后产生，在 bar[i+1] 的 open 成交。
   策略函数拿到的是 `Bars` 只读视图，访问未来下标会抛 LookaheadError——
   这是结构性防护，不是口头约定。
4. 每根 K 线落盘时存 open_time + 原始 12 字段 + sha256(原始字段 canonical json)。
   任意一根都能单独复算校验；整份数据另有 fingerprint。

不做的事（同样重要）：
- 不做参数网格搜索。策略参数写死在 STRATEGIES 里，改参数=新实验，要重跑重报。
- 不做样本内挑最优再报样本外。IS/OOS 一次切分，60/40，切点写进结果。
- 不把手续费当零。默认 taker 10bps 单边 + 滑点 2bps 单边，往返 24bps。

用法：
    python research/lite/alpha_lite.py selftest
    python research/lite/alpha_lite.py pull --days 180
    python research/lite/alpha_lite.py run --strategies ma,donchian --mc 2000
    python research/lite/alpha_lite.py verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path

# ---------------------------------------------------------------- 常量

INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
SYMBOL = "BTCUSDT"

_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def interval_ms(interval: str) -> int:
    """把 "15m"/"1h"/"4h"/"1d" 解析成毫秒。不认识的格式直接报错，不猜。"""
    try:
        return int(interval[:-1]) * _UNIT_MS[interval[-1]]
    except (ValueError, KeyError):
        raise SystemExit(f"无法解析 interval={interval!r}，支持如 15m/1h/4h/1d")

HOSTS = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
REPORT_DIR = HERE / "reports"

USER_AGENT = "alpha-lite/1.0 (research; public market data only)"

# Binance /api/v3/klines 返回的 12 个字段，顺序固定
RAW_FIELDS = (
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
)

Bar = namedtuple("Bar", "t o h l c v ct h16")


class LookaheadError(RuntimeError):
    """策略试图读取尚未收盘的 K 线。"""


# ---------------------------------------------------------------- 基础工具

def sha16(row) -> str:
    """对一根 K 线的原始 12 字段做 canonical JSON 哈希，取前 16 位。"""
    payload = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def utc(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def fingerprint(records) -> str:
    """整份数据的指纹：对 "open_time:hash" 逐行排序后拼接再哈希。"""
    lines = sorted(f"{r['t']}:{r['h']}" for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def find_gaps(open_times, step_ms: int = INTERVAL_MS):
    """找出非同周期相邻的间隔。返回 [(前一根 open_time, 后一根 open_time), ...]"""
    return [(a, b) for a, b in zip(open_times, open_times[1:]) if b - a != step_ms]


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 取数

def http_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_host(preferred: str | None = None) -> str:
    """选一个能连上的公开端点。都不通就报错，不假装成功。"""
    order = ([preferred] if preferred else []) + [h for h in HOSTS if h != preferred]
    errors = []
    for host in order:
        try:
            http_json(host + "/api/v3/time", timeout=8)
            return host
        except Exception as exc:  # noqa: BLE001 - 探测用，记录后换下一个
            errors.append(f"{host}: {type(exc).__name__} {exc}")
    raise SystemExit("所有 Binance 公开端点都不可达:\n  " + "\n  ".join(errors))


def fetch_klines(host: str, symbol: str, interval: str,
                 end_time: int | None = None, limit: int = 1000):
    """返回 (rows, server_time_ms)。rows 里可能含未收盘的最后一根，由调用方过滤。"""
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    if end_time is not None:
        params["endTime"] = str(end_time)
    url = f"{host}/api/v3/klines?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            rows = http_json(url)
            break
        except urllib.error.HTTPError as exc:
            # 418/429 是限频信号：立刻停，不退避重试（退避会把封禁时间拉长）
            if exc.code in (418, 429):
                retry_after = exc.headers.get("Retry-After", "?")
                raise SystemExit(
                    f"触发限频 HTTP {exc.code}，Retry-After={retry_after}s。立即停止，不重试。"
                ) from exc
            if attempt == 2:
                raise
            time.sleep(1.0 + attempt)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(1.0 + attempt)
    st = http_json(host + "/api/v3/time")["serverTime"]
    return rows, st


def split_closed(rows, server_time: int):
    """把一页拆成 (已收盘, 未收盘)。close_time < serverTime 才算收盘。"""
    closed, unclosed = [], []
    for r in rows:
        (closed if int(r[6]) < server_time else unclosed).append(r)
    return closed, unclosed


# ---------------------------------------------------------------- 落盘

def store_path(symbol: str, interval: str, tag: str = "") -> Path:
    prefix = f"{tag}_" if tag else ""
    return DATA_DIR / f"{prefix}{symbol.lower()}_{interval}.jsonl"


def manifest_path(symbol: str, interval: str, tag: str = "") -> Path:
    prefix = f"{tag}_" if tag else ""
    return DATA_DIR / f"{prefix}{symbol.lower()}_{interval}.manifest.json"


def load_store(path: Path) -> dict:
    """读 jsonl 成 {open_time: record}。逐行校验哈希，坏行直接报错。"""
    store = {}
    if not path.exists():
        return store
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if sha16(rec["r"]) != rec["h"]:
                raise SystemExit(f"{path.name}:{lineno} 哈希不匹配，数据文件被改过。先跑 verify。")
            if int(rec["r"][0]) != rec["t"]:
                raise SystemExit(f"{path.name}:{lineno} t 与原始字段不一致。")
            store[rec["t"]] = rec
    return store


def save_store(path: Path, store: dict) -> None:
    recs = [store[t] for t in sorted(store)]
    atomic_write(path, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs))


def write_manifest(symbol: str, interval: str, store: dict, host: str,
                   dropped_unclosed: int, tag: str = "") -> dict:
    recs = [store[t] for t in sorted(store)]
    gaps = [{"from": a, "to": b}
            for a, b in find_gaps([r["t"] for r in recs], interval_ms(interval))]
    man = {
        "symbol": symbol,
        "interval": interval,
        "tag": tag or None,
        "source": f"{host}/api/v3/klines",
        "api_key_used": False,
        "bars": len(recs),
        "first_open_time": recs[0]["t"] if recs else None,
        "last_open_time": recs[-1]["t"] if recs else None,
        "first_close_time": int(recs[0]["r"][6]) if recs else None,
        "last_close_time": int(recs[-1]["r"][6]) if recs else None,
        "first_utc": utc(recs[0]["t"]) if recs else None,
        "last_utc": utc(recs[-1]["t"]) if recs else None,
        "gaps": gaps,
        "gap_count": len(gaps),
        "dropped_unclosed_this_run": dropped_unclosed,
        "fingerprint": fingerprint(recs),
        "hash_algo": "sha256(canonical_json(raw_12_fields))[:16] per bar",
        "closed_rule": "close_time < serverTime at fetch; re-checked against run start at backtest",
        "fields": list(RAW_FIELDS),
        "written_at_utc": utc(int(time.time() * 1000)),
    }
    atomic_write(manifest_path(symbol, interval, tag),
                 json.dumps(man, indent=2, ensure_ascii=False) + "\n")
    return man


# ---------------------------------------------------------------- pull

def pull(args) -> int:
    host = pick_host(args.host)
    path = store_path(args.symbol, args.interval, args.tag)
    store = load_store(path)
    before = len(store)
    target = args.days * (86_400_000 // interval_ms(args.interval))
    dropped = 0
    requests = 0

    print(f"host={host} symbol={args.symbol} interval={args.interval}")
    print(f"已有 {before} 根，目标 ≥{target} 根（{args.days} 天）")

    # 第一段：补尾部（从最新往前，直到碰到已有数据）
    anchor_max = max(store) if store else None
    end = None
    while True:
        rows, st = fetch_klines(host, args.symbol, args.interval,
                                end_time=end, limit=args.page_limit)
        requests += 1
        closed, unclosed = split_closed(rows, st)
        dropped += len(unclosed)
        if not closed:
            break
        for r in closed:
            store[int(r[0])] = {"t": int(r[0]), "r": r, "h": sha16(r)}
        save_store(path, store)
        print(f"  tail: +{len(closed)} 根，累计 {len(store)}，最新 {utc(int(closed[-1][0]))}")
        if len(rows) < args.page_limit:
            break
        oldest = min(int(r[0]) for r in rows)
        if anchor_max is None:
            break  # 空库：最新一页够用，历史窗口交给 head 段按 target 补
        if oldest <= anchor_max:
            break
        end = oldest - 1
        time.sleep(args.sleep)

    # 第二段：补历史（从最早往前，直到达到目标根数或到达上市首根）
    while len(store) < target:
        end = min(store) - 1
        rows, st = fetch_klines(host, args.symbol, args.interval,
                                end_time=end, limit=args.page_limit)
        requests += 1
        closed, unclosed = split_closed(rows, st)
        dropped += len(unclosed)
        if not closed:
            print("  已到该交易对最早一根，停止。")
            break
        gained_before = len(store)
        for r in closed:
            store[int(r[0])] = {"t": int(r[0]), "r": r, "h": sha16(r)}
        if len(store) == gained_before:
            print(f"  警告：本页无新增（end={end}），停止以免死循环。", file=sys.stderr)
            break
        save_store(path, store)
        print(f"  head: +{len(closed)} 根，累计 {len(store)}，最早 {utc(min(store))}")
        if len(rows) < args.page_limit:
            print("  已到该交易对最早一根，停止。")
            break
        time.sleep(args.sleep)

    man = write_manifest(args.symbol, args.interval, store, host, dropped, args.tag)
    print(f"\n完成：{man['bars']} 根  {man['first_utc']} → {man['last_utc']}")
    print(f"缺口 {man['gap_count']} 个；本次丢弃未收盘 {dropped} 根；请求 {requests} 次")
    print(f"fingerprint {man['fingerprint']}")
    if man["gap_count"]:
        print("警告：存在缺口，回测会直接拒绝运行。", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------- 载入 + 校验

def load_bars(symbol: str, interval: str, tag: str = ""):
    path = store_path(symbol, interval, tag)
    mpath = manifest_path(symbol, interval, tag)
    if not path.exists():
        raise SystemExit(f"没有数据文件 {path}，先跑 pull。")
    if not mpath.exists():
        raise SystemExit(f"没有 manifest {mpath}，先跑 pull。")
    store = load_store(path)
    man = json.loads(mpath.read_text(encoding="utf-8"))
    recs = [store[t] for t in sorted(store)]
    now_ms = int(time.time() * 1000)

    # 回测时刻再挡一次未收盘：数据文件可能是旧的、也可能被人手工塞了新行
    unclosed = [r for r in recs if int(r["r"][6]) >= now_ms]
    if unclosed:
        raise SystemExit(
            f"数据里有 {len(unclosed)} 根 close_time >= 当前时刻，拒绝运行。"
            f"最早一根 {utc(int(unclosed[0]['r'][6]))}"
        )

    step = interval_ms(interval)
    gaps = find_gaps([r["t"] for r in recs], step)
    if gaps:
        raise SystemExit(f"数据有缺口 {len(gaps)} 处，拒绝运行（不静默跳过）：{gaps[:3]}")

    for r in recs:
        raw = r["r"]
        if int(raw[6]) - int(raw[0]) != step - 1:
            raise SystemExit(f"close_time 与 open_time 不匹配: {r['t']}")
        if not (float(raw[2]) >= float(raw[3])):
            raise SystemExit(f"high < low: {r['t']}")

    bars = [
        Bar(int(r["r"][0]), float(r["r"][1]), float(r["r"][2]), float(r["r"][3]),
            float(r["r"][4]), float(r["r"][5]), int(r["r"][6]), r["h"])
        for r in recs
    ]
    return bars, man


# ---------------------------------------------------------------- 只读视图

class Bars:
    """策略只能看到 [0, i] 的 K 线；越界访问未来直接抛错。"""

    __slots__ = ("_b", "_n")

    def __init__(self, bars, i):
        self._b = bars
        self._n = i + 1

    def __len__(self):
        return self._n

    def __getitem__(self, k):
        if isinstance(k, slice):
            raise TypeError("切片会绕过前视防护；请用负下标，如 v[-1]")
        j = k if k >= 0 else self._n + k
        if j >= self._n:
            raise LookaheadError(f"试图读取 bar[{k}]，当前只允许到 bar[{self._n - 1}]")
        if j < 0:
            raise IndexError(k)
        return self._b[j]


# ---------------------------------------------------------------- 策略（参数写死）

def strat_ma(v: Bars) -> int:
    """双均线：快 20 / 慢 60（15m→快 5h、慢 15h）。上穿做多，下穿做空，否则空仓。"""
    fast, slow = 20, 60
    if len(v) < slow + 2:
        return 0
    f_now = sum(v[-k].c for k in range(1, fast + 1)) / fast
    f_prev = sum(v[-k].c for k in range(2, fast + 2)) / fast
    s_now = sum(v[-k].c for k in range(1, slow + 1)) / slow
    s_prev = sum(v[-k].c for k in range(2, slow + 2)) / slow
    if f_prev <= s_prev and f_now > s_now:
        return 1
    if f_prev >= s_prev and f_now < s_now:
        return -1
    return 0


def strat_donchian(v: Bars) -> int:
    """32 根（8h）通道突破：收盘破前高做多、破前低做空。"""
    n = 32
    if len(v) < n + 1:
        return 0
    window = [v[-k] for k in range(2, n + 2)]  # 不含当根
    hi = max(b.h for b in window)
    lo = min(b.l for b in window)
    c = v[-1].c
    if c > hi:
        return 1
    if c < lo:
        return -1
    return 0


def strat_mom(v: Bars) -> int:
    """24 根（6h）动量：过去 6h 涨做多、跌做空。"""
    n = 24
    if len(v) < n + 1:
        return 0
    past = v[-n - 1].c
    now = v[-1].c
    if now > past:
        return 1
    if now < past:
        return -1
    return 0


# 参数全部写死。要改就是新实验，必须重跑并重报，不许"扫一圈挑最好"。
STRATEGIES = {
    "ma": (strat_ma, dict(hold=16, warmup=80)),
    "donchian": (strat_donchian, dict(hold=32, warmup=40)),
    "mom": (strat_mom, dict(hold=24, warmup=30)),
}


# ---------------------------------------------------------------- 回测

def fill_price(px: float, direction: int, slip_bps: float, entering: bool) -> float:
    """滑点永远对自己不利：买入抬价、卖出压价。"""
    adj = direction * slip_bps / 10_000.0
    if entering:
        return px * (1 + adj)
    return px * (1 - adj)


def backtest(bars, strat, *, hold: int, warmup: int):
    """只产信号骨架：{dir, 入场下标 _e, 出场下标 _x}。

    这里刻意不碰成本——成本只影响净额，不该影响信号。定价交给 reprice()。
    信号在 bar[i] 收盘产生，bar[i+1] open 成交。
    """
    trades = []
    i = max(warmup, 1)
    n = len(bars)
    last_entry = n - 2  # 最后一根不能开仓：它没有 i+1 可以成交
    while i <= last_entry:
        d = strat(Bars(bars, i))
        if d not in (-1, 0, 1):
            raise ValueError(f"策略返回非法方向 {d!r}，只允许 -1/0/1")
        if d == 0:
            i += 1
            continue
        e = i + 1
        x = min(e + hold, n - 1)
        trades.append({
            "dir": d,
            "signal_time": bars[i].t,
            "entry_time": bars[e].t,
            "exit_time": bars[x].t,
            "bars_held": x - e,
            "forced_exit": (e + hold) > (n - 1),
            "_e": e,
            "_x": x,
        })
        i = x  # 非重叠：平仓那根收盘后才允许再开
    return trades


def reprice(trades, bars, fee_bps: float, slip_bps: float):
    """给定信号骨架和成本参数，算出每笔价格与净收益。

    gross_bps 是**纯价格**收益，不含手续费也不含滑点；滑点单独作用在成交价上。
    这样 "毛 / 成本" 才是可比的：成本 = 2*(fee+slip)。
    """
    out = []
    for t in trades:
        d, e, x = t["dir"], t["_e"], t["_x"]
        raw_entry = bars[e].o
        raw_exit = bars[x].o if x < len(bars) - 1 else bars[x].c
        gross = d * (raw_exit / raw_entry - 1.0)  # 纯价格，不含任何成本
        entry = fill_price(raw_entry, d, slip_bps, entering=True)
        exit_px = fill_price(raw_exit, d, slip_bps, entering=False)
        net = d * (exit_px / entry - 1.0) - 2 * fee_bps / 10_000.0
        nt = dict(t)
        nt.update(
            entry=round(entry, 2),
            exit=round(exit_px, 2),
            gross_bps=round(gross * 10_000, 2),
            net_bps=round(net * 10_000, 2),
            net_pct=round(net * 100, 4),
        )
        out.append(nt)
    return out


def stats(trades, fee_bps, slip_bps):
    if not trades:
        return {"trades": 0, "net_pct": 0.0, "win_rate": None, "pf": None,
                "max_dd_pct": 0.0, "avg_bps": None, "t_stat": None,
                "long": 0, "short": 0, "forced_exit": 0, "fee_bps_roundtrip": 2 * fee_bps}
    nets = [t["net_bps"] / 10_000 for t in trades]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in nets:
        equity *= (1 + r)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    wins = [r for r in nets if r > 0]
    losses = [r for r in nets if r < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    t_stat = None
    if len(nets) >= 3:
        sd = statistics.stdev(nets)
        if sd > 0:
            t_stat = statistics.mean(nets) / (sd / (len(nets) ** 0.5))
    return {
        "trades": len(trades),
        "net_pct": round((equity - 1) * 100, 4),
        "win_rate": round(len(wins) / len(nets) * 100, 2),
        "pf": round(pf, 4) if pf is not None else None,
        "max_dd_pct": round(max_dd * 100, 4),
        "avg_bps": round(statistics.mean(nets) * 10_000, 2),
        "avg_gross_bps": round(statistics.mean([t["gross_bps"] for t in trades]), 2),
        "t_stat": round(t_stat, 3) if t_stat is not None else None,
        "long": sum(1 for t in trades if t["dir"] > 0),
        "short": sum(1 for t in trades if t["dir"] < 0),
        "forced_exit": sum(1 for t in trades if t["forced_exit"]),
        "cost_bps_roundtrip": 2 * (fee_bps + slip_bps),
        "fee_bps_roundtrip": 2 * fee_bps,
        "slip_bps_side": slip_bps,
    }


def random_control(bars, trades, *, iter_n: int, fee_bps: float, slip_bps: float,
                   seed: int = 20261125):
    """固定入场/出场时刻，随机方向。看真实结果在随机分布里的分位。"""
    if not trades or iter_n <= 0:
        return None
    import random
    rng = random.Random(seed)
    cost = 2 * fee_bps / 10_000.0
    real = sum(t["net_bps"] for t in trades) / 10_000
    sims = []
    for _ in range(iter_n):
        tot = 0.0
        for t in trades:
            d = 1 if rng.random() < 0.5 else -1
            entry = fill_price(bars[t["_e"]].o, d, slip_bps, entering=True)
            px = bars[t["_x"]].o if t["_x"] < len(bars) - 1 else bars[t["_x"]].c
            exit_px = fill_price(px, d, slip_bps, entering=False)
            tot += d * (exit_px / entry - 1.0) - cost
        sims.append(tot)
    sims.sort()
    below = sum(1 for s in sims if s < real)
    return {
        "iterations": iter_n,
        "basis": "sum of per-trade net returns (NOT compounded; compounding saturates at -100%)",
        "real_total_pct": round(real * 100, 4),
        "random_mean_pct": round(statistics.mean(sims) * 100, 4),
        "random_median_pct": round(sims[len(sims) // 2] * 100, 4),
        "p_value_upper": round(1 - below / len(sims), 4),
        "real_percentile": round(below / len(sims) * 100, 2),
    }


# ---------------------------------------------------------------- run

def evaluate(bars, name, *, fee_bps, slip_bps, mc):
    fn, cfg = STRATEGIES[name]
    hold, warmup = cfg["hold"], cfg["warmup"]
    skeleton = backtest(bars, fn, hold=hold, warmup=warmup)
    trades = reprice(skeleton, bars, fee_bps, slip_bps)

    cut = int(len(bars) * 0.6)
    cut_t = bars[cut].t
    is_t = [t for t in trades if t["entry_time"] < cut_t]
    oos_t = [t for t in trades if t["entry_time"] >= cut_t]

    out = {
        "strategy": name,
        "hold_bars": hold,
        "warmup": warmup,
        "all": stats(trades, fee_bps, slip_bps),
        "in_sample": stats(is_t, fee_bps, slip_bps),
        "out_of_sample": stats(oos_t, fee_bps, slip_bps),
        "split_at": utc(cut_t),
        "cost_sensitivity": {},
    }
    for mult in (0.0, 1.0, 2.0, 5.0):
        t2 = reprice(skeleton, bars, fee_bps * mult, slip_bps * mult)
        s2 = stats(t2, fee_bps * mult, slip_bps * mult)
        out["cost_sensitivity"][f"{mult:g}x"] = {
            "net_pct": s2["net_pct"], "trades": s2["trades"], "pf": s2["pf"],
        }
    rc = random_control(bars, trades, iter_n=mc, fee_bps=fee_bps, slip_bps=slip_bps)
    out["random_control"] = rc
    out["trades"] = trades
    return out


def cmd_run(args) -> int:
    bars, man = load_bars(args.symbol, args.interval, args.tag)
    print(f"数据 {man['bars']} 根  {man['first_utc']} → {man['last_utc']}  "
          f"缺口 {man['gap_count']}")
    print(f"指纹 {man['fingerprint']}")
    print(f"成本：taker {args.fee_bps}bps/边 + 滑点 {args.slip_bps}bps/边 "
          f"= 往返 {2 * (args.fee_bps + args.slip_bps)}bps")
    print(f"IS/OOS 切点：前 60% / 后 40%\n")

    results = []
    for name in args.strategies.split(","):
        name = name.strip()
        if name not in STRATEGIES:
            raise SystemExit(f"未知策略 {name}，可选 {list(STRATEGIES)}")
        r = evaluate(bars, name, fee_bps=args.fee_bps, slip_bps=args.slip_bps, mc=args.mc)
        results.append(r)
        a = r["all"]
        ratio = (a["avg_gross_bps"] / a["cost_bps_roundtrip"]) if a["trades"] else 0
        print(f"[{name}] hold={r['hold_bars']} 根  "
              f"笔数 {a['trades']} (多{a['long']}/空{a['short']})")
        print(f"  全样本 净 {a['net_pct']:+.2f}%  胜率 {a['win_rate']}%  PF {a['pf']}  "
              f"单笔净 {a['avg_bps']}bps  毛 {a['avg_gross_bps']}bps  "
              f"成本 {a['cost_bps_roundtrip']}bps  毛/成本 {ratio:.2f}  t={a['t_stat']}")
        print(f"  最大回撤 {a['max_dd_pct']}%")
        for seg in ("in_sample", "out_of_sample"):
            s = r[seg]
            print(f"  {seg:13s} 净 {s['net_pct']:+.2f}%  笔数 {s['trades']}  "
                  f"PF {s['pf']}  单笔 {s['avg_bps']}bps")
        print("  成本敏感性 " + "  ".join(
            f"{k}:{v['net_pct']:+.2f}%" for k, v in r["cost_sensitivity"].items()))
        if r["random_control"]:
            rc = r["random_control"]
            print(f"  随机对照 {rc['iterations']}次（累加口径）均值 {rc['random_mean_pct']:+.2f}% "
                  f"中位 {rc['random_median_pct']:+.2f}%  真实累加 {rc['real_total_pct']:+.2f}%  "
                  f"分位 {rc['real_percentile']}%  p(upper)={rc['p_value_upper']}")
        print()

    stamp = int(time.time())
    if args.save_trades:
        for r in results:
            tp = REPORT_DIR / f"trades_{r['strategy']}_{args.symbol.lower()}_{args.interval}_{stamp}.csv"
            lines = ["signal_time_utc,entry_time_utc,exit_time_utc,dir,bars_held,"
                     "entry,exit,gross_bps,net_bps,net_pct,forced_exit"]
            for t in r["trades"]:
                lines.append(",".join(str(x) for x in (
                    utc(t["signal_time"]), utc(t["entry_time"]), utc(t["exit_time"]),
                    t["dir"], t["bars_held"], t["entry"], t["exit"],
                    t["gross_bps"], t["net_bps"], t["net_pct"], int(t["forced_exit"]),
                )))
            atomic_write(tp, "\n".join(lines) + "\n")
            print(f"逐笔明细 {tp}")

    slim = []
    for r in results:
        r2 = {k: v for k, v in r.items() if k != "trades"}
        slim.append(r2)

    payload = {
        "generated_at_utc": utc(int(time.time() * 1000)),
        "symbol": args.symbol,
        "interval": args.interval,
        "data": {k: man[k] for k in
                 ("bars", "first_utc", "last_utc", "gap_count", "fingerprint", "source")},
        "cost": {"fee_bps_side": args.fee_bps, "slip_bps_side": args.slip_bps,
                 "roundtrip_bps": 2 * (args.fee_bps + args.slip_bps)},
        "split": "in_sample first 60% by entry_time, out_of_sample rest",
        "results": slim,
    }
    out = REPORT_DIR / (f"run_{args.symbol.lower()}_{args.interval}"
                        f"_fee{args.fee_bps:g}_slip{args.slip_bps:g}_{stamp}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"结果已写入 {out}")
    return 0


# ---------------------------------------------------------------- verify

def cmd_verify(args) -> int:
    path = store_path(args.symbol, args.interval, args.tag)
    store = load_store(path)
    recs = [store[t] for t in sorted(store)]
    mpath = manifest_path(args.symbol, args.interval, args.tag)
    man = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {}

    ok = True
    fp = fingerprint(recs)
    if man.get("fingerprint") != fp:
        print(f"fingerprint 不一致: manifest {man.get('fingerprint')} vs 实算 {fp}")
        ok = False
    else:
        print(f"fingerprint 一致 {fp}")

    # 逐根抽验 + 全量结构校验
    bad = [r["t"] for r in recs if sha16(r["r"]) != r["h"]]
    print(f"逐根哈希：{len(recs)} 根，不匹配 {len(bad)}")
    if bad:
        ok = False

    gaps = find_gaps([r["t"] for r in recs], interval_ms(args.interval))
    print(f"时间连续性：缺口 {len(gaps)}")
    if gaps:
        ok = False

    now_ms = int(time.time() * 1000)
    future = [r["t"] for r in recs if int(r["r"][6]) >= now_ms]
    print(f"未收盘检查：{len(future)} 根 close_time >= now")
    if future:
        ok = False

    print(f"范围 {utc(recs[0]['t'])} → {utc(recs[-1]['t'])}" if recs else "空")
    return 0 if ok else 1


# ---------------------------------------------------------------- selftest

def selftest() -> int:
    """不联网的前视/未收盘/成本防护自检。全部通过才返回 0。"""
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    # 1. 收盘判定
    rows = [[0, "1", "2", "0.5", "1.5", "10", 899_999, "0", 1, "0", "0", "0"],
            [900_000, "1", "2", "0.5", "1.5", "10", 1_799_999, "0", 1, "0", "0", "0"]]
    closed, unclosed = split_closed(rows, 1_000_000)
    check("未收盘过滤只留 close_time<serverTime",
          len(closed) == 1 and closed[0][0] == 0 and len(unclosed) == 1)

    # 2. 每根哈希可复算、改一个字段就变
    h = sha16(rows[0])
    check("哈希可复算", h == sha16(list(rows[0])))
    tampered = list(rows[0])
    tampered[4] = "1.50000001"
    check("篡改收盘价哈希改变", sha16(tampered) != h)

    # 3. 前视防护
    bars = [Bar(i * INTERVAL_MS, 1, 1, 1, 1, 1, i * INTERVAL_MS + INTERVAL_MS - 1, "x")
            for i in range(100)]
    v = Bars(bars, 10)
    check("len 等于 i+1", len(v) == 11)
    check("v[-1] 是当前根", v[-1].t == 10 * INTERVAL_MS)
    try:
        v[11]
        check("访问未来应抛错", False)
    except LookaheadError:
        check("访问未来抛 LookaheadError", True)
    try:
        v[0:2]
        check("切片应被拒绝", False)
    except TypeError:
        check("切片被拒绝", True)

    # 4. 成交价必须是下一根 open
    seq = []
    for i in range(20):
        px = 100.0 + (1.0 if i == 11 else 0.0)  # 只有 i=11 那根 open 被抬高
        seq.append(Bar(i * INTERVAL_MS, px, px, px, px, 1, i * INTERVAL_MS + INTERVAL_MS - 1, "x"))
    tr = reprice(backtest(seq, lambda v: 1 if len(v) == 11 else 0, hold=1, warmup=2),
                 seq, 0, 0)
    check("只产生一笔", len(tr) == 1, f"got {len(tr)}")
    if tr:
        check("在下一根 open 成交", tr[0]["entry"] == 101.0, str(tr[0]["entry"]))
        check("entry_time = 信号后一根", tr[0]["entry_time"] == 11 * INTERVAL_MS)

    # 5. 最后一根不开仓
    tr2 = backtest(seq, lambda v: 1 if len(v) == 20 else 0, hold=1, warmup=2)
    check("最后一根不产生成交", len(tr2) == 0)

    # 6. 成本方向：零波动下多空都亏往返成本
    flat = [Bar(i * INTERVAL_MS, 100, 100, 100, 100, 1, i * INTERVAL_MS + INTERVAL_MS - 1, "x")
            for i in range(50)]
    trl = reprice(backtest(flat, lambda v: 1 if len(v) == 5 else 0, hold=2, warmup=2),
                  flat, 10, 2)
    check("往返成本=24bps", trl and abs(trl[0]["net_bps"] + 24) < 1e-6,
          str(trl[0]["net_bps"]) if trl else "no trade")

    # 7. 缺口检测（中间少一根就会被抓出来）
    gapped = [0, INTERVAL_MS, 3 * INTERVAL_MS, 4 * INTERVAL_MS]
    check("缺口可被识别", find_gaps(gapped) == [(INTERVAL_MS, 3 * INTERVAL_MS)])
    check("连续序列无缺口", find_gaps([0, INTERVAL_MS, 2 * INTERVAL_MS]) == [])
    check("1h 步长不被误判为缺口",
          find_gaps([0, 3_600_000, 7_200_000], interval_ms("1h")) == [])
    check("interval 解析", interval_ms("15m") == 900_000 and interval_ms("1d") == 86_400_000)

    # 8. 指纹稳定且对内容敏感
    recs = [{"t": 0, "h": "a"}, {"t": 900000, "h": "b"}]
    check("指纹稳定", fingerprint(recs) == fingerprint(list(reversed(recs))))
    check("指纹对哈希敏感", fingerprint(recs) != fingerprint([{"t": 0, "h": "z"}, recs[1]]))

    width = max(len(n) for n, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 老环境没有 reconfigure
        pass

    p = argparse.ArgumentParser(description="零依赖 15m 策略验证管线（公开行情，无需 API Key）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--symbol", default=SYMBOL)
        sp.add_argument("--interval", default=INTERVAL)
        sp.add_argument("--tag", default="",
                        help="文件名前缀，用于区分同 symbol 的不同市场（如 spot）")

    sp_pull = sub.add_parser("pull", help="拉取并落盘已收盘 K 线")
    common(sp_pull)
    sp_pull.add_argument("--days", type=int, default=180)
    sp_pull.add_argument("--host", default=None)
    sp_pull.add_argument("--page-limit", type=int, default=1000)
    sp_pull.add_argument("--sleep", type=float, default=0.3)
    sp_pull.set_defaults(func=pull)

    sp_run = sub.add_parser("run", help="回测（下一根开盘成交）")
    common(sp_run)
    sp_run.add_argument("--strategies", default="ma,donchian")
    sp_run.add_argument("--fee-bps", type=float, default=10.0, help="单边手续费，默认 taker 10bps")
    sp_run.add_argument("--slip-bps", type=float, default=2.0, help="单边滑点，默认 2bps")
    sp_run.add_argument("--mc", type=int, default=2000, help="随机对照次数，0 关闭")
    sp_run.add_argument("--save-trades", action="store_true",
                        help="额外导出逐笔 CSV（默认只存汇总，报告保持几 KB）")
    sp_run.set_defaults(func=cmd_run)

    sp_ver = sub.add_parser("verify", help="校验每根哈希与时间连续性")
    common(sp_ver)
    sp_ver.set_defaults(func=cmd_verify)

    sub.add_parser("selftest", help="不联网的防护自检").set_defaults(func=lambda a: selftest())

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
