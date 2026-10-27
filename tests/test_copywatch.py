"""Tests for the CopyWatch D account.

These tests use a fake event source but the real ledger, risk gate and accounting code,
so the arithmetic that the dashboard shows is the arithmetic that actually ran.
"""
from __future__ import annotations

import json

import pytest

from research.copywatch import sources, store
from research.copywatch.agent import ConfigError, CopyWatchAgent, load_config
from research.copywatch.risk import evaluate

START_MS = 1_790_000_000_000
BASE_POOL = {
    "network": "base",
    "pool": "0x72ab388e2e2f6facef59e3c3fa2c4e29011c2d38",
    "base_symbol": "PEPE",
    "quote_symbol": "USDC",
    "fee_bps": 1.0,
    "gas_usd": 0.02,
}
ETH_POOL = {
    "network": "eth",
    "pool": "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640",
    "base_symbol": "PEPE",
    "quote_symbol": "USDC",
    "fee_bps": 5.0,
    "gas_usd": 2.4,
}
WALLET_A = "0xaaaa000000000000000000000000000000000001"
WALLET_B = "0xbbbb000000000000000000000000000000000002"


class FakeSource:
    """Deterministic stand-in for the public pool API."""

    def __init__(self, pools: dict[str, dict]):
        self.pools = {cfg["pool"]: cfg for cfg in pools}
        self.events: list[sources.SwapEvent] = []
        self.price = {"": None}
        self.prices: dict[str, float | None] = {cfg["pool"]: 2000.0 for cfg in pools}
        self.fail = False

    def add(self, *, wallet: str, kind: str = "buy", usd: float = 600.0,
            block_ms: int, price: float = 2000.0, anomalous: bool = False,
            pool: str | None = None) -> sources.SwapEvent:
        cfg = self.pools[pool] if pool else next(iter(self.pools.values()))
        event = sources.SwapEvent(
            event_id=f"{cfg['network']}:{wallet}:{block_ms}:{kind}",
            network=cfg["network"], pool=cfg["pool"], wallet=wallet, kind=kind, usd=usd,
            price_usd=price, base_symbol=cfg["base_symbol"], quote_symbol=cfg["quote_symbol"],
            block_ts_ms=block_ms, block_number=1, tx_hash=f"0x{block_ms:x}",
            anomalous=anomalous, discovered_at_ms=block_ms, latency_ms=0,
        )
        self.events.append(event)
        return event

    def swaps(self, pool_cfg, min_usd, now_ms):
        if self.fail:
            raise sources.SourceError("模拟网络失败")
        return [
            item for item in self.events
            if item.pool == pool_cfg["pool"] and item.usd >= min_usd
        ]

    def pool(self, pool_cfg, now_ms):
        return sources.PoolInfo(
            network=pool_cfg["network"], pool=pool_cfg["pool"], name="WETH / USDC",
            base_symbol=pool_cfg["base_symbol"], quote_symbol=pool_cfg["quote_symbol"],
            reserve_usd=4_500_000.0, price_usd=self.prices.get(pool_cfg["pool"]),
            fetched_at_ms=now_ms,
        )


