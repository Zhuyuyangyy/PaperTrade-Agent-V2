"""Generate a self-contained light dashboard for a local paper-trading snapshot.

Account and signal values are read-only snapshots from local Freqtrade dry-run ledgers
and validated signal JSON files. The page makes no network calls and cannot place orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path
import json
import sqlite3
import sys
from urllib.parse import quote

try:
    from .report import read_equity_history_data, read_mark, starting_cash_for, summarize
except ImportError:  # pragma: no cover - supports direct script execution
    from report import read_equity_history_data, read_mark, starting_cash_for, summarize

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from research.counterpart import CounterpartDesk

try:
    import numpy as np
    import empyrical as _ep
except ImportError:  # pragma: no cover - metrics are optional
    np = None
    _ep = None


def risk_metrics(equity: list[float]) -> dict:
    """Standard risk stats from a real equity series.

    Returns {} when the series is too short or empyrical is missing: a panel
    that cannot compute Sharpe shows nothing rather than a made-up number.
    """
    if np is None or _ep is None or len(equity) < 8:
        return {}
    values = np.asarray(equity, dtype=float)
    rets = np.diff(values) / values[:-1]
    if not np.all(np.isfinite(rets)) or np.all(rets == 0):
        return {}
    try:
        return {
            "sharpe": round(float(_ep.sharpe_ratio(rets, annualization=8760)), 3),
            "sortino": round(float(_ep.sortino_ratio(rets, annualization=8760)), 3),
            "max_drawdown": round(float(_ep.max_drawdown(rets)) * 100.0, 3),
            "calmar": round(float(_ep.calmar_ratio(rets, annualization=8760)), 3),
            "var_95": round(float(_ep.value_at_risk(rets, cutoff=0.05)) * 100.0, 3),
            "cvar_95": round(float(_ep.conditional_value_at_risk(rets, cutoff=0.05)) * 100.0, 3),
            "samples": len(equity),
        }
    except (ZeroDivisionError, ValueError, FloatingPointError):
        return {}

PAIRS = ("BTC/USDT", "ETH/USDT")
HOUR_MS = 3_600_000
TIMEFRAME_MS = {"baseline": HOUR_MS, "hybrid": HOUR_MS, "fast": 15 * 60_000}
EXPECTED_STRATEGY = {"baseline": "PaperBaseline", "hybrid": "PaperHybrid", "fast": "PaperFast"}
MARKS_DIR = ROOT / "user_data" / "marks"
SNAPSHOT_JOURNAL = ROOT / "data" / "equity_snapshots.jsonl"


def account_paths(kind: str) -> tuple[Path, Path]:
    if kind not in ("baseline", "hybrid", "fast"):
        raise ValueError("Unapproved account kind")
    return (
        ROOT / "runtime" / kind / f"{kind}.dryrun.sqlite",
        ROOT / "user_data" / f"config.{kind}.json",
    )



def _parse_db_datetime_ms(value: object) -> int | None:
    """Parse Freqtrade's naive-UTC SQLite datetime values without guessing local time."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def _runtime_heartbeat(kind: str, now_ms: int) -> dict:
    """Read an atomic per-strategy heartbeat; a running container label is not evidence."""
    timeframe = {"baseline": "1h", "hybrid": "1h", "fast": "15m"}[kind]
    expected = EXPECTED_STRATEGY[kind]
    runtime = ROOT / "runtime" / kind
    candidates = [runtime / "strategy_heartbeat.json"]
    if kind == "fast":
        # Backward compatibility with the first FastLane heartbeat implementation.
        candidates.append(runtime / "fast_heartbeat.json")
    result = {
        "valid": False,
        "status": "尚未检测到策略心跳",
        "last_loop_at_ms": None,
        "age_seconds": None,
        "timeframe": timeframe,
    }
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            stamp = data.get("last_loop_at_ms")
            source_ok = data.get("source") == expected
            if path.name == "fast_heartbeat.json" and kind == "fast":
                source_ok = data.get("source") == "PaperFast"
            valid = (
                data.get("schema_version") == 1
                and source_ok
                and data.get("mode") == "dry_run"
                and data.get("status") == "running"
                and data.get("timeframe") == timeframe
                and type(stamp) is int
                and stamp <= now_ms
                and now_ms - stamp <= 60_000
            )
            if valid:
                age = max(0, (now_ms - stamp) // 1000)
                return {
                    "valid": True,
                    "status": "运行中",
                    "last_loop_at_ms": stamp,
                    "age_seconds": age,
                    "timeframe": timeframe,
                }
            result["status"] = "心跳过期或未通过校验"
        except (OSError, ValueError, TypeError, AttributeError):
            result["status"] = "心跳文件不可读"
    return result


def _pair_strategy_status(kind: str, pair: str, now_ms: int) -> dict:
    """Read only status written by the strategy; never infer a BUY from missing data."""
    timeframe_ms = TIMEFRAME_MS[kind]
    expected = EXPECTED_STRATEGY[kind]
    path = ROOT / "runtime" / kind / f"strategy_status_{pair.replace('/', '_')}.json"
    result = {"valid": False, "action": None, "reason": None, "status": "暂无策略状态", "observed_at_ms": None, "age_seconds": None}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            return result
        data = json.loads(path.read_text(encoding="utf-8"))
        observed = data.get("observed_at_ms")
        action = data.get("action")
        reason = data.get("reason")
        structurally_valid = (
            data.get("schema_version") == 1
            and data.get("pair") == pair
            and data.get("strategy") == expected
            and data.get("mode") == "dry_run"
            and data.get("timeframe") == {"baseline": "1h", "hybrid": "1h", "fast": "15m"}[kind]
            and type(observed) is int
            and observed <= now_ms + 5_000
            and action in ("BUY", "HOLD")
            and isinstance(reason, str)
            and len(reason) <= 240
        )
        if not structurally_valid:
            result["status"] = "策略状态未通过校验"
            return result
        age = max(0, (now_ms - observed) // 1000)
        if now_ms - observed > 2 * timeframe_ms:
            result.update({"status": "策略状态已过期", "observed_at_ms": observed, "age_seconds": age})
            return result
        return {
            "valid": True,
            "action": action,
            "reason": reason,
            "status": "候选买入" if action == "BUY" else "等待条件",
            "observed_at_ms": observed,
            "age_seconds": age,
        }
    except (OSError, ValueError, TypeError, AttributeError):
        result["status"] = "策略状态文件不可读"
        return result


def account_activity_snapshot(kind: str, db_path: Path, now_ms: int) -> dict:
    """Summarize last fill, active pair locks, heartbeat and next candle boundary."""
    timeframe_ms = TIMEFRAME_MS[kind]
    next_eval = (now_ms // timeframe_ms + 1) * timeframe_ms
    result = {
        "timeframe": "15m" if kind == "fast" else "1h",
        "heartbeat": _runtime_heartbeat(kind, now_ms),
        "last_fill_at_ms": None,
        "last_fill_age_seconds": None,
        "last_fill_pair": None,
        "last_fill_side": None,
        "last_fill_status": "账本尚未读取",
        "lock_data_available": False,
        "lock_status": "锁表尚未读取",
        "active_locks": [],
        "next_evaluation_at_ms": next_eval,
        "seconds_to_next_evaluation": max(0, (next_eval - now_ms) // 1000),
        "pair_statuses": {pair: _pair_strategy_status(kind, pair, now_ms) for pair in PAIRS},
    }
    if db_path.is_symlink() or not db_path.is_file():
        result["last_fill_status"] = "账本不可读"
        result["lock_status"] = "账本不可读"
        return result

    conn = None
    try:
        uri = f"file:{quote(str(db_path.resolve()), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
        conn.execute("PRAGMA busy_timeout=200")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        if "orders" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
            required = {"order_filled_date", "ft_order_side", "ft_pair"}
            if required.issubset(columns):
                row = conn.execute(
                    "SELECT order_filled_date, ft_order_side, ft_pair FROM orders "
                    "WHERE order_filled_date IS NOT NULL ORDER BY order_filled_date DESC LIMIT 1"
                ).fetchone()
                if row:
                    stamp = _parse_db_datetime_ms(row[0])
                    if stamp is not None and stamp <= now_ms + 5_000:
                        result.update({
                            "last_fill_at_ms": stamp,
                            "last_fill_age_seconds": max(0, (now_ms - stamp) // 1000),
                            "last_fill_side": str(row[1]) if row[1] else None,
                            "last_fill_pair": str(row[2]) if row[2] else None,
                            "last_fill_status": "已读取最近成交",
                        })
                    else:
                        result["last_fill_status"] = "最近成交时间异常"
                else:
                    result["last_fill_status"] = "暂无成交记录"
            else:
                result["last_fill_status"] = "订单表缺少成交字段"
        else:
            result["last_fill_status"] = "订单表不可读"

        if "pairlocks" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pairlocks)")}
            required = {"pair", "reason", "lock_end_time", "active"}
            if required.issubset(columns):
                for pair, reason, end_value, active in conn.execute(
                    "SELECT pair, reason, lock_end_time, active FROM pairlocks WHERE active = 1"
                ).fetchall():
                    end_ms = _parse_db_datetime_ms(end_value)
                    if end_ms is None or end_ms <= now_ms:
                        continue
                    result["active_locks"].append({
                        "pair": str(pair),
                        "reason": str(reason)[:180] if reason is not None else "未注明原因",
                        "lock_end_at_ms": end_ms,
                        "remaining_seconds": max(0, (end_ms - now_ms) // 1000),
                    })
                result["active_locks"].sort(key=lambda item: item["lock_end_at_ms"])
                result["lock_data_available"] = True
                result["lock_status"] = (
                    f"{len(result['active_locks'])} 个活跃锁"
                    if result["active_locks"] else "无活跃锁"
                )
            else:
                result["lock_status"] = "锁表缺少必要字段"
        else:
            result["lock_status"] = "锁表不可读"
    except (sqlite3.Error, ValueError, TypeError, OSError):
        result["last_fill_status"] = "账本读取失败"
        result["lock_data_available"] = False
        result["lock_status"] = "锁状态暂不可读"
    finally:
        if conn is not None:
            conn.close()
    return result


def account_snapshot(kind: str, now_ms: int | None = None) -> dict:
    db_path, config_path = account_paths(kind)
    now_ms = now_ms if now_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    activity = account_activity_snapshot(kind, db_path, now_ms)
    start_cash, cash_source = starting_cash_for(config_path)
    data = summarize(db_path, config_path, MARKS_DIR, now_ms)
    if data is None:
        return {
            "kind": kind,
            "has_ledger": False,
            "starting_cash_usdt": start_cash,
            "starting_cash_source": cash_source,
            "realized_usdt": None,
            "unrealized_usdt": None,
            "equity_usdt": None,
            "equity_change_usdt": None,
            "equity_change_pct": None,
            "equity_complete": False,
            "unpriced_open_count": None,
            "mark_age_seconds": None,
            "closed_count": None,
            "open_count": None,
            "win_rate": None,
            "win_count": None,
            "loss_count": None,
            "equity_history": [],
            "equity_max_drawdown": None,
            "equity_sample_count": 0,
            "recent_trades": [],
            "open_positions": [],
            "activity": activity,
            "label": "尚未启动或账本暂不可读",
        }
    data["kind"] = kind
    data["has_ledger"] = True
    data["activity"] = activity
    history, drawdown, sample_count = read_equity_history_data(SNAPSHOT_JOURNAL, kind)
    data["equity_history"] = history
    data["equity_max_drawdown"] = drawdown
    data["equity_sample_count"] = sample_count
    data["label"] = "已读取本地账本"
    return data


def signal_snapshot(pair: str, now_ms: int) -> dict:
    """Only expose an action if the signal passes basic provenance and freshness checks."""
    path = ROOT / "user_data" / "signals" / (pair.replace("/", "_") + ".json")
    result = {
        "pair": pair,
        "action": None,
        "valid": False,
        "status": "无有效信号",
        "observed_at_ms": None,
        "observed_label": "尚无记录",
        "source": None,
        "risk_veto": None,
        "votes": {},
    }
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32_768:
            return result
        data = json.loads(path.read_text(encoding="utf-8"))
        observed = data.get("observed_at_ms")
        expires = data.get("expires_at_ms")
        candle = data.get("candle_open_ms")
        action = data.get("action")
        if type(observed) is int:
            result["observed_at_ms"] = observed
            result["observed_label"] = datetime.fromtimestamp(
                observed / 1000, tz=timezone.utc
            ).strftime("%m-%d %H:%M UTC")
        result["source"] = data.get("source") if isinstance(data.get("source"), str) else None
        result["risk_veto"] = data.get("risk_veto") if type(data.get("risk_veto")) is bool else None
        votes = data.get("votes")
        result["votes"] = votes if isinstance(votes, dict) else {}

        structurally_valid = (
            data.get("schema_version") == 1
            and data.get("pair") == pair
            and data.get("timeframe") == "1h"
            and data.get("source") == "ccxt-public:kraken"
            and type(observed) is int
            and type(expires) is int
            and type(candle) is int
            and expires == candle + 2 * HOUR_MS
            and action in ("BUY", "HOLD", "SELL")
            and type(data.get("risk_veto")) is bool
        )
        fresh = (
            structurally_valid
            and observed <= now_ms < expires
            and now_ms - observed <= HOUR_MS
        )
        if fresh:
            result.update({
                "action": action,
                "valid": True,
                "status": "信号有效",
            })
        elif structurally_valid:
            result["status"] = "信号已过期"
        else:
            result["status"] = "数据未通过校验"
    except (OSError, ValueError, TypeError, OverflowError, AttributeError):
        result["status"] = "信号文件不可读"
    return result


def fast_heartbeat_snapshot(now_ms: int) -> dict:
    """Validate the fast account's local dry-run heartbeat; never infer it from a container label."""
    path = ROOT / "runtime" / "fast" / "fast_heartbeat.json"
    result = {"valid": False, "status": "尚未检测到运行心跳", "last_loop_at_ms": None, "age_seconds": None}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return result
        data = json.loads(path.read_text(encoding="utf-8"))
        stamp = data.get("last_loop_at_ms")
        if (data.get("schema_version") != 1 or data.get("source") != "PaperFast"
                or data.get("mode") != "dry_run" or data.get("status") != "running"
                or data.get("timeframe") != "15m" or type(stamp) is not int
                or stamp > now_ms or now_ms - stamp > 60_000):
            result["status"] = "心跳过期或未通过校验"
            return result
        result.update({"valid": True, "status": "快速实验运行中", "last_loop_at_ms": stamp,
                       "age_seconds": max(0, (now_ms - stamp) // 1000)})
    except (OSError, ValueError, TypeError, AttributeError):
        result["status"] = "心跳文件不可读"
    return result


def copywatch_snapshot(now_ms: int) -> dict:
    """Read the CopyWatch D ledger (event-driven, independent from A/B/C).

    Missing heartbeat, missing tables and stale equity samples are reported as such.
    This function never claims activity that the ledger cannot prove.
    """
    runtime_dir = ROOT / "runtime" / "copywatch"
    db_path = runtime_dir / "copywatch.sqlite"
    config_path = ROOT / "user_data" / "config.copywatch.json"
    result = {
        "kind": "copywatch",
        "label": "尚未启动",
        "has_ledger": False,
        "running": False,
        "heartbeat": {"valid": False, "status": "尚未检测到 CopyWatch 心跳", "age_seconds": None},
        "starting_cash_usdt": None,
        "cash_usdt": None,
        "equity_usdt": None,
        "equity_last_sample_usdt": None,
        "equity_history": [],
        "equity_complete": False,
        "equity_sample_age_seconds": None,
        "realized_pnl_usd": None,
        "unrealized_pnl_usd": None,
        "open_count": None,
        "closed_count": None,
        "win_count": None,
        "loss_count": None,
        "swap_events": None,
        "actionable_events": None,
        "decisions": None,
        "paper_fills": None,
        "skipped": None,
        "wallets_observed": None,
        "wallets_tracked": None,
        "wallets_demoted": None,
        "pool_count": None,
        "provider": None,
        "poll_seconds": None,
        "last_poll_at_ms": None,
        "next_poll_at_ms": None,
        "status": "尚未启动",
        "status_level": "warn",
        "status_detail": "未发现 runtime/copywatch 账本",
        "skip_breakdown": {},
        "events": [],
        "open_positions": [],
    }

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(config, dict):
            cash = config.get("starting_cash_usdt")
            result["starting_cash_usdt"] = float(cash) if isinstance(cash, (int, float)) else None
            pools = (config.get("event_source") or {}).get("pools") or []
            result["pool_count"] = len(pools) if isinstance(pools, list) else 0
            result["provider"] = (config.get("event_source") or {}).get("provider")
            result["poll_seconds"] = config.get("poll_seconds")
    except (OSError, ValueError, TypeError):
        result["status_detail"] = "config.copywatch.json 不可读"

    heartbeat = {"valid": False, "status": "尚未检测到 CopyWatch 心跳", "age_seconds": None}
    hb_path = runtime_dir / "heartbeat.json"
    try:
        if hb_path.is_file() and not hb_path.is_symlink() and hb_path.stat().st_size <= 8192:
            data = json.loads(hb_path.read_text(encoding="utf-8"))
            stamp = data.get("last_loop_at_ms")
            age = max(0, (now_ms - stamp) // 1000) if type(stamp) is int else None
            expected_gap = int(data.get("poll_seconds") or 45) * 3 + 30
            valid = (
                data.get("schema_version") == 1
                and data.get("source") == "CopyWatch"
                and data.get("mode") == "dry_run"
                and age is not None
                and stamp <= now_ms + 5_000
                and age <= expected_gap
            )
            heartbeat = {
                "valid": valid,
                "status": data.get("status") if isinstance(data.get("status"), str) else "unknown",
                "age_seconds": age,
                "poll_seconds": data.get("poll_seconds"),
                "last_error": data.get("last_error"),
            }
            if valid:
                result["last_poll_at_ms"] = stamp
                poll_seconds = int(data.get("poll_seconds") or 45)
                result["next_poll_at_ms"] = stamp + poll_seconds * 1000
    except (OSError, ValueError, TypeError):
        heartbeat["status"] = "心跳文件不可读"
    result["heartbeat"] = heartbeat
    result["running"] = heartbeat["valid"]

    if not db_path.is_file() or db_path.is_symlink():
        result["status"] = "尚未启动" if not heartbeat["valid"] else "等待首个账本样本"
        result["status_detail"] = "未发现 copywatch.sqlite；不显示任何模拟成交"
        return result

    conn = None
    try:
        uri = f"file:{quote(str(db_path.resolve()), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
        conn.execute("PRAGMA busy_timeout=200")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"positions", "wallets", "decisions", "events", "equity_samples"}
        if not required.issubset(tables):
            result["status"] = "账本结构不完整"
            result["status_detail"] = "copywatch.sqlite 缺少必要表，等待 agent 初始化"
            return result
        result["has_ledger"] = True

        latest = conn.execute(
            "SELECT ts_ms, cash_usd, positions_usd, equity_usd, complete, open_count"
            " FROM equity_samples ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        if latest:
            age = max(0, (now_ms - int(latest[0])) // 1000)
            result.update({
                "cash_usdt": float(latest[1]),
                "equity_last_sample_usdt": float(latest[3]),
                "equity_sample_age_seconds": age,
                "open_count": int(latest[5]),
                "equity_complete": bool(latest[4]) and age <= 300,
            })
            result["equity_usdt"] = float(latest[3]) if result["equity_complete"] else None

        # Real samples only: the line chart plots this series and nothing else.
        # Incomplete samples (a position without a fresh mark) are excluded, the
        # same rule the equity chart on the plain dashboard already uses.
        history = conn.execute(
            "SELECT ts_ms, equity_usd FROM equity_samples"
            " WHERE equity_usd IS NOT NULL AND complete = 1"
            " ORDER BY ts_ms DESC LIMIT 240"
        ).fetchall()
        result["equity_history"] = [
            {"t": int(row[0]), "v": float(row[1])} for row in reversed(history)
        ]
        result["risk"] = risk_metrics([p["v"] for p in result["equity_history"]])

        slip_bps = 5.0
        try:
            slippage = json.loads(config_path.read_text(encoding="utf-8")).get("slippage_bps")
            if isinstance(slippage, (int, float)):
                slip_bps = float(slippage)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        unrealized = 0.0
        unpriced = 0
        for row in conn.execute(
            "SELECT id, symbol, network, opened_at_ms, entry_cost_usd, mark_price, mark_at_ms,"
            " qty, fee_bps, gas_usd FROM positions WHERE status='open' ORDER BY id DESC"
        ).fetchall():
            mark, mark_at = row[5], row[6]
            if mark is None or mark_at is None or (now_ms - int(mark_at)) > 300_000:
                unpriced += 1
                continue
            gross = float(row[7]) * float(mark)
            net = gross - gross * slip_bps / 10_000 - gross * float(row[8]) / 10_000 - float(row[9])
            unrealized += net - float(row[4])
        result["unrealized_pnl_usd"] = unrealized if unpriced == 0 else None
        result["unpriced_open_count"] = unpriced

        closed = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl_usd),0),"
            " COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),0),"
            " COALESCE(SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END),0)"
            " FROM positions WHERE status='closed'"
        ).fetchone()
        result["closed_count"] = int(closed[0])
        result["realized_pnl_usd"] = float(closed[1])
        result["win_count"] = int(closed[2])
        result["loss_count"] = int(closed[3])

        wallets = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tracked),0),"
            " COALESCE(SUM(CASE WHEN demoted_at_ms IS NOT NULL THEN 1 ELSE 0 END),0)"
            " FROM wallets"
        ).fetchone()
        result["wallets_observed"] = int(wallets[0])
        result["wallets_tracked"] = int(wallets[1])
        result["wallets_demoted"] = int(wallets[2])

        if "swaps" in tables:
            swaps = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(actionable),0) FROM swaps"
            ).fetchone()
            result["swap_events"] = int(swaps[0])
            result["actionable_events"] = int(swaps[1])

        decisions = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN verdict='PAPER' THEN 1 ELSE 0 END),0),"
            " COALESCE(SUM(CASE WHEN verdict='SKIP' THEN 1 ELSE 0 END),0) FROM decisions"
        ).fetchone()
        result["decisions"] = int(decisions[0])
        result["paper_fills"] = int(decisions[1])
        result["skipped"] = int(decisions[2])
        for code, count in conn.execute(
            "SELECT primary_code, COUNT(*) FROM decisions WHERE verdict='SKIP'"
            " AND primary_code IS NOT NULL GROUP BY primary_code ORDER BY COUNT(*) DESC LIMIT 6"
        ).fetchall():
            result["skip_breakdown"][str(code)] = int(count)

        result["events"] = [
            {
                "ts_ms": int(row[0]),
                "tag": str(row[1]),
                "wallet": row[2],
                "symbol": row[3],
                "usd": float(row[4]) if row[4] is not None else None,
                "title": str(row[5]),
                "detail": str(row[6]),
            }
            for row in conn.execute(
                "SELECT ts_ms, tag, wallet, symbol, usd, title, detail FROM events"
                " ORDER BY id DESC LIMIT 36"
            ).fetchall()
        ]
        result["open_positions"] = [
            {
                "id": int(row[0]),
                "symbol": str(row[1]),
                "network": str(row[2]),
                "opened_at_ms": int(row[3]),
                "entry_cost_usd": float(row[4]),
                "mark_price": float(row[5]) if row[5] is not None else None,
            }
            for row in conn.execute(
                "SELECT id, symbol, network, opened_at_ms, entry_cost_usd, mark_price"
                " FROM positions WHERE status='open' ORDER BY id DESC LIMIT 8"
            ).fetchall()
        ]
    except (sqlite3.Error, ValueError, TypeError, OSError):
        result["status"] = "账本读取失败"
        result["status_level"] = "bad"
        result["status_detail"] = "copywatch.sqlite 不可读或正在写入"
        return result
    finally:
        if conn is not None:
            conn.close()

    if not heartbeat["valid"]:
        result["status"] = "未运行"
        result["status_level"] = "bad"
        age = heartbeat.get("age_seconds")
        result["status_detail"] = (
            f"心跳已过期（{age}s）· 权益显示为最后一次样本，不代表当前值"
            if age is not None else "未检测到心跳；不推断运行状态"
        )
    elif not result["pool_count"]:
        result["status"] = "等待数据：未配置池子"
        result["status_level"] = "warn"
        result["status_detail"] = "没有真实事件源时不会产生任何模拟成交"
    elif heartbeat.get("last_error"):
        result["status"] = "运行中 · 数据源异常"
        result["status_level"] = "warn"
        result["status_detail"] = str(heartbeat["last_error"])[:160]
    else:
        result["status"] = "运行中 · 事件驱动"
        result["status_level"] = "good"
        result["status_detail"] = (
            f"{result['provider'] or 'public API'} · {result['pool_count']} 个池子 · "
            f"{int(result['poll_seconds'] or 45)}s 轮询"
        )
    result["label"] = "已读取 D 账户账本"
    return result


