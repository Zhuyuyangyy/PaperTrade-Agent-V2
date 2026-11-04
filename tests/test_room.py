"""The multi-agent room must never show a number the plain dashboard refuses."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.room import build_room_html


def _state(html: str) -> dict:
    match = re.search(r'<script id="state-data" type="application/json">(.*?)</script>', html, re.S)
    assert match, "room page must embed the state payload"
    return json.loads(match.group(1))


def test_room_page_embeds_the_same_state_as_the_dashboard() -> None:
    now = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
    html = build_room_html(now)
    state = _state(html)
    for key in ("portfolio", "accounts", "copywatch", "memehunter"):
        assert key in state
    # The invariant is equality with the dashboard's own state, not a magic
    # number: equities depend on which ledgers exist on the machine. The room
    # page must embed what the dashboard would show, byte for byte.
    from scripts.dashboard import build_state

    live = build_state(now)
    assert state["portfolio"] == live["portfolio"]
    assert state["copywatch"] == live["copywatch"]
    assert state["memehunter"] == live["memehunter"]
    # The opening balance, though, is config-declared: a fresh clone with no
    # runtime/ at all must still report the full desk (this caught CI showing
    # 2500 because E and C had no ledger yet).
    assert state["portfolio"]["starting_total_usdt"] == pytest.approx(4500.0)


def test_room_page_has_no_external_runtime_dependencies() -> None:
    html = build_room_html()
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html
    assert "<script src=" not in html
    assert "<link " not in html


def test_room_page_renders_placeholders_instead_of_fake_numbers() -> None:
    html = build_room_html()
    # Every element the JS writes into must exist, otherwise the page silently
    # shows an empty box instead of an honest "--".
    for element_id in (
        "k-eq", "k-pnl", "k-eng", "k-win", "ladder", "room", "tape", "pos", "funnel",
        "chart", "legend", "abc",
    ):
        assert f'id="{element_id}"' in html


def test_room_portfolio_total_only_exists_when_every_ledger_can_be_valued() -> None:
    state = _state(build_room_html())
    portfolio = state["portfolio"]
    parts = [
        portfolio["freqtrade"]["equity_usdt"],
        portfolio["copywatch"]["equity_usdt"],
        portfolio["memehunter"]["equity_usdt"],
        portfolio["counterpart"]["equity_usdt"],
    ]
    if any(part is None for part in parts):
        assert portfolio["total_usdt"] is None
        assert portfolio["pnl_usdt"] is None
    else:
        assert portfolio["total_usdt"] == pytest.approx(sum(parts), abs=1e-6)


# ---------------------------------------------------------------- trading floor
from scripts.room import build_floor_html


def test_floor_page_embeds_the_same_state_as_the_room() -> None:
    now = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
    html = build_floor_html(now)
    state = _state(html)
    assert state["portfolio"] == build_room_state(now)["portfolio"]
    assert state["memehunter"] == build_room_state(now)["memehunter"]
    # the template placeholder must be gone, otherwise the page silently
    # falls back to demo numbers while looking live
    assert "__STATE_DATA__" not in html


def build_room_state(now: datetime) -> dict:
    from scripts.dashboard import build_state

    return build_state(now)


def test_floor_page_is_offline_and_self_contained() -> None:
    html = build_floor_html()
    assert "https://" not in html
    assert "<script src=" not in html
    assert "<link " not in html
    # no canvas library, no isometric helper: one file, stdlib only
    assert "three" not in html.lower()
    assert "<canvas" in html


def test_floor_page_wires_every_agent_and_hud_node() -> None:
    html = build_floor_html()
    for agent in ("SEARCH", "RISK", "GMGN", "JUDGE", "GROK", "FOMO", "EXIT"):
        assert f"key:'{agent}'" in html
    for element_id in ("scene", "k-eq", "k-pnl", "k-eng", "k-win", "roster", "feed", "inspect"):
        assert f'id="{element_id}"' in html


def test_floor_page_never_invents_numbers_when_a_step_is_missing() -> None:
    html = build_floor_html()
    # brief() must fall back to a word, not to a made-up figure
    assert "待研判" in html and "待调度" in html
