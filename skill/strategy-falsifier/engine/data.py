"""Data loading for the falsification engine: candles and signals.

Two candle sources, one signal source, all strict:

  * CSV candles  -- columns t,o,h,l,c[,v]; t is ISO-8601 or epoch milliseconds.
                    Rows are validated (ohlc > 0, high >= max(o,c), low <=
                    min(o,c)) and sorted; a bad row is an error, not a skip.
  * market cache -- the same sqlite the research audit downloaded (klines table
                    keyed by symbol/interval/venue), so a report can be replayed
                    offline from data that was already fetched.
  * CSV signals  -- columns t,side; t maps to the first candle at or after it.
                    A signal that cannot be mapped is counted, never dropped.

No network. If the data is not local, the engine says so instead of guessing.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


class DataError(RuntimeError):
    """Raised when a market cache cannot be read.

    Defined locally (not imported from the audit fetcher) so the engine
    carries no dependency beyond pathsim when it is vendored into a
    standalone skill package.
    """


class CandleError(ValueError):
    """Raised when candle data is missing, malformed, or out of order."""


# Column aliases customers actually have. Exchange downloads, freqtrade
# backtest exports and TradingView CSVs all use different names; demanding a
# literal `t` is how a paying customer decides the tool is not for them.
CANDLE_ALIASES = {
    "t": ("t", "date", "time", "timestamp", "open_time", "opentime", "datetime", "date_time"),
    "o": ("o", "open"),
    "h": ("h", "high"),
    "l": ("l", "low"),
    "c": ("c", "close"),
    "v": ("v", "volume", "vol", "amount", "quote_volume"),
}
SIGNAL_ALIASES = {
    "t": ("t", "date", "time", "timestamp", "datetime", "date_time", "signal_time"),
    "side": ("side", "direction", "signal", "position", "type"),
    "reason": ("reason", "exit_reason", "tag", "note"),
}


def _pick(row: dict, field: str, aliases: tuple[str, ...]) -> str | None:
    """First present alias for a logical column, or None. Extra columns are
    ignored on purpose -- real exports carry dozens of them."""
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for a in aliases:
        if a in lowered and lowered[a] not in (None, ""):
            return lowered[a]
    return None


def _to_ms(t: str) -> int:
    """ISO-8601 or epoch milliseconds -> epoch ms. Anything else is an error."""
    t = t.strip()
    if not t:
        raise CandleError("空时间戳")
    try:
        v = float(t)
    except ValueError:
        pass
    else:
        # Heuristic that is also a rule: seconds-era numbers are ~1e9, ms ~1e12.
        return int(v * 1000) if v < 1e11 else int(v)
    import datetime as _dt

    # Naive timestamps are UTC: exchange data is UTC, and .timestamp() on a
    # naive datetime would silently apply the machine's local zone.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = _dt.datetime.strptime(t.replace("T", " "), fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    raise CandleError(f"无法解析时间戳 {t!r}（支持 ISO-8601 UTC 或 epoch ms）")


def _validate_candle(row: dict, lineno: int) -> dict:
    t_raw = _pick(row, "t", CANDLE_ALIASES["t"])
    if t_raw is None:
        raise CandleError(f"第 {lineno} 行缺少时间列（接受 {CANDLE_ALIASES['t']} 之一）")
    try:
        t = _to_ms(str(t_raw))
        o = float(_pick(row, "o", CANDLE_ALIASES["o"]))
        h = float(_pick(row, "h", CANDLE_ALIASES["h"]))
        l = float(_pick(row, "l", CANDLE_ALIASES["l"]))
        c = float(_pick(row, "c", CANDLE_ALIASES["c"]))
    except (TypeError, ValueError) as exc:
        raise CandleError(f"第 {lineno} 行 OHLC 不是有效数字: {exc}") from exc
    v_raw = _pick(row, "v", CANDLE_ALIASES["v"])
    try:
        v = float(v_raw) if v_raw is not None else 0.0
    except ValueError:
        v = 0.0  # volume is optional context, not a price
    if min(o, h, l, c) <= 0:
        raise CandleError(f"第 {lineno} 行存在非正价格（t={t}）")
    if h < max(o, c) - 1e-12 or l > min(o, c) + 1e-12:
        raise CandleError(f"第 {lineno} 行 OHLC 不自洽: o={o} h={h} l={l} c={c}")
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def load_candles_csv(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"K 线文件不存在: {p}")
    candles: list[dict] = []
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CandleError("K 线 CSV 是空文件")
        lowered = {str(f).strip().lower() for f in reader.fieldnames}
        for field, aliases in CANDLE_ALIASES.items():
            if field != "v" and not lowered & set(aliases):
                raise CandleError(
                    f"K 线 CSV 缺少 {field} 列（接受 {aliases} 之一），实际得到 {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):
            candles.append(_validate_candle(row, i))
    if len(candles) < 2:
        raise CandleError("K 线不足 2 根，无法回放")
    candles.sort(key=lambda c: c["t"])
    return candles


def load_candles_market(db_path: str | Path, symbol: str, interval: str, venue: str) -> list[dict]:
    p = Path(db_path)
    if not p.is_file():
        raise CandleError(f"行情缓存不存在: {p}（可传 --candles CSV 替代）")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT open_time, open, high, low, close, volume FROM klines"
            " WHERE symbol=? AND interval=? AND venue=? ORDER BY open_time",
            (symbol.upper(), interval, venue),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise CandleError(f"缓存中没有 {symbol} {interval} {venue} 的 K 线")
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5] or 0)}
        for r in rows
    ]


def load_funding_csv(path: str | Path) -> list[dict]:
    """Funding series from a CSV (columns t,rate), or [] when unusable.

    Lets a spec carry its own funding series so a published case replays
    identically on a machine with no market cache.
    """
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"资金费文件不存在: {p}")
    out: list[dict] = []
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "t" not in reader.fieldnames or "rate" not in reader.fieldnames:
            raise CandleError(f"资金费 CSV 需要列 t,rate，实际得到 {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):
            try:
                out.append({"t": _to_ms(row["t"]), "rate": float(row["rate"])})
            except (ValueError, KeyError):
                raise CandleError(f"资金费 CSV 第 {i} 行无法解析: {row}") from None
    if not out:
        raise CandleError(f"资金费 CSV 没有数据行: {p}")
    out.sort(key=lambda r: r["t"])
    return out


def load_funding(db_path: str | Path, symbol: str) -> list[dict] | None:
    """Funding series for a symbol, or None when the cache has none.

    Funding only exists on futures books; the cache is keyed by symbol alone,
    which is safe because spot symbols carry no funding rows.
    """
    p = Path(db_path)
    if not p.is_file():
        return None
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT funding_time, funding_rate FROM funding WHERE symbol=? ORDER BY funding_time",
            (symbol.upper(),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return [{"t": int(r[0]), "rate": float(r[1])} for r in rows]


def load_signals_csv(path: str | Path, candles: list[dict]) -> tuple[dict[int, str], dict]:
    """Map signal timestamps onto bar indices.

    Returns (entries, stats). A signal maps to the first candle whose open time
    is at or after the signal time -- acting inside the bar that produced the
    signal is look-ahead. Signals that land after the last usable bar, or
    before the first candle, are counted in stats['skipped'] with reasons.
    """
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"信号文件不存在: {p}")
    entries: dict[int, str] = {}
    stats = {"total": 0, "mapped": 0, "skipped_after_end": 0, "skipped_before_start": 0, "bad_rows": 0}
    times = [c["t"] for c in candles]
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CandleError("信号 CSV 是空文件")
        lowered = {str(f).strip().lower() for f in reader.fieldnames}
        if not lowered & set(SIGNAL_ALIASES["t"]):
            raise CandleError(
                f"信号 CSV 缺少时间列（接受 {SIGNAL_ALIASES['t']} 之一），实际得到 {reader.fieldnames}")
        for i, row in enumerate(reader, start=2):
            stats["total"] += 1
            try:
                t = _to_ms(str(_pick(row, "t", SIGNAL_ALIASES["t"])))
            except (CandleError, TypeError):
                stats["bad_rows"] += 1
                continue
            side_raw = _pick(row, "side", SIGNAL_ALIASES["side"])
            side = (side_raw or "long").strip().lower()
            if side not in ("long", "short"):
                # common spellings customers actually use
                side = {"buy": "long", "sell": "short", "l": "long", "s": "short",
                        "1": "long", "-1": "short"}.get(side, side)
            if side not in ("long", "short"):
                stats["bad_rows"] += 1
                continue
            if t > times[-1]:
                stats["skipped_after_end"] += 1
                continue
            if t < times[0]:
                stats["skipped_before_start"] += 1
                continue
            # first bar at or after the signal
            lo, hi = 0, len(times) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if times[mid] >= t:
                    hi = mid
                else:
                    lo = mid + 1
            idx = lo
            if idx >= len(candles) - 1:
                # pathsim needs the fill bar to exist; the signal is real but
                # unresolvable inside this window -- counted, not dropped.
                stats["skipped_after_end"] += 1
                continue
            entries[idx] = side
            stats["mapped"] += 1
    return entries, stats


def load_exit_signals_csv(path: str | Path, candles: list[dict]) -> dict[int, str]:
    """Map exit-signal timestamps onto bar indices: bar index -> reason.

    Same mapping rule as entries: the first candle at or after the signal
    timestamp, because a signal confirmed on one bar's close acts on the next.
    The reason string rides along into the simulator's exit bookkeeping.
    """
    p = Path(path)
    if not p.is_file():
        raise CandleError(f"退出信号文件不存在: {p}")
    out: dict[int, str] = {}
    times = [c["t"] for c in candles]
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CandleError("退出信号 CSV 是空文件")
        lowered = {str(f).strip().lower() for f in reader.fieldnames}
        if not lowered & set(SIGNAL_ALIASES["t"]):
            raise CandleError(
                f"退出信号 CSV 缺少时间列（接受 {SIGNAL_ALIASES['t']} 之一），实际得到 {reader.fieldnames}")
        for row in reader:
            try:
                t = _to_ms(str(_pick(row, "t", SIGNAL_ALIASES["t"])))
            except (CandleError, TypeError):
                continue
            if t < times[0] or t > times[-1]:
                continue
            lo, hi = 0, len(times) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if times[mid] >= t:
                    hi = mid
                else:
                    lo = mid + 1
            reason = _pick(row, "reason", SIGNAL_ALIASES["reason"])
            out[lo] = (reason or "signal_exit").strip() or "signal_exit"
    return out


def describe_market_cache(db_path: str | Path) -> list[tuple]:
    """What is actually in a market cache: (symbol, interval, venue, bars, from, to)."""
    p = Path(db_path)
    if not p.is_file():
        raise DataError(f"行情缓存不存在: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT symbol, interval, venue, COUNT(*), MIN(open_time), MAX(open_time)"
            " FROM klines GROUP BY symbol, interval, venue ORDER BY COUNT(*) DESC"
        ).fetchall()
    finally:
        conn.close()
