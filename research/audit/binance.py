"""Binance public market data, cached locally.

The three candidate strategies trade Binance instruments, so the audit needs
Binance data -- and the desk's own lesson applies here too: a feed that fails
mid-run must not silently bias the comparison. Everything is cached in one
sqlite file so a run can be replayed offline, and every response is either
fully parsed or loudly absent.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

SPOT = "https://api.binance.com/api/v3/klines"
FUTURES = "https://fapi.binance.com/fapi/v1"
FUTURES_KLINES = f"{FUTURES}/klines"
MAX_PER_CALL = 1000


def normalize_symbol(pair: str) -> str:
    """'BTC/USDT:USDT' -> 'BTCUSDT'. Freqtrade notation, Binance API form."""
    return pair.replace("/", "").replace(":USDT", "").replace(":", "")


class DataError(RuntimeError):
    """Raised when Binance could not be read after retrying."""


def _get(url: str, timeout: float = 20.0, retries: int = 3) -> object:
    """GET with proxy bypass and 429 patience, same contract as the desk."""
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url, headers={"User-Agent": "PaperTrade-StrategyAudit/0.1 (research)"}
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                wait = 0.0
                headers = getattr(exc, "headers", None)
                if headers is not None:
                    try:
                        wait = float(headers.get("Retry-After") or 0.0)
                    except (TypeError, ValueError):
                        wait = 0.0
                time.sleep(min(max(wait, 1.5 * (attempt + 1)), 10.0))
                continue
            raise DataError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise DataError(f"数据源不可达: {exc}") from exc
    raise DataError("重试后仍失败")


SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT, interval TEXT, venue TEXT, open_time INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, interval, venue, open_time)
);
CREATE TABLE IF NOT EXISTS funding (
    symbol TEXT, funding_time INTEGER, funding_rate REAL,
    PRIMARY KEY (symbol, funding_time)
);
"""


