"""Experiment arms: same universe, same moment, one variable, own ledger.

An arm is a config file plus a ledger name. The tests here guard the two ways
that gets quietly broken: an arm writing into another arm's ledger, and an arm
writing into the shared control group (C). Both would make every number
afterwards unattributable.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.pipeline.orchestrator import Pipeline  # noqa: E402

ARMS = ("arm_stop22", "arm_tp20", "arm_nojudge")


@pytest.fixture()
def arm_root(tmp_path: Path) -> Path:
    (tmp_path / "user_data").mkdir(parents=True)
    for name in ARMS:
        (tmp_path / "user_data" / f"config.{name}.json").write_text(
            json.dumps({"mode": "dry_run", "entry": {"stop_loss_pct": -12.0}}),
            encoding="utf-8",
        )
    return tmp_path


def test_an_arm_writes_its_own_ledger(arm_root: Path):
    pipe = Pipeline(arm_root, db_name="arm_stop22.sqlite")
    try:
        assert pipe.desk.db_path.name == "arm_stop22.sqlite"
        assert pipe.arm_name == "arm_stop22"
    finally:
        pipe.close()


def test_the_live_desk_has_no_arm_name(arm_root: Path):
    pipe = Pipeline(arm_root)
    try:
        assert pipe.arm_name == ""
    finally:
        pipe.close()


def test_a_run_row_records_which_arm_produced_it(arm_root: Path, monkeypatch):
    pipe = Pipeline(arm_root, db_name="arm_tp20.sqlite")
    try:
        monkeypatch.setattr("research.pipeline.agents.fetch_universe", lambda: ([], []))
        pipe.run_once()
        row = pipe.desk.conn.execute("SELECT arm FROM pipeline_runs LIMIT 1").fetchone()
        assert row[0] == "arm_tp20"
    finally:
        pipe.close()


def test_an_old_run_table_is_upgraded_with_column(arm_root: Path):
    """pipeline_runs predates `arm`; CREATE TABLE IF NOT EXISTS never upgrades."""
    (arm_root / "runtime" / "memehunter").mkdir(parents=True)
    db = arm_root / "runtime" / "memehunter" / "arm_old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " started_ms INTEGER NOT NULL, finished_ms INTEGER, version INTEGER NOT NULL,"
        " ok INTEGER NOT NULL DEFAULT 1)"
    )
    conn.commit()
    conn.close()
    pipe = Pipeline(arm_root, db_name="arm_old.sqlite")
    try:
        columns = {r[1] for r in pipe.desk.conn.execute("PRAGMA table_info(pipeline_runs)")}
        assert "arm" in columns
    finally:
        pipe.close()


@pytest.mark.parametrize("name", ARMS)
def test_every_arm_config_leaves_the_control_group_alone(name: str):
    """C is enabled by DEFAULT_CONFIG; an arm that runs it would contaminate it."""
    cfg = json.loads((ROOT / "user_data" / f"config.{name}.json").read_text(encoding="utf-8"))
    assert cfg.get("counterpart", {}).get("enabled") is False
    assert cfg["_arm"]["hypothesis"], "an arm without a hypothesis is just a hunch"
    assert cfg["_arm"]["falsify"], "an arm without a falsify rule can never be retired"


@pytest.mark.parametrize("name", ARMS)
def test_arm_configs_differ_from_the_baseline_in_a_documented_way(name: str):
    base = json.loads((ROOT / "user_data" / "config.pipeline.json").read_text(encoding="utf-8"))
    arm = json.loads((ROOT / "user_data" / f"config.{name}.json").read_text(encoding="utf-8"))
    changed = [k for k in set(arm) | set(base)
               if k not in ("_comment", "_arm", "counterpart") and arm.get(k) != base.get(k)]
    assert len(changed) <= 2, f"{name} changes too much to attribute a result: {changed}"