def _memehunter_starting_cash() -> float:
    """E account opening balance, read from its config file when present."""
    path = ROOT / "user_data" / "config.memehunter.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1000.0
    value = data.get("starting_cash_usdt")
    return float(value) if isinstance(value, (int, float)) else 1000.0


AGENT_COLORS = {
    "SEARCH": "#2dd4bf",
    "RISK": "#a78bfa",
    "GMGN": "#4a9eff",
    "GROK": "#f0a13a",
    "FOMO": "#35d07f",
    "EXIT": "#8fa3b6",
}


def _safe_detail(raw: object, limit: int = 1200) -> dict:
    """Parse a step's detail JSON. A broken blob yields {} rather than raising."""
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def memehunter_snapshot(now_ms: int) -> dict:
    """Read the MemeHunter E ledger (self-directed meme picks, isolated account).

    Read-only and fail-closed: if the ledger or a mark is missing, the fields are
    reported as absent rather than filled with a plausible-looking number.
    """
    runtime_dir = ROOT / "runtime" / "memehunter"
    db_path = runtime_dir / "memehunter.sqlite"
    result = {
        "kind": "memehunter",
        "label": "尚未启动",
        "has_ledger": False,
        "starting_cash_usdt": None,
        "cash_usdt": None,
        "positions_usd": None,
        "equity_usdt": None,
        "pnl_usd": None,
        "pnl_pct": None,
        "realized_usd": None,
        "open_count": 0,
        "closed_count": 0,
        "win_rate": None,
        "last_scan_age_seconds": None,
        "tokens_seen": None,
        "tokens_eligible": None,
        "scans": 0,
        "veto_breakdown": {},
        "open_positions": [],
        "closed_positions": [],
        "recent_picks": [],
        "pipeline": None,
        "status": "尚未启动",
        "status_level": "warn",
        "status_detail": "未发现 runtime/memehunter 账本",
    }
    if not db_path.is_file() or db_path.is_symlink():
        return result

    try:
        conn = sqlite3.connect(f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        result["status_detail"] = f"无法只读打开账本: {exc}"
        return result

    try:
        positions = list(conn.execute("SELECT * FROM positions ORDER BY id"))
        scans = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        last_scan = conn.execute(
            "SELECT scanned_at_ms, tokens_seen, tokens_eligible FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()

        opens = [p for p in positions if p["status"] == "open"]
        closed = [p for p in positions if p["status"] == "closed"]
        # Cash is derived the same way the desk derives it: principal plus entry
        # fee leaves on open, proceeds net of exit fee return on close.
        spent = sum(float(p["notional_usd"]) + float(p["entry_cost_usd"] or 0.0) for p in positions)
        back = sum(
            float(p["qty"]) * float(p["exit_fill_price"]) - float(p["exit_cost_usd"] or 0.0)
            for p in closed
            if p["exit_fill_price"] is not None
        )
        unpriced = [p["symbol"] for p in opens if not p["mark_price"]]
        holdings = sum(float(p["mark_price"]) * float(p["qty"]) for p in opens if p["mark_price"])
        result["unpriced_count"] = len(unpriced)
        result["equity_complete"] = not unpriced
        starting = float(_memehunter_starting_cash())
        cash = starting - spent + back

        result["starting_cash_usdt"] = starting
        result["cash_usdt"] = round(cash, 4)
        result["positions_usd"] = round(holdings, 4)
        result["equity_usdt"] = round(cash + holdings, 4)
        result["pnl_usd"] = round(cash + holdings - starting, 4)
        result["pnl_pct"] = round((cash + holdings - starting) / starting * 100.0, 3)
        result["realized_usd"] = round(sum(float(p["pnl_usd"] or 0.0) for p in closed), 4)
        result["open_count"] = len(opens)
        result["closed_count"] = len(closed)
        if closed:
            wins = sum(1 for p in closed if float(p["pnl_usd"] or 0) > 0)
            result["win_rate"] = round(wins / len(closed) * 100.0, 1)
        result["scans"] = scans
        if last_scan:
            result["last_scan_age_seconds"] = max(0, (now_ms - int(last_scan["scanned_at_ms"])) // 1000)
            result["tokens_seen"] = int(last_scan["tokens_seen"])
            result["tokens_eligible"] = int(last_scan["tokens_eligible"])

        scan_id = conn.execute("SELECT MAX(id) FROM scans").fetchone()[0]
        if scan_id:
            for row in conn.execute(
                "SELECT reject_reason, COUNT(*) n FROM candidates WHERE scan_id=? "
                "GROUP BY reject_reason ORDER BY n DESC",
                (scan_id,),
            ):
                key = row["reject_reason"] or "PASS"
                result["veto_breakdown"][key] = int(row["n"])
            for row in conn.execute(
                "SELECT symbol, score, eligible, reject_reason FROM candidates "
                "WHERE scan_id=? ORDER BY score DESC LIMIT 6",
                (scan_id,),
            ):
                result["recent_picks"].append(
                    {
                        "symbol": row["symbol"],
                        "score": round(float(row["score"] or 0.0), 1),
                        "eligible": bool(row["eligible"]),
                        "reason": row["reject_reason"] or "PASS",
                    }
                )

        for p in opens:
            mark = float(p["mark_price"] or 0.0)  # display only; equity excludes unpriced
            entry = float(p["entry_fill_price"])
            result["open_positions"].append(
                {
                    "symbol": p["symbol"],
                    "entry": entry,
                    "mark": mark,
                    "change_pct": round((mark / entry - 1.0) * 100.0, 2) if mark else None,
                    "value_usd": round(mark * float(p["qty"]), 4),
                    "age_min": round((now_ms - int(p["opened_at_ms"])) / 60_000.0, 1),
                    "score": round(float(p["score"] or 0.0), 1),
                    "peak_pct": round((float(p["peak_price"] or entry) / entry - 1.0) * 100.0, 2),
                }
            )
        for p in reversed(closed[-6:]):
            result["closed_positions"].append(
                {
                    "symbol": p["symbol"],
                    "reason": p["exit_reason"],
                    "pnl_usd": round(float(p["pnl_usd"] or 0.0), 4),
                    "pnl_pct": round(float(p["pnl_pct"] or 0.0), 2),
                }
            )

        age = result["last_scan_age_seconds"]
        result["has_ledger"] = True
        result["label"] = "已读取 E 账户账本"

        # Risk metrics from this desk's own real equity samples.
        equity_series = [
            row[0]
            for row in conn.execute(
                "SELECT equity_usd FROM equity WHERE complete=1 ORDER BY at_ms"
            )
        ]
        result["risk"] = risk_metrics(equity_series)

        # Six-agent pipeline: the latest run, one row per agent. Read-only; a
        # desk that never ran the pipeline simply reports no pipeline at all.
        try:
            # Only a finished round: the pipeline commits its run row first and
            # its step rows last, so an in-flight round reads as zero agents.
            run = conn.execute(
                "SELECT id, started_ms, finished_ms, ok FROM pipeline_runs"
                " WHERE finished_ms IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if run:
                steps = [
                    {
                        "seq": int(row[0]),
                        "agent": str(row[1]),
                        "ok": bool(row[2]),
                        "summary": str(row[3]),
                        "produced": int(row[4]),
                        "rejected": int(row[5]),
                        "elapsed_ms": int(row[6]),
                        "detail": _safe_detail(row[7]),
                        "color": AGENT_COLORS.get(str(row[1]), "#8fa3b6"),
                    }
                    for row in conn.execute(
                        "SELECT seq, agent, ok, summary, produced, rejected, elapsed_ms, detail"
                        " FROM pipeline_steps WHERE run_id=? ORDER BY seq",
                        (run[0],),
                    )
                ]
                result["pipeline"] = {
                    "run_id": int(run[0]),
                    "started_ms": int(run[1]),
                    "finished_ms": int(run[2]) if run[2] is not None else None,
                    "ok": bool(run[3]),
                    "steps": steps,
                    "age_seconds": max(0, (now_ms - int(run[1])) // 1000),
                }
        except sqlite3.Error:
            result["pipeline"] = None

        if age is None:
            result["status"] = "等待首轮扫描"
            result["status_level"] = "warn"
        elif age > 900:
            # A stale scan means exits are not being evaluated, so the equity
            # printed below is a frozen snapshot, not a live mark.
            result["status"] = "已停止 · 最后扫描 " + f"{age // 60} 分钟前"
            result["status_level"] = "warn"
            result["status_detail"] = "进程未运行：止盈止损不会触发，权益为冻结快照"
        else:
            result["status"] = "运行中 · 自选标的"
            result["status_level"] = "good"
            result["status_detail"] = (
                f"最近一轮看到 {result['tokens_seen']} 个代币，"
                f"{result['tokens_eligible']} 个通过门槛"
            )
    except sqlite3.Error as exc:
        result["status_detail"] = f"读取账本失败: {exc}"
        result["status_level"] = "warn"
    finally:
        conn.close()
    return result


def _portfolio_snapshot(accounts: dict, copywatch: dict, memehunter: dict, counterpart: dict | None = None) -> dict:
    """Whole-desk snapshot: A/B/C dry-run accounts plus the D and E paper desks.

    The top KPI used to aggregate only the three freqtrade accounts, so a stopped
    container stack left a frozen "3000" on screen while D/E were trading. This
    keeps every engine visible and labels each part with its freshness instead of
    silently dropping or silently trusting it.
    """
    # Only A and B: C moved to the control group (counterpart.sqlite).
    ft_equities = [accounts[k].get("equity_usdt") for k in ("baseline", "hybrid")]
    ft_starts = [item.get("starting_cash_usdt") for item in accounts.values()]
    freqtrade_equity = (
        sum(float(v) for v in ft_equities)
        if ft_equities and all(isinstance(v, (int, float)) for v in ft_equities)
        else None
    )
    freqtrade_start = (
        # C is no longer a freqtrade dry-run: it is the control group in
        # runtime/memehunter/counterpart.sqlite, so only A and B stay here.
        sum(float(accounts[k]["starting_cash_usdt"]) for k in ("baseline", "hybrid"))
    )
    freqtrade_closed = sum(int(item["closed_count"] or 0) for item in accounts.values())
    freqtrade_open = sum(int(item["open_count"] or 0) for item in accounts.values())

    cw_equity = copywatch.get("equity_usdt")
    if cw_equity is None:
        cw_equity = copywatch.get("equity_last_sample_usdt")
    cw_stale = copywatch.get("equity_usdt") is None and cw_equity is not None
    mh_equity = memehunter.get("equity_usdt")
    mh_age = memehunter.get("last_scan_age_seconds")
    mh_stale = mh_equity is not None and mh_age is not None and mh_age > 900

    cp = counterpart or {}
    cp_equity = cp.get("equity_usdt")
    cp_equity = float(cp_equity) if isinstance(cp_equity, (int, float)) else None

    parts = [freqtrade_equity, cw_equity, mh_equity, cp_equity]
    total = sum(parts) if all(v is not None for v in parts) else None
    starting_total = (
        freqtrade_start
        + float(copywatch.get("starting_cash_usdt") or 0.0)
        + float(memehunter.get("starting_cash_usdt") or 0.0)
        + float(cp.get("starting_cash_usdt") or 0.0)
    )
    cw_heartbeat = (copywatch.get("heartbeat") or {})
    cw_valid = bool(copywatch.get("running"))
    freqtrade_running = any(
        (item.get("activity") or {}).get("heartbeat", {}).get("valid") for item in accounts.values()
    )
    engines = [
        {
            "key": "freqtrade",
            "name": "A/B/C · freqtrade dry-run",
            "running": freqtrade_running,
            "detail": (
                f"账本 {freqtrade_closed} 笔平仓 / {freqtrade_open} 笔持仓 · "
                "容器未运行时不产生任何行情与成交"
                if freqtrade_closed == 0 and freqtrade_open == 0
                else f"账本 {freqtrade_closed} 笔平仓 / {freqtrade_open} 笔持仓"
            ),
        },
        {
            "key": "copywatch",
            "name": "D · CopyWatch 链上跟单",
            "running": cw_valid,
            "detail": (
                f"心跳 {cw_heartbeat.get('age_seconds')}s 前 · 持仓 {copywatch.get('open_count') or 0} 笔"
                if cw_valid
                else (
                    f"心跳已过期（{cw_heartbeat.get('age_seconds')}s）· 权益为最后样本"
                    if cw_heartbeat.get("age_seconds") is not None
                    else "未检测到心跳"
                )
            ),
        },
        {
            "key": "memehunter",
            "name": "E · MemeHunter 自选标的",
            "running": memehunter.get("status_level") == "good",
            "detail": (
                f"最近扫描 {mh_age} 秒前 · 持仓 {memehunter.get('open_count') or 0} 笔"
                if mh_age is not None
                else "等待首轮扫描"
            ),
        },
    ]
    return {
        "starting_total_usdt": round(starting_total, 4),
        "freqtrade": {
            "start_usdt": round(freqtrade_start, 4),
            "equity_usdt": round(freqtrade_equity, 4) if freqtrade_equity is not None else None,
            "closed_count": freqtrade_closed,
            "open_count": freqtrade_open,
            "frozen": freqtrade_closed == 0 and freqtrade_open == 0,
        },
        "copywatch": {
            "start_usdt": round(float(copywatch.get("starting_cash_usdt") or 0.0), 4),
            "equity_usdt": round(cw_equity, 4) if cw_equity is not None else None,
            "stale": cw_stale,
            "sample_age_seconds": copywatch.get("equity_sample_age_seconds"),
        },
        "memehunter": {
            "start_usdt": round(float(memehunter.get("starting_cash_usdt") or 0.0), 4),
            "equity_usdt": round(mh_equity, 4) if mh_equity is not None else None,
            "stale": mh_stale,
            "sample_age_seconds": mh_age,
        },
        "counterpart": {
            "start_usdt": round(float(cp.get("starting_cash_usdt") or 0.0), 4),
            "equity_usdt": round(cp_equity, 4) if cp_equity is not None else None,
            "stale": False,
        },
        "total_usdt": round(total, 4) if total is not None else None,
        "pnl_usdt": round(total - starting_total, 4) if total is not None else None,
        "pnl_pct": round((total / starting_total - 1.0) * 100.0, 3) if total is not None and starting_total else None,
        "engines": engines,
        "running_count": sum(1 for engine in engines if engine["running"]),
        "engine_count": len(engines),
    }


def counterpart_snapshot() -> dict:
    """C account: the control group that trades the same candidates as E.

    Read-only view of research/counterpart.py's ledger. A missing ledger means
    the control group has never run, which is reported as absent rather than as
    a plausible 1,000.00.
    """
    db_path = ROOT / "runtime" / "memehunter" / "counterpart.sqlite"
    empty = {
        "kind": "counterpart", "has_ledger": False, "starting_cash_usdt": None,
        "cash_usdt": None, "equity_usdt": None, "pnl_usd": None, "pnl_pct": None,
        "open_count": 0, "closed_count": 0, "win_count": 0, "realized_usd": None,
        "open_positions": [],
        "unpriced_count": 0, "equity_complete": True,
        "risk": {},
    }
    if not db_path.is_file():
        return empty
    try:
        desk = CounterpartDesk(ROOT)
        snapshot = desk.snapshot()
        series = [
            row[0]
            for row in desk.desk.conn.execute(
                "SELECT equity_usd FROM equity WHERE complete=1 ORDER BY at_ms"
            )
        ]
        snapshot["risk"] = risk_metrics(series)
        desk.close()
        return snapshot
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return empty


def _read_equity_series(db_path: Path, table: str, ts_col: str, val_col: str,
                         copy_first: bool = False) -> list[tuple[int, float]]:
    """(t, equity) pairs from one ledger, oldest first.

    The CopyWatch ledger lives in WAL mode and its path contains non-ASCII
    characters, so a URI open fails; copying the file first is the reliable
    read. Only complete samples are used.
    """
    import shutil
    import tempfile

    if not db_path.is_file():
        return []
    target = db_path
    tmp_dir = None
    if copy_first:
        tmp_dir = tempfile.mkdtemp()
        target = Path(tmp_dir) / db_path.name
        try:
            shutil.copy(db_path, target)
        except OSError:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return []
    try:
        conn = sqlite3.connect(str(target), timeout=10.0)
        try:
            rows = conn.execute(
                f"SELECT {ts_col}, {val_col} FROM {table} WHERE complete=1"
                f" AND {val_col} IS NOT NULL ORDER BY {ts_col}"
            ).fetchall()
        finally:
            conn.close()
        return [(int(r[0]), float(r[1])) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def equity_curves() -> list[dict]:
    """D/E/C return curves on one time axis, in percent of each book's start.

    C's ledger is young (its equity table only started filling recently), so
    its curve starts late -- that is honest, and it is visible as a short line.
    """
    series = {
        "d": _read_equity_series(
            ROOT / "runtime" / "copywatch" / "copywatch.sqlite",
            "equity_samples", "ts_ms", "equity_usd", copy_first=True),
        "e": _read_equity_series(
            ROOT / "runtime" / "memehunter" / "memehunter.sqlite",
            "equity", "at_ms", "equity_usd"),
        "c": _read_equity_series(
            ROOT / "runtime" / "memehunter" / "counterpart.sqlite",
            "equity", "at_ms", "equity_usd"),
    }
    starts = {"d": 500.0, "e": 1000.0, "c": 1000.0}
    for key in ("d", "e", "c"):
        if series[key]:
            starts[key] = series[key][0][1]
    # Downsample each series to at most 180 points so the SVG stays light.
    out: list[dict] = []
    keys = [k for k in ("d", "e", "c") if series[k]]
    if not keys:
        return []
    t_min = min(series[k][0][0] for k in keys)
    t_max = max(series[k][-1][0] for k in keys)
    span = max(t_max - t_min, 1)
    buckets = 180
    for i in range(buckets + 1):
        t = t_min + span * i / buckets
        point: dict = {"t": int(t)}
        for k in keys:
            vals = [v for (tt, v) in series[k] if tt <= t]
            point[k] = round((vals[-1] / starts[k] - 1.0) * 100.0, 3) if vals else None
        out.append(point)
    # Drop leading points where nothing has started yet.
    while out and all(out[0].get(k) is None for k in keys):
        out.pop(0)
    return out


def build_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    accounts = {kind: account_snapshot(kind, now_ms) for kind in ("baseline", "hybrid", "fast")}
    signals = {pair: signal_snapshot(pair, now_ms) for pair in PAIRS}
    marks = {pair: read_mark(MARKS_DIR, pair, now_ms) for pair in PAIRS}
    fast_status = fast_heartbeat_snapshot(now_ms)
    has_live_data = any(item["has_ledger"] for item in accounts.values()) or any(
        item["valid"] for item in signals.values()
    ) or any(item is not None for item in marks.values()) or fast_status["valid"] or any(
        item.get("activity", {}).get("heartbeat", {}).get("valid") for item in accounts.values()
    )
    start_cash, _ = starting_cash_for(ROOT / "user_data" / "config.baseline.json")
    starting_total = sum(item["starting_cash_usdt"] for item in accounts.values())
    fully_marked = all(item["has_ledger"] and item["equity_complete"] for item in accounts.values())
    equity_total = sum(item["equity_usdt"] for item in accounts.values()) if fully_marked else None
    copywatch = copywatch_snapshot(now_ms)
    memehunter = memehunter_snapshot(now_ms)
    counterpart = counterpart_snapshot()
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "generated_at_ms": now_ms,
        "has_live_data": has_live_data,
        "paper_only": True,
        "starting_cash_usdt": start_cash,
        "starting_cash_total_usdt": starting_total,
        "equity_total_usdt": equity_total,
        "equity_total_change_usdt": (equity_total - starting_total) if equity_total is not None else None,
        "equity_complete": fully_marked,
        "portfolio": _portfolio_snapshot(accounts, copywatch, memehunter, counterpart),
        "accounts": accounts,
        "copywatch": copywatch,
        "memehunter": memehunter,
        "counterpart": counterpart,
        "curves": equity_curves(),
        "signals": signals,
        "marks": marks,
        "fast_status": fast_status,
    }


def build_html(now: datetime | None = None) -> str:
    state = build_state(now)
    safe_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    # Prevent a local data field from closing the script element.
    safe_json = safe_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template_path = ROOT / "templates" / "dashboard.html"
    template = template_path.read_text(encoding="utf-8")
    return template.replace("__STATE_DATA__", safe_json).replace(
        "__GENERATED_AT__", escape(state["generated_at"])
    )


def main() -> None:
    out = ROOT / "data" / "paper_dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(), encoding="utf-8")
    print(f"PaperTrade Agent dashboard generated: {out}")
    print("Read-only local snapshot; no real orders or synthetic P&L are generated.")


if __name__ == "__main__":
    main()
