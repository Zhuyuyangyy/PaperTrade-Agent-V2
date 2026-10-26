from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import zipfile

from scripts.export_experiment import export_experiment


def test_export_is_credential_redacted_and_contains_hash_manifest(tmp_path):
    (tmp_path / "user_data" / "strategies").mkdir(parents=True)
    (tmp_path / "user_data" / "signals").mkdir(parents=True)
    (tmp_path / "user_data" / "marks").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "runtime" / "baseline").mkdir(parents=True)
    (tmp_path / "runtime" / "hybrid").mkdir(parents=True)
    (tmp_path / "runtime" / "fast").mkdir(parents=True)
    (tmp_path / "docker-compose.yml").write_text("image: freqtradeorg/freqtrade:2026.9\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "docs" / "EXPERIMENT.md").write_text("protocol", encoding="utf-8")
    (tmp_path / "docs" / "SECURITY.md").write_text("security", encoding="utf-8")
    for name in ("PaperBaseline.py", "PaperHybrid.py", "PaperFast.py", "signal_gate.py"):
        (tmp_path / "user_data" / "strategies" / name).write_text(f"# {name}\n", encoding="utf-8")
    for kind in ("baseline", "hybrid", "fast"):
        cfg = {
            "dry_run_wallet": 1000,
            "exchange": {"key": "secret-key", "secret": "secret-value", "password": "secret-pass"},
            "telegram": {"enabled": False, "token": "telegram-secret", "chat_id": "private-chat"},
            "api_server": {"enabled": False, "jwt_secret_key": "jwt-secret", "password": "api-secret", "username": "private-user"},
        }
        (tmp_path / "user_data" / f"config.{kind}.json").write_text(json.dumps(cfg), encoding="utf-8")
        db = tmp_path / "runtime" / kind / f"{kind}.dryrun.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trades (is_open INTEGER, close_profit_abs REAL)")
        conn.commit()
        conn.close()
    (tmp_path / "data" / "equity_snapshots.jsonl").write_text('{"kind":"baseline"}\n', encoding="utf-8")
    (tmp_path / "user_data" / "marks" / "BTC_USDT.json").write_text('{"price":1}\n', encoding="utf-8")

    archive = export_experiment(
        tmp_path,
        tmp_path / "exports",
        datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc),
    )
    assert archive.is_file()
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "runtime/baseline/baseline.dryrun.sqlite" in names
        assert "runtime/hybrid/hybrid.dryrun.sqlite" in names
        assert "runtime/fast/fast.dryrun.sqlite" in names
        assert "data/equity_snapshots.jsonl" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["credentials_redacted"] is True
        assert manifest["freqtrade_image_tag"] == "2026.9"
        assert len(manifest["included_artifacts"]) >= 8
        baseline_cfg = json.loads(zf.read("configs/config.baseline.json"))
        assert baseline_cfg["exchange"]["key"] == ""
        assert baseline_cfg["exchange"]["secret"] == ""
        assert baseline_cfg["exchange"]["password"] == ""
        assert baseline_cfg["telegram"]["token"] == ""
        assert baseline_cfg["telegram"]["chat_id"] == ""
        assert baseline_cfg["api_server"]["jwt_secret_key"] == ""
        assert baseline_cfg["api_server"]["password"] == ""
        assert baseline_cfg["api_server"]["username"] == ""
        assert all("sha256" in artifact for artifact in manifest["included_artifacts"])
        assert any("exploratory" in item.lower() for item in manifest["omissions"])
