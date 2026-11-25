#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无门控纯 Funding 截面的周频只读影子记录器。

零资金、不进 ladder、不下单。每周一 UTC 00:10 记一次理论持仓，按冻结规则
（25 币宽池 / 动态 Top 12 / 14 日 funding / 3 多 3 空 / 周频）向前推进一条净值，
专门积累 2026-11-25 之后的纯未来样本外。

它不产生任何 alpha 结论。它的唯一价值是：等 3-6 个月后回来看时，
这段时间的规则、数据指纹、成本假设都是当时就写死的，没有被事后修改的可能。

用法：
    python research/live/funding_shadow.py bootstrap   # 建账本，期初空仓
    python research/live/funding_shadow.py run         # 跑一次（幂等）
    python research/live/funding_shadow.py loop        # 每周一 00:10 UTC 自动跑
    python research/live/funding_shadow.py status
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "lite"))

import alpha_lite as al  # noqa: E402
import cross_section as cs  # noqa: E402
import phase2b_signal as p2b  # noqa: E402
import cost_policy as cp  # noqa: E402

DAY_MS = 86_400_000
CFG_PATH = HERE / "funding_shadow.json"
DATA_DIR = HERE / "shadow_data"
STATE_PATH = HERE / "state" / "funding_shadow_state.json"
LEDGER_PATH = HERE / "state" / "funding_shadow_ledger.jsonl"


class ShadowError(RuntimeError):
    pass


# ---------------------------------------------------------------- 契约

def load_config(path: Path = CFG_PATH) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    spec_path = ROOT / cfg["research_contract"]
    raw = spec_path.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != cfg["research_contract_sha256"]:
        raise SystemExit(
            f"研究契约哈希失配：{spec_path.name}={digest[:16]}…，"
            f"影子配置要求 {cfg['research_contract_sha256'][:16]}…\n"
            f"规则被动过就不是同一个策略，前向记录也失去意义。"
        )
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return {"cfg": cfg, "spec": spec}


# ---------------------------------------------------------------- 数据

def _bars_path(sym: str) -> Path:
    return DATA_DIR / f"{sym}_1d.jsonl"


def _fund_path(sym: str) -> Path:
    return DATA_DIR / f"{sym}_funding.jsonl"


