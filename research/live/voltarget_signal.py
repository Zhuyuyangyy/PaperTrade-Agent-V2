#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3 基线 1 的实盘信号层（只读）。

规则（来自 research/lite/phase3_spec.json，Phase 3 契约，未改一个参数）：
    sigma20 = stdev(最近 20 个日收盘收益率) * sqrt(365)
    raw     = min(0.30 / sigma20, 1.0)      # 目标年化波动 30%，不加杠杆
    w_new   = raw        if |raw - w_prev| > 0.10   # 10% 缓冲带
            = w_prev     otherwise

这一层只做三件事：取数校验、算目标权重、返回一个 dict。
**不写账本、不下单、不改任何状态。** 物理隔离靠这个签名保证，不靠自觉。

数据校验（三条，全部复用 alpha_lite 的实现，不另写一套）：
    1. 只认已收盘 1h K 线：close_time < serverTime
    2. 信号窗口（最近 30 天）内有缺口 → 抛 DataError，不降级、不用旧值顶替
    3. 每根 1h 保留 sha256[:16]，按天聚合出日指纹，窗口再聚合出窗口指纹

用法：
    python research/live/voltarget_signal.py            # 打印当日信号
    python research/live/voltarget_signal.py --refresh  # 先补最新数据再算
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "research" / "lite"))
import alpha_lite as al  # noqa: E402

CFG_PATH = HERE / "voltarget_live.json"
DAY_MS = 86_400_000


class DataError(RuntimeError):
    """数据不可用。调用方必须冻结，不能用上一次的仓位顶替。"""


# ---------------------------------------------------------------- 契约

def load_config(path: Path = CFG_PATH) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    spec_path = ROOT / cfg["research_contract"]
    raw = spec_path.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != cfg["research_contract_sha256"]:
        raise SystemExit(
            f"研究契约哈希失配：{spec_path.name} = {digest[:16]}…，"
            f"实盘配置要求 {cfg['research_contract_sha256'][:16]}…\n"
            f"规则被动过就必须重新评估，不能拿旧结论接实盘。"
        )
    return cfg


# ---------------------------------------------------------------- 取数

@contextlib.contextmanager
def direct_connection():
    """本机的本地代理会把公开端点拖到超时，直连反而稳定（实测 0.5-1.0s）。

    只在取数期间摘掉代理变量，退出时原样恢复，不污染调用方环境。
    """
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def refresh(days_back: int = 45, sleep: float = 0.3) -> dict:
    """把最新已收盘的 1h K 线补进本地库。复用 alpha_lite.pull，不另写取数逻辑。"""
    cfg = load_config()
    ns = argparse.Namespace(symbol=cfg["symbol"], interval=cfg["interval"],
                            days=days_back, tag=cfg["tag"], host=None,
                            sleep=sleep, page_limit=1000)
    with direct_connection():
        al.pull(ns)
    return {"refreshed_at_utc": al.utc(int(time.time() * 1000)), "days_back": days_back}


def day_digest(hours: list[str]) -> str:
    return hashlib.sha256("\n".join(hours).encode("utf-8")).hexdigest()[:16]


def load_days(cfg: dict | None = None) -> list[dict]:
    """现货 1h → 日线（UTC）。不足 min_hours 的当天丢弃，不补、不前向填充。"""
    cfg = cfg or load_config()
    path = al.store_path(cfg["symbol"], cfg["interval"], cfg["tag"])
    store = al.load_store(path)
    if not store:
        raise DataError(f"本地无数据：{path.name}。先跑 --refresh。")

    min_hours = cfg["data_policy"]["min_hours_per_day"]
    by_day: dict[int, dict] = {}
    for t in sorted(store):
        rec = store[t]
        d = t - (t % DAY_MS)
        r = rec["r"]
        o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
        b = by_day.get(d)
        if b is None:
            by_day[d] = {"day": d, "o": o, "h": h, "l": l, "c": c,
                         "hours": 1, "digests": [rec["h"]]}
        else:
            b["h"] = max(b["h"], h)
            b["l"] = min(b["l"], l)
            b["c"] = c
            b["hours"] += 1
            b["digests"].append(rec["h"])

    days = []
    for d in sorted(by_day):
        b = by_day[d]
        if b["hours"] < min_hours:
            continue
        days.append({"day": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                     "hours": b["hours"], "digest": day_digest(b["digests"])})
    if len(days) < 30:
        raise DataError(f"日线只有 {len(days)} 天，不足以计算 20 日波动")
    return days


