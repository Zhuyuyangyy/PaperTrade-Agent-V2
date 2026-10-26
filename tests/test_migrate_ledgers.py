from __future__ import annotations

import sqlite3

import pytest

from scripts.migrate_ledgers import migrate_one, ledger_counts, legacy_source_for


def _symlinks_supported(directory) -> bool:
    """Windows refuses symlink creation without SeCreateSymbolicLinkPrivilege."""
    probe = directory / "_symlink_probe"
    target = directory / "_symlink_target"
    try:
        target.write_text("probe", encoding="utf-8")
        probe.symlink_to(target)
    except (OSError, NotImplementedError):
        return False
    finally:
        # Deleting a dangling symlink can fail on Windows sandboxes; the probe result
        # is what matters, not the cleanup.
        for path in (probe, target):
            try:
                path.unlink()
            except OSError:
                pass
    return True


def make_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (is_open INTEGER, close_profit_abs REAL)")
    conn.executemany("INSERT INTO trades VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def test_migration_uses_sqlite_backup_and_preserves_trade_counts(tmp_path):
    src = tmp_path / "old" / "user_data" / "baseline.dryrun.sqlite"
    dst = tmp_path / "new" / "runtime" / "baseline" / "baseline.dryrun.sqlite"
    make_db(src, [(1, None), (0, 4.5), (0, -1.0)])
    assert migrate_one(src, dst) == "migrated open=1 closed=2"
    assert ledger_counts(dst) == (1, 2)


def test_migration_never_overwrites_existing_target(tmp_path):
    src = tmp_path / "old.sqlite"
    dst = tmp_path / "target.sqlite"
    make_db(src, [(0, 1.0)])
    make_db(dst, [(0, 99.0)])
    assert migrate_one(src, dst) == "target-exists-not-overwritten"
    conn = sqlite3.connect(dst)
    assert conn.execute("SELECT close_profit_abs FROM trades").fetchone()[0] == 99.0
    conn.close()


def test_migration_skips_missing_legacy_database(tmp_path):
    assert migrate_one(tmp_path / "missing.sqlite", tmp_path / "runtime" / "target.sqlite") == "source-missing"


def test_legacy_source_prefers_isolated_runtime_then_old_user_data(tmp_path):
    runtime_db = tmp_path / "runtime" / "baseline" / "baseline.dryrun.sqlite"
    old_db = tmp_path / "user_data" / "baseline.dryrun.sqlite"
    make_db(runtime_db, [(0, 1.0)])
    make_db(old_db, [(0, 2.0)])
    assert legacy_source_for(tmp_path, "baseline") == runtime_db
    runtime_db.unlink()
    assert legacy_source_for(tmp_path, "baseline") == old_db


def test_legacy_source_ignores_symlinked_runtime_ledger(tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlink creation needs SeCreateSymbolicLinkPrivilege on this host")
    old_db = tmp_path / "user_data" / "hybrid.dryrun.sqlite"
    make_db(old_db, [(0, 2.0)])
    runtime_dir = tmp_path / "runtime" / "hybrid"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "hybrid.dryrun.sqlite").symlink_to(old_db)
    assert legacy_source_for(tmp_path, "hybrid") == old_db
