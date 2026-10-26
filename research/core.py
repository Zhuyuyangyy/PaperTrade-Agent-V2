"""Deterministic, fully observable research voters; no orders or account access."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
from typing import Any

HOUR_MS = 3_600_000
PAIR_MAP = {"BTC/USDT": "BTC-USD", "ETH/USDT": "ETH-USD"}


@dataclass(frozen=True)
class ClosedCandle:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def checked_closed_candles(rows: list[list[float]], now_ms: int) -> list[ClosedCandle]:
    """Reject gaps or invalid data, and never consume an unclosed candle."""
    parsed = []
    for row in rows:
        if len(row) < 6:
            raise ValueError("OHLCV row incomplete")
        t, opened, high, low, close, vol = row[:6]
        values = [opened, high, low, close, vol]
        if type(t) is not int or t % HOUR_MS != 0 or not all(
            isinstance(v, (float, int)) and math.isfinite(v) for v in values
        ):
            raise ValueError("Nonfinite / misaligned OHLCV")
        if min(opened, high, low, close) <= 0 or vol < 0 or not (low <= opened <= high and low <= close <= high):
            raise ValueError("Invalid OHLCV range")
        if t + HOUR_MS <= now_ms - 30_000:
            parsed.append(ClosedCandle(int(t), *[float(v) for v in values]))
    if len(parsed) < 60:
        raise ValueError("Need >=60 closed hourly candles")
    if any(b.open_ms - a.open_ms != HOUR_MS for a, b in zip(parsed[-60:-1], parsed[-59:])):
        raise ValueError("Last 60 candles are not contiguous")
    if now_ms - (parsed[-1].open_ms + HOUR_MS) >= HOUR_MS:
        raise ValueError("Most recent closed candle is stale")
    return parsed


def stance_trend(closes: list[float]) -> tuple[str, float, float]:
    s20, s50 = statistics.fmean(closes[-20:]), statistics.fmean(closes[-50:])
    if s20 > s50 and closes[-1] > s20:
        return "BUY", s20, s50
    if s20 < s50 or closes[-1] < s20:
        return "SELL", s20, s50
    return "HOLD", s20, s50


def stance_momentum(closes: list[float]) -> tuple[str, float]:
    momentum = closes[-1] / closes[-7] - 1.0
    if momentum > 0.005:
        return "BUY", momentum
    if momentum < -0.005:
        return "SELL", momentum
    return "HOLD", momentum


def stance_risk(closes: list[float]) -> tuple[bool, float]:
    log_returns = [math.log(a / b) for a, b in zip(closes[-24:], closes[-25:-1])]
    vol = statistics.stdev(log_returns)
    return vol > 0.03, vol


def load_ta_bias(root: Path, pair: str, now_ms: int) -> str | None:
    """Optional daily TradingAgents stance. Absent or malformed bias blocks in require_buy mode."""
    path = root / (pair.replace("/", "_") + ".json")
    try:
        if path.is_symlink() or path.stat().st_size > 8192:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        created = data.get("observed_at_ms")
        today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()
        if (data.get("pair") != pair or data.get("source") != "TradingAgents"
                or data.get("analysis_date") != today
                or data.get("action") not in ("BUY", "HOLD", "SELL")
                or type(created) is not int or not (0 <= now_ms - created < 86_400_000)):
            return None
        return data["action"]
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def build_decision(pair: str, candles: list[ClosedCandle], now_ms: int,
                   ta_policy: str = "off", ta_root: Path | None = None) -> dict[str, Any]:
    if pair not in PAIR_MAP or len(candles) < 60:
        raise ValueError("Unapproved pair or too few bars")
    last = candles[-1]
    if last.open_ms % HOUR_MS != 0 or not (last.open_ms + HOUR_MS <= now_ms < last.open_ms + 2 * HOUR_MS):
        raise ValueError("Candle not timely")
    closes = [bar.close for bar in candles]
    trend, sma20, sma50 = stance_trend(closes)
    momentum, mom6h = stance_momentum(closes)
    veto, sigma = stance_risk(closes)
    if ta_policy not in ("off", "require_buy"):
        raise ValueError("Unapproved TradingAgents policy")
    ta_stance = load_ta_bias(ta_root, pair, now_ms) if ta_policy == "require_buy" and ta_root else None
    ta_ok = ta_policy == "off" or ta_stance == "BUY"
    action = "BUY" if (trend == "BUY" and momentum == "BUY" and not veto and ta_ok) else "HOLD"
    return {
        "schema_version": 1,
        "pair": pair,
        "timeframe": "1h",
        "source": "ccxt-public:kraken",
        "candle_open_ms": last.open_ms,
        "observed_at_ms": now_ms,
        "expires_at_ms": last.open_ms + 2 * HOUR_MS,
        "action": action,
        "risk_veto": veto,
        "votes": {"trend": trend, "momentum": momentum, "tradingagents": ta_stance},
        "indicators": {"sma20": round(sma20, 8), "sma50": round(sma50, 8),
                       "momentum6h": round(mom6h, 8), "volatility_hourly": round(sigma, 8)},
        "policy": ta_policy,
        "notes": "Entry filter only; baseline exits/stoploss always remain active.",
    }


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".paper-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_decisions(outdir: Path, decisions: list[dict[str, Any]], logpath: Path | None = None) -> int:
    """Only log decisions whose candle differs; use atomic writes per pair."""
    for d in decisions:
        pair = d["pair"]
        # All decisions (including HOLD) require rigorous self-consistency.
        if (pair not in PAIR_MAP or d["source"] != "ccxt-public:kraken" or
            d["expires_at_ms"] != d["candle_open_ms"] + 2 * HOUR_MS):
            raise ValueError("Malformed decision")
    count = 0
    for d in decisions:
        path = outdir / (d["pair"].replace("/", "_") + ".json")
        try:
            before = json.loads(path.read_text(encoding="utf-8"))
            if before.get("candle_open_ms") == d["candle_open_ms"]:
                continue
        except (FileNotFoundError, ValueError, AttributeError):
            pass
        atomic_write_json(path, d)
        if logpath:
            logpath.parent.mkdir(parents=True, exist_ok=True)
            with logpath.open("a", encoding="utf-8") as log:
                log.write(json.dumps(d, ensure_ascii=True, allow_nan=False) + "\n")
        count += 1
    return count
