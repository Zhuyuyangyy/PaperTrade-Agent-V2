#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3 基线 1 的 dryrun 运行器：账本 + ladder 状态机。

与信号层的分工是物理的：
    voltarget_signal.py  纯函数，拿日线返回目标权重，不碰状态
    voltarget_runner.py  读信号 → 记账 → 过 ladder → 落盘

每日 UTC 00:00:10 跑一次，一轮做两件事：
    1. 结算昨天（现在有了昨天的收盘价）
    2. 决策今天（用昨天收盘算出的目标权重，按今天开盘价记成交基准）

判定与冻结全部交给 ladder.py：
    - 数据缺口 / 陈旧 → DataError → freeze（不是"用昨天仓位顶替"）
    - 对账不平 → ladder.evaluate(reconciliation_gap=1) → halt
    - 回撤 / 连亏 → ladder 自动 demote 或 halt

用法：
    python research/live/voltarget_runner.py bootstrap   # 建初始状态，不记假成交
    python research/live/voltarget_runner.py once        # 跑一轮（幂等）
    python research/live/voltarget_runner.py loop        # 每天 00:00:10 UTC 自动跑
    python research/live/voltarget_runner.py selfcheck   # 与 Phase 3 回测逐日对齐
    python research/live/voltarget_runner.py drill       # 断流/缺口/对账 冻结演练
    python research/live/voltarget_runner.py status
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "lite"))

import alpha_lite as al  # noqa: E402
import voltarget_signal as sv  # noqa: E402
from research.live.ladder import Ladder  # noqa: E402

DAY_MS = sv.DAY_MS
STATE_DIR = HERE / "state"
STATE_PATH = STATE_DIR / "voltarget_state.json"
LADDER_PATH = STATE_DIR / "voltarget_ladder.json"
LEDGER_PATH = STATE_DIR / "voltarget_ledger.jsonl"
DAY_TOLERANCE_MS = 2 * 3600_000   # 00:00 后 2 小时内算准时


# ---------------------------------------------------------------- 落盘

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_state(path: Path = STATE_PATH) -> dict:
    st = _load_json(path, None)
    if st is None:
        st = {"version": 1, "nav": 1.0, "weight": 0.0, "pending": None,
              "last_settled_day": None, "last_decision_day": None,
              "halted": False, "halt_reason": "", "period": {"cum": 1.0, "days": 0},
              "decisions": 0, "settled": 0}
    st.setdefault("period", {"cum": 1.0, "days": 0})
    return st


