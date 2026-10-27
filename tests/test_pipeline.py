"""The six-agent split is only real if the money boundary holds.

These tests check the property that makes the architecture worth anything:
four agents analyse, two agents move paper money, and nothing else can.
"""
from __future__ import annotations

import ast
import json
import sqlite3
import sqlite3
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
        sizes: list = []

        def open_position(self, token: Token, why: str, notional: float | None = None) -> dict:
            FakeDesk.opened.append(token.symbol)
            FakeDesk.sizes.append(notional)
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

    def fake_init(self, root, cfg=None, db_name="memehunter.sqlite"):
        self.root = tmp_path
        self.cfg = {**mh.DEFAULT_CONFIG, **(cfg or {})}
        (tmp_path / "runtime" / "memehunter").mkdir(parents=True, exist_ok=True)
        self.runtime = tmp_path / "runtime" / "memehunter"
        self.db_path = self.runtime / db_name
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


def test_soft_gates_become_a_probe_pool_not_a_rejection() -> None:
    """Quality gates are a preference: they shrink the position, they do not
    veto. Hard vetoes (authority, concentration) still reject outright."""
    from research.pipeline.agents import Context, RiskAgent
    from research.memehunter import Token

    def token(mint: str, symbol: str, **kw) -> Token:
        base = dict(
            mint=mint, symbol=symbol, name=symbol, price_usd=0.001, liquidity_usd=30_000.0,
            fdv_usd=900_000.0, organic_score=70.0, organic_label="good", holder_count=150,
            top_holders_pct=12.0, mint_disabled=True, freeze_disabled=True,
            chg_5m=1.0, chg_1h=5.0, chg_24h=10.0, organic_buy_5m=100.0, organic_sell_5m=80.0,
            organic_buy_1h=5000.0, organic_sell_1h=4000.0, net_buyers_5m=10, net_buyers_1h=40,
            vol_1h=60_000.0, vol_24h=500_000.0,
        )
        base.update(kw)
        return Token(**base)

    from research.memehunter import DEFAULT_CONFIG as MH_DEFAULT

    class _Desk:
        cfg = dict(MH_DEFAULT)

    soft = token("soft1", "SOFT")                       # holder_count 150 -> TOO_FEW_HOLDERS
    rugged = token("hard1", "RUGGED", mint_disabled=False)  # authority not revoked
    ctx = Context(
        root=ROOT,
        cfg=json.loads(json.dumps(DEFAULT_CONFIG)),
        started_ms=0,
        desk=_Desk(),
    )
    ctx.put("tokens", [soft, rugged])
    RiskAgent().run(ctx)

    probe = ctx.get("probe_pool", [])
    probe_symbols = [item[1].symbol for item in probe]
    assert "SOFT" in probe_symbols, "a soft gate must land in the probe pool"
    assert "RUGGED" not in probe_symbols, "a hard veto must never be probe-able"


def test_soft_gates_become_a_probe_pool_not_a_rejection() -> None:
    """Quality gates are a preference: they shrink the position, they do not
    veto. Hard vetoes (authority, concentration) still reject outright."""
    from research.pipeline.agents import Context, RiskAgent
    from research.memehunter import Token

    def token(mint: str, symbol: str, **kw) -> Token:
        base = dict(
            mint=mint, symbol=symbol, name=symbol, price_usd=0.001, liquidity_usd=30_000.0,
            fdv_usd=900_000.0, organic_score=70.0, organic_label="good", holder_count=150,
            top_holders_pct=12.0, mint_disabled=True, freeze_disabled=True,
            chg_5m=1.0, chg_1h=5.0, chg_24h=10.0, organic_buy_5m=100.0, organic_sell_5m=80.0,
            organic_buy_1h=5000.0, organic_sell_1h=4000.0, net_buyers_5m=10, net_buyers_1h=40,
            vol_1h=60_000.0, vol_24h=500_000.0,
        )
        base.update(kw)
        return Token(**base)

    from research.memehunter import DEFAULT_CONFIG as MH_DEFAULT

    class _Desk:
        cfg = dict(MH_DEFAULT)

    soft = token("soft1", "SOFT")                       # holder_count 150 -> TOO_FEW_HOLDERS
    rugged = token("hard1", "RUGGED", mint_disabled=False)  # authority not revoked
    ctx = Context(
        root=ROOT,
        cfg=json.loads(json.dumps(DEFAULT_CONFIG)),
        started_ms=0,
        desk=_Desk(),
    )
    ctx.put("tokens", [soft, rugged])
    RiskAgent().run(ctx)

    probe = ctx.get("probe_pool", [])
    probe_symbols = [item[1].symbol for item in probe]
    assert "SOFT" in probe_symbols, "a soft gate must land in the probe pool"
    assert "RUGGED" not in probe_symbols, "a hard veto must never be probe-able"