def fetch_symbol(sym: str, start_ms: int, end_ms: int, sleep: float = 0.15) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    st = cs.server_time()
    rows = cs.fapi_get("/fapi/v1/klines", {
        "symbol": sym, "interval": "1d",
        "startTime": start_ms, "endTime": end_ms, "limit": 1500})
    closed = [r for r in rows if int(r[6]) < st]
    recs = [{"t": int(r[0]), "r": r, "h": al.sha16(r)} for r in closed]
    al.atomic_write(_bars_path(sym),
                    "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in recs))

    fund, cursor = [], start_ms
    for _ in range(8):
        fr = cs.fapi_get("/fapi/v1/fundingRate", {
            "symbol": sym, "startTime": cursor, "endTime": end_ms, "limit": 1000})
        if not fr:
            break
        for x in fr:
            t = int(x["fundingTime"])
            if start_ms < t <= end_ms:
                fund.append({"t": t, "r": x["fundingRate"]})
        if len(fr) < 1000:
            break
        cursor = int(fr[-1]["fundingTime"]) + 1
        time.sleep(sleep)

    uniq = {x["t"]: {"t": x["t"], "r": x["r"],
                     "h": al.sha16([x["t"], x["r"]])} for x in fund}
    frecs = [uniq[t] for t in sorted(uniq)]
    al.atomic_write(_fund_path(sym),
                    "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in frecs))

    man = {"symbol": sym, "bars": len(recs),
           "first_utc": al.utc(recs[0]["t"]) if recs else None,
           "last_utc": al.utc(recs[-1]["t"]) if recs else None,
           "dropped_unclosed": len(rows) - len(closed),
           "funding_rows": len(frecs),
           "bars_fingerprint": al.fingerprint(recs),
           "funding_fingerprint": al.fingerprint(frecs)}
    al.atomic_write(DATA_DIR / f"{sym}_1d.manifest.json",
                    json.dumps(man, indent=2, ensure_ascii=False) + "\n")
    return man


def load_all(spec: dict):
    bars_all, fund_all = {}, {}
    for sym in spec["universe"]:
        bp, fp = _bars_path(sym), _fund_path(sym)
        if not bp.exists() or not fp.exists():
            raise ShadowError(f"{sym} 数据缺失，先跑 run（会拉数）")
        bars = {}
        for line in bp.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if al.sha16(rec["r"]) != rec["h"]:
                raise ShadowError(f"{sym} 1d 哈希不匹配 t={rec['t']}")
            r = rec["r"]
            bars[int(r[0])] = {"o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                               "c": float(r[4]), "qv": float(r[7]), "digest": rec["h"]}
        fund = {}
        for line in fp.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            fund[int(rec["t"])] = float(rec["r"])
        bars_all[sym] = bars
        ts = sorted(fund)
        fund_all[sym] = (ts, [fund[t] for t in ts])
    return bars_all, fund_all


def pull_all(spec: dict, sleep: float = 0.15) -> list[dict]:
    start = int(datetime(2023, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end = int(time.time() * 1000)
    out = []
    for sym in spec["universe"]:
        out.append(fetch_symbol(sym, start, end, sleep))
        time.sleep(sleep)
    return out


# ---------------------------------------------------------------- 指纹

def window_fingerprint(bars_all: dict, day: int, days: int = 35) -> tuple[str, dict]:
    """最近 N 天、全池的逐根哈希。事后可逐字节复算，判断记录有没有被改过。"""
    per = {}
    for sym, bars in bars_all.items():
        recs = [{"t": t, "h": bars[t]["digest"]}
                for t in sorted(bars) if day - days * DAY_MS < t <= day]
        per[sym] = al.fingerprint(recs) if recs else None
    joined = "\n".join(f"{s}:{per[s]}" for s in sorted(per))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16], per


# ---------------------------------------------------------------- 日历

def weekly_grid(spec: dict, start_ms: int, until_ms: int) -> list[int]:
    anchor = int(datetime.fromisoformat(spec["rebalance"]["first_rebalance"])
                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
    step = spec["rebalance"]["freq_days"] * DAY_MS
    k = max(0, math.ceil((start_ms - anchor) / step))
    out, d = [], anchor + k * step
    while d <= until_ms:
        out.append(d)
        d += step
    return out


def next_monday_utc(now_ms: int) -> int:
    day = now_ms - (now_ms % DAY_MS)
    wd = datetime.fromtimestamp(day / 1000, tz=timezone.utc).weekday()   # 0 = Monday
    target = day + ((7 - wd) % 7) * DAY_MS + 600_000
    return target if target > now_ms else target + 7 * DAY_MS


# ---------------------------------------------------------------- 账本

def load_state(path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_state(st: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    al.atomic_write(path, json.dumps(st, indent=2, ensure_ascii=False) + "\n")


def append_ledger(rec: dict, path: Path = LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


# ---------------------------------------------------------------- 结算

def settle(position: dict, bars_all: dict, fund_all: dict,
           entry_day: int, exit_day: int) -> tuple[float, dict]:
    """入场开盘 → 出场开盘。funding 按研究口径只计 [D, D+6)，与研究实现一致。"""
    gross, detail = 0.0, {}
    for sym, w in position.items():
        bars = bars_all[sym]
        b0, b1 = bars.get(entry_day), bars.get(exit_day)
        if not b0 or not b1:
            raise ShadowError(f"{sym} 缺少 {al.utc(entry_day)[:10]} 或 {al.utc(exit_day)[:10]} 的日线")
        pr = b1["o"] / b0["o"] - 1.0
        fts, fr = fund_all[sym]
        lo = bisect.bisect_left(fts, entry_day)
        hi = bisect.bisect_left(fts, entry_day + 6 * DAY_MS)
        fu = sum(fr[lo:hi])
        c = w * (pr - fu)
        gross += c
        detail[sym] = {"w": round(w, 6), "price_ret_pct": round(pr * 100, 4),
                       "funding_pct": round(fu * 100, 4), "contrib_pct": round(c * 100, 4)}
    return gross, detail


def turnover(old: dict, new: dict) -> float:
    keys = set(old) | set(new)
    return sum(abs(new.get(s, 0.0) - old.get(s, 0.0)) for s in keys)


# ---------------------------------------------------------------- 一轮

def run_once(*, refresh: bool = True, now_ms: int | None = None,
             state_path: Path = STATE_PATH, ledger_path: Path = LEDGER_PATH) -> dict:
    conf = load_config()
    cfg, spec = conf["cfg"], conf["spec"]
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    st = load_state(state_path)
    if st is None:
        raise ShadowError("账本不存在，先跑 bootstrap")

    if refresh:
        pull_all(spec)
    bars_all, fund_all = load_all(spec)

    start = int(datetime.fromisoformat(cfg["start_date"]).replace(tzinfo=timezone.utc)
                .timestamp() * 1000)
    grid = weekly_grid(spec, start, now_ms)
    if not grid:
        return {"event": "waiting", "reason": f"还没有到第一个调仓日（{al.utc(start)[:10]} 之后第一个周一）"}
    due = grid[-1]
    last = st.get("last_rebalance_day")

    if last and due <= last:
        return {"event": "skip", "reason": "本调仓日已记录（幂等）",
                "last_rebalance_day": al.utc(last)[:10]}

    plan = next(iter(p2b.build_plans(spec, bars_all, fund_all, dates=[due])), None)
    new_pos = dict(plan["w"]) if plan else {}
    missed = 0 if not last else int(round((due - last) / (7 * DAY_MS))) - 1

    # 结算旧仓位
    settled, gross, detail = None, 0.0, {}
    if st.get("position") and last:
        gross, detail = settle(st["position"], bars_all, fund_all, last, due)

    od = turnover(st.get("position", {}), new_pos)
    nav = dict(st["nav"])
    cost_pct = {}
    for bps in cp.SCENARIOS:
        c = od * bps / 10_000.0
        nav[cp.scenario_key(bps)] = nav[cp.scenario_key(bps)] * (1.0 + gross - c)
        cost_pct[cp.scenario_key(bps)] = round(c * 100, 4)

    fp, per = window_fingerprint(bars_all, due)
    record = {
        "event": "rebalance",
        "at_utc": al.utc(now_ms),
        "decision_day": al.utc(due)[:10],
        "decision_day_ms": due,
        "variant": cfg["variant"],
        "settled_week": None if not detail else {
            "from": al.utc(last)[:10], "to": al.utc(due)[:10],
            "gross_pct": round(gross * 100, 4), "legs": detail},
        "turnover": round(od, 4),
        "cost_model_bps_per_side": cfg["cost"]["default_bps_per_side"],
        "cost_pct": cost_pct,
        "nav": {k: round(v, 6) for k, v in nav.items()},
        "position": {s: round(w, 6) for s, w in sorted(new_pos.items())},
        "longs": plan["longs"] if plan else [],
        "shorts": plan["shorts"] if plan else [],
        "pool": plan["pool"] if plan else [],
        "signal_funding_pct": plan["funding"] if plan else {},
        "missed_weeks": missed,
        "data": {"window_fingerprint": fp, "days": 35, "per_symbol": per},
        **cp.metadata(cp.PERP),
    }
    append_ledger(record, ledger_path)

    st.update({"nav": nav, "position": new_pos,
               "last_rebalance_day": due,
               "weeks": st.get("weeks", 0) + 1,
               "last_record_at_utc": al.utc(now_ms)})
    save_state(st, state_path)
    return record


# ---------------------------------------------------------------- 命令

def cmd_bootstrap(args) -> int:
    conf = load_config()
    cfg, spec = conf["cfg"], conf["spec"]
    if STATE_PATH.exists() and not args.force:
        print(f"账本已存在：{STATE_PATH}（重建加 --force）")
        return 1
    pull_all(spec)
    day = int(time.time() * 1000)
    st = {"version": 1, "name": cfg["name"], "variant": cfg["variant"],
          "started_at_utc": al.utc(day),
          "start_date": cfg["start_date"],
          "nav": {cp.scenario_key(b): 1.0 for b in cp.SCENARIOS},
          "position": {}, "last_rebalance_day": None, "weeks": 0,
          "cost_model": cp.metadata(cp.PERP)}
    save_state(st)
    append_ledger({"event": "bootstrap", "at_utc": al.utc(day),
                   "start_date": cfg["start_date"],
                   "first_rebalance_rule": cfg["first_rebalance_rule"],
                   "variant": cfg["variant"],
                   "contract_sha256": cfg["research_contract_sha256"],
                   **cp.metadata(cp.PERP)})
    print(f"影子账本已建立：{cfg['name']}（{cfg['variant']}），期初空仓 NAV 1.0")
    print(f"起点 {cfg['start_date']}，第一个调仓日按周一日历自动确定")
    return 0


def cmd_run(args) -> int:
    out = run_once(refresh=not args.no_refresh)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_loop(args) -> int:
    stop = HERE / "STOP"
    print("影子记录循环启动：每周一 UTC 00:10 跑一次；建 STOP 文件退出。")
    while True:
        if stop.exists():
            print("检测到 STOP，退出。")
            return 0
        now = int(time.time() * 1000)
        nxt = next_monday_utc(now)
        wait = max(1.0, (nxt - now) / 1000.0)
        print(f"{al.utc(now)} 等待 {wait/3600:.1f}h 至 {al.utc(nxt)}")
        time.sleep(min(wait, 3600.0))
        if int(time.time() * 1000) < nxt - 1000:
            continue
        try:
            print(json.dumps(run_once(refresh=True), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 单轮失败不杀循环
            print(f"本轮异常：{type(exc).__name__} {exc}", file=sys.stderr)


def cmd_status(args) -> int:
    st = load_state()
    if st is None:
        print("尚无账本。先跑 bootstrap。")
        return 1
    print(json.dumps(st, indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="无门控纯 funding 截面：只读前向记录器")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap"); b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_bootstrap)
    r = sub.add_parser("run"); r.add_argument("--no-refresh", action="store_true")
    r.set_defaults(func=cmd_run)
    sub.add_parser("loop").set_defaults(func=cmd_loop)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