def save_state(st: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    al.atomic_write(path, json.dumps(st, indent=2, ensure_ascii=False) + "\n")


def append_ledger(record: dict, path: Path = LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def load_ladder(cfg: dict, path: Path = LADDER_PATH) -> Ladder:
    lad = Ladder.load(path, name=cfg["name"])
    lad.tier = cfg["dryrun"]["tier"]
    lad.halt_rules = {
        "max_consecutive_losses": cfg["dryrun"]["max_consecutive_losses"],
        "max_data_gap_s": cfg["dryrun"]["max_data_gap_s"],
        "reconciliation_tolerance": 0,
    }
    return lad


# ---------------------------------------------------------------- 守卫

def reconciliation_gap(st: dict) -> int:
    """本地账本自洽检查。持仓权重必须等于最后一次决策的输出，否则对账不平。"""
    expected = st["pending"]["new_weight"] if st.get("pending") else 0.0
    return 0 if abs(st.get("weight", 0.0) - expected) < 1e-12 else 1


def freeze(st: dict, ladder: Ladder, reason: str, **extra) -> dict:
    st["halted"] = True
    st["halt_reason"] = reason
    ladder.halt()
    ladder.halt_reason = reason
    return {"event": "halt", "reason": reason, **extra}


# ---------------------------------------------------------------- 一轮

def process(cfg: dict, st: dict, ladder: Ladder, days: list[dict],
            now_ms: int, *, offline: bool = False, ledger_path: Path = LEDGER_PATH) -> dict:
    """一轮 = 守卫 → 结算 → 决策。所有返回路径都不抛异常，只返回发生了什么。"""
    check = sv.check_window(days, cfg, now_ms)
    base = {"at_utc": al.utc(now_ms), "last_closed_day": check["last_closed_day"]}

    # 守卫 1：数据缺口 / 陈旧。不降级、不用旧值顶替。
    try:
        sv.require_clean(check, cfg)
    except sv.DataError as exc:
        out = freeze(st, ladder, f"数据不可用：{exc}", **base)
        append_ledger(out, ledger_path)
        return out

    # 守卫 2：对账
    gap = reconciliation_gap(st)
    if gap:
        v = ladder.evaluate(reconciliation_gap=gap)
        st["halted"], st["halt_reason"] = True, v.reason
        out = {"event": "halt", "reason": v.reason, **base}
        append_ledger(out, ledger_path)
        return out

    last = days[-1]
    settled = None

    # ---- 结算昨天的决策（此刻已有昨天的收盘价） ----
    p = st.get("pending")
    if p and p["day"] <= last["day"]:
        idx = next(i for i, d in enumerate(days) if d["day"] == p["day"])
        prev_c, o, c = days[idx - 1]["c"], days[idx]["o"], days[idx]["c"]
        overnight = p["prev_weight"] * (o / prev_c - 1.0)
        intraday = p["new_weight"] * (c / o - 1.0)
        net = overnight + intraday - p["cost"]
        st["nav"] *= (1.0 + net)
        st["settled"] += 1
        st["last_settled_day"] = p["day"]
        st["period"]["cum"] *= (1.0 + net)
        st["period"]["days"] += 1

        # 换仓即结束一个持仓期，把这一期作为一个"笔"交给 ladder
        if p["closed_period"]:
            ladder.record(st["period"]["cum"] - 1.0, equity=st["nav"])
            st["period"] = {"cum": 1.0, "days": 0}

        settled = {"day": al.utc(p["day"])[:10], "net_pct": round(net * 100, 4),
                   "nav": round(st["nav"], 6), "overnight_pct": round(overnight * 100, 4),
                   "intraday_pct": round(intraday * 100, 4),
                   "cost_pct": round(-p["cost"] * 100, 4),
                   "weight": p["new_weight"], "fill_ref_price": p.get("fill_ref_price"),
                   "fill_source": p.get("fill_source"),
                   "period_closed": p["closed_period"]}
        st["pending"] = None

    # ---- ladder 评估：数据年龄用真实陈旧度 ----
    verdict = ladder.evaluate(equity=st["nav"],
                              data_age_s=check["stale_days"] * 86400,
                              reconciliation_gap=0)
    if verdict.action == "halt":
        st["halted"], st["halt_reason"] = True, verdict.reason
        out = {"event": "halt", "reason": verdict.reason, "settled": settled, **base}
        append_ledger(out, ledger_path)
        return out
    if verdict.action == "demote":
        before = ladder.tier
        ladder.demote()
        append_ledger({"event": "demote", "from": before, "to": ladder.tier,
                       "reason": verdict.reason, **base}, ledger_path)

    # ---- 决策今天 ----
    D = last["day"] + DAY_MS
    out = {"event": "decision", **base, "settled": settled,
           "decision_for_day": al.utc(D)[:10]}

    if st.get("last_decision_day") and st["last_decision_day"] >= D:
        out.update({"event": "skip", "reason": "该决策日已处理（幂等）"})
        append_ledger(out, ledger_path)
        return out

    sig = sv.compute(days, cfg, st["weight"], now_ms=now_ms)
    fill = {"fill_ref_price": None, "fill_source": "offline"}
    if not offline:
        try:
            fill = sv.fetch_day_open(cfg, D)
        except sv.DataError as exc:
            out.update({"event": "halt", "reason": f"取成交基准价失败：{exc}"})
            freeze(st, ladder, out["reason"], **base)
            append_ledger(out, ledger_path)
            return out

    new_w = sig["target_weight"]
    # 只有"本来就有持仓、且这一期已经跑过至少一天"才算结束一个持仓期。
    # 从空仓建仓不是平仓，不该给 ladder 记一笔零收益的假交易。
    closed = (abs(new_w - st["weight"]) > 1e-12) and st["period"]["days"] > 0
    st["pending"] = {
        "day": D, "prev_weight": st["weight"], "new_weight": new_w,
        "cost": abs(new_w - st["weight"]) * cfg["execution"]["cost_per_side_bps"] / 10_000.0,
        "closed_period": closed,
        "fill_ref_price": fill.get("fill_ref_price"),
        "fill_source": fill.get("fill_source"),
        "target_raw": sig["raw_target"],
        "sigma20_ann": sig["sigma20_ann"],
        "window_fingerprint": sig["data"]["window_fingerprint"],
        "last_day_digest": sig["data"]["last_day_digest"],
    }
    st["weight"] = new_w
    st["last_decision_day"] = D
    st["decisions"] += 1

    out.update({
        "sigma20_ann": sig["sigma20_ann"], "raw_target": sig["raw_target"],
        "prev_weight": sig["prev_weight"], "target_weight": new_w,
        "rebalance": sig["rebalance"],
        "cost_bps_if_filled": sig["cost_bps_if_filled"],
        "fill_ref_price": fill.get("fill_ref_price"),
        "fill_source": fill.get("fill_source"),
        "window_fingerprint": sig["data"]["window_fingerprint"],
        "late_hours": round(max(0.0, (now_ms - D) / 3600_000.0), 3),
    })
    append_ledger(out, ledger_path)
    return out


def run_once(*, refresh: bool = True, offline: bool = False,
             now_ms: int | None = None, state_dir: Path = STATE_DIR) -> dict:
    cfg = sv.load_config()
    state_path = state_dir / "voltarget_state.json"
    ladder_path = state_dir / "voltarget_ladder.json"
    ledger_path = state_dir / "voltarget_ledger.jsonl"

    st = load_state(state_path)
    ladder = load_ladder(cfg, ladder_path)
    if st.get("halted"):
        return {"event": "already_halted", "reason": st.get("halt_reason")}

    if refresh and not offline:
        sv.refresh()

    try:
        days = sv.load_days(cfg)
    except sv.DataError as exc:
        out = freeze(st, ladder, f"数据不可用：{exc}")
        save_state(st, state_path)
        ladder.save(ladder_path)
        append_ledger(out, ledger_path)
        return out

    out = process(cfg, st, ladder, days, now_ms or int(time.time() * 1000),
                  offline=offline, ledger_path=ledger_path)
    save_state(st, state_path)
    ladder.save(ladder_path)
    return out


# ---------------------------------------------------------------- 自检：与回测逐日对齐

def selfcheck() -> int:
    """把实盘信号层重放三年，与 Phase 3 的 run_baseline1 逐日比对。

    对齐三件套：每一天的净收益、总调仓次数、平均仓位。三者都到机器精度，
    才说明信号层没有悄悄改口径。
    """
    cfg = sv.load_config()
    days = sv.load_days(cfg)
    print(f"日线 {len(days)} 天  {al.utc(days[0]['day'])[:10]} → {al.utc(days[-1]['day'])[:10]}")

    sys.path.insert(0, str(ROOT / "research" / "lite"))
    import vol_target as vt

    spec, lock = vt.load_contract()
    bt_start = spec["data"]["backtest_start"]
    start_ms = int(__import__("datetime").datetime.fromisoformat(bt_start)
                   .replace(tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    start = next(i for i, d in enumerate(days) if d["day"] >= start_ms)

    mine = sv.replay_returns(days[start:], cfg)
    ref_raw, n_ref, avg_ref = vt.run_baseline1(
        [d["day"] for d in days[start:]],
        # 索引约定 (day, open, high, close, low)：第 3 位必须是收盘价
        [(d["day"], d["o"], d["h"], d["c"], d["l"]) for d in days[start:]],
        spec)

    if len(mine) != len(ref_raw):
        print(f"FAIL 长度不一致：live {len(mine)} vs backtest {len(ref_raw)}")
        return 1

    diffs = [(abs(a[1] - b[1]), a[0]) for a, b in zip(mine, ref_raw)]
    worst, worst_day = max(diffs)
    total_live = 1.0
    total_ref = 1.0
    for _, r in mine:
        total_live *= (1 + r)
    for _, r in ref_raw:
        total_ref *= (1 + r)

    path = sv.replay(days[start:], cfg)
    n_live = sum(1 for p in path if p["rebalanced"])
    avg_live = sum(p["weight"] for p in path) / max(1, len(path))

    ok = (worst <= 1e-12 and n_live == n_ref and abs(avg_live - avg_ref) <= 1e-9)
    print(f"逐日净收益最大偏差 {worst:.2e}（{al.utc(worst_day)[:10]}）")
    print(f"净值 live {total_live:.8f} vs backtest {total_ref:.8f}")
    print(f"调仓次数 live {n_live} vs backtest {n_ref}")
    print(f"平均仓位 live {avg_live:.8f} vs backtest {avg_ref:.8f}")
    print(f"契约 {lock['contract_sha256'][:16]}…  样本 {len(mine)} 天")
    print("PASS 零口径漂移" if ok else "FAIL 与回测不一致，禁止进入 dryrun")
    return 0 if ok else 1


# ---------------------------------------------------------------- 演练：冻结

def drill() -> int:
    """三种故障各打一次，确认系统是冻结而不是硬撑。"""
    cfg = sv.load_config()
    days = sv.load_days(cfg)
    now = int(time.time() * 1000)
    results = []

    def fresh(tmp: Path):
        st = {"version": 1, "nav": 1.0, "weight": 0.0, "pending": None,
              "last_settled_day": None, "last_decision_day": None,
              "halted": False, "halt_reason": "", "period": {"cum": 1.0, "days": 0},
              "decisions": 0, "settled": 0}
        return st, load_ladder(cfg, tmp / "ladder.json"), tmp / "ledger.jsonl"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 1. 信号窗口缺口
        st, lad, led = fresh(tmp)
        holed = [d for d in days if d["day"] != days[-5]["day"]]
        out = process(cfg, st, lad, holed, now, offline=True, ledger_path=led)
        ok = out["event"] == "halt" and "缺口" in out["reason"] and st["halted"]
        results.append(("缺口 → 冻结", ok, out.get("reason", "")))

        # 2. 数据陈旧（最后一根日线在 4 天前）
        st, lad, led = fresh(tmp)
        stale = [d for d in days if d["day"] <= days[-1]["day"] - 4 * DAY_MS]
        out = process(cfg, st, lad, stale, now, offline=True, ledger_path=led)
        ok = out["event"] == "halt" and "陈旧" in out["reason"] and st["halted"]
        results.append(("断流 4 天 → 冻结", ok, out.get("reason", "")))

        # 3. 对账不平（账本持仓与决策输出不一致）
        st, lad, led = fresh(tmp)
        st["pending"] = {"day": days[-1]["day"] + DAY_MS, "prev_weight": 0.0,
                         "new_weight": 0.5, "cost": 0.0, "closed_period": True}
        st["weight"] = 0.37          # 被手工改过
        out = process(cfg, st, lad, days, now, offline=True, ledger_path=led)
        ok = out["event"] == "halt" and "对账不平" in out["reason"] and st["halted"]
        results.append(("账本被改 → 冻结", ok, out.get("reason", "")))

    print("冻结演练")
    for name, ok, reason in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  —  {reason}")
    return 0 if all(r[1] for r in results) else 1


# ---------------------------------------------------------------- 命令

def cmd_bootstrap(args) -> int:
    cfg = sv.load_config()
    if STATE_PATH.exists() and not args.force:
        print(f"状态已存在：{STATE_PATH}（要重建加 --force）")
        return 1
    days = sv.load_days(cfg)
    check = sv.check_window(days, cfg)
    sv.require_clean(check, cfg)
    sig = sv.compute(days, cfg, 0.0)
    st = {"version": 1, "nav": 1.0, "weight": 0.0, "pending": None,
          "last_settled_day": None, "last_decision_day": None,
          "halted": False, "halt_reason": "", "period": {"cum": 1.0, "days": 0},
          "decisions": 0, "settled": 0,
          "bootstrapped_at_utc": al.utc(int(time.time() * 1000)),
          "bootstrap_last_closed_day": check["last_closed_day"],
          "bootstrap_fingerprint": check["window_fingerprint"]}
    save_state(st)
    lad = load_ladder(cfg)
    lad.save(LADDER_PATH)
    append_ledger({"event": "bootstrap", "at_utc": al.utc(int(time.time() * 1000)),
                   "last_closed_day": check["last_closed_day"],
                   "next_decision_day": sig["decision_for_day"],
                   "preview_target_weight": sig["target_weight"],
                   "preview_sigma20_ann": sig["sigma20_ann"],
                   "window_fingerprint": check["window_fingerprint"]})
    print(f"初始状态已建立：NAV 1.0，空仓")
    print(f"最后已收盘日 {check['last_closed_day']}，窗口指纹 {check['window_fingerprint']}")
    print(f"下一个决策日 {sig['decision_for_day']} 的预览目标仓位 "
          f"{sig['target_weight']*100:.1f}%（σ20 {sig['sigma20_ann']*100:.1f}%）")
    return 0


def cmd_once(args) -> int:
    out = run_once(refresh=not args.no_refresh)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out["event"] != "halt" else 2


def cmd_loop(args) -> int:
    stop = HERE / "STOP"
    print(f"dryrun 循环启动，每天 UTC 00:00:10 跑一次；建 {stop} 文件可退出。")
    while True:
        if stop.exists():
            print("检测到 STOP 文件，退出。")
            return 0
        now = int(time.time() * 1000)
        nxt = (now // DAY_MS + 1) * DAY_MS + 10_000
        wait = max(1.0, (nxt - now) / 1000.0)
        print(f"{al.utc(now)} 等待 {wait/3600:.2f}h 至 {al.utc(nxt)}")
        time.sleep(min(wait, 3600.0))
        if int(time.time() * 1000) < nxt - 1000:
            continue
        try:
            out = run_once(refresh=True)
            print(json.dumps(out, ensure_ascii=False))
            if out["event"] == "halt":
                print(f"已冻结：{out.get('reason')}，循环退出。")
                return 2
        except Exception as exc:  # noqa: BLE001 - 单轮失败不能杀死循环
            print(f"本轮异常：{type(exc).__name__} {exc}", file=sys.stderr)


def cmd_status(args) -> int:
    st = load_state()
    cfg = sv.load_config()
    lad = load_ladder(cfg)
    print(json.dumps({"state": st, "ladder": {"tier": lad.tier, "halted": lad.halted,
                                              "halt_reason": lad.halt_reason,
                                              "stats": lad.stats()}},
                     indent=2, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(description="BTC 现货 30% 波动率目标：dryrun 运行器")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap"); b.add_argument("--force", action="store_true")
    b.set_defaults(func=cmd_bootstrap)
    o = sub.add_parser("once"); o.add_argument("--no-refresh", action="store_true")
    o.set_defaults(func=cmd_once)
    sub.add_parser("loop").set_defaults(func=cmd_loop)
    sub.add_parser("selfcheck").set_defaults(func=lambda a: selfcheck())
    sub.add_parser("drill").set_defaults(func=lambda a: drill())
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
