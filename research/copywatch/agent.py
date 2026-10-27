"""CopyWatch agent loop.

Design boundaries, all deliberate:

* The event source is public on-chain swap data, not a moving-average crossover.
  Nothing trades because a candle closed; something only happens because a real
  wallet made a real swap that passed the risk gate.
* The paper fill uses the price observed when we discovered the signal, adjusted
  adversely by a fixed slippage. The distance between the leader's fill price and
  our fill price is stored per position (``drift_bps``), so follow latency has a
  measurable cost instead of an assumed one.
* No wallet addresses or data source means the account reports "waiting for data".
  It never invents activity.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import sources, store
from .risk import REASON_LABELS, evaluate

SCHEMA_VERSION = 1
DEFAULT_CONFIG: dict = {
    "schema_version": SCHEMA_VERSION,
    "mode": "dry_run",
    "starting_cash_usdt": 500.0,
    "poll_seconds": 45,
    "slippage_bps": 5.0,
    "event_source": {
        "provider": "geckoterminal",
        "base_url": "https://api.geckoterminal.com/api/v2",
        "min_trade_usd": 500.0,
        "pools": [],
        # Pool discovery: watching only WETH/USDC meant 100% of signals were one
        # token. Trending pools are where new memes actually trade.
        "pool_discovery": {
            "enabled": True,
            "max_pools": 6,
            "refresh_every_rounds": 40,
            "min_reserve_usd": 50_000.0,
            # Trending is global; keep whatever chain the pools are on and use
            # the gas map in _maybe_discover_pools. Set to a list to restrict.
            "networks": [],
            "exclude_symbols": ["WETH", "USDC", "USDT", "DAI", "WBTC", "USDE", "USDG", "TON"],
        },
    },
    "wallet_policy": {
        "min_swaps": 3,
        "min_notional_usd": 2000.0,
        "whale_usd": 20000.0,
        # A wallet whose paper fills lose twice in a row stops being copied.
        "demote_after_losses": 2,
        # ...and cannot be promoted again for this long after being dropped.
        "demote_cooldown_hours": 24,
        # The ledger showed 3,964 ALREADY_OPEN skips: many tracked wallets buy
        # the same token minutes apart. One signal per symbol per window.
        "symbol_cooldown_minutes": 30,
        # Tracking 434 wallets produced heavily overlapping signals. Cap the
        # active set to the highest-volume wallets.
        "max_tracked_wallets": 120,
        # Many tracked wallets buy the same token minutes apart; only the
        # largest wallet's signal per symbol is followed, the rest are dupes.
        "max_wallets_per_symbol": 1,
    },
    "risk": {
        "notional_usdt": 100.0,
        "max_cost_bps": 60.0,
        "max_pool_impact_bps": 5.0,
        "min_pool_reserve_usd": 250_000.0,
        "max_price_age_seconds": 180,
        "cooldown_minutes": 20,
        "max_open_positions": 3,
        "max_daily_loss_usdt": 30.0,
    },
    "exit": {"take_profit_bps": 120.0, "stop_loss_bps": 80.0, "max_hold_minutes": 45},
}


class ConfigError(RuntimeError):
    pass


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"配置不可读：{exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"配置不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是对象")
    if raw.get("mode") != "dry_run":
        raise ConfigError("只允许 dry_run：本程序不存在实盘下单路径")
    config = _merge(DEFAULT_CONFIG, raw)
    if float(config["starting_cash_usdt"]) <= 0:
        raise ConfigError("starting_cash_usdt 必须为正数")
    for pool in config["event_source"]["pools"]:
        for key in ("network", "pool", "base_symbol"):
            if not isinstance(pool.get(key), str) or not pool[key]:
                raise ConfigError(f"池子配置缺少 {key}")
    return config


class HttpSource:
    """Thin wrapper so tests can inject a fake source with the same shape."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def swaps(self, pool_cfg: dict, min_usd: float, now_ms: int):
        return sources.fetch_swaps(
            network=pool_cfg["network"],
            pool=pool_cfg["pool"],
            base_symbol=pool_cfg["base_symbol"],
            quote_symbol=pool_cfg.get("quote_symbol", "USDT"),
            min_usd=min_usd,
            base_url=self.base_url,
            now_ms=now_ms,
        )

    def pool(self, pool_cfg: dict, now_ms: int):
        return sources.fetch_pool(
            network=pool_cfg["network"], pool=pool_cfg["pool"],
            base_url=self.base_url, now_ms=now_ms,
        )


