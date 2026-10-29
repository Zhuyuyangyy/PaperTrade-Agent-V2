"""A-share convertible-bond data, cached locally, with the gaps disclosed.

The same lesson the Binance audit learned applies here: a feed that fails
mid-run must not silently bias the comparison. So every response is either
fully parsed or loudly absent, and the run reports which bonds had no data
rather than quietly measuring a shorter list than it claims.

Source status at build time (2026-10-29), verified by running:
  * convertible daily bars  (sina)   -- AVAILABLE but the series stops
                                         somewhere between 2023 and 2025-07
                                         depending on the bond
  * underlying stock daily  (sina)   -- CURRENT (through 2026-10-28)
  * conversion price map    (ths)    -- CURRENT (973 bonds)
  * live snapshot            (sina)   -- CURRENT (327 bonds)
Eastmoney history nodes were unreachable from this machine; that is why the
audit window is 2023-07 .. 2025-07 and says so, rather than pretending.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import akshare as ak

SCHEMA = """
CREATE TABLE IF NOT EXISTS conv_map (
    bond_code TEXT PRIMARY KEY, bond_name TEXT, stock_code TEXT, stock_name TEXT,
    conv_price REAL, list_date TEXT, maturity TEXT
);
CREATE TABLE IF NOT EXISTS bond_daily (
    bond_code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (bond_code, date)
);
CREATE TABLE IF NOT EXISTS stock_daily (
    stock_code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (stock_code, date)
);
"""


class DataError(RuntimeError):
    pass


def _sina_symbol(code: str) -> str:
    """'113050' -> 'sh113050'; '128140' -> 'sz128140'."""
    code = code.strip()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("11", "113", "118")):     # Shanghai convertible
        return "sh" + code
    if code.startswith(("12", "127", "128")):     # Shenzhen convertible
        return "sz" + code
    raise DataError(f"无法判断市场: {code}")


def _stock_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if code.startswith(("6", "9")):               # Shanghai stock
        return "sh" + code
    if code.startswith(("0", "2", "3")):          # Shenzhen stock
        return "sz" + code
    if code.startswith(("4", "8")):               # Beijing stock
        return "bj" + code
    raise DataError(f"无法判断市场: {code}")


class BondData:
    """One sqlite file of everything the convertible audit has downloaded."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30.0)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- conversion price map ----------
    def conv_map(self, refresh: bool = False) -> list[dict]:
        have = self.conn.execute("SELECT COUNT(*) FROM conv_map").fetchone()[0]
        if refresh or not have:
            df = ak.bond_zh_cov_info_ths()
            rows = []
            for _, r in df.iterrows():
                try:
                    price = float(r["转股价格"]) if r["转股价格"] not in (None, "") else 0.0
                except (TypeError, ValueError):
                    price = 0.0
                rows.append((str(r["债券代码"]), str(r["债券简称"]), str(r["正股代码"]),
                             str(r["正股简称"]), price, str(r.get("上市日期") or ""),
                             str(r.get("到期时间") or "")))
            self.conn.executemany(
                "INSERT OR REPLACE INTO conv_map VALUES (?,?,?,?,?,?,?)", rows)
            self.conn.commit()
        out = self.conn.execute(
            "SELECT bond_code, bond_name, stock_code, stock_name, conv_price,"
            " list_date, maturity FROM conv_map WHERE conv_price > 0").fetchall()
        return [{"bond": r[0], "bond_name": r[1], "stock": r[2], "stock_name": r[3],
                 "conv_price": float(r[4]), "list_date": r[5], "maturity": r[6]}
                for r in out]

    # ---------- daily bars ----------
    def bond_daily(self, code: str, refresh: bool = False) -> list[dict]:
        if not refresh and self.conn.execute(
                "SELECT 1 FROM bond_daily WHERE bond_code=?", (code,)).fetchone():
            return self._read("bond_daily", "bond_code", code)
        try:
            df = ak.bond_zh_hs_cov_daily(symbol=_sina_symbol(code))
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"{code} 转债日线失败: {exc}") from exc
        if df is None or df.empty or "date" not in df.columns:
            raise DataError(f"{code} 无历史日线（可能未上市或已摘牌）")
        rows = [(code, str(r["date"]), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r["volume"]))
                for _, r in df.iterrows()]
        self.conn.executemany(
            "INSERT OR REPLACE INTO bond_daily VALUES (?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return [{"date": r[1], "o": r[2], "h": r[3], "l": r[4], "c": r[5], "v": r[6]}
                for r in rows]

    def stock_daily(self, code: str, refresh: bool = False) -> list[dict]:
        if not refresh and self.conn.execute(
                "SELECT 1 FROM stock_daily WHERE stock_code=?", (code,)).fetchone():
            return self._read("stock_daily", "stock_code", code)
        try:
            df = ak.stock_zh_a_daily(symbol=_stock_symbol(code),
                                     start_date="20230101", end_date="20261029")
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"{code} 正股日线失败: {exc}") from exc
        if df is None or df.empty or "date" not in df.columns:
            raise DataError(f"{code} 正股无数据")
        rows = [(code, str(r["date"]), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r["volume"]))
                for _, r in df.iterrows()]
        self.conn.executemany(
            "INSERT OR REPLACE INTO stock_daily VALUES (?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return [{"date": r[1], "o": r[2], "h": r[3], "l": r[4], "c": r[5], "v": r[6]}
                for r in rows]

    def _read(self, table: str, key_col: str, key: str) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT date, open, high, low, close, volume FROM {table}"
            f" WHERE {key_col}=? ORDER BY date", (key,)).fetchall()
        return [{"date": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
                for r in rows]

    # ---------- the premium rate ----------
    def premium_series(self, bond_code: str, stock_code: str, conv_price: float) -> list[dict]:
        """Daily conversion premium: bond_close / (100/conv_price * stock_close) - 1.

        Only dates where BOTH traded are returned; a bond that was suspended
        that day has no premium, and a missing value is not a zero.
        """
        bond = {r["date"]: r for r in self.bond_daily(bond_code)}
        stock = {r["date"]: r for r in self.stock_daily(stock_code)}
        out = []
        for date, b in bond.items():
            s = stock.get(date)
            if not s or not s["c"] or not conv_price:
                continue
            parity = 100.0 / conv_price * s["c"]      # 转股价值
            out.append({"date": date, "bond": b["c"], "stock": s["c"],
                        "parity": parity, "premium": b["c"] / parity - 1.0,
                        "bond_high": b["h"], "bond_low": b["l"]})
        return out


def polite_pause(seconds: float = 0.4) -> None:
    """Sina rate-limits; a polite crawler is not a banned crawler."""
    time.sleep(seconds)
