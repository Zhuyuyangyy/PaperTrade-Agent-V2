"""Contract lock: the anti-cheat seal on pre-declared rules.

The whole instrument rests on one promise: the falsification rules were
written BEFORE any number was read. An agent iterating on a strategy has a
structural temptation to break that promise quietly -- moving the bar from
CI-lower-bound>0 to "close enough", or deleting the rule that just fired.
Humans do it too. The lock makes the break loud and permanent:

  python -m research.verify --lock-contract --spec strategy.json
      -> writes strategy.verdict_lock.json: the sha256 of the spec's
         falsification contract (the falsify_if block plus the strategy's
         identity), the timestamp, and the rules in full.

  python -m research.verify --spec strategy.json --lock strategy.verdict_lock.json
      -> recomputes the hash and compares. Any difference in the contract
         exits 99 (CONTRACT_TAMPERING_DETECTED) and stamps the report:
         the run is void as evidence, whatever its numbers say.

What the hash covers -- deliberately narrow:
  - falsify_if, verbatim (every declared rule and threshold)
  - name / symbol / interval / venue (the strategy's identity: a lock from
    one strategy cannot launder another)
What it does NOT cover: signals, exit rules, candles paths, comments. Those
are the strategy under test -- locking them would freeze the very iteration
the experiment protocol exists to support. The lock protects the JUDGE, not
the defendant.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

LOCK_VERSION = 1
EXIT_TAMPERED = 99


def contract_payload(spec: dict) -> dict:
    """The part of a spec the lock protects: the judge, not the defendant."""
    return {
        "name": str(spec.get("name", "")),
        "symbol": str(spec.get("symbol", "")),
        "interval": str(spec.get("interval", "")),
        "venue": str(spec.get("venue", "")),
        "falsify_if": spec.get("falsify_if") or {},
    }


def contract_hash(spec: dict) -> str:
    """Stable sha256 over the canonical JSON of the contract payload."""
    payload = contract_payload(spec)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def write_lock(spec: dict, lock_path: str | Path) -> Path:
    p = Path(lock_path)
    doc = {
        "lock_version": LOCK_VERSION,
        "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy": str(spec.get("name", "")),
        "contract_sha256": contract_hash(spec),
        "contract": contract_payload(spec),
    }
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def check_lock(spec: dict, lock_path: str | Path) -> tuple[bool, str]:
    """Returns (ok, detail). ok=False means the contract moved after locking."""
    p = Path(lock_path)
    if not p.is_file():
        return False, f"锁文件不存在：{p}"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"锁文件不是合法 JSON：{exc}"
    if "contract_sha256" not in doc:
        return False, "锁文件缺少 contract_sha256 字段"

    now = contract_hash(spec)
    if now == doc["contract_sha256"]:
        return True, f"契约校验通过（sha256 {now[:16]}…，锁定于 {doc.get('locked_at', '?')}）"

    # diagnose WHICH field moved, so the report can say it precisely
    old = doc.get("contract") or {}
    new = contract_payload(spec)
    diffs = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            diffs.append(f"{key}: 锁定值 {json.dumps(old.get(key), ensure_ascii=False)} → "
                         f"当前值 {json.dumps(new.get(key), ensure_ascii=False)}")
    detail = "契约在锁定后被修改——" + "；".join(diffs)
    return False, detail