class CopyWatchAgent:
    def __init__(self, *, root: Path, config_path: Path, db_path: Path, runtime_dir: Path,
                 source=None, now_ms_fn=None, log=print):
        self.root = Path(root)
        self.config_path = Path(config_path)
        self.db_path = Path(db_path)
        self.runtime_dir = Path(runtime_dir)
        self.config = load_config(self.config_path)
        self.source = source or HttpSource(self.config["event_source"]["base_url"])
        self.now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
        self.log = log
        self._pool_cache: dict[tuple[str, str], dict] = {}
        self._stop = False

    # ---------------------------------------------------------------- helpers
    def _cash(self, conn) -> float:
        start = float(self.config["starting_cash_usdt"])
        row = conn.execute(
            "SELECT COALESCE(SUM(entry_cost_usd), 0) AS open_cost FROM positions WHERE status = 'open'"
        ).fetchone()
        realized = store.closed_stats(conn)["realized_pnl_usd"]
        return start - float(row["open_cost"] or 0.0) + realized

    def _pool_view(self, conn, pool_cfg: dict, now_ms: int) -> dict:
        """Fetch (and cache for one loop) the pool snapshot used for pricing and filters."""
        key = (pool_cfg["network"], pool_cfg["pool"])
        cached = self._pool_cache.get(key)
        if cached is not None:
            return cached
        try:
            info = self.source.pool(pool_cfg, now_ms)
            view = {
                "ok": True,
                "network": info.network,
                "pool": info.pool,
                "name": info.name,
                "base_symbol": info.base_symbol,
                "quote_symbol": info.quote_symbol,
                "reserve_usd": info.reserve_usd,
                "price_usd": info.price_usd,
                "fetched_at_ms": info.fetched_at_ms,
            }
        except sources.SourceError as exc:
            view = {"ok": False, "error": str(exc), "price_usd": None,
                    "reserve_usd": None, "fetched_at_ms": now_ms, "name": pool_cfg["pool"]}
        self._pool_cache[key] = view
        return view

    def _net_position_value(self, position, mark: float) -> tuple[float, float]:
        slip_bps = float(self.config["slippage_bps"])
        gross = float(position["qty"]) * mark
        exit_slip = gross * slip_bps / 10_000
        exit_fee = gross * float(position["fee_bps"]) / 10_000
        gas = float(position["gas_usd"])
        net = gross - exit_slip - exit_fee - gas
        return net, net - float(position["entry_cost_usd"])

    def _write_heartbeat(self, payload: dict) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        target = self.runtime_dir / "heartbeat.json"
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, target)

    # ------------------------------------------------------------------- loop
    def _maybe_discover_pools(self, conn, source_cfg: dict, now_ms: int, summary: dict) -> None:
        """Refresh the discovered pool list every N rounds.

        Failures are non-fatal: the desk keeps whatever it discovered last time,
        and a first-round failure simply leaves the configured pools only.
        """
        cfg = source_cfg.get("pool_discovery") or {}
        if not cfg.get("enabled"):
            return
        every = int(cfg.get("refresh_every_rounds", 40))
        counter = store.kv_get(conn, "discovery_round")
        round_no = int(counter) + 1 if counter else 1
        store.kv_set(conn, "discovery_round", str(round_no))
        # Back off after a rate-limited attempt, otherwise a 429 turns into a
        # request every 45 seconds and the whole desk gets throttled.
        retry_after = int(store.kv_get(conn, "discovery_retry_after") or 0)
        if round_no < retry_after:
            return
        if round_no % every != 1 and store.kv_get(conn, "discovered_pools"):
            return
        base_url = source_cfg.get("base_url", "https://api.geckoterminal.com/api/v2")
        floor = float(cfg.get("min_reserve_usd", 50_000.0))
        excluded = {str(x).upper() for x in cfg.get("exclude_symbols", [])}
        # Rough per-chain gas for the paper fill cost. Unknown chains fall back
        # to a middle value; the risk gate rejects anything too expensive.
        gas_map = {"base": 0.05, "eth": 2.4, "solana": 0.5, "bsc": 0.2, "robinhood": 0.1,
                   "ton": 0.1, "arbitrum": 0.1, "polygon": 0.05}
        wanted = [str(n) for n in cfg.get("networks", [])] or None
        found: list[dict] = []
        try:
            rows = sources.fetch_trending_pools(network="", base_url=base_url, limit=20)
        except sources.SourceError as exc:
            summary["errors"].append(f"discovery:{exc}")
            store.kv_set(conn, "discovery_retry_after", str(round_no + 20))
            rows = []
        for row in rows:
            if wanted and row["network"] not in wanted:
                continue
            if str(row.get("base_symbol", "")).upper() in excluded:
                continue
            reserve = row.get("reserve_usd") or 0.0
            if reserve < floor:
                continue
            found.append(
                {
                    "network": row["network"],
                    "pool": row["pool"],
                    "base_symbol": row["base_symbol"],
                    "quote_symbol": row["quote_symbol"],
                    "label": row["label"],
                    "fee_bps": 30.0,
                    "gas_usd": gas_map.get(row["network"], 0.5),
                }
            )
        found = found[: int(cfg.get("max_pools", 6))]
        if found:
            store.kv_set(conn, "discovered_pools", json.dumps(found, ensure_ascii=False))
            store.add_event(
                conn, ts_ms=now_ms, tag="SYSTEM",
                title=f"池子发现：新增 {len(found)} 个活跃池",
                detail=" · ".join(f"{f['base_symbol']}/{f['quote_symbol']}({f['network']})" for f in found[:4]),
            )
            summary["discovered_pools"] = len(found)

    def _dedupe_by_symbol(self, conn, events: list, top_n: int) -> tuple[list, list]:
        """Keep the top_n largest-wallet signals per symbol, drop the dupes.

        Returns (kept, dropped). A wallet's size is its cumulative observed
        notional, so "the largest wallet bought X" survives and the nine
        smaller wallets that bought the same X minutes later do not.
        """
        if top_n <= 0 or len(events) <= 1:
            return events, []
        by_symbol: dict[str, list] = {}
        for event in events:
            by_symbol.setdefault(event.base_symbol, []).append(event)
        kept: list = []
        dropped: list = []
        for _symbol, group in by_symbol.items():
            if len(group) <= top_n:
                kept.extend(group)
                continue
            sized = []
            for event in group:
                row = conn.execute(
                    "SELECT notional_usd FROM wallets WHERE address = ? AND network = ?",
                    (event.wallet, event.network),
                ).fetchone()
                sized.append((float(row[0]) if row else 0.0, event))
            sized.sort(key=lambda item: item[0], reverse=True)
            kept.extend(event for _size, event in sized[:top_n])
            dropped.extend(event for _size, event in sized[top_n:])
        return kept, dropped

    def _symbol_cooling(self, conn, symbol: str, network: str, now_ms: int, window_ms: int) -> bool:
        """True when this symbol already produced a signal inside the window.

        Copy-tracking wallets cluster: dozens of tracked addresses buy the same
        token minutes apart. Without this gate every repeat is evaluated and
        skipped as ALREADY_OPEN, which is where 3,964 of our skips came from.
        """
        if window_ms <= 0:
            return False
        row = conn.execute(
            "SELECT MAX(COALESCE(exit_at_ms, 0), opened_at_ms) AS ts FROM positions"
            " WHERE symbol = ? AND network = ?",
            (symbol, network),
        ).fetchone()
        return bool(row and row[0] and now_ms - int(row[0]) < window_ms)

    def _maybe_demote(
        self, conn, position: sqlite3.Row, realized: float, now_ms: int, policy: dict, summary: dict
    ) -> None:
        """Drop a wallet whose last N paper fills all lost money.

        The streak is counted over this wallet's closed paper positions, newest
        first. One win resets the count, so a single bad fill never gets a wallet
        dropped, and the cooldown is enforced at promotion time.
        """
        limit = int(policy.get("demote_after_losses", 2))
        if limit <= 0:
            return
        trades = store.wallet_closed_trades(conn, position["wallet"], position["network"], limit)
        if len(trades) < limit:
            return
        streak = all(float(t["pnl_usd"]) < 0 for t in trades[:limit])
        if not streak:
            return
        losses = ", ".join(f"{t['symbol']} {float(t['pnl_usd']):+.2f}" for t in trades[:limit])
        note = f"连续 {limit} 笔亏损，自动下架：{losses}"
        if store.demote_wallet(conn, position["wallet"], position["network"], now_ms, note):
            summary.setdefault("demotions", []).append(position["wallet"])
            store.add_event(
                conn, ts_ms=now_ms, tag="DEMOTE", wallet=position["wallet"], symbol=position["symbol"],
                usd=realized,
                title=f"[DEMOTE] 钱包 {position['wallet'][:6]}…{position['wallet'][-4:]} 连续 {limit} 笔亏损，停止跟踪",
                detail=f"{losses} · 该钱包的链上成交不再产生跟单信号，"
                       f"{policy.get('demote_cooldown_hours', 24)} 小时后可重新纳入",
            )

    def poll_once(self) -> dict:
        now_ms = int(self.now_ms_fn())
        self.config = load_config(self.config_path)  # pick up config edits without restart
        self._pool_cache.clear()
        conn = store.connect(self.db_path)
        store.ensure_schema(conn)
        source_cfg = self.config["event_source"]
        pools_cfg = list(source_cfg.get("pools") or [])
        policy = self.config["wallet_policy"]
        risk_cfg = self.config["risk"]
        exit_cfg = self.config["exit"]
        slip_bps = float(self.config["slippage_bps"])
        min_usd = float(source_cfg.get("min_trade_usd", 0) or 0)
        summary = {"pools": len(pools_cfg), "fetched": 0, "new_swaps": 0, "actionable": 0,
                   "papers": 0, "skips": 0, "exits": 0, "demotions": [], "errors": []}
        # Discover trending pools first so this round already trades them.
        self._maybe_discover_pools(conn, source_cfg, now_ms, summary)
        discovery = source_cfg.get("pool_discovery") or {}
        if discovery.get("enabled"):
            found = store.kv_get(conn, "discovered_pools")
            if found:
                try:
                    extra = json.loads(found)
                except ValueError:
                    extra = []
                known = {item.get("pool") for item in pools_cfg}
                pools_cfg += [item for item in extra if item.get("pool") not in known]
        # Round-robin the pool list: GeckoTerminal's free tier throttles long
        # before 8 pools are done, and a skipped pool is simply queried next
        # round instead of burning the whole cycle on 429s.
        if len(pools_cfg) > 2:
            offset_raw = store.kv_get(conn, "pool_offset")
            offset = int(offset_raw) if offset_raw else 0
            total = len(pools_cfg)
            half = (total + 1) // 2
            start = offset % total
            rotated = pools_cfg[start:] + pools_cfg[:start]
            pools_cfg = rotated[:half]
            store.kv_set(conn, "pool_offset", str((start + half) % total))
        summary["pools"] = len(pools_cfg)

        if not pools_cfg:
            store.add_event(conn, ts_ms=now_ms, tag="SYSTEM", title="等待数据：未配置链上池子",
                            detail="config.copywatch.json 的 event_source.pools 为空。没有真实事件源时不会产生任何模拟成交。")
            conn.commit()
            conn.close()
            self._write_heartbeat({
                "schema_version": SCHEMA_VERSION, "source": "CopyWatch", "mode": "dry_run",
                "status": "waiting_for_data", "last_loop_at_ms": now_ms,
                "poll_seconds": int(self.config["poll_seconds"]),
                "detail": "未配置任何链上池子，等待数据源",
            })
            return {**summary, "status": "waiting_for_data"}

        actionable_window_ms = max(10 * 60_000, int(self.config["poll_seconds"]) * 1000 * 12)

        for pool_cfg in pools_cfg:
            cursor_key = f"cursor:{pool_cfg['network']}:{pool_cfg['pool']}"
            cursor_raw = store.kv_get(conn, cursor_key)
            first_poll = cursor_raw is None
            cursor_ts = int(cursor_raw) if cursor_raw else 0
            try:
                events = self.source.swaps(pool_cfg, min_usd, now_ms)
            except sources.SourceError as exc:
                summary["errors"].append(f"{pool_cfg['network']}:{exc}")
                store.add_event(conn, ts_ms=now_ms, tag="WARN", title="链上数据源读取失败",
                                detail=f"{pool_cfg['network']} · {pool_cfg['pool'][:10]}… · {exc}")
                continue
            summary["fetched"] += len(events)
            candidates = []
            for event in events:
                actionable = (
                    not first_poll
                    and event.block_ts_ms > cursor_ts
                    and (now_ms - event.block_ts_ms) <= actionable_window_ms
                )
                is_new = store.insert_swap(conn, event, actionable)
                if not is_new:
                    continue
                summary["new_swaps"] += 1
                wallet_row = store.upsert_wallet(conn, event)
                if not wallet_row["tracked"]:
                    demoted_at = wallet_row["demoted_at_ms"]
                    cooldown_ms = int(policy.get("demote_cooldown_hours", 24)) * 3_600_000
                    cooling = demoted_at is not None and now_ms - int(demoted_at) < cooldown_ms
                    gates = (
                        not cooling
                        and int(wallet_row["swaps"]) >= int(policy["min_swaps"])
                        and float(wallet_row["notional_usd"]) >= float(policy["min_notional_usd"])
                    )
                    if gates:
                        store.promote_wallet(
                            conn, event.wallet, event.network, now_ms,
                            f"链上活跃度达标：{int(wallet_row['swaps'])} 笔 / "
                            f"${float(wallet_row['notional_usd']):,.0f}",
                        )
                        store.add_event(
                            conn, ts_ms=now_ms, tag="PROMOTE", wallet=event.wallet,
                            symbol=event.base_symbol,
                            title=f"钱包 {event.wallet[:6]}…{event.wallet[-4:]} 纳入跟踪",
                            detail=f"链上可验证活跃度达标：{int(wallet_row['swaps'])} 笔成交 / "
                                   f"${float(wallet_row['notional_usd']):,.0f}（门槛 {policy['min_swaps']} 笔 / "
                                   f"${float(policy['min_notional_usd']):,.0f}）",
                        )
                if actionable and event.kind == "buy" and float(event.usd) >= min_usd:
                    cooling = self._symbol_cooling(
                        conn, event.base_symbol, event.network, now_ms,
                        int(policy.get("symbol_cooldown_minutes", 30)) * 60_000,
                    )
                    if cooling:
                        summary["skips"] += 1
                        store.add_event(
                            conn, ts_ms=now_ms, tag="SKIP", wallet=event.wallet,
                            symbol=event.base_symbol, usd=event.usd,
                            title=f"[SKIP] {event.base_symbol} 同标的冷却中",
                            detail=f"该标的 {policy.get('symbol_cooldown_minutes', 30)} 分钟内已有信号，避免跟踪钱包重复买入造成的信号轰炸",
                        )
                        continue
                    candidates.append(event)
            # One signal per symbol: the largest wallet's buy survives, the
            # duplicates from smaller wallets in the same window do not.
            top_n = int(policy.get("max_wallets_per_symbol", 1))
            if top_n > 0 and len(candidates) > 1:
                kept, dropped = self._dedupe_by_symbol(conn, candidates, top_n)
                for event in dropped:
                    summary["skips"] += 1
                    store.add_event(
                        conn, ts_ms=now_ms, tag="SKIP", wallet=event.wallet,
                        symbol=event.base_symbol, usd=event.usd,
                        title=f"[SKIP] {event.base_symbol} 同标的重复信号",
                        detail=f"该标的本轮已有更大型钱包的信号，按 max_wallets_per_symbol={top_n} 去重",
                    )
                candidates = kept
            if events:
                newest = max(item.block_ts_ms for item in events)
                store.kv_set(conn, cursor_key, str(max(newest, cursor_ts)))
            if first_poll and events:
                store.add_event(
                    conn, ts_ms=now_ms, tag="SYSTEM",
                    title=f"{pool_cfg['network']} 首次接入：回填 {len(events)} 条真实链上成交",
                    detail="首轮只用于建立钱包活跃度基线，不产生模拟成交；下一轮起只处理新事件。",
                )
            summary["actionable"] += len(candidates)

            for event in candidates:
                if store.position_opened(conn, event.event_id):
                    continue
                view = self._pool_view(conn, pool_cfg, now_ms)
                fee_bps = float(pool_cfg.get("fee_bps", 30))
                gas_usd = float(pool_cfg.get("gas_usd", 1.0))
                store.add_event(
                    conn, ts_ms=now_ms, tag="WATCH", wallet=event.wallet, symbol=event.base_symbol,
                    usd=event.usd,
                    title=f"[WATCH] {event.base_symbol} 买入 ${event.usd:,.0f} · {event.wallet[:8]}…",
                    detail=f"链上真实成交 tx {event.tx_hash[:12]}… · 区块 {event.block_number} · "
                           f"源成交价 ${event.price_usd:,.4f} · 发现延迟 {event.latency_ms / 1000:.0f}s",
                )
                decision = evaluate(
                    event=event,
                    pool=view,
                    wallet=dict(conn.execute(
                        "SELECT * FROM wallets WHERE address = ? AND network = ?",
                        (event.wallet, event.network),
                    ).fetchone() or {}),
                    policy=policy,
                    risk=risk_cfg,
                    slippage_bps=slip_bps,
                    fee_bps=fee_bps,
                    gas_usd=gas_usd,
                    context={
                        "now_ms": now_ms,
                        "cash_usd": self._cash(conn),
                        "open_count": len(store.open_positions(conn)),
                        "has_open_symbol": store.has_open_for(conn, event.base_symbol, event.network),
                        "last_open_ms": store.last_open_ms(conn),
                        "today_realized": store.realized_between(
                            conn, now_ms - now_ms % 86_400_000, now_ms + 1
                        ),
                    },
                )
                store.record_decision(
                    conn, event_id=event.event_id, now_ms=now_ms, verdict=decision.verdict,
                    signal_class=decision.signal_class, codes=decision.as_codes(), detail=decision.detail,
                )
                if decision.verdict == "PAPER" and view.get("price_usd"):
                    ref_price = float(view["price_usd"])
                    fill_price = ref_price * (1 + slip_bps / 10_000)
                    notional = float(risk_cfg["notional_usdt"])
                    qty = notional / fill_price
                    fee_usd = notional * fee_bps / 10_000
                    cost = qty * fill_price + fee_usd + gas_usd
                    store.insert_position(
                        conn, event=event, signal_class=decision.signal_class or "copy",
                        ref_price=ref_price, fill_price=fill_price, qty=qty, notional_usd=notional,
                        fee_usd=fee_usd, gas_usd=gas_usd, cost_usd=cost, fee_bps=fee_bps, now_ms=now_ms,
                    )
                    summary["papers"] += 1
                    drift = (ref_price - event.price_usd) / event.price_usd * 10_000 if event.price_usd else 0.0
                    store.add_event(
                        conn, ts_ms=now_ms, tag="PAPER", wallet=event.wallet, symbol=event.base_symbol,
                        usd=notional,
                        title=f"[PAPER] 模拟买入 {event.base_symbol} @ ${fill_price:,.4f}",
                        detail=f"{decision.signal_class} 信号 · 名义 ${notional:,.2f} · 手续费 ${fee_usd:.3f} · "
                               f"gas ${gas_usd:.2f} · 滑点 {slip_bps:.0f}bps · 相对源成交价偏离 {drift:+.1f}bps",
                    )
                else:
                    summary["skips"] += 1
                    primary = decision.primary_code or "UNKNOWN"
                    store.add_event(
                        conn, ts_ms=now_ms, tag="SKIP", wallet=event.wallet, symbol=event.base_symbol,
                        usd=event.usd,
                        title=f"[SKIP] {REASON_LABELS.get(primary, primary)}",
                        detail=decision.detail,
                    )

        # ------------------------------------------------ manage open positions
        open_rows = store.open_positions(conn)
        marks: dict[int, tuple[float, int]] = {}
        position_values: list[tuple[float, bool]] = []
        for position in open_rows:
            pool_cfg = next(
                (item for item in pools_cfg
                 if item["network"] == position["network"] and item["pool"] == position["pool"]),
                None,
            )
            if pool_cfg is None:
                position_values.append((0.0, False))
                continue
            view = self._pool_view(conn, pool_cfg, now_ms)
            price = view.get("price_usd")
            price_age = now_ms - int(view.get("fetched_at_ms") or 0)
            if not price or price_age > int(risk_cfg.get("max_price_age_seconds", 180)) * 1000:
                position_values.append((0.0, False))
                continue
            price = float(price)
            marks[int(position["id"])] = (price, now_ms)
            net, pnl = self._net_position_value(position, price)
            pnl_bps = pnl / float(position["entry_cost_usd"]) * 10_000 if position["entry_cost_usd"] else 0.0
            held_min = (now_ms - int(position["opened_at_ms"])) / 60_000
            reason = None
            if pnl_bps >= float(exit_cfg["take_profit_bps"]):
                reason = "TAKE_PROFIT"
            elif pnl_bps <= -abs(float(exit_cfg["stop_loss_bps"])):
                reason = "STOP_LOSS"
            elif held_min >= float(exit_cfg["max_hold_minutes"]):
                reason = "TIMEOUT"
            if reason is None:
                position_values.append((net, True))
                continue
            exit_fill = price * (1 - slip_bps / 10_000)
            exit_fee = float(position["qty"]) * exit_fill * float(position["fee_bps"]) / 10_000
            exit_gas = float(position["gas_usd"])
            proceeds = float(position["qty"]) * exit_fill - exit_fee - exit_gas
            realized = proceeds - float(position["entry_cost_usd"])
            store.close_position(
                conn, position_id=int(position["id"]), now_ms=now_ms, reason=reason,
                exit_fill=exit_fill, fee_usd=exit_fee, gas_usd=exit_gas,
                proceeds=proceeds, pnl=realized,
            )
            summary["exits"] += 1
            self._maybe_demote(conn, position, realized, now_ms, policy, summary)
            store.add_event(
                conn, ts_ms=now_ms, tag="EXIT", symbol=position["symbol"], usd=realized,
                title=f"[EXIT] {position['symbol']} {reason} · 净 ${realized:+,.2f}",
                detail=f"退出价 ${exit_fill:,.4f} · 净收益 {pnl_bps:+.1f}bps（含成本）· "
                       f"持有 {held_min:.1f} 分钟 · 入场成本 ${float(position['entry_cost_usd']):,.2f}",
            )
        if marks:
            store.update_marks(conn, marks)

        cash = self._cash(conn)
        open_after = store.open_positions(conn)
        positions_usd = 0.0
        complete = True
        for position in open_after:
            price_row = conn.execute(
                "SELECT mark_price, mark_at_ms FROM positions WHERE id = ?", (int(position["id"]),)
            ).fetchone()
            mark = price_row["mark_price"] if price_row else None
            age = now_ms - int(price_row["mark_at_ms"] or 0) if price_row and price_row["mark_at_ms"] else None
            if not mark or age is None or age > int(risk_cfg.get("max_price_age_seconds", 180)) * 1000:
                complete = False
                continue
            net, _ = self._net_position_value(position, float(mark))
            positions_usd += net
        equity = cash + positions_usd if complete else None
        store.add_equity_sample(
            conn, ts_ms=now_ms, cash=cash, positions=positions_usd,
            equity=equity if equity is not None else cash + positions_usd,
            complete=complete, open_count=len(open_after),
        )
        store.prune_events(conn)
        conn.commit()
        wallet_counts = store.wallet_stats(conn)
        conn.close()

        self._write_heartbeat({
            "schema_version": SCHEMA_VERSION,
            "source": "CopyWatch",
            "mode": "dry_run",
            "status": "running",
            "last_loop_at_ms": now_ms,
            "poll_seconds": int(self.config["poll_seconds"]),
            "pools": [f"{item['network']}:{item['pool'][:10]}…" for item in pools_cfg],
            "provider": source_cfg.get("provider"),
            "last_poll": summary,
            "wallets_observed": wallet_counts["observed"],
            "wallets_tracked": wallet_counts["tracked"],
            "open_positions": len(open_after),
            "equity_complete": complete,
        })
        return summary

    def run(self) -> None:
        # Unattended guards: a STOP file halts within one round, and the log
        # rotates so a long run cannot fill the disk.
        from ..guards import guard_round
        log_path = self.runtime_dir / "agent.log"
        while not self._stop:
            may_run, note = guard_round(self.root, log_path)
            if not may_run:
                print(f"[copywatch] halted: {note}", flush=True)
                return
            if note:
                print(f"[copywatch] {note}", flush=True)
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the desk
                print(f"[copywatch] poll failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(float(self.config.get("poll_seconds", 45)))

    def _run_loop_legacy(self) -> None:
        interval = max(10, int(self.config["poll_seconds"]))
        self.log(f"[copywatch] starting · poll {interval}s · db {self.db_path}")
        while not self._stop:
            started = time.monotonic()
            try:
                summary = self.poll_once()
                self.log(f"[copywatch] {json.dumps(summary, ensure_ascii=False)}")
            except Exception as exc:  # keep the heartbeat honest instead of dying silently
                self.log(f"[copywatch] poll failed: {exc}")
                self._write_heartbeat({
                    "schema_version": SCHEMA_VERSION, "source": "CopyWatch", "mode": "dry_run",
                    "status": "error", "last_loop_at_ms": int(self.now_ms_fn()),
                    "poll_seconds": interval, "last_error": str(exc)[:300],
                })
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))

    def stop(self, *_args) -> None:
        self._stop = True


def _default_paths() -> tuple[Path, Path, Path, Path]:
    root = Path(__file__).resolve().parents[2]
    return (
        root,
        root / "user_data" / "config.copywatch.json",
        root / "runtime" / "copywatch" / "copywatch.sqlite",
        root / "runtime" / "copywatch",
    )


def main(argv: list[str] | None = None) -> int:
    root, config_path, db_path, runtime_dir = _default_paths()
    parser = argparse.ArgumentParser(description="CopyWatch on-chain follow simulation (paper only)")
    parser.add_argument("command", nargs="?", default="loop", choices=["loop", "once"])
    parser.add_argument("--config", default=str(config_path))
    parser.add_argument("--db", default=str(db_path))
    parser.add_argument("--runtime", default=str(runtime_dir))
    args = parser.parse_args(argv)

    agent = CopyWatchAgent(root=root, config_path=Path(args.config), db_path=Path(args.db),
                           runtime_dir=Path(args.runtime))
    signal.signal(signal.SIGTERM, agent.stop)
    signal.signal(signal.SIGINT, agent.stop)
    if args.command == "once":
        summary = agent.poll_once()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