@pytest.fixture
def env(tmp_path):
    clock = {"now": START_MS}
    config = {
        "schema_version": 1,
        "mode": "dry_run",
        "starting_cash_usdt": 500.0,
        "poll_seconds": 45,
        "slippage_bps": 5.0,
        "event_source": {
            "provider": "fake",
            "base_url": "https://example.invalid",
            "min_trade_usd": 500.0,
            "pools": [dict(BASE_POOL)],
        },
        "wallet_policy": {"min_swaps": 3, "min_notional_usd": 2000.0, "whale_usd": 20000.0},
        "risk": {"notional_usdt": 100.0, "max_cost_bps": 60.0, "cooldown_minutes": 20,
                 "max_open_positions": 2, "max_daily_loss_usdt": 30.0,
                 # this fixture tests cost accounting at a FIXED clip; dynamic
                 # copy sizing has its own test below
                 "copy_ratio": None},
        "exit": {"take_profit_bps": 120.0, "stop_loss_bps": 80.0, "max_hold_minutes": 45},
    }
    config_path = tmp_path / "user_data" / "config.copywatch.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    source = FakeSource([BASE_POOL])

    def build(source_obj=None, config_override: dict | None = None) -> CopyWatchAgent:
        if config_override:
            merged = json.loads(json.dumps(config))
            for key, value in config_override.items():
                merged[key] = value
            config_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        return CopyWatchAgent(
            root=tmp_path,
            config_path=config_path,
            db_path=tmp_path / "runtime" / "copywatch" / "copywatch.sqlite",
            runtime_dir=tmp_path / "runtime" / "copywatch",
            source=source_obj or source,
            now_ms_fn=lambda: clock["now"],
            log=lambda *_: None,
        )

    return {"agent": build, "source": source, "clock": clock, "tmp": tmp_path,
            "db": tmp_path / "runtime" / "copywatch" / "copywatch.sqlite"}


def _seed_history(source: FakeSource, base_ms: int, wallet: str = WALLET_A, size: float = 600.0) -> None:
    for index in range(3):
        source.add(wallet=wallet, usd=size, block_ms=base_ms + index * 1000)


def test_first_poll_backfills_wallets_without_fabricating_fills(env):
    source = env["source"]
    _seed_history(source, START_MS - 60_000)
    agent = env["agent"]()

    summary = agent.poll_once()

    assert summary["new_swaps"] == 3
    assert summary["papers"] == 0
    conn = store.connect(env["db"])
    assert store.open_positions(conn) == []
    wallets = store.wallet_stats(conn)
    assert wallets["observed"] == 1
    assert wallets["tracked"] == 0, "3 笔 / $1,800 未达 $2,000 门槛，不应被跟踪"
    tags = [row["tag"] for row in store.recent_events(conn, 10)]
    assert "SYSTEM" in tags and "PAPER" not in tags
    conn.close()
    heartbeat = json.loads((env["tmp"] / "runtime" / "copywatch" / "heartbeat.json").read_text("utf-8"))
    assert heartbeat["status"] == "running"
    assert heartbeat["source"] == "CopyWatch"
    assert heartbeat["mode"] == "dry_run"


def test_tracked_wallet_buy_opens_position_with_full_cost_accounting(env):
    source = env["source"]
    _seed_history(source, START_MS - 60_000, size=800.0)  # 3 × 800 = 2,400 → tracked
    agent = env["agent"]()
    agent.poll_once()

    env["clock"]["now"] = START_MS + 60_000
    source.add(wallet=WALLET_A, usd=900.0, block_ms=START_MS + 30_000)
    summary = agent.poll_once()

    assert summary["actionable"] == 1
    assert summary["papers"] == 1
    conn = store.connect(env["db"])
    position = store.open_positions(conn)[0]
    expected_fill = 2000.0 * (1 + 5 / 10_000)
    expected_fee = 100.0 * 1.0 / 10_000
    expected_cost = (100.0 / expected_fill) * expected_fill + expected_fee + 0.02
    assert position["entry_fill_price"] == pytest.approx(expected_fill)
    assert position["entry_fee_usd"] == pytest.approx(expected_fee)
    assert position["entry_cost_usd"] == pytest.approx(expected_cost)
    assert position["qty"] == pytest.approx(100.0 / expected_fill)
    assert position["drift_bps"] == pytest.approx(0.0)  # 信号价与发现价相同
    latest = store.latest_equity(conn)
    assert latest["cash_usd"] == pytest.approx(500.0 - expected_cost)
    assert latest["complete"] == 1
    assert latest["equity_usd"] < 500.0, "开仓瞬间就应该体现往返成本"
    tags = [row["tag"] for row in store.recent_events(conn, 10)]
    assert "WATCH" in tags and "PAPER" in tags
    conn.close()