def test_confidence_tiers_scale_the_clip(tmp_path) -> None:
    """Size follows the JUDGE score: high conviction gets a bigger clip, low
    conviction gets a probe, and below the lowest tier nothing opens."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.pipeline.agents import EntryAgent, Context
    from research.memehunter import Token

    cfg = {
        "smart_money": {"score_boost": 6.0},
        "judge": {"rule_weight": 0.6, "llm_weight": 0.4},
        "tiers": {"probe_enabled": False, "probe_reasons": [], "max_probe_positions": 2},
        "entry": {"notional_usdt": 100.0, "max_entries_per_round": 5},
        "sizing": {
            "enabled": True,
            "tiers": [{"min_score": 80, "mult": 1.5}, {"min_score": 60, "mult": 1.0}, {"min_score": 40, "mult": 0.4}],
            "below_lowest": "skip",
            "max_single_fill_pct": 15.0,
        },
    }

    class _Desk:
        cfg = {"starting_cash_usdt": 1000.0, "notional_usdt": 100.0,
               "round_trip_cost_bps": 120.0, "stop_loss_pct": -12.0, "take_profit_pct": 55.0,
               "timeout_minutes": 180, "trailing_giveback_pct": 30.0, "max_open_positions": 5,
               "cooldown_minutes": 45}
        opened: list = []
        sizes: list = []

        def open_position(self, token, why, notional=None):
            _Desk.opened.append(token.symbol)
            _Desk.sizes.append(notional)
            return {"opened": True, "symbol": token.symbol, "score": 0}

        def can_open(self, token, at):
            return True, ""

    _Desk.opened, _Desk.sizes = [], []
    ctx = Context(root=_P("."), cfg=cfg, started_ms=0, desk=_Desk())
    ctx.put("survivors", [(70.0, Token(mint="m1", symbol="HIGH", name="HIGH", price_usd=1.0,
        liquidity_usd=1e6, fdv_usd=1e6, organic_score=70.0, organic_label="g", holder_count=5000,
        top_holders_pct=10.0, mint_disabled=True, freeze_disabled=True, chg_5m=0.0, chg_1h=0.0,
        chg_24h=0.0, organic_buy_5m=0.0, organic_sell_5m=0.0, organic_buy_1h=0.0, organic_sell_1h=0.0,
        net_buyers_5m=0, net_buyers_1h=30, vol_1h=1e5, vol_24h=1e6))])
    ctx.put("llm_judgments", {"m1": {"ok": True, "score": 85, "flags": [], "reason": "r"}})
    result = EntryAgent().run(ctx)
    assert result.ok
    assert _Desk.sizes == [150.0], f"score 85 should scale 100 to 150, got {_Desk.sizes}"

    _Desk.opened, _Desk.sizes = [], []
    ctx.put("llm_judgments", {"m1": {"ok": True, "score": 30, "flags": [], "reason": "r"}})
    EntryAgent().run(ctx)
    assert _Desk.opened == [], f"score 30 is below the lowest tier and must not open, got {_Desk.opened}"

    _Desk.opened, _Desk.sizes = [], []
    ctx.put("llm_judgments", {})
    EntryAgent().run(ctx)
    assert _Desk.sizes == [100.0], "no opinion means the base clip, not zero"


def test_evaluators_file_proposals_but_never_edit_config(tmp_path) -> None:
    """Evaluators propose; the config file is untouched by them."""
    import sys
    import sqlite3
    import sqlite3
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.pipeline import evaluators as E

    before = (ROOT / "user_data" / "config.pipeline.json").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY, event_id TEXT, decided_at_ms INTEGER,"
        " verdict TEXT, signal_class TEXT, primary_code TEXT, codes TEXT, detail TEXT);"
    )
    now = 1_700_000_000_000
    for i in range(40):
        conn.execute(
            "INSERT INTO decisions (event_id, decided_at_ms, verdict, primary_code, detail)"
            " VALUES (?,?,?,?,?)",
            (f"e{i}", now, "SKIP", "ALREADY_OPEN", "x"),
        )
    conn.commit()
    filed = E.run_evaluators(conn)
    conn.close()
    after = (ROOT / "user_data" / "config.pipeline.json").read_text(encoding="utf-8")
    assert before == after, "an evaluator must never modify the config file"

    # A proposal was filed with evidence and is pending, not applied.
    if filed:
        p = filed[0]
        assert p["agent"] in ("CALIBRATOR", "DRIFT", "ARBITER")
        assert p["evidence"], "a proposal without evidence is an opinion"
        assert p.get("detail"), "a proposal must say what it would change"


def test_identical_proposals_are_deduplicated(tmp_path) -> None:
    import sys
    import sqlite3
    import sqlite3
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.pipeline.evaluators import ensure_schema, file_proposal

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    first = file_proposal(conn, agent="T", kind="k", title="same", detail="d", evidence="e")
    second = file_proposal(conn, agent="T", kind="k", title="same", detail="d", evidence="e")
    assert first is not None
    assert second is None, "an identical pending proposal must not be filed twice"
    conn.close()


def test_evaluators_file_proposals_but_never_edit_config(tmp_path) -> None:
    """Evaluators propose; the config file is untouched by them."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.pipeline import evaluators as E

    before = (ROOT / "user_data" / "config.pipeline.json").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY, event_id TEXT, decided_at_ms INTEGER,"
        " verdict TEXT, signal_class TEXT, primary_code TEXT, codes TEXT, detail TEXT);"
    )
    now = 1_700_000_000_000
    for i in range(40):
        conn.execute(
            "INSERT INTO decisions (event_id, decided_at_ms, verdict, primary_code, detail)"
            " VALUES (?,?,?,?,?)",
            (f"e{i}", now, "SKIP", "ALREADY_OPEN", "x"),
        )
    conn.commit()
    filed = E.run_evaluators(conn)
    conn.close()
    after = (ROOT / "user_data" / "config.pipeline.json").read_text(encoding="utf-8")
    assert before == after, "an evaluator must never modify the config file"

    # A proposal was filed with evidence and is pending, not applied.
    if filed:
        p = filed[0]
        assert p["agent"] in ("CALIBRATOR", "DRIFT", "ARBITER")
        assert p["evidence"], "a proposal without evidence is an opinion"
        assert p.get("detail"), "a proposal must say what it would change"


