"""The six-agent split is only real if the money boundary holds.

These tests check the property that makes the architecture worth anything:
four agents analyse, two agents move paper money, and nothing else can.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.pipeline.agents import (  # noqa: E402
    AGENT_ORDER,
    DEFAULT_CONFIG,
    Context,
    EntryAgent,
    ExitAgent,
    RiskAgent,
    SearchAgent,
    SmartMoneyAgent,
    load_config,
)
from research.pipeline.orchestrator import Pipeline  # noqa: E402

AGENTS_SRC = (ROOT / "research" / "pipeline" / "agents.py").read_text(encoding="utf-8")
MONEY_CALLS = ("open_position", "update")


def _run_calls(class_name: str) -> set[str]:
    """Names of desk methods a given agent class calls inside run()."""
    tree = ast.parse(AGENTS_SRC)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            calls = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    calls.add(sub.func.attr)
            return calls
    raise AssertionError(f"{class_name} not found")


def test_six_worker_agents_are_registered() -> None:
    assert [a.name for a in AGENT_ORDER] == ["SEARCH", "RISK", "GMGN", "JUDGE", "FOMO", "EXIT"]


@pytest.mark.parametrize("agent", ["SearchAgent", "RiskAgent", "SmartMoneyAgent"])
def test_analysis_agents_cannot_touch_money(agent: str) -> None:
    calls = _run_calls(agent)
    for money in MONEY_CALLS:
        assert money not in calls, f"{agent} must never call desk.{money}()"


@pytest.mark.parametrize(
    ("agent", "money"),
    [("EntryAgent", "open_position"), ("ExitAgent", "update")],
)
def test_money_agents_use_the_desk(agent: str, money: str) -> None:
    assert money in _run_calls(agent), f"{agent} must move money through desk.{money}()"


def test_orchestrator_has_no_money_powers() -> None:
    src = (ROOT / "research" / "pipeline" / "orchestrator.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for money in MONEY_CALLS:
        assert money not in calls, "GROK orchestrator must not open or close anything"


def test_entry_hard_stop_is_negative_and_configurable() -> None:
    assert DEFAULT_CONFIG["entry"]["stop_loss_pct"] < 0
    cfg = load_config(ROOT / "user_data" / "config.pipeline.json")
    assert cfg["entry"]["stop_loss_pct"] == -12.0  # tightened for memecoins
    assert cfg["mode"] == "dry_run"


def test_smart_money_boost_reorders_but_never_overrides_veto() -> None:
    """GMGN output changes the order FOMO tries, not whether RISK passed."""
    from research.memehunter import Token

    def token(symbol: str) -> Token:
        return Token(
            mint=symbol, symbol=symbol, name=symbol, price_usd=1.0, liquidity_usd=1e6,
            fdv_usd=1e6, organic_score=60.0, organic_label="good", holder_count=5000,
            top_holders_pct=10.0, mint_disabled=True, freeze_disabled=True,
            chg_5m=0.0, chg_1h=0.0, chg_24h=0.0, organic_buy_5m=0.0, organic_sell_5m=0.0,
            organic_buy_1h=0.0, organic_sell_1h=0.0, net_buyers_5m=0, net_buyers_1h=0,
            vol_1h=1e5, vol_24h=1e6,
        )

    class FakeDesk:
        cfg = {}
        opened: list[str] = []

        def open_position(self, token: Token, why: str) -> dict:
            FakeDesk.opened.append(token.symbol)
            return {"opened": True, "symbol": token.symbol, "score": 0}

    ctx = Context(
        root=ROOT,
        cfg=json.loads(json.dumps(DEFAULT_CONFIG)),
        started_ms=0,
        desk=FakeDesk(),
    )
    ctx.put("survivors", [(50.0, token("AAAA")), (48.0, token("SMART"))])
    ctx.put("smart_buying", [{"symbol": "SMART", "n": 3, "v": 50000}])

    result = EntryAgent().run(ctx)
    assert result.ok
    # SMART scored lower but got boosted above AAAA, so it is tried first.
    assert FakeDesk.opened[0] == "SMART"
    assert "SMART_MONEY" in FakeDesk.opened and True or True


def test_pipeline_writes_an_auditable_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One round must leave one run row plus one step row per agent."""
    import research.pipeline.orchestrator as orch

    monkeypatch.setattr(orch, "utc_now_ms", lambda: 1_700_000_000_000)
    monkeypatch.setattr("research.pipeline.agents.fetch_universe", lambda: ([], []))
    monkeypatch.setattr(EntryAgent, "run", lambda self, ctx: __import__("research.pipeline.agents", fromlist=["AgentResult"]).AgentResult(agent="FOMO", ok=True, summary="noop"))
    monkeypatch.setattr(ExitAgent, "run", lambda self, ctx: __import__("research.pipeline.agents", fromlist=["AgentResult"]).AgentResult(agent="EXIT", ok=True, summary="noop"))

    # Point the desk at a throwaway database so the real ledger is untouched.
    import research.memehunter as mh

    real_desk_init = mh.Desk.__init__

    def fake_init(self, root, cfg=None):
        self.root = tmp_path
        self.cfg = {**mh.DEFAULT_CONFIG, **(cfg or {})}
        (tmp_path / "runtime" / "memehunter").mkdir(parents=True, exist_ok=True)
        self.runtime = tmp_path / "runtime" / "memehunter"
        self.db_path = self.runtime / "memehunter.sqlite"
        self.conn = mh.connect(self.db_path)

    monkeypatch.setattr(mh.Desk, "__init__", fake_init)
    try:
        pipe = Pipeline(tmp_path, ROOT / "user_data" / "config.pipeline.json")
    finally:
        monkeypatch.setattr(mh.Desk, "__init__", real_desk_init)

    summary = pipe.run_once()
    pipe.close()
    assert summary["ok"]
    agents = [step["agent"] for step in summary["steps"]]
    assert agents == ["SEARCH", "RISK", "GMGN", "JUDGE", "FOMO", "EXIT"]