def test_expensive_chain_is_skipped_for_cost_and_reason_is_recorded(env):
    source = FakeSource([ETH_POOL])
    _seed_history(source, START_MS - 60_000, size=800.0)
    agent = env["agent"](source_obj=source, config_override={
        "event_source": {"provider": "fake", "base_url": "https://example.invalid",
                         "min_trade_usd": 500.0, "pools": [dict(ETH_POOL)]},
    })
    agent.poll_once()
    env["clock"]["now"] = START_MS + 60_000
    source.add(wallet=WALLET_A, usd=900.0, block_ms=START_MS + 30_000)
    summary = agent.poll_once()

    assert summary["papers"] == 0
    assert summary["skips"] == 1
    conn = store.connect(env["db"])
    row = conn.execute("SELECT primary_code FROM decisions WHERE verdict='SKIP'").fetchone()
    assert row["primary_code"] == "COST_TOO_HIGH"
    assert store.open_positions(conn) == []
    conn.close()


def test_unverified_wallet_only_observed(env):
    source = env["source"]
    source.add(wallet=WALLET_B, usd=600.0, block_ms=START_MS - 120_000)
    agent = env["agent"]()
    agent.poll_once()
    env["clock"]["now"] = START_MS + 60_000
    source.add(wallet=WALLET_B, usd=600.0, block_ms=START_MS + 30_000)
    summary = agent.poll_once()

    assert summary["papers"] == 0
    conn = store.connect(env["db"])
    row = conn.execute("SELECT primary_code FROM decisions WHERE verdict='SKIP'").fetchone()
    assert row["primary_code"] == "WALLET_UNVERIFIED"
    conn.close()


def test_missing_price_blocks_the_fill(env):
    source = env["source"]
    _seed_history(source, START_MS - 60_000, size=800.0)
    agent = env["agent"]()
    agent.poll_once()
    env["clock"]["now"] = START_MS + 60_000
    source.prices[BASE_POOL["pool"]] = None
    source.add(wallet=WALLET_A, usd=900.0, block_ms=START_MS + 30_000)
    summary = agent.poll_once()

    assert summary["papers"] == 0
    conn = store.connect(env["db"])
    row = conn.execute("SELECT primary_code FROM decisions WHERE verdict='SKIP'").fetchone()
    assert row["primary_code"] == "NO_FRESH_PRICE"
    latest = store.latest_equity(conn)
    assert latest["complete"] == 1  # 没有持仓，估值依然完整
    conn.close()


def test_take_profit_exit_realises_net_pnl_after_costs(env):
    source = env["source"]
    _seed_history(source, START_MS - 60_000, size=800.0)
    agent = env["agent"]()
    agent.poll_once()
    env["clock"]["now"] = START_MS + 60_000
    source.add(wallet=WALLET_A, usd=900.0, block_ms=START_MS + 30_000)
    agent.poll_once()

    env["clock"]["now"] = START_MS + 180_000
    source.prices[BASE_POOL["pool"]] = 2040.0  # +2%
    summary = agent.poll_once()

    assert summary["exits"] == 1
    conn = store.connect(env["db"])
    closed = conn.execute("SELECT * FROM positions WHERE status='closed'").fetchone()
    slip = 5 / 10_000
    qty = 100.0 / (2000.0 * (1 + slip))
    exit_fill = 2040.0 * (1 - slip)
    proceeds = qty * exit_fill - qty * exit_fill * 1.0 / 10_000 - 0.02
    expected_pnl = proceeds - (100.0 + 100.0 * 1.0 / 10_000 + 0.02)
    assert closed["exit_reason"] == "TAKE_PROFIT"
    assert closed["pnl_usd"] == pytest.approx(expected_pnl, abs=1e-9)
    assert closed["pnl_usd"] > 0
    stats = store.closed_stats(conn)
    assert stats["win_count"] == 1
    latest = store.latest_equity(conn)
    assert latest["equity_usd"] == pytest.approx(500.0 + expected_pnl, abs=1e-9)
    assert store.open_positions(conn) == []
    conn.close()


