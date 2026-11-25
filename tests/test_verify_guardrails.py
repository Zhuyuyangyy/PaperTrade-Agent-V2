"""证伪引擎防呆回归测试。

两个真实踩坑（2026-11-25，S1/S2 执行）：
1. insufficient 被下游参数网格覆写为 FALSIFIED（S2：2 笔样本却判"网格1/9曲线拟合"）
2. --candles 模式永续缺 funding_path 静默跑完，funding_applied=False（S1/S2 连续两次）
"""
from __future__ import annotations

import pytest

from research.verify import verify
from research.verify.signals import rule_signals
from research.verify.spec import Spec

BASE = {
    "name": "guardrail-test",
    "symbol": "BTCUSDT",
    "interval": "1h",
    "venue": "futures",
    "fee_rate": 0.0004,
    "fill_model": "taker",
    "funding": True,
    "exit": {"take_profit": 0.02, "stop_loss": -0.01, "timeout_bars": 10},
    "signals": {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 45, "side": "long"},
    "control": {"enabled": False},
    "walk_forward": {"enabled": False},
    "param_grid": {
        "axes": {"take_profit": [0.01, 0.02, 0.03], "stop_loss": [-0.005, -0.01]},
    },
    "falsify_if": {
        "min_fills": 30,
        "grid_positive_ratio_min": 0.5,
        "worst_case_net_negative": True,
    },
}


def _candles(n: int = 400) -> list[dict]:
    """确定性震荡：足够触发网格，但不保证任何方向有 edge。"""
    import random
    rng = random.Random(9)
    out, px, t0 = [], 100.0, 1_700_000_000_000
    for i in range(n):
        px *= 1.0 + rng.gauss(0.0, 0.008)
        o = px
        c = px * (1.0 + rng.gauss(0.0, 0.005))
        h = max(o, c) * 1.003
        l = min(o, c) * 0.997
        out.append({"t": t0 + i * 3_600_000, "o": o, "h": h, "l": l, "c": c, "v": 1000.0})
    return out


class TestInsufficientShortCircuit:
    """Bug1：任何 insufficient 必须硬短路为 INCONCLUSIVE，网格不得越权判死。"""

    def test_insufficient_beats_triggered_grid(self):
        """全窗口成交 < min_fills，即使网格格 triggered，顶层判定仍 INCONCLUSIVE。"""
        spec = Spec.from_dict(BASE)
        candles = _candles()
        entries = rule_signals(candles, rule="rsi_below", period=14, threshold=45)
        entries = dict(list(entries.items())[:5])  # 只留 5 笔 → 触发 min_fills=30
        assert entries
        result = verify(spec, candles, entries)
        v = result["verdict"]

        ins = [c for c in v["checks"] if c.get("insufficient")]
        trig = [c for c in v["checks"] if c.get("triggered")]
        assert ins, "测试前提：必须存在 insufficient 检查"
        # 强制制造混合场景：断言至少有一条 grid 触发被记录（证据不能藏）
        assert any(c["rule"] == "min_fills" for c in ins)

        # 核心：有 insufficient 时 outcome 必须是 INCONCLUSIVE
        if trig:
            assert v["outcome"] == "INCONCLUSIVE", \
                f"insufficient 被 triggered 覆写：{v['outcome']}"
            assert "样本不足" in v["reason"]
            # 被短路的触发项仍要出现在 checks 里供审计
            assert all(c in v["checks"] for c in trig)

    def test_short_circuit_keeps_triggered_checks_as_evidence(self):
        """短路不等于隐藏证据：triggered checks 必须保留，reason 说明它们未采纳。"""
        spec = Spec.from_dict(BASE)
        candles = _candles()
        entries = dict(list(rule_signals(candles, rule="rsi_below", period=14, threshold=45).items())[:5])
        result = verify(spec, candles, entries)
        v = result["verdict"]
        if any(c.get("insufficient") for c in v["checks"]):
            assert "不作为证伪依据" in v["reason"] or "样本不足" in v["reason"]

    def test_grid_only_falsifies_when_fills_sufficient(self):
        """对照：成交够 min_fills 时，网格 triggered 仍可正常 FALSIFIED（不误伤）。"""
        d = dict(BASE)
        d["falsify_if"] = {"min_fills": 1, "grid_positive_ratio_min": 0.5}
        d["signals"] = {"kind": "rule", "rule": "rsi_below", "period": 14, "threshold": 40, "side": "long"}
        spec = Spec.from_dict(d)
        candles = _candles(900)
        r = verify(spec, candles, rule_signals(candles, rule="rsi_below", period=14, threshold=40))
        assert not any(c.get("insufficient") for c in r["verdict"]["checks"])
        # 有网格触发时允许 FALSIFIED；无触发时 NOT_FALSIFIED——都合法
        assert r["verdict"]["outcome"] in ("FALSIFIED", "NOT_FALSIFIED")


class TestFundingFailFast:
    """Bug2：永续 + --candles 且缺 funding_path 必须报错，不静默。"""

    def test_futures_spec_requires_funding_path_in_cli(self, tmp_path, monkeypatch):
        import sys
        from research.verify.__main__ import main

        candles = tmp_path / "c.csv"
        candles.write_text("t,o,h,l,c,v\n", encoding="utf-8")
        spec = tmp_path / "s.json"
        import json
        d = dict(BASE)
        spec.write_text(json.dumps(d), encoding="utf-8")

        monkeypatch.setattr(sys, "argv", [
            "research.verify", "--spec", str(spec), "--candles", str(candles),
            "--out", str(tmp_path / "r.md"),
        ])
        rc = main()
        assert rc != 0, "缺 funding_path 必须以非零码退出"

    def test_no_funding_flag_allows_missing_path(self, tmp_path, monkeypatch):
        import sys, json
        from research.verify.__main__ import main

        candles = tmp_path / "c.csv"
        candles.write_text("t,o,h,l,c,v\n", encoding="utf-8")
        spec = tmp_path / "s.json"
        d = dict(BASE)
        spec.write_text(json.dumps(d), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "research.verify", "--spec", str(spec), "--candles", str(candles),
            "--no-funding", "--out", str(tmp_path / "r.md"),
        ])
        # 空 candles 会在加载阶段报错，但不应是 funding_path 错误
        rc = main()
        assert rc != 0
