"""TSMOM 批量实验运行器。

设计原则（这是整个研究的骨架，不是胶水代码）：

1. **信号生成与损益测量彻底分离**。本文件不计算任何一笔盈亏，全部由
   strategy-falsifier 引擎推演后读回。跑的人和判卷的人不能是同一个人。

2. **多空必须分腿**。引擎的随机对照组与买入持有基线都只镜像「主方向」，
   多空混合（如 138 空 / 137 多）会让两条基线同时失效：
     - 对照组变成「在一只涨了 14 倍的资产上随机做空」，必然巨亏，
       于是「选择性 proven」是白送的，不是发现；
     - 买入持有变成「做空 BTC 九年」，得到 -1321% 这种荒谬基准。
   所以每个策略都跑 long 腿和 short 腿两份，各自有合法的对照组与基准。

3. **复利与算术两个口径都报**。引擎的 sum_pct 是等权算术求和，对「一次只
   持一个仓、前后首尾相接」的 TSMOM，真实权益是复利的。同一个 275 笔的
   book，算术 801.89% 而复利 3725%——只报一个都是误导。

4. **不凑样本、不为好看调参**。所有参数在跑之前由 scan 网格一次性声明，
   跑完不改。看完成绩再挑回看期，就是在给自己造证据。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ENGINE = ROOT / "skill" / "strategy-falsifier" / "agensi" / "scripts" / "verify.py"
OUT = HERE / "_out"
SPECS = HERE / "specs"


def run_engine(spec_name: str, candles: Path) -> dict:
    """跑一次引擎，读回 JSON。任何非零退出都当硬错误，不吞。"""
    spec_path = SPECS / spec_name
    out_md = OUT / (spec_path.stem + ".md")
    out_json = OUT / (spec_path.stem + ".json")
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ENGINE), "--spec", str(spec_path),
           "--candles", str(candles), "--out", str(out_md), "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out_json.is_file():
        raise RuntimeError(f"引擎失败 {spec_name}\nSTDOUT:{r.stdout[-2000:]}\nSTDERR:{r.stderr[-2000:]}")
    return json.loads(out_json.read_text(encoding="utf-8"))


def compound(nets: list[float]) -> float:
    """复利权益倍数。等权算术求和会把「赚了 800%」和「复利 38 倍」混为一谈。"""
    eq = 1.0
    for v in nets:
        eq *= (1.0 + v)
    return eq


def digest(d: dict, leg: str = "") -> dict:
    """把一份引擎 JSON 压缩成一行可比较的记录。

    只搬运引擎给的数字，不做二次统计——除了复利倍数这一项，它是引擎没有
    而 TSMOM 语境下必须有的口径，算法写在 compound() 里、一句能验完。
    """
    ws = d["runs"]["worst_case"]["stats"]
    bs = d["runs"]["best_case"]["stats"]
    nets = ws.get("nets", [])
    eq = compound(nets) if nets else None

    ctl = d.get("control") or {}
    bh = d.get("buy_and_hold") or {}
    # 混合多空时引擎的两条基线都失效，必须显式标出来，不能让它们冒充证据
    bh_valid = not bh.get("mixed_sides", False)

    out = {
        "name": d["spec"]["name"],
        "leg": leg,
        "symbol": d["spec"]["symbol"],
        "bars": d["window"]["bars"],
        "first": d["window"]["from"][:10],
        "last": d["window"]["to"][:10],
        "fills": d["runs"]["worst_case"]["fills"],
        "open_signals": d["runs"]["worst_case"]["open_signals"],
        "net_sum_pct": ws["sum_pct"],
        "net_mean_pct": ws["mean_pct"],
        "compound_x": eq,
        "compound_pct": (eq - 1) * 100 if eq else None,
        "win_pct": ws["win_pct"],
        "profit_factor": ws["profit_factor"],
        "max_dd_pct": ws["max_dd_pct"],
        "best_case_sum_pct": bs["sum_pct"],
        "worst_case_sum_pct": ws["sum_pct"],
        "coverage_pct": (d["runs"]["worst_case"]["fills"] / d["n_signals"] * 100) if d["n_signals"] else None,
        "control_n": ctl.get("n_random"),
        "control_side": ctl.get("side"),
        "control_diff_pct": ctl.get("diff_pct"),
        "control_ci": ctl.get("diff_ci"),
        "selectivity": ctl.get("selectivity"),
        "bh_valid": bh_valid,
        "bh_mixed_sides": bh.get("mixed_sides"),
        "bh_side": bh.get("side"),
        "bh_matched_pct": bh.get("matched_bh_pct"),
        "bh_diff_pct": bh.get("diff_pct"),
        "exposure_pct": (bh.get("exposure") or 0) * 100,
        "verdict": d["verdict"]["outcome"],
        "verdict_reason": d["verdict"]["reason"],
        "checks": [{"rule": k["rule"], "triggered": k["triggered"]} for k in d["verdict"]["checks"]],
        "cost": {str(r["fee_multiple"]): r["net_sum_pct"] for r in d["cost_sensitivity"]},
        "by_reason": d["runs"]["worst_case"]["by_reason"],
    }
    wf = d.get("walk_forward")
    if wf:
        oos = wf.get("out_sample")
        out["wf_folds"] = wf["folds"]
        out["wf_boundary"] = wf["boundary_ts"][:10]
        out["wf_is_sum_pct"] = (wf.get("in_sample") or {}).get("runs", {}).get("worst_case", {}).get("stats", {}).get("sum_pct")
        out["wf_oos_sum_pct"] = (oos or {}).get("runs", {}).get("worst_case", {}).get("stats", {}).get("sum_pct")
        out["wf_oos_fills"] = (oos or {}).get("runs", {}).get("worst_case", {}).get("fills")
    return out


def build_spec(name: str, symbol: str, entries: str, exits: str, *,
               hypothesis: str, timeout: int = 100000,
               folds: int | None = 4, grid: bool = True) -> Path:
    """生成一个 spec。所有 falsify_if 规则在这里写死——先于任何数字存在。"""
    spec = {
        "name": name,
        "hypothesis": hypothesis,
        "source": "Moskowitz/Ooi/Pedersen (2012) 时间序列动量 · 本仓库自建日线样本",
        "symbol": symbol,
        "interval": "1d",
        "venue": "futures",
        "fee_rate": 0.0004,
        "fill_model": "taker",
        "funding": False,
        "exit": {"timeout_bars": timeout, "exit_signals_path": exits},
        "signals": {"kind": "csv", "path": entries},
        "control": {"enabled": True, "n_random": 300, "seed": 42},
        "param_grid": ({"enabled": True,
                        "axes": {"take_profit": [0.05, 0.1, 0.2, 0.5],
                                 "stop_loss": [-0.05, -0.1, -0.2]}}
                       if grid else {"enabled": False}),
        "fee_multiples": [1, 2, 5],
        "falsify_if": {
            # 事前声明，四项。任何一项触发就判死。
            "worst_case_net_negative": True,          # 最坏口径不赚钱 = 死
            "net_negative_at_fee_multiple": 2,        # 双倍成本就不赚 = 死（edge 付不起 toll）
            "control_lift_ci_includes_zero": True,    # 与随机入场无显著差异 = 死
            "grid_positive_ratio_min": 0.5,           # 退出参数一半以上为正，否则是拟合
            "min_fills": 30,
        },
    }
    if folds:
        spec["walk_forward"] = {"enabled": True, "folds": folds}
    else:
        spec["walk_forward"] = {"enabled": False}
    p = SPECS / f"{name}.json"
    SPECS.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p