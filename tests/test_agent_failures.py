"""A swallowed agent error must surface as that agent's verdict, not as a
second ValueError from unpacking an error dict as a tuple.

Every agent that unpacks a multi-part result from `_timed` used to raise
"not enough values to unpack (expected N, got 1)" whenever its work function
failed -- which hid the real cause and left ctx half-populated, so the next
agent ran on an empty hand and reported cheerful zeroes. Run 839 on
2026-10-27 lost its entire pool that way.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.pipeline.agents import (  # noqa: E402
    DEFAULT_CONFIG,
    Context,
    EntryAgent,
    RiskAgent,
    SearchAgent,
    SmartMoneyAgent,
)

BOOM = {"error": "HTTPError: 429 concurrency reached"}


@pytest.fixture()
def ctx(tmp_path: Path) -> Context:
    desk = SimpleNamespace(conn=None, cfg=copy.deepcopy(DEFAULT_CONFIG))
    return Context(root=tmp_path, cfg=copy.deepcopy(DEFAULT_CONFIG), started_ms=0, desk=desk)


@pytest.mark.parametrize(
    "agent_cls",
    [SearchAgent, RiskAgent, SmartMoneyAgent, EntryAgent],
)
def test_swallowed_error_becomes_the_agents_own_failure(agent_cls, ctx, monkeypatch):
    agent = agent_cls()
    monkeypatch.setattr(
        type(agent), "_timed", lambda self, ctx, fn: (dict(BOOM), 7, False)
    )
    result = agent.run(ctx)  # must not raise ValueError
    assert result.ok is False
    assert "429 concurrency" in result.summary
    assert result.detail.get("failed") is True


def test_search_failure_leaves_an_empty_pool_not_a_broken_one(ctx, monkeypatch):
    monkeypatch.setattr(
        SearchAgent, "_timed", lambda self, ctx, fn: (dict(BOOM), 7, False)
    )
    SearchAgent().run(ctx)
    assert ctx.get("tokens") == []
    assert ctx.get("source_errors") == []


def test_risk_failure_leaves_downstream_agents_a_well_formed_hand(ctx, monkeypatch):
    monkeypatch.setattr(
        RiskAgent, "_timed", lambda self, ctx, fn: (dict(BOOM), 7, False)
    )
    RiskAgent().run(ctx)
    assert ctx.get("survivors") == []
    assert ctx.get("rejects") == {}
    assert ctx.get("probe_pool") == []
    assert ctx.get("effective_min_score") == float(ctx.cfg["risk"]["min_score"])