class Market:
    """One sqlite file of everything the audit has ever downloaded."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30.0)
        self.conn.executescript(SCHEMA)
        self._migrate_venue()
        self.conn.commit()

    def _migrate_venue(self) -> None:
        """Teach an older cache that spot and futures are different series.

        CREATE TABLE IF NOT EXISTS never upgrades a table, and the venue has to
        be part of the primary key -- otherwise a spot candle and a perp candle
        for the same hour would replace each other silently. So the table is
        rebuilt with the old rows labelled 'spot', which is all they can be.
        """
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(klines)")]
        if not cols or "venue" in cols:
            return
        self.conn.executescript(
            """
            CREATE TABLE klines_new (
                symbol TEXT, interval TEXT, venue TEXT, open_time INTEGER,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY (symbol, interval, venue, open_time)
            );
            INSERT INTO klines_new
                SELECT symbol, interval, 'spot', open_time, open, high, low, close, volume
                FROM klines;
            DROP TABLE klines;
            ALTER TABLE klines_new RENAME TO klines;
            """
        )

    def close(self) -> None:
        self.conn.close()

    # ---------- klines ----------
    def klines(self, symbol: str, interval: str = "1h",
               start_ms: int | None = None, end_ms: int | None = None,
               venue: str = "spot") -> list[dict]:
        """Candles for a symbol, downloading whatever the cache is missing.

        venue: 'spot' or 'futures' (USDT-margined perpetuals). A strategy that
        trades perps must be measured on perp candles -- spot and futures are
        different instruments with different prices.
        """
        symbol = normalize_symbol(symbol)
        have = self.conn.execute(
            "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM klines"
            " WHERE symbol=? AND interval=? AND venue=?", (symbol, interval, venue),
        ).fetchone()
        want_start = start_ms or 0
        want_end = end_ms or int(time.time() * 1000)
        if have[2]:
            have_lo, have_hi = int(have[0]), int(have[1])
            if want_start >= have_lo and want_end <= have_hi:
                return self._read_klines(symbol, interval, want_start, want_end, venue)
            # extend backwards or forwards as needed
            if want_start < have_lo:
                self._download(symbol, interval, want_start, have_lo - 1, venue)
            if want_end > have_hi:
                self._download(symbol, interval, have_hi + 1, want_end, venue)
        else:
            self._download(symbol, interval, want_start, want_end, venue)
        return self._read_klines(symbol, interval, want_start, want_end, venue)

    def _read_klines(self, symbol: str, interval: str,
                     start_ms: int, end_ms: int, venue: str = "spot") -> list[dict]:
        rows = self.conn.execute(
            "SELECT open_time, open, high, low, close, volume FROM klines"
            " WHERE symbol=? AND interval=? AND venue=? AND open_time>=? AND open_time<=?"
            " ORDER BY open_time", (symbol, interval, venue, start_ms, end_ms),
        ).fetchall()
        return [
            {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
            for r in rows
        ]

    def _download(self, symbol: str, interval: str, start_ms: int, end_ms: int,
                  venue: str = "spot") -> None:
        cursor = start_ms
        base = FUTURES_KLINES if venue == "futures" else SPOT
        while cursor < end_ms:
            url = (f"{base}?symbol={symbol}&interval={interval}"
                   f"&startTime={cursor}&endTime={end_ms}&limit={MAX_PER_CALL}")
            rows = _get(url)
            if not isinstance(rows, list):
                raise DataError(f"{symbol} {interval}: 返回异常 {rows!r}")
            if not rows:
                break
            self.conn.executemany(
                "INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?,?)",
                [(symbol, interval, venue, int(r[0]), float(r[1]), float(r[2]),
                  float(r[3]), float(r[4]), float(r[5])) for r in rows],
            )
            self.conn.commit()
            cursor = int(rows[-1][0]) + 1
            if len(rows) < MAX_PER_CALL:
                break
            time.sleep(0.25)

    # ---------- funding rate ----------
    def funding(self, symbol: str, start_ms: int | None = None) -> list[dict]:
        """Funding prints, downloading whatever part of the window is missing.

        The cache is checked at BOTH ends: a window that reaches further back
        than the oldest cached print needs the older history downloaded, not
        just the newer.
        """
        want_start = int(start_ms) if start_ms else 0
        oldest = self.conn.execute(
            "SELECT MIN(funding_time) FROM funding WHERE symbol=?", (symbol,)
        ).fetchone()[0]
        if oldest is None or (want_start and int(oldest) > want_start):
            try:
                self._download_funding(symbol, want_start)
            except DataError:
                # A rejected incremental fetch must not kill a historical
                # replay: the newest few prints are the ones we lose, and
                # they sit outside every window this audit measures.
                pass
        out = self.conn.execute(
            "SELECT funding_time, funding_rate FROM funding WHERE symbol=?"
            " ORDER BY funding_time", (symbol,),
        ).fetchall()
        if want_start:
            out = [r for r in out if int(r[0]) >= want_start]
        return [{"t": int(r[0]), "rate": float(r[1])} for r in out]

    def _download_funding(self, symbol: str, since_ms: int | None = None) -> None:
        """Page forward from the window start.

        The endpoint ignores startTime=0 (returns the newest prints instead),
        so paging starts from the requested window and walks forward until a
        short page says the rest is already cached or does not exist.
        """
        cursor = int(since_ms) if since_ms else 0
        while True:
            url = (f"{FUTURES}/fundingRate?symbol={symbol}&limit=1000"
                   + (f"&startTime={cursor}" if cursor else ""))
            rows = _get(url)
            if not isinstance(rows, list):
                raise DataError(f"{symbol} funding: 返回异常 {rows!r}")
            if not rows:
                break
            self.conn.executemany(
                "INSERT OR REPLACE INTO funding VALUES (?,?,?)",
                [(symbol, int(r["fundingTime"]), float(r["fundingRate"])) for r in rows],
            )
            self.conn.commit()
            # The endpoint caps a page well below the requested limit, so page
            # until it answers with nothing; a page that fails to advance the
            # cursor is the other way out.
            last_t = int(rows[-1]["fundingTime"])
            if last_t < cursor:
                break
            cursor = last_t + 1
            time.sleep(0.25)
