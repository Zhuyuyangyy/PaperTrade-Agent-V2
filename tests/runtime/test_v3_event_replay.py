"""V3 固定事件序列回放：每一步动作都必须真实改变账本，且能从日志追溯。

测试的是"决策 → 执行 → 日志"这条链路，不是策略收益。
"""
from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

from research.decision.position import PositionDecision
from research.runtime.v2_engine import V2Config

from .test_v2_engine import NOW, CountingProvider, _engine


class ScriptedManager:
    """按固定顺序给出动作；前面的动作必须真的发生，后面的才有意义。"""

    name = "scripted"
    version = "v1"

    def __init__(self, r: D):
        self.r = r
        self.entry: D | None = None
        self.calls: list[str] = []

    def decide(self, snap) -> PositionDecision:
        # 第一次调用时记住实际成交均价（不是信号价）
        if self.entry is None:
            self.entry = snap.entry_price
        idx = len(self.calls)
        self.calls.append(snap.side)
        mark = snap.mark_price
        common = dict(provider=self.name, version=self.version,
                      data_as_of_ms=snap.data_as_of_ms)
        if idx == 0:
            return PositionDecision(action="HOLD",
                                    rationale="noise, keep", **common)
        if idx == 1:
            return PositionDecision(
                action="MOVE_STOP", new_stop=mark - self.r * D("0.3"),
                rationale="trail stop after 0.5R", confidence=D("0.8"),
                invalid_if=("1h trend reversal",), **common)
        if idx == 2:
            return PositionDecision(
                action="CLOSE_PARTIAL", fraction=D("0.5"),
                rationale="take part off at 1.5R", confidence=D("0.9"),
                invalid_if=("price back below entry",), **common)
        return PositionDecision(
            action="CLOSE_ALL", rationale="thesis invalidated",
            confidence=D("0.9"), invalid_if=(), **common)


def _managed(root: Path, name: str = "deepseek_5m") -> list[dict]:
    p = root / "v2" / f"{name}_decisions.jsonl"
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        e = (json.loads(line).get("entry") or {})
        if e.get("managed"):
            out.append(e["managed"])
    return out


def test_fixed_event_sequence_updates_ledger_and_log(tmp_path: Path) -> None:
    """HOLD → MOVE_STOP → CLOSE_PARTIAL → CLOSE_ALL 全链路。"""
    # 只让 deepseek_5m 这本账启用管理，其余账不消费同一个 stub 的动作序列
    e = _engine(tmp_path, deepseek=CountingProvider(),
                config=V2Config(position_manager="off", cooldown_bars=0))
    e.experiment = True
    e.book_configs = {"deepseek_5m": V2Config(position_manager="ai", cooldown_bars=0)}
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    entry, stop = D(pos["entry_price"]), D(pos["stop_price"])
    r = abs(entry - stop)
    sign = D("1") if pos["side"] == "LONG" else D("-1")
    mgr = ScriptedManager(r)
    e.position_ai = mgr

    # 一次一个事件，推进一根 5m
    e.feed.price_value = float(entry + sign * D("0.05") * r)   # 小噪声
    e.feed.seeded_until_ms = NOW + 1 * 300_000
    e.on_5m_close(NOW + 1 * 300_000)
    assert len(e.books["deepseek_5m"].open_positions()) == 1

    e.feed.price_value = float(entry + sign * D("0.5") * r)    # 0.5R

    e.feed.seeded_until_ms = NOW + 2 * 300_000
    e.on_5m_close(NOW + 2 * 300_000)
    moved = e.books["deepseek_5m"].open_positions()[0]
    # 多头止损上移 / 空头止损下移
    if pos["side"] == "LONG":
        assert D(moved["stop_price"]) > stop
    else:
        assert D(moved["stop_price"]) < stop

    e.feed.price_value = float(entry + sign * D("1.5") * r)    # 1.5R

    e.feed.seeded_until_ms = NOW + 3 * 300_000
    e.on_5m_close(NOW + 3 * 300_000)
    partial = e.books["deepseek_5m"].open_positions()
    assert len(partial) == 1 and D(partial[0]["qty"]) < D(pos["qty"])

    e.feed.price_value = float(entry + sign * D("2.0") * r)    # 反转化

    e.feed.seeded_until_ms = NOW + 4 * 300_000
    e.on_5m_close(NOW + 4 * 300_000)
    assert not e.books["deepseek_5m"].open_positions()

    actions = [m["action"] for m in _managed(tmp_path)]
    assert actions == ["HOLD", "MOVE_STOP", "CLOSE_PARTIAL", "CLOSE_ALL"], actions
    for m in _managed(tmp_path):
        assert m["decision"]["data_as_of_ms"] > 0
        assert m["snapshot"]["entry_price"] == str(entry)
        assert m["executed"] is True


def test_add_to_winner_is_refused_even_when_profitable(tmp_path: Path) -> None:
    """加仓是独立实验，不能混进动态退出对照。"""
    class AddOnly:
        def decide(self, snap):
            return PositionDecision("add", "v1", "ADD_TO_WINNER", "looks strong",
                                    D("0.9"), notional=D("50"),
                                    data_as_of_ms=snap.data_as_of_ms)

    # 只让 deepseek_5m 这本账启用管理，其余账不消费同一个 stub 的动作序列
    e = _engine(tmp_path, deepseek=CountingProvider(),
                config=V2Config(position_manager="off", cooldown_bars=0))
    e.experiment = True
    e.book_configs = {"deepseek_5m": V2Config(position_manager="ai", cooldown_bars=0)}
    e.on_5m_close(NOW)
    pos = e.books["deepseek_5m"].open_positions()[0]
    r = abs(D(pos["entry_price"]) - D(pos["stop_price"]))
    sign = D("1") if pos["side"] == "LONG" else D("-1")
    e.position_ai = AddOnly()
    e.feed.price_value = float(D(pos["entry_price"]) + sign * r * 5)
    e.feed.seeded_until_ms = NOW + 300_000
    e.on_5m_close(NOW + 300_000)
    actions = _managed(tmp_path)
    assert actions[-1]["action"] == "ADD_TO_WINNER"
    assert actions[-1]["executed"] is False
    assert "禁用" in actions[-1]["refused"]
    assert len(e.books["deepseek_5m"].open_positions()) == 1