# ---------------------------------------------------------------- 校验

def check_window(days: list[dict], cfg: dict, now_ms: int | None = None) -> dict:
    """只看信号窗口：缺口、陈旧度、指纹。历史缺口与当日信号无关，不阻断。"""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    win = cfg["data_policy"]["gap_window_days"]
    last = days[-1]["day"]

    recent = [d for d in days if d["day"] >= last - (win - 1) * DAY_MS]
    gaps = [(a["day"], b["day"]) for a, b in zip(recent, recent[1:])
            if b["day"] - a["day"] != DAY_MS]

    # 陈旧度：最后一根已收盘日线结束之后过了几天
    stale_days = (now_ms - (last + DAY_MS)) / DAY_MS

    window = "\n".join(f"{d['day']}:{d['digest']}" for d in recent[-25:])
    return {
        "last_closed_day": al.utc(last)[:10],
        "last_closed_day_ms": last,
        "last_close": days[-1]["c"],
        "days_total": len(days),
        "gap_days_30": len(gaps),
        "gap_detail": [(al.utc(a)[:10], al.utc(b)[:10]) for a, b in gaps[:3]],
        "stale_days": round(stale_days, 4),
        "last_day_digest": days[-1]["digest"],
        "window_fingerprint": hashlib.sha256(window.encode("utf-8")).hexdigest()[:16],
        "window_days_fingerprinted": min(25, len(recent)),
    }


def require_clean(check: dict, cfg: dict) -> None:
    if check["gap_days_30"]:
        raise DataError(f"信号窗口有 {check['gap_days_30']} 处缺口：{check['gap_detail']}")
    if check["stale_days"] > cfg["data_policy"]["max_stale_days"]:
        raise DataError(f"数据陈旧 {check['stale_days']:.1f} 天，超过 "
                        f"{cfg['data_policy']['max_stale_days']} 天上限")


# ---------------------------------------------------------------- 信号

def daily_returns(days: list[dict]) -> list[float]:
    rets = [0.0]
    for i in range(1, len(days)):
        rets.append(days[i]["c"] / days[i - 1]["c"] - 1.0)
    return rets


def sigma_at(rets: list[float], i: int, win: int, annualize_days: int) -> float:
    hist = rets[i - win:i]
    return statistics.stdev(hist) * math.sqrt(annualize_days)


def apply_buffer(raw: float, prev: float, buf: float) -> tuple[float, bool]:
    """缓冲带：变动不超过 buf 就不动。返回 (应用后的权重, 是否调仓)。"""
    return (raw, True) if abs(raw - prev) > buf else (prev, False)


def compute(days: list[dict], cfg: dict, prev_weight: float,
            now_ms: int | None = None) -> dict:
    """给出「下一个交易日」的目标权重。days 的最后一根必须是已收盘日线。

    索引关系与 Phase 3 回测严格一致：回测在 i 日用 rets[i-W:i]（截止 i-1 收盘），
    这里 i = len(days)（下一日），窗口同样是截止最后一根收盘的 20 个收益率。
    """
    v = cfg["volatility"]
    win, ann, target = v["window_days"], v["annualize_days"], v["target_annual"]
    buf = cfg["position"]["buffer_abs"]
    if len(days) < win + 1:
        raise DataError(f"需要至少 {win + 1} 天日线，实际 {len(days)}")
    rets = daily_returns(days)
    i = len(days)
    sig = sigma_at(rets, i, win, ann)
    raw = min(target / sig, cfg["position"]["max_weight"]) if sig > 0 else 0.0
    w_new, rebalanced = apply_buffer(raw, prev_weight, buf)

    check = check_window(days, cfg, now_ms)
    return {
        "strategy": cfg["name"],
        "generated_at_utc": al.utc(now_ms if now_ms is not None else int(time.time() * 1000)),
        "decision_for_day": al.utc(days[-1]["day"] + DAY_MS)[:10],
        "decision_for_day_ms": days[-1]["day"] + DAY_MS,
        "last_closed_day": check["last_closed_day"],
        "last_close": check["last_close"],
        "sigma20_ann": round(sig, 8),
        "raw_target": round(raw, 8),
        "prev_weight": round(prev_weight, 8),
        "delta": round(w_new - prev_weight, 8),
        "target_weight": round(w_new, 8),
        "rebalance": rebalanced,
        "buffer_abs": buf,
        "cost_bps_if_filled": round(abs(w_new - prev_weight) * cfg["execution"]["cost_per_side_bps"], 4),
        "data": check,
    }