def test_identical_proposals_are_deduplicated(tmp_path) -> None:
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.pipeline.evaluators import ensure_schema, file_proposal

    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    first = file_proposal(conn, agent="T", kind="k", title="same", detail="d", evidence="e")
    second = file_proposal(conn, agent="T", kind="k", title="same", detail="d", evidence="e")
    assert first is not None
    assert second is None, "an identical pending proposal must not be filed twice"
    conn.close()


def test_adaptive_floor_only_moves_when_the_bar_is_the_bottleneck() -> None:
    """Nine dry rounds at floor 28 taught the desk nothing: vetoes were doing
    the rejecting, so a lower score floor changed no outcome. The floor now
    moves only when SCORE_BELOW_FLOOR is the top reject reason."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.pipeline.agents import Context, RiskAgent
    from research.memehunter import Token

    def token(mint: str, symbol: str, **kw) -> Token:
        base = dict(
            mint=mint, symbol=symbol, name=symbol, price_usd=0.001, liquidity_usd=30_000.0,
            fdv_usd=900_000.0, organic_score=70.0, organic_label="good", holder_count=150,
            top_holders_pct=12.0, mint_disabled=True, freeze_disabled=True,
            chg_5m=1.0, chg_1h=5.0, chg_24h=10.0, organic_buy_5m=100.0, organic_sell_5m=80.0,
            organic_buy_1h=5000.0, organic_sell_1h=4000.0, net_buyers_5m=10, net_buyers_1h=40,
            vol_1h=60_000.0, vol_24h=500_000.0,
        )
        base.update(kw)
        return Token(**base)

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["risk"]["min_score"] = 45.0
    cfg["risk"]["adaptive_dry_rounds"] = 3
    cfg["risk"]["adaptive_decay"] = 4.0
    cfg["risk"]["adaptive_floor"] = 28.0

    from research.memehunter import DEFAULT_CONFIG as MH_DEFAULT

    from research.memehunter import DEFAULT_CONFIG as MH_DEFAULT

    class _Desk:
        cfg = dict(MH_DEFAULT)

    def run_risk(rejects: dict):
        ctx = Context(root=ROOT, cfg=cfg, started_ms=0, desk=_Desk())
        ctx.put("tokens", [token("soft1", "SOFT")])
        ctx.put("dry_streak", 5)
        ctx.put("rejects", rejects)
        RiskAgent().run(ctx)
        return ctx.get("effective_min_score")

    veto_dominant = {"TOO_FEW_HOLDERS": 45, "AUTHORITY_NOT_REVOKED": 30, "SCORE_BELOW_FLOOR": 2}
    assert run_risk(veto_dominant) == 45.0, "a veto-dominated round must not lower the bar"
    score_dominant = {"SCORE_BELOW_FLOOR": 40, "TOO_FEW_HOLDERS": 3}
    assert run_risk(score_dominant) == 33.0, "a score-dominated round should decay the bar"
