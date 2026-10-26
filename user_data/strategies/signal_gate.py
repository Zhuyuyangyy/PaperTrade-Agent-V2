"""Pure and testable gate. Treat all research output as untrusted, non-executable data."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

HOUR_MS = 3_600_000
MAX_FILE_BYTES = 32_768
ALLOWED_PAIRS = ("BTC/USDT", "ETH/USDT")


def candle_open_ms(dt: Any) -> int:
    """Convert Freqtrade UTC opening candle timestamps to epoch ms."""
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    if not isinstance(dt, datetime):
        raise ValueError("Not a candle datetime")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def signal_filename(pair: str) -> str:
    if pair not in ALLOWED_PAIRS:
        raise ValueError("Unapproved pair")
    return pair.replace("/", "_") + ".json"


def load_signal(root: Path, pair: str) -> dict[str, Any] | None:
    try:
        path = root / signal_filename(pair)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def validate_buy_signal(
    data: dict[str, Any] | None, pair: str, candle_ms: int, now_ms: int
) -> tuple[bool, str]:
    """Fail closed unless candle, clocks, origin, votes and veto match exactly."""
    if not isinstance(data, dict):
        return False, "signal_missing"
    if pair not in ALLOWED_PAIRS:
        return False, "pair_not_allowed"
    if data.get("schema_version") != 1 or data.get("pair") != pair:
        return False, "schema_or_pair_mismatch"
    if data.get("timeframe") != "1h" or data.get("source") != "ccxt-public:kraken":
        return False, "source_or_timeframe_mismatch"
    for field in ("candle_open_ms", "observed_at_ms", "expires_at_ms"):
        if type(data.get(field)) is not int:
            return False, "bad_timestamp"
    if data["candle_open_ms"] != candle_ms or candle_ms % HOUR_MS != 0:
        return False, "candle_mismatch"
    closed_ms = candle_ms + HOUR_MS
    if data["expires_at_ms"] != closed_ms + HOUR_MS:
        return False, "expiry_mismatch"
    if not (closed_ms <= data["observed_at_ms"] <= now_ms + 5_000):
        return False, "future_or_preclose_analysis"
    if not (closed_ms <= now_ms < data["expires_at_ms"]):
        return False, "expired_or_unclosed"
    if now_ms - data["observed_at_ms"] > HOUR_MS:
        return False, "stale_analysis"
    votes = data.get("votes")
    if not isinstance(votes, dict):
        return False, "missing_votes"
    if (data.get("action") != "BUY"
            or votes.get("trend") != "BUY"
            or votes.get("momentum") != "BUY"
            or data.get("risk_veto") is not False):
        return False, "not_approved"
    details = data.get("indicators", {})
    if not isinstance(details, dict):
        return False, "bad_indicators"
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
           for v in details.values()):
        return False, "nonfinite_indicator"
    return True, "approved"


def approved_from_file(root: Path, pair: str, candle_ms: int, now_ms: int) -> tuple[bool, str]:
    return validate_buy_signal(load_signal(root, pair), pair, candle_ms, now_ms)