def test_no_configured_pools_reports_waiting_instead_of_activity(env):
    agent = env["agent"](config_override={
        "event_source": {"provider": "none", "base_url": "https://example.invalid",
                         "min_trade_usd": 500.0, "pools": []},
    })
    summary = agent.poll_once()
    assert summary["status"] == "waiting_for_data"
    heartbeat = json.loads((env["tmp"] / "runtime" / "copywatch" / "heartbeat.json").read_text("utf-8"))
    assert heartbeat["status"] == "waiting_for_data"
    assert heartbeat["source"] == "CopyWatch"


def test_source_failure_keeps_heartbeat_honest_and_writes_no_fills(env):
    source = env["source"]
    source.fail = True
    agent = env["agent"]()
    summary = agent.poll_once()
    assert summary["papers"] == 0
    assert summary["errors"]
    conn = store.connect(env["db"])
    assert store.open_positions(conn) == []
    assert any(row["tag"] == "WARN" for row in store.recent_events(conn, 5))
    conn.close()


def test_config_refuses_any_mode_other_than_dry_run(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mode": "live", "event_source": {"pools": []}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_ledger_is_fully_separate_from_the_abc_accounts(env):
    agent = env["agent"]()
    agent.poll_once()
    runtime = env["tmp"] / "runtime"
    assert (runtime / "copywatch" / "copywatch.sqlite").is_file()
    for other in ("baseline", "hybrid", "fast"):
        assert not (runtime / other).exists()


def test_risk_gate_reports_every_check(env):
    agent = env["agent"]()
    config = agent.config
    event = sources.SwapEvent(
        event_id="x", network="base", pool=BASE_POOL["pool"], wallet=WALLET_A, kind="buy",
        usd=900.0, price_usd=2000.0, base_symbol="WETH", quote_symbol="USDC",
        block_ts_ms=START_MS, block_number=1, tx_hash="0xabc", anomalous=False,
        discovered_at_ms=START_MS, latency_ms=0,
    )
    decision = evaluate(
        event=event, pool={"price_usd": 2000.0, "reserve_usd": 4_500_000.0, "fetched_at_ms": START_MS},
        wallet={"tracked": 1, "swaps": 5, "notional_usd": 9000.0}, policy=config["wallet_policy"],
        risk=config["risk"], slippage_bps=5.0, fee_bps=1.0, gas_usd=0.02,
        context={"now_ms": START_MS, "cash_usd": 500.0, "open_count": 0, "has_open_symbol": False,
                 "last_open_ms": None, "today_realized": 0.0},
    )
    assert decision.verdict == "PAPER"
    assert decision.signal_class == "copy"
    assert {item.code for item in decision.checks} >= {"WALLET_UNVERIFIED", "COST_TOO_HIGH", "DAILY_LOSS"}
    assert all(item.ok for item in decision.checks)


def test_whale_path_marks_signal_class_but_keeps_costs(env):
    agent = env["agent"]()
    config = agent.config
    event = sources.SwapEvent(
        event_id="y", network="base", pool=BASE_POOL["pool"], wallet=WALLET_B, kind="buy",
        usd=25_000.0, price_usd=2000.0, base_symbol="WETH", quote_symbol="USDC",
        block_ts_ms=START_MS, block_number=1, tx_hash="0xdef", anomalous=False,
        discovered_at_ms=START_MS, latency_ms=0,
    )
    decision = evaluate(
        event=event, pool={"price_usd": 2000.0, "reserve_usd": 4_500_000.0, "fetched_at_ms": START_MS},
        wallet={"tracked": 0, "swaps": 1, "notional_usd": 600.0}, policy=config["wallet_policy"],
        risk=config["risk"], slippage_bps=5.0, fee_bps=1.0, gas_usd=0.02,
        context={"now_ms": START_MS, "cash_usd": 500.0, "open_count": 0, "has_open_symbol": False,
                 "last_open_ms": None, "today_realized": 0.0},
    )
    assert decision.signal_class == "whale"
    assert decision.verdict == "PAPER"


def test_swap_price_takes_the_base_token_on_both_sides(monkeypatch):
    """A sell's USD price lives in price_from_in_usd, not price_to_in_usd.

    Reading one field for both directions recorded the quote token's ~1.00 USD
    price on every sell. That is a 100% corruption rate on one side of the tape,
    and it silently turned every replayed signal into a -99% trade.
    """
    payload = {
        "data": [
            {
                "id": "t-buy",
                "attributes": {
                    "tx_from_address": WALLET_A,
                    "tx_hash": "0xbuy",
                    "kind": "buy",
                    "volume_in_usd": "1500",
                    "block_timestamp": "2026-10-24T18:00:00Z",
                    "price_from_in_usd": "1.0002",
                    "price_to_in_usd": "2360.5",
                },
            },
            {
                "id": "t-sell",
                "attributes": {
                    "tx_from_address": WALLET_B,
                    "tx_hash": "0xsell",
                    "kind": "sell",
                    "volume_in_usd": "900",
                    "block_timestamp": "2026-10-24T18:01:00Z",
                    "price_from_in_usd": "2362.75",
                    "price_to_in_usd": "1.0006",
                },
            },
        ]
    }
    monkeypatch.setattr(sources, "_http_json", lambda url, timeout=0: payload)

    events = sources.fetch_swaps(
        network="base",
        pool=BASE_POOL["pool"],
        base_symbol="WETH",
        quote_symbol="USDC",
        min_usd=100,
        base_url="https://example.invalid",
        now_ms=START_MS,
    )
    by_kind = {event.kind: event for event in events}
    assert by_kind["buy"].price_usd == pytest.approx(2360.5)
    assert by_kind["sell"].price_usd == pytest.approx(2362.75), "sell must price the base token"
    assert all(event.price_usd > 1000 for event in events), "no quote-token price may leak in"




def test_same_symbol_keeps_only_the_largest_wallet(tmp_path) -> None:
    """Duplicated buys of one token by many tracked wallets are one signal."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.copywatch import agent as agent_mod, store

    db = _P(tmp_path) / "dedupe.sqlite"
    conn = store.connect(db)
    store.ensure_schema(conn)
    for address, notional in (("0xbig", 9_000_000.0), ("0xmid", 500_000.0), ("0xsmall", 20_000.0)):
        conn.execute(
            "INSERT INTO wallets(address, network, first_seen_ms, last_seen_ms, swaps, buys,"
            " sells, notional_usd) VALUES(?,?,?,?,?,?,?,?)",
            (address, "base", 1, 1, 10, 10, 0, notional),
        )
    conn.commit()

    class _Ev:
        def __init__(self, wallet: str, symbol: str):
            self.wallet = wallet
            self.network = "base"
            self.base_symbol = symbol
            self.usd = 1000.0

    events = [_Ev("0xsmall", "PEPE"), _Ev("0xbig", "PEPE"), _Ev("0xmid", "PEPE"), _Ev("0xbig", "DOGE")]
    desk = agent_mod.CopyWatchAgent.__new__(agent_mod.CopyWatchAgent)
    kept, dropped = desk._dedupe_by_symbol(conn, events, 1)
    assert [e.wallet for e in kept] == ["0xbig", "0xbig"]
    assert sorted(e.wallet for e in dropped) == ["0xmid", "0xsmall"]
    conn.close()


def test_demotion_appends_to_the_summary_list(tmp_path) -> None:
    """The poll summary initialises demotions as a list; appending a demoted
    wallet to an int is what crashed D for three hours."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.copywatch import agent as agent_mod, store

    db = _P(tmp_path) / "demote.sqlite"
    conn = store.connect(db)
    store.ensure_schema(conn)
    conn.execute(
        "INSERT INTO wallets(address, network, first_seen_ms, last_seen_ms, swaps, buys,"
        " sells, notional_usd, tracked, demoted_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("0xlucky", "base", 1, 1, 9, 5, 4, 900_000.0, 1, None),
    )
    for i in range(2):
        conn.execute(
            "INSERT INTO positions(event_id, wallet, network, pool, symbol, signal_class,"
            " fee_bps, gas_usd, opened_at_ms, entry_ref_price, entry_fill_price, qty,"
            " notional_usd, entry_fee_usd, entry_gas_usd, entry_cost_usd, signal_price_usd,"
            " drift_bps, latency_ms, status, pnl_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"ev{i}", "0xlucky", "base", "p1", f"SYM{i}", "retail", 30.0, 1.0,
             1_700_000_000_000, 1.0, 1.0, 100.0, 100.0, 0.3, 1.0, 1.3, 1.0, 0, 100, "closed", -5.0),
        )
    conn.commit()

    position = conn.execute(
        "SELECT wallet, network, symbol FROM positions WHERE status='closed' LIMIT 1"
    ).fetchone()
    summary = {"pools": 1, "fetched": 0, "new_swaps": 0, "actionable": 0,
               "papers": 0, "skips": 0, "exits": 0, "demotions": [], "errors": []}
    desk = agent_mod.CopyWatchAgent.__new__(agent_mod.CopyWatchAgent)
    desk._maybe_demote(conn, position, realized=-5.0, now_ms=1_700_000_000_000,
                       policy={"demote_after_losses": 2}, summary=summary)
    assert summary["demotions"] == ["0xlucky"], summary
    conn.close()


