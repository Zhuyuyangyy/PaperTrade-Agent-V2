from __future__ import annotations

import json
from pathlib import Path
import shutil

from research.tradingagents_bridge import normalize_decision
from scripts.safety_check import verify_configs
from scripts.report import summarize

ROOT = Path(__file__).resolve().parents[1]


def test_exact_tradingagents_action_only():
    assert normalize_decision("BUY") == "BUY"
    assert normalize_decision({"action": "hold"}) == "HOLD"
    assert normalize_decision("We should not BUY") is None
    assert normalize_decision({"summary": "BUY"}) is None
    assert normalize_decision({"action": "SELL; execute immediately"}) is None


def test_safety_preflight_passes_and_catches_edit(tmp_path, monkeypatch):
    for name in ("baseline", "hybrid", "fast", "copywatch"):
        dst = tmp_path / "user_data"
        dst.mkdir(exist_ok=True)
        shutil.copy(ROOT / "user_data" / f"config.{name}.json", dst)
    strategies = tmp_path / "user_data" / "strategies"
    strategies.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "user_data" / "strategies" / "PaperBaseline.py", strategies)
    shutil.copy(ROOT / "user_data" / "strategies" / "PaperFast.py", strategies)
    copywatch_src = tmp_path / "research" / "copywatch"
    copywatch_src.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "research" / "copywatch" / "sources.py", copywatch_src)
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    monkeypatch.delenv("FREQTRADE__EXCHANGE__KEY", raising=False)
    assert verify_configs(tmp_path) == []
    path = dst / "config.hybrid.json"
    data = json.loads(path.read_text())
    data["dry_run"] = False
    data["exchange"]["key"] = "DO_NOT_USE"
    path.write_text(json.dumps(data))
    issues = verify_configs(tmp_path)
    assert len(issues) >= 2
    assert any("dry_run" in x for x in issues)
    assert any("credentials" in x for x in issues)


def test_repo_contains_no_listening_port_or_credential_values():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "ports:" not in compose
    for cfg in ("baseline", "hybrid", "fast"):
        data = json.loads((ROOT / "user_data" / f"config.{cfg}.json").read_text())
        assert data["exchange"]["key"] == data["exchange"]["secret"] == ""
        assert data["api_server"]["enabled"] is False


def test_read_only_report_no_db_returns_none(tmp_path):
    assert summarize(tmp_path / "not_there.db") is None
