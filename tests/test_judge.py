"""JUDGE boundaries: opinion only, never a veto, never a prediction."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.memehunter import Token  # noqa: E402
from research.pipeline import judge as judge_mod  # noqa: E402


def _token(symbol: str = "TEST", **kw) -> Token:
    base = dict(
        mint="mint1", symbol=symbol, name=symbol, price_usd=0.001, liquidity_usd=500_000.0,
        fdv_usd=1_000_000.0, organic_score=60.0, organic_label="good", holder_count=5000,
        top_holders_pct=10.0, mint_disabled=True, freeze_disabled=True,
        chg_5m=0.0, chg_1h=0.0, chg_24h=0.0, organic_buy_5m=0.0, organic_sell_5m=0.0,
        organic_buy_1h=0.0, organic_sell_1h=0.0, net_buyers_5m=0, net_buyers_1h=30,
        vol_1h=100_000.0, vol_24h=1_000_000.0,
    )
    base.update(kw)
    return Token(**base)


CFG = {"rule_weight": 0.6, "llm_weight": 0.4, "model": "deepseek-chat", "base_url": "https://x"}


def test_blend_uses_the_rule_score_when_there_is_no_opinion() -> None:
    assert judge_mod.blend(70.0, None, CFG) == 70.0


def test_blend_mixes_by_weight() -> None:
    # 0.6*70 + 0.4*20 = 50
    assert judge_mod.blend(70.0, 20.0, CFG) == pytest.approx(50.0)


def test_no_api_key_means_no_opinion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # The operator's local key file must not leak into a unit test either.
    monkeypatch.setattr(judge_mod, "_local_override", lambda root=None: {})
    assert judge_mod.judge_token(_token(), CFG) is None


def test_bad_response_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setattr(judge_mod, "_local_override", lambda root=None: {})

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "not json at all"}}]}).encode()

    monkeypatch.setattr(judge_mod.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    out = judge_mod.judge_token(_token(), CFG)
    # A malformed answer is recorded as a failed call, not swallowed: the audit
    # trail needs to show the model was asked and answered garbage.
    assert out is not None and out["ok"] is False


def test_score_is_clamped_and_flags_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setattr(judge_mod, "_local_override", lambda root=None: {})
    payload = {"choices": [{"message": {"content": json.dumps({
        "score": 900, "flags": [f"f{i}" for i in range(20)], "reason": "r" * 500,
    })}}]}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setattr(judge_mod.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    out = judge_mod.judge_token(_token(), CFG)
    assert out is not None and out["ok"]
    assert out["score"] == 100
    assert len(out["flags"]) <= 6
    assert len(out["reason"]) <= 200


def test_every_call_is_recorded(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "j.sqlite")
    judge_mod.ensure_schema(conn)
    token = _token()
    judge_mod.record_judgment(conn, token, None, CFG, {
        "ok": True, "score": 42, "flags": ["thin"], "reason": "流动性偏薄",
        "latency_ms": 800, "raw": "{}",
    })
    row = conn.execute("SELECT symbol, score, flags, reason, ok FROM llm_judgments").fetchone()
    assert row[0] == "TEST" and row[1] == 42 and row[2] == '["thin"]' and row[4] == 1
    conn.close()


def test_agent_order_puts_judge_before_entry() -> None:
    from research.pipeline.agents import AGENT_ORDER

    names = [a.name for a in AGENT_ORDER]
    assert names == ["SEARCH", "RISK", "GMGN", "JUDGE", "FOMO", "EXIT"]
    assert names.index("JUDGE") < names.index("FOMO")