def test_copy_position_scales_with_the_target_wallet_fill() -> None:
    """A wallet putting $5k behind a token says more than one putting $50; the
    clip follows the wallet, bounded by a floor and a cap."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.copywatch.sizing import dynamic_notional

    risk = {"notional_usdt": 100.0, "copy_ratio": 0.5,
            "min_notional_usdt": 25.0, "max_notional_usdt": 150.0}

    assert dynamic_notional(200.0, risk) == (100.0, "0.5x $200")
    # a small signal is still worth filling, but only at the floor
    n, note = dynamic_notional(40.0, risk)
    assert n == 25.0 and "floor" in note
    # a whale cannot empty the book: the cap wins
    n, note = dynamic_notional(20_000.0, risk)
    assert n == 150.0 and "cap" in note
    # unset ratio keeps the fixed clip (backward compatible)
    assert dynamic_notional(1_000.0, {"notional_usdt": 100.0}) == (100.0, "fixed")


def test_impact_gate_uses_the_size_that_gets_filled() -> None:
    """The decision must carry the notional its own gates checked, so the fill
    can never trade a different size than the one that passed PRICE_IMPACT."""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    from research.copywatch import risk as risk_mod

    class _Ev:
        usd = 20_000.0
        anomalous = False
        price_usd = 1.0
        base_symbol = "PEPE"

    decision = risk_mod.evaluate(
        event=_Ev(),
        pool={"price_usd": 1.0, "fetched_at_ms": 1_700_000_000_000, "reserve_usd": 100_000.0},
        wallet=None,
        policy={"whale_usd": 20_000.0},
        risk={"notional_usdt": 100.0, "copy_ratio": 0.5, "min_notional_usdt": 25.0,
              "max_notional_usdt": 150.0, "max_cost_bps": 60.0, "max_pool_impact_bps": 5.0,
              "min_pool_reserve_usd": 250_000.0, "max_price_age_seconds": 180,
              "cooldown_minutes": 20, "max_open_positions": 3, "max_daily_loss_usdt": 30.0},
        slippage_bps=50.0, fee_bps=30.0, gas_usd=1.0,
        context={"now_ms": 1_700_000_000_000, "cash_usd": 500.0, "open_count": 0,
                 "has_open_symbol": False, "last_open_ms": 0, "today_realized": 0.0},
    )
    # the whale-sized signal caps at 150 and the decision reports that size
    assert decision.notional == 150.0
    assert decision.size_note and "cap" in decision.size_note
