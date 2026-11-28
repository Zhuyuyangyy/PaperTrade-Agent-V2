"""回放器的等价性证明：先逐位复现引擎，才有资格改引擎的假设。

三道门，缺一不可：
  1. replay_trades(engine_compat) 与引擎 simulate() 逐笔 net 相同；
  2. 引擎 JSON 里的 nets 是排序后的（这是报告口径，不是成交顺序）；
  3. strict 模式与 engine_compat 的差异只来自「退出推迟到下一根开盘」，
     不复现引擎时不许出现额外的成本差异。

任一道门失败，2×2 消融的所有数字都不可信。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import replay as rp
import signals as sg
from run import OUT, SPECS, build_spec, run_engine


def load_engine_module():
    spec = importlib.util.spec_from_file_location(
        "engmod", ROOT / "skill" / "strategy-falsifier" / "agensi" / "scripts" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engmod"] = mod
    spec.loader.exec_module(mod)
    return mod


def verify(symbol: str = "BTCUSDT", lookback: int = 30) -> dict:
    eng = load_engine_module()
    meta = sg.build(symbol, "tsmom_long", lookback,
                    offset_start="2020-09-01", offset_end="2026-11-26")
    name = f"REPLAY_VERIFY_{symbol}"
    build_spec(name, symbol, "../" + meta["entries_path"].replace("\\", "/"),
               "../" + meta["exits_path"].replace("\\", "/"),
               hypothesis="回放器逐位等价验证", folds=None, grid=False)
    report = run_engine(f"{name}.json", HERE / "data" / f"{symbol}.csv")

    candles = rp.read_candles(HERE / "data" / f"{symbol}.csv")
    ent_t = rp.read_signal_csv(HERE / meta["entries_path"], "side")
    exi_t = rp.read_signal_csv(HERE / meta["exits_path"], "reason")
    idx = {c["t"]: i for i, c in enumerate(candles)}

    rule = eng.Exit(timeout_bars=100000, exit_signals={idx[t]: s for t, s in exi_t.items()})
    sim = eng.simulate(candles, {idx[t]: s for t, s in ent_t.items()},
                       rule, fee_rate=0.0004)
    eng_nets = [f.net_pct for f in sim.fills]
    compat = rp.replay_trades(candles, ent_t, exi_t, mode="engine_compat")
    strict = rp.replay_trades(candles, ent_t, exi_t, mode="strict")
    rep_nets = report["runs"]["worst_case"]["stats"]["nets"]

    gate1 = (len(eng_nets) == len(compat) and
             all(abs(a - b["net"]) < 1e-9 for a, b in zip(eng_nets, compat)))
    gate2 = ([round(x, 6) for x in rep_nets] ==
             [round(x, 6) for x in sorted(eng_nets)])
    gate3 = all(abs(t["fee"] - 2 * 0.0004) < 1e-12 for t in compat + strict)

    out = {
        "symbol": symbol, "lookback": lookback,
        "fills": len(eng_nets),
        "gate1_trade_by_trade": gate1,
        "gate2_report_nets_sorted": gate2,
        "gate3_cost_identical": gate3,
        "compat_sum_pct": round(sum(t["net"] for t in compat) * 100, 4),
        "strict_sum_pct": round(sum(t["net"] for t in strict) * 100, 4),
        "engine_sum_pct": round(sum(eng_nets) * 100, 4),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if not (gate1 and gate2 and gate3):
        raise SystemExit("回放器等价性未通过，禁止继续 2×2 实验")
    return out


if __name__ == "__main__":
    verify()
