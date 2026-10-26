"""Export a reproducible, credential-redacted experiment snapshot as a ZIP.

Live SQLite files are copied with SQLite's backup API (not raw filesystem copy) so
an export taken while Freqtrade is running is transactionally consistent. Runtime
ledgers and price marks remain excluded from Git and are archived only on demand.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
from urllib.parse import quote
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ("config.baseline.json", "config.hybrid.json", "config.fast.json")
STRATEGIES = ("PaperBaseline.py", "PaperHybrid.py", "PaperFast.py", "signal_gate.py")
MAX_SMALL_ARTIFACT_BYTES = 50 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_regular_file(src: Path, dst: Path, max_bytes: int = MAX_SMALL_ARTIFACT_BYTES) -> bool:
    if src.is_symlink() or not src.is_file() or src.stat().st_size > max_bytes:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def copy_sqlite_snapshot(src: Path, dst: Path) -> bool:
    if src.is_symlink() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{quote(str(src.resolve()), safe='/:')}?mode=ro"
    source = None
    target = None
    try:
        source = sqlite3.connect(uri, uri=True, timeout=2.0)
        source.execute("PRAGMA busy_timeout=2000")
        target = sqlite3.connect(dst)
        source.backup(target, pages=256, sleep=0.05)
        target.commit()
        return True
    except sqlite3.Error:
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()


def redacted_config(src: Path, dst: Path) -> bool:
    if src.is_symlink() or not src.is_file():
        return False
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    exchange = data.get("exchange")
    if isinstance(exchange, dict):
        for key in ("key", "secret", "password", "uid", "token"):
            if key in exchange:
                exchange[key] = ""
    telegram = data.get("telegram")
    if isinstance(telegram, dict):
        for key in ("token", "chat_id", "authorized_users"):
            if key in telegram:
                telegram[key] = "" if key != "authorized_users" else []
    api_server = data.get("api_server")
    if isinstance(api_server, dict):
        for key in ("jwt_secret_key", "password", "username"):
            if key in api_server:
                api_server[key] = ""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=2, check=True,
        )
        value = result.stdout.strip()
        return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def export_experiment(root: Path = ROOT, out_dir: Path | None = None,
                      now: datetime | None = None, include_dbs: bool = True) -> Path:
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    protocol = root / "docs" / "EXPERIMENT.md"
    protocol_hash = sha256_file(protocol) if protocol.is_file() else "missing"
    run_id = f"paper-run-{now.strftime('%Y%m%dT%H%M%SZ')}-{protocol_hash[:8]}"
    out_dir = out_dir or (root / "data" / "exports")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / f"{run_id}.zip"

    with tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=out_dir) as tmp:
        stage = Path(tmp) / run_id
        stage.mkdir()
        copied: list[Path] = []

        for name in CONFIGS:
            src = root / "user_data" / name
            dst = stage / "configs" / name
            if redacted_config(src, dst):
                copied.append(dst)

        for name in STRATEGIES:
            src = root / "user_data" / "strategies" / name
            dst = stage / "strategies" / name
            if copy_regular_file(src, dst):
                copied.append(dst)

        for rel in ("docker-compose.yml", "docs/EXPERIMENT.md", "docs/SECURITY.md", "README.md"):
            src = root / rel
            dst = stage / rel
            if copy_regular_file(src, dst):
                copied.append(dst)

        if include_dbs:
            for kind in ("baseline", "hybrid", "fast"):
                src = root / "runtime" / kind / f"{kind}.dryrun.sqlite"
                dst = stage / "runtime" / kind / f"{kind}.dryrun.sqlite"
                if copy_sqlite_snapshot(src, dst):
                    copied.append(dst)

        for rel in ("data/decisions.jsonl", "data/equity_snapshots.jsonl"):
            src = root / rel
            dst = stage / rel
            if copy_regular_file(src, dst):
                copied.append(dst)

        for rel_root in ("user_data/signals", "user_data/marks", "data/ta_bias"):
            src_dir = root / rel_root
            if not src_dir.is_dir() or src_dir.is_symlink():
                continue
            for src in sorted(src_dir.glob("*.json")):
                if src.is_symlink() or not src.is_file():
                    continue
                dst = stage / rel_root / src.name
                if copy_regular_file(src, dst, max_bytes=8192):
                    copied.append(dst)

        compose_text = (root / "docker-compose.yml").read_text(encoding="utf-8") if (root / "docker-compose.yml").is_file() else ""
        image_match = re.search(r"image:\s*freqtradeorg/freqtrade:([^\s]+)", compose_text)
        artifacts = []
        for path in sorted(copied):
            artifacts.append({
                "path": path.relative_to(stage).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "exported_at_utc": now.isoformat(),
            "purpose": "paper-trading research reproducibility; not proof of strategy profitability",
            "execution_mode": "Freqtrade dry-run only",
            "freqtrade_image_tag": image_match.group(1) if image_match else None,
            "python_version": platform.python_version(),
            "git_revision": git_revision(root),
            "experiment_protocol_sha256": protocol_hash,
            "credentials_redacted": True,
            "sqlite_copy_method": "sqlite3.Connection.backup from read-only source connection",
            "included_artifacts": artifacts,
            "omissions": [
                "Files absent, symlinked, invalid, or larger than the configured per-file limit are omitted.",
                "A missing database is omitted; check the run log before interpreting this export.",
                "The fast account is exploratory and is excluded from the primary baseline-vs-hybrid A/B comparison.",
                "Marks are point-in-time public ticker snapshots and may be stale at export time.",
            ],
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        tmp_zip = archive_path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    zf.write(path, path.relative_to(stage).as_posix())
        os.replace(tmp_zip, archive_path)
    return archive_path


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Export a redacted, reproducible paper-trading experiment snapshot")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data" / "exports")
    parser.add_argument("--no-dbs", action="store_true", help="Do not snapshot the SQLite ledgers")
    args = parser.parse_args()
    path = export_experiment(ROOT, args.out_dir, include_dbs=not args.no_dbs)
    print(f"Experiment snapshot exported: {path}")
    print("Inspect manifest.json and confirm each expected account DB is included before archiving results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
