#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本与滑点记录规范：任何模拟 / 纸面 / 影子产物都必须带上这一块。

问题不是"成本算多少"，是"事后没人知道当时算的是多少"。
所以费率、滑点、敏感性档位和数据指纹要跟着产物一起落盘，而不是活在某个函数的默认参数里。

两个市场模板（Binance 公开费率，VIP0、无 BNB 抵扣）：
    现货    taker 10bps/边  + 滑点 1bps  = 11bps/边
    永续    taker  5bps/边  + 滑点 2bps  =  7bps/边（模型一律取 12bps/边，留 5bps 余量）

敏感性档位：6 / 12 / 18 / 24 bps 每边（覆盖"做市返佣"到"高滑点"的整段区间）。
"""

from __future__ import annotations

SPOT = {
    "market": "spot",
    "taker_bps_per_side": 10.0,
    "slippage_bps_per_side": 1.0,
    "model_bps_per_side": 11.0,
    "note": "Phase 3 用的就是这个口径",
}

PERP = {
    "market": "perp",
    "taker_bps_per_side": 5.0,
    "slippage_bps_per_side": 2.0,
    "model_bps_per_side": 12.0,
    "note": "实际 taker 5 + 滑点 2 = 7，模型取 12 留余量；Phase 2/2B 的 24bps 往返即此口径",
}

# 每边 bps。6 = 做市级别，12 = 默认，18/24 = 压力档
SCENARIOS = (6.0, 12.0, 18.0, 24.0)


def metadata(template: dict, extra: dict | None = None) -> dict:
    """产物里要附带的标准块。"""
    out = {
        "cost_model": {
            "taker_bps_per_side": template["taker_bps_per_side"],
            "slippage_bps_per_side": template["slippage_bps_per_side"],
            "model_bps_per_side": template["model_bps_per_side"],
            "note": template["note"],
        },
        "sensitivity_bps_per_side": list(SCENARIOS),
        "formula": "cost = sum(|w_new - w_old|) * bps_per_side / 10000",
    }
    if extra:
        out.update(extra)
    return out


def scenario_key(bps: float) -> str:
    return f"{bps:g}"
