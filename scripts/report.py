"""Read-only summaries for Freqtrade dry-run SQLite ledgers.

Open-position P&L is marked to fresh public Kraken ticker snapshots. A total-equity
value is emitted only when every open position can be valued with a fresh, validated
mark. Historical equity comes from the separate sampler journal, not from closed
trades alone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math
import sqlite3
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
FALLBACK_STARTING_CASH_USDT = 1000.0
MAX_RECENT_TRADES = 30
MAX_MARK_AGE_MS = 5 * 60 * 1000
ALLOWED_PAIRS = {"BTC/USDT", "ETH/USDT"}
MARK_SOURCE = "ccxt-public:kraken"


def starting_cash_for(config_path: Path | None, fallback: float = FALLBACK_STARTING_CASH_USDT) -> tuple[float, str]:
    """Read initial virtual cash from the actual Freqtrade config; report fallback use."""
    if config_path is None or not config_path.is_file():
        return float(fallback), "fallback"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        value = data.get("dry_run_wallet")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            return float(value), "config"
    except (OSError, ValueError, TypeError):
        pass
    return float(fallback), "fallback"


def configured_fee_for(config_path: Path | None, fallback: float = 0.001) -> float:
    """Use the configured taker fee when an open Trade has no close-fee estimate yet."""
    if config_path is None or not config_path.is_file():
        return float(fallback)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        value = data.get("fee")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value < 0.1:
            return float(value)
    except (OSError, ValueError, TypeError):
        pass
    return float(fallback)


def read_mark(mark_dir: Path, pair: str, now_ms: int) -> dict | None:
    """Read one recent, correctly sourced mark. Invalid/stale marks are never guessed."""
    if pair not in ALLOWED_PAIRS:
        return None
    path = mark_dir / f"{pair.replace('/', '_')}.json"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        price = data.get("price")
        observed = data.get("observed_at_ms")
        exchange_ts = data.get("exchange_timestamp_ms")
        exchange_ts_valid = (
            exchange_ts is None
            or (
                type(exchange_ts) is int
                and exchange_ts <= now_ms + 30_000
                and now_ms - exchange_ts <= MAX_MARK_AGE_MS
            )
        )
        if (
            data.get("schema_version") != 1
            or data.get("pair") != pair
            or data.get("source") != MARK_SOURCE
            or type(observed) is not int
            or observed > now_ms
            or now_ms - observed > MAX_MARK_AGE_MS
            or not exchange_ts_valid
            or not isinstance(price, (int, float))
            or isinstance(price, bool)
            or not math.isfinite(price)
            or price <= 0
        ):
            return None
        return {
            "pair": pair,
            "price": float(price),
            "observed_at_ms": observed,
            "age_seconds": max(0, (now_ms - observed) // 1000),
            "source": MARK_SOURCE,
        }
    except (OSError, ValueError, TypeError, OverflowError, AttributeError):
        return None


def read_equity_history_data(path: Path, kind: str, max_points: int = 1200) -> tuple[list[dict], float | None, int]:
    """Read complete snapshots, compute full-resolution drawdown, and downsample for UI."""
    if not path.is_file() or kind not in ("baseline", "hybrid", "fast"):
        return [], None, 0
    rows: list[dict] = []
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 90 * 24 * 60 * 60 * 1000
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if len(line) > 16384:
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                equity = item.get("equity_usdt")
                sampled = item.get("sampled_at_ms")
                if (
                    item.get("schema_version") == 1
                    and item.get("kind") == kind
                    and item.get("complete") is True
                    and type(sampled) is int
                    and sampled >= cutoff_ms
                    and isinstance(equity, (int, float))
                    and not isinstance(equity, bool)
                    and math.isfinite(equity)
                    and equity > 0
                ):
                    rows.append({
                        "time": datetime.fromtimestamp(sampled / 1000, tz=timezone.utc).isoformat(),
                        "equity": round(float(equity), 6),
                        "realized_usdt": item.get("realized_usdt"),
                        "unrealized_usdt": item.get("unrealized_usdt"),
                    })
    except OSError:
        return [], None, 0
    rows.sort(key=lambda row: row["time"])
    sample_count = len(rows)
    max_drawdown = None
    if len(rows) >= 2:
        peak = float(rows[0]["equity"])
        mdd = 0.0
        for row in rows:
            equity = float(row["equity"])
            if equity > peak:
                peak = equity
            if peak > 0:
                mdd = max(mdd, (peak - equity) / peak)
        max_drawdown = round(mdd, 8)

    # Preserve first/last and each bucket's extrema. This keeps the SVG small while
    # retaining visible spikes/dips; the KPI drawdown above is calculated before downsampling.
    if max_points >= 4 and len(rows) > max_points:
        bucket_count = max(1, max_points // 4)
        bucket_size = math.ceil(len(rows) / bucket_count)
        reduced: list[dict] = []
        for offset in range(0, len(rows), bucket_size):
            bucket = rows[offset:offset + bucket_size]
            if not bucket:
                continue
            indices = {0, len(bucket) - 1}
            indices.add(min(range(len(bucket)), key=lambda i: bucket[i]["equity"]))
            indices.add(max(range(len(bucket)), key=lambda i: bucket[i]["equity"]))
            reduced.extend(bucket[i] for i in sorted(indices))
        rows = reduced
    return rows, max_drawdown, sample_count


def read_equity_history(path: Path, kind: str, max_points: int = 1200) -> list[dict]:
    """Compatibility wrapper returning only chart-ready complete equity points."""
    return read_equity_history_data(path, kind, max_points)[0]


def summarize(
    db: Path,
    config_path: Path | None = None,
    marks_dir: Path | None = None,
    now_ms: int | None = None,
) -> dict | None:
    """Return a read-only ledger summary; return None for missing, locked or unknown DBs."""
    if not db.is_file():
        return None
    marks_dir = marks_dir or (ROOT / "user_data" / "marks")
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    starting_cash, cash_source = starting_cash_for(config_path)
    configured_fee = configured_fee_for(config_path)
    uri = f"file:{quote(str(db.resolve()), safe='/:')}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
        conn.execute("PRAGMA busy_timeout=200")
        fields = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        if not {"is_open", "close_profit_abs"}.issubset(fields):
            return None

        possible = [
            "is_open", "close_profit_abs", "close_date", "pair", "open_rate", "amount",
            "fee_open", "fee_close", "is_short", "realized_profit",
        ]
        selected = [name for name in possible if name in fields]
        rows = conn.execute(f"SELECT {', '.join(selected)} FROM trades").fetchall()

        closed: list[dict] = []
        open_positions: list[dict] = []
        for row in rows:
            item = dict(zip(selected, row))
            if bool(item["is_open"]):
                open_positions.append(item)
                continue
            raw_pnl = item.get("close_profit_abs")
            pnl = float(raw_pnl or 0.0)
            if not math.isfinite(pnl):
                return None
            close_date = item.get("close_date")
            pair = item.get("pair")
            closed.append({
                "time": str(close_date) if close_date else None,
                "pair": str(pair) if pair else None,
                "pnl_usdt": round(pnl, 6),
            })

        dated = [x for x in closed if x["time"]]
        dated.sort(key=lambda x: x["time"] or "")
        total_realized = round(sum(x["pnl_usdt"] for x in closed), 6)
        win_count = sum(x["pnl_usdt"] > 0 for x in closed)
        loss_count = sum(x["pnl_usdt"] < 0 for x in closed)
        win_rate = round(win_count / len(closed), 4) if closed else None

        position_rows: list[dict] = []
        unrealized_parts: list[float] = []
        mark_ages: list[int] = []
        unpriced_open_count = 0
        required_for_mark = {"pair", "open_rate", "amount"}
        for item in open_positions:
            pair = item.get("pair")
            mark = read_mark(marks_dir, str(pair), now_ms) if pair else None
            pnl = None
            reason = None
            if not required_for_mark.issubset(item.keys()):
                reason = "ledger schema lacks mark-to-market fields"
            elif mark is None:
                reason = "missing, stale or invalid public mark"
            else:
                try:
                    amount = float(item.get("amount"))
                    open_rate = float(item.get("open_rate"))
                    raw_fee_open = item.get("fee_open")
                    raw_fee_close = item.get("fee_close")
                    fee_open = configured_fee if raw_fee_open is None else float(raw_fee_open)
                    fee_close = configured_fee if raw_fee_close is None else float(raw_fee_close)
                    realized_inside_trade = float(item.get("realized_profit") or 0.0)
                    is_short = bool(item.get("is_short", False))
                    values = [amount, open_rate, fee_open, fee_close, realized_inside_trade]
                    if (
                        is_short
                        or not all(math.isfinite(v) for v in values)
                        or amount <= 0
                        or open_rate <= 0
                        or not (0 <= fee_open < 0.1 and 0 <= fee_close < 0.1)
                    ):
                        reason = "unsupported or invalid open-trade fields"
                    else:
                        # Matches Freqtrade's spot-long fee model: open value includes
                        # entry fee; close value is net of the estimated exit fee.
                        open_value = amount * open_rate * (1.0 + fee_open)
                        close_value = amount * mark["price"] * (1.0 - fee_close)
                        pnl = close_value - open_value + realized_inside_trade
                        if not math.isfinite(pnl):
                            pnl = None
                            reason = "non-finite mark-to-market result"
                        else:
                            mark_ages.append(mark["age_seconds"])
                            unrealized_parts.append(pnl)
                except (TypeError, ValueError, OverflowError):
                    reason = "invalid open-trade fields"
            if pnl is None:
                unpriced_open_count += 1
            position_rows.append({
                "pair": str(pair) if pair else None,
                "mark_price": mark["price"] if mark else None,
                "mark_age_seconds": mark["age_seconds"] if mark else None,
                "unrealized_usdt": round(pnl, 6) if pnl is not None else None,
                "mark_valid": pnl is not None,
                "mark_status": "marked" if pnl is not None else (reason or "unavailable"),
            })

        equity_complete = unpriced_open_count == 0
        unrealized = round(sum(unrealized_parts), 6) if equity_complete else None
        equity = round(starting_cash + total_realized + (unrealized or 0.0), 6) if equity_complete else None
        equity_change = round(equity - starting_cash, 6) if equity is not None else None
        equity_change_pct = round(equity_change / starting_cash * 100.0, 6) if equity_change is not None else None
        chronological = dated if dated else closed
        recent_trades = list(reversed(chronological[-MAX_RECENT_TRADES:]))
        return {
            "starting_cash_usdt": round(starting_cash, 6),
            "starting_cash_source": cash_source,
            "closed_count": len(closed),
            "open_count": len(open_positions),
            "realized_usdt": total_realized,
            "unrealized_usdt": unrealized,
            "equity_usdt": equity,
            "equity_change_usdt": equity_change,
            "equity_change_pct": equity_change_pct,
            "equity_complete": equity_complete,
            "unpriced_open_count": unpriced_open_count,
            "mark_age_seconds": max(mark_ages) if mark_ages else None,
            "open_positions": position_rows,
            "win_rate": win_rate,
            "win_count": win_count,
            "loss_count": loss_count,
            "recent_trades": recent_trades,
        }
    except (sqlite3.Error, ValueError, TypeError, OSError):
        # SQLite can be busy while Freqtrade commits a trade. A dashboard read should
        # degrade to an unavailable snapshot, not return HTTP 500 or a stale guess.
        return None
    finally:
        if conn is not None:
            conn.close()


def main() -> None:
    for kind in ("baseline", "hybrid", "fast"):
        db = ROOT / "runtime" / kind / f"{kind}.dryrun.sqlite"
        cfg = ROOT / "user_data" / f"config.{kind}.json"
        data = summarize(db, cfg, ROOT / "user_data" / "marks")
        if data is None:
            print(f"{kind:8s} no readable DB; start Docker first")
        else:
            print(
                f"{kind:8s} equity={data['equity_usdt']} realized={data['realized_usdt']} "
                f"unrealized={data['unrealized_usdt']} complete={data['equity_complete']}"
            )


if __name__ == "__main__":
    main()