def replay(days: list[dict], cfg: dict) -> list[dict]:
    """从空仓起步重放整段历史，用于和 Phase 3 回测逐日对齐。

    与 vol_target.run_baseline1 的循环完全同构：i 从 W+1 开始，w 初值 0。
    """
    v = cfg["volatility"]
    win, ann, target = v["window_days"], v["annualize_days"], v["target_annual"]
    buf = cfg["position"]["buffer_abs"]
    rets = daily_returns(days)
    out, w = [], 0.0
    for i in range(win + 1, len(days)):
        sig = sigma_at(rets, i, win, ann)
        raw = min(target / sig, cfg["position"]["max_weight"]) if sig > 0 else 0.0
        w_new, rebalanced = apply_buffer(raw, w, buf)
        out.append({"day": days[i]["day"], "weight": w_new, "prev_weight": w,
                    "sigma": sig, "raw": raw, "rebalanced": rebalanced})
        w = w_new
    return out


def replay_returns(days: list[dict], cfg: dict) -> list[tuple[int, float]]:
    """重放并计算日收益，公式与 run_baseline1 逐项相同（overnight/intraday/cost）。"""
    cost_bps = cfg["execution"]["cost_per_side_bps"]
    path = replay(days, cfg)
    idx = {d["day"]: i for i, d in enumerate(days)}
    out = []
    for step in path:
        i = idx[step["day"]]
        prev_c = days[i - 1]["c"]
        o, c = days[i]["o"], days[i]["c"]
        cost = abs(step["weight"] - step["prev_weight"]) * cost_bps / 10_000.0
        overnight = step["prev_weight"] * (o / prev_c - 1.0)
        intraday = step["weight"] * (c / o - 1.0)
        out.append((step["day"], overnight + intraday - cost))
    return out


# ---------------------------------------------------------------- 行情快照

def fetch_day_open(cfg: dict, day_ms: int) -> dict:
    """取决策日 D 的开盘价（D 00:00 UTC 那根 1m 的 open）。

    未收盘 K 线的 open 是不可变字段，用它做成交基准不构成前视；
    信号仍然只用已收盘日线。取不到就退回最新成交价，并如实标注来源。
    """
    host = al.pick_host(None)
    with direct_connection():
        try:
            rows, st = al.fetch_klines(host, cfg["symbol"], "1m", limit=2)
            for r in rows:
                if int(r[0]) == day_ms:
                    return {"fill_ref_price": float(r[1]), "fill_source": "1m_open",
                            "quoted_at_utc": al.utc(int(time.time() * 1000))}
            px = al.http_json(host + "/api/v3/ticker/price?symbol=" + cfg["symbol"])
            return {"fill_ref_price": float(px["price"]), "fill_source": "ticker_fallback",
                    "quoted_at_utc": al.utc(int(time.time() * 1000))}
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"取成交基准价失败：{type(exc).__name__} {exc}") from exc


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="BTC 现货 30% 波动率目标：只读信号层")
    p.add_argument("--refresh", action="store_true", help="先补最新已收盘 1h K 线")
    p.add_argument("--days-back", type=int, default=45)
    p.add_argument("--prev-weight", type=float, default=0.0)
    args = p.parse_args(argv)

    cfg = load_config()
    if args.refresh:
        refresh(args.days_back)
    days = load_days(cfg)
    sig = compute(days, cfg, args.prev_weight)
    require_clean(sig["data"], cfg)
    print(json.dumps(sig, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
