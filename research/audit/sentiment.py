"""The Fear & Greed index, cached like everything else.

TrendRider gates entries on 25 <= fng <= 85. That is a real external
dependency, and a research audit must say what it did with it: fetched and
applied, fetched and ignored, or unavailable. Silence here is how a port
quietly diverges from the strategy it claims to be.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

URL = "https://api.alternative.me/fng/?limit=0&format=json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fear_greed (
    day INTEGER PRIMARY KEY, value INTEGER
);
"""


def fetch(cache: Path, *, max_age_s: int = 6 * 3600) -> dict[int, int]:
    """day (unix seconds, UTC midnight) -> index value 0..100."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache), timeout=30.0)
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT COUNT(*) FROM fear_greed").fetchone()[0]
    if not rows:
        try:
            request = urllib.request.Request(URL, headers={"User-Agent": "research"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=20) as response:
                payload = json.load(response)
            data = payload.get("data") or []
            conn.executemany(
                "INSERT OR REPLACE INTO fear_greed VALUES (?,?)",
                [(int(d["timestamp"]), int(d["value"])) for d in data],
            )
            conn.commit()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            pass  # an unavailable sentiment feed must not crash a run
    out = {int(d): int(v) for d, v in conn.execute("SELECT day, value FROM fear_greed")}
    conn.close()
    return out


def fng_at(series: dict[int, int], when_ms: int) -> int | None:
    """The value in force at a moment: the newest reading at or before it."""
    if not series:
        return None
    day = (when_ms // 1000 // 86400) * 86400
    best = None
    for d, v in series.items():
        if d <= day and (best is None or d > best[0]):
            best = (d, v)
    return best[1] if best else None
