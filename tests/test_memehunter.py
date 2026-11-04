"""Tests for the meme-token paper desk (E account).

Two things are worth guarding here. First, the survival vetoes: they are the
only reason a 100 USDT clip is not instantly down 60% on a honeypot, so each
veto needs a case that proves it fires and a case that proves it does not fire
on a merely-unattractive token. Second, the equity arithmetic: a desk that
counts a position's market value while leaving its principal in cash reports a
fake profit the moment it buys anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import memehunter  # noqa: E402
from research.memehunter import (  # noqa: E402
    Desk, Token, quality, record_candidates, score, timing, veto,
)


def make_token(**overrides) -> Token:
    """A token that clears every veto, so a test only fails on what it changes."""
    base = dict(
        mint="So11111111111111111111111111111111111111112",
        symbol="GOODCAT",
        name="Good Cat",
        price_usd=0.01,
        liquidity_usd=400_000.0,
        fdv_usd=8_000_000.0,
        organic_score=80.0,
        organic_label="high",
        holder_count=12_000,
        top_holders_pct=18.0,
        mint_disabled=True,
        freeze_disabled=True,
        chg_5m=1.5,
        chg_1h=4.0,
        chg_24h=12.0,
        organic_buy_5m=9_000.0,
        organic_sell_5m=1_000.0,
        organic_buy_1h=40_000.0,
        organic_sell_1h=20_000.0,
        net_buyers_5m=40,
        net_buyers_1h=200,
        vol_1h=150_000.0,
        vol_24h=2_000_000.0,
    )
    base.update(overrides)
    return Token(**base)


@pytest.fixture()
def desk(tmp_path) -> Desk:
    return Desk(tmp_path)


# ---------- survival vetoes ----------

def test_clean_token_survives_every_veto():
    """Baseline: if this fails, every other veto test below is meaningless."""
    ok, reason = veto(make_token(), memehunter.DEFAULT_CONFIG)
    assert ok, reason


def test_active_mint_authority_is_rejected():
    """Unrevoked mint authority means supply can be inflated against the holder."""
    ok, reason = veto(make_token(mint_disabled=False), memehunter.DEFAULT_CONFIG)
    assert not ok and reason == "AUTHORITY_NOT_REVOKED"


def test_missing_audit_fields_are_treated_as_unrevoked():
    """Absent is not the same as safe: an unknown authority state must fail closed."""
    token = make_token(mint_disabled=False, freeze_disabled=False)
    ok, reason = veto(token, memehunter.DEFAULT_CONFIG)
    assert not ok and reason == "AUTHORITY_NOT_REVOKED"


def test_wash_traded_volume_is_rejected():
    """A high organicScore cannot outvote a tiny organic share of volume."""
    token = make_token(organic_buy_1h=600.0, organic_sell_1h=150.0, vol_1h=34_000.0)
    ok, reason = veto(token, memehunter.DEFAULT_CONFIG)
    assert not ok and reason == "VOLUME_IS_WASH_TRADED"


def test_blue_chip_is_rejected_as_too_large():
    """A $1.9B FDV token is not a meme setup, whatever its 5m chart does."""
    ok, reason = veto(make_token(fdv_usd=1_900_000_000.0), memehunter.DEFAULT_CONFIG)
    assert not ok and reason == "TOO_LARGE_TO_BE_MEME"


def test_collapse_in_five_minutes_is_rejected():
    ok, reason = veto(make_token(chg_5m=-14.0), memehunter.DEFAULT_CONFIG)
    assert not ok and reason == "COLLAPSING"


def test_a_modest_five_minute_dip_is_not_a_collapse():
    """A -4% print is noise at this volatility; it must not veto on its own."""
    ok, reason = veto(make_token(chg_5m=-4.0), memehunter.DEFAULT_CONFIG)
    assert ok, reason


# ---------- scoring ----------

def test_score_falls_as_a_token_gets_more_expensive_to_exit():
    """Thin liquidity and concentrated supply must both cost points."""
    healthy = score(make_token(), memehunter.DEFAULT_CONFIG)
    thin = score(make_token(liquidity_usd=45_000.0), memehunter.DEFAULT_CONFIG)
    concentrated = score(make_token(top_holders_pct=52.0), memehunter.DEFAULT_CONFIG)
    assert thin < healthy
    assert concentrated < healthy


def test_score_penalises_a_vertical_move():
    """Buying a token already up 200% in an hour is chasing, and must score worse."""
    flat = score(make_token(chg_1h=5.0), memehunter.DEFAULT_CONFIG)
    vertical = score(make_token(chg_1h=200.0), memehunter.DEFAULT_CONFIG)
    assert vertical < flat


# ---------- ledger arithmetic ----------

def test_opening_a_position_removes_principal_from_cash(desk):
    """The bug this guards: cash stayed at 1,000 minus the fee only, so equity
    printed 1,099 on a 1,000 account the instant the first clip filled."""
    token = make_token()
    result = desk.open_position(token, "test")
    assert result["opened"], result

    cfg = desk.cfg
    notional = float(cfg["notional_usdt"])
    position = desk.conn.execute("SELECT * FROM positions").fetchone()
    expected_cash = float(cfg["starting_cash_usdt"]) - notional - float(position["entry_cost_usd"])
    assert desk.cash() == pytest.approx(expected_cash, abs=1e-6)


def test_equity_is_flat_at_entry_apart_from_costs(desk):
    """Right after the fill, equity must equal cash plus holdings and be *below*
    the starting balance -- never above it."""
    desk.open_position(make_token(), "test")
    report = desk.report()
    assert report["equity_usd"] < report["starting_usd"]
    assert report["equity_usd"] == pytest.approx(
        report["cash_usd"] + report["positions_usd"], abs=1e-6
    )
    # Costs are the clip's fee plus entry slippage; a few tenths of a percent.
    assert report["pnl_usd"] == pytest.approx(-0.9, abs=0.6)


def test_closing_returns_proceeds_and_books_realised_pnl(desk):
    desk.open_position(make_token(), "test")
    position = desk.conn.execute("SELECT * FROM positions").fetchone()
    mark = float(position["entry_fill_price"]) * 1.5  # a 50% winner
    desk._close(position, mark, "TAKE_PROFIT", int(position["opened_at_ms"]) + 60_000)

    report = desk.report()
    assert report["closed_count"] == 1
    assert report["realized_usd"] > 0
    # All principal is back in cash once nothing is open.
    assert report["positions_usd"] == 0
    assert report["equity_usd"] == pytest.approx(report["starting_usd"] + report["realized_usd"], abs=1e-6)


def test_no_cash_no_position(desk):
    """Cash must be a real constraint, not a suggestion."""
    for _ in range(int(desk.cfg["max_open_positions"])):
        # Distinct mints so ALREADY_OPEN does not mask the cash check.
        desk.open_position(make_token(mint=f"mint{_}{desk.cfg['notional_usdt']}"), "fill")
    ok, reason = desk.can_open(make_token(mint="fresh"), memehunter.now_ms())
    assert not ok and reason == "MAX_POSITIONS"


def test_missing_organic_data_is_not_called_wash_trading():
    """Absent organic data is a different fact from manufactured volume.

    Collapsing both into VOLUME_IS_WASH_TRADED mislabelled every mid-cap memecoin
    that Jupiter simply does not instrument, which made the skip log read as if
    the whole universe were fake.
    """
    token = make_token(organic_buy_1h=0.0, organic_sell_1h=0.0, organic_buy_5m=0.0, organic_sell_5m=0.0)
    ok, reason = veto(token, memehunter.DEFAULT_CONFIG)
    assert not ok
    assert reason == "NO_ORGANIC_DATA"


def test_a_token_without_organic_data_cannot_reach_the_buy_floor():
    """The veto above is a label, not a hidden extra filter: without organic
    evidence the score is capped below the floor anyway."""
    token = make_token(
        organic_buy_1h=0.0, organic_sell_1h=0.0, organic_buy_5m=0.0, organic_sell_5m=0.0,
        organic_score=0.0, vol_1h=5_000_000.0, liquidity_usd=5_000_000.0,
        holder_count=200_000, chg_5m=5.0, chg_1h=20.0,
    )
    assert score(token, memehunter.DEFAULT_CONFIG) < memehunter.DEFAULT_CONFIG["min_score_to_buy"]


def test_missing_organic_data_is_not_called_wash_trading():
    """Absent organic data is a different fact from manufactured volume.

    Collapsing both into VOLUME_IS_WASH_TRADED mislabelled every mid-cap memecoin
    that Jupiter simply does not instrument, which made the skip log read as if
    the whole universe were fake.
    """
    token = make_token(organic_buy_1h=0.0, organic_sell_1h=0.0, organic_buy_5m=0.0, organic_sell_5m=0.0)
    ok, reason = veto(token, memehunter.DEFAULT_CONFIG)
    assert not ok
    assert reason == "NO_ORGANIC_DATA"


def test_a_token_without_organic_data_cannot_reach_the_buy_floor():
    """The veto above is a label, not a hidden extra filter: without organic
    evidence the score is capped below the floor anyway."""
    token = make_token(
        organic_buy_1h=0.0, organic_sell_1h=0.0, organic_buy_5m=0.0, organic_sell_5m=0.0,
        organic_score=0.0, vol_1h=5_000_000.0, liquidity_usd=5_000_000.0,
        holder_count=200_000, chg_5m=5.0, chg_1h=20.0,
    )
    assert score(token, memehunter.DEFAULT_CONFIG) < memehunter.DEFAULT_CONFIG["min_score_to_buy"]


def test_already_run_up_is_refused_before_scoring():
    """The one cohort with a reliably negative forward return.

    Across 539 eligible candidates the 1h-up-15-to-60% bucket returned -5.3%
    and -5.7% over the next 20-300 minutes (n=44 and n=18); in the unbiased
    quote-only subsample it returned -12.9%. A score penalty alone still let
    the cleanest of them through on volume and holder count, which is exactly
    how the desk bought every top it traded.
    """
    ok, reason = veto(make_token(chg_1h=15.1), memehunter.DEFAULT_CONFIG)
    assert not ok
    assert reason == "ALREADY_RUN_UP"
    # Just under the line still trades: the ceiling is a cohort filter, not a
    # ban on anything green.
    ok2, _ = veto(make_token(chg_1h=14.9), memehunter.DEFAULT_CONFIG)
    assert ok2


def test_score_prefers_a_flat_hour_to_a_rising_one():
    """Momentum used to be a bonus; that is what made the score buy tops.

    Same token, only the last hour differs. The flat one must score higher,
    and the relationship has to hold in both directions (a small rise still
    beats a large one).
    """
    flat = score(make_token(chg_1h=0.0, chg_5m=0.0), memehunter.DEFAULT_CONFIG)
    small = score(make_token(chg_1h=4.0, chg_5m=1.5), memehunter.DEFAULT_CONFIG)
    large = score(make_token(chg_1h=12.0, chg_5m=6.0), memehunter.DEFAULT_CONFIG)
    assert flat > small > large


def test_score_punishes_a_falling_knife_not_a_red_candle():
    """-2% over five minutes is noise and should cost little; -9% is a dump."""
    red = score(make_token(chg_5m=-2.0, chg_1h=0.0), memehunter.DEFAULT_CONFIG)
    knife = score(make_token(chg_5m=-9.0, chg_1h=0.0), memehunter.DEFAULT_CONFIG)
    assert knife < red


def test_health_is_silent_until_the_window_is_full(desk):
    """A stop condition that fires on five trades is a coin flip with a label."""
    h = desk.health(window=30)
    assert h["ready"] is False
    assert h["halt"] is False


def test_health_halts_a_window_that_loses_money(desk):
    for _ in range(31):
        desk.conn.execute(
            "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd, "
            "entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd, "
            "entry_reason, status, exit_at_ms, exit_reason, exit_fill_price, exit_cost_usd, "
            "pnl_usd, pnl_pct) VALUES (1,'m','X',50,60,1,1,60,0,0.3,'t','closed',"
            "2,'TIMEOUT',0.99,0.3,-1.2,-2.0)"
        )
    h = desk.health(window=30)
    assert h["ready"] is True
    assert h["halt"] is True
    assert h["expectancy_pct"] < 0


def test_health_does_not_halt_a_profitable_window(desk):
    for _ in range(30):
        desk.conn.execute(
            "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd, "
            "entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd, "
            "entry_reason, status, exit_at_ms, exit_reason, exit_fill_price, exit_cost_usd, "
            "pnl_usd, pnl_pct) VALUES (1,'m','X',50,60,1,1,60,0,0.3,'t','closed',"
            "2,'TAKE_PROFIT',1.3,0.3,17.7,29.5)"
        )
    h = desk.health(window=30)
    assert h["ready"] is True
    assert h["halt"] is False


def test_already_run_up_is_refused_before_scoring():
    """The one cohort with a reliably negative forward return.

    Across 539 eligible candidates the 1h-up-15-to-60% bucket returned -5.3%
    and -5.7% over the next 20-300 minutes (n=44 and n=18); in the unbiased
    quote-only subsample it returned -12.9%. A score penalty alone still let
    the cleanest of them through on volume and holder count, which is exactly
    how the desk bought every top it traded.
    """
    ok, reason = veto(make_token(chg_1h=15.1), memehunter.DEFAULT_CONFIG)
    assert not ok
    assert reason == "ALREADY_RUN_UP"
    # Just under the line still trades: the ceiling is a cohort filter, not a
    # ban on anything green.
    ok2, _ = veto(make_token(chg_1h=14.9), memehunter.DEFAULT_CONFIG)
    assert ok2


def test_score_prefers_a_flat_hour_to_a_rising_one():
    """Momentum used to be a bonus; that is what made the score buy tops.

    Same token, only the last hour differs. The flat one must score higher,
    and the relationship has to hold in both directions (a small rise still
    beats a large one).
    """
    flat = score(make_token(chg_1h=0.0, chg_5m=0.0), memehunter.DEFAULT_CONFIG)
    small = score(make_token(chg_1h=4.0, chg_5m=1.5), memehunter.DEFAULT_CONFIG)
    large = score(make_token(chg_1h=12.0, chg_5m=6.0), memehunter.DEFAULT_CONFIG)
    assert flat > small > large


def test_score_punishes_a_falling_knife_not_a_red_candle():
    """-2% over five minutes is noise and should cost little; -9% is a dump."""
    red = score(make_token(chg_5m=-2.0, chg_1h=0.0), memehunter.DEFAULT_CONFIG)
    knife = score(make_token(chg_5m=-9.0, chg_1h=0.0), memehunter.DEFAULT_CONFIG)
    assert knife < red


def test_health_is_silent_until_the_window_is_full(desk):
    """A stop condition that fires on five trades is a coin flip with a label."""
    h = desk.health(window=30)
    assert h["ready"] is False
    assert h["halt"] is False


def test_health_halts_a_window_that_loses_money(desk):
    for _ in range(31):
        desk.conn.execute(
            "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd, "
            "entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd, "
            "entry_reason, status, exit_at_ms, exit_reason, exit_fill_price, exit_cost_usd, "
            "pnl_usd, pnl_pct) VALUES (1,'m','X',50,60,1,1,60,0,0.3,'t','closed',"
            "2,'TIMEOUT',0.99,0.3,-1.2,-2.0)"
        )
    h = desk.health(window=30)
    assert h["ready"] is True
    assert h["halt"] is True
    assert h["expectancy_pct"] < 0


def test_health_does_not_halt_a_profitable_window(desk):
    for _ in range(30):
        desk.conn.execute(
            "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd, "
            "entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd, "
            "entry_reason, status, exit_at_ms, exit_reason, exit_fill_price, exit_cost_usd, "
            "pnl_usd, pnl_pct) VALUES (1,'m','X',50,60,1,1,60,0,0.3,'t','closed',"
            "2,'TAKE_PROFIT',1.3,0.3,17.7,29.5)"
        )
    h = desk.health(window=30)
    assert h["ready"] is True
    assert h["halt"] is False


# ---------- quality and timing are separate decisions ----------

def _record_one(desk, token):
    scan_id = desk.conn.execute(
        "INSERT INTO scans (scanned_at_ms, tokens_seen, tokens_eligible, errors) "
        "VALUES (1,1,0,'[]')"
    ).lastrowid
    record_candidates(desk.conn, scan_id, 1, [token], desk.cfg)
    return desk.conn.execute("SELECT * FROM candidates ORDER BY id DESC LIMIT 1").fetchone()


def test_clean_but_badly_timed_is_refused_on_timing_not_rescued_by_liquidity(desk):
    """The exact failure that made the old score buy tops.

    A token with 12k holders, a $400k pool and organic volume scores well on
    quality, but it is up 10% on the hour and 8% on the minute. Blended into
    one number those quality points outvoted the price action and it scored
    above the buy floor -- while falling, in the live ledger.
    """
    token = make_token(chg_1h=10.0, chg_5m=8.0)
    assert quality(token, desk.cfg) >= desk.cfg["min_quality_to_buy"]
    assert timing(token, desk.cfg) < desk.cfg["min_timing_to_buy"]
    row = _record_one(desk, token)
    assert row["eligible"] == 0
    assert row["reject_reason"] == "TIMING_BELOW_FLOOR"


def test_a_well_timed_clean_token_still_passes(desk):
    """The mirror case: turning the vetoes up must not leave a desk that never
    trades. Flat hour, no spike, clean supply -- this one is buyable."""
    token = make_token(chg_1h=1.0, chg_5m=0.0)
    row = _record_one(desk, token)
    assert row["eligible"] == 1, row["reject_reason"]
    assert row["quality"] is not None and row["timing"] is not None


def test_a_dirty_token_is_refused_on_quality_even_with_a_perfect_entry(desk):
    """Timing cannot buy its way past a rug profile."""
    token = make_token(chg_1h=1.0, chg_5m=0.0, top_holders_pct=40.0, holder_count=350)
    assert timing(token, desk.cfg) >= desk.cfg["min_timing_to_buy"]
    row = _record_one(desk, token)
    assert row["eligible"] == 0
    assert row["reject_reason"] in ("QUALITY_BELOW_FLOOR", "HOLDERS_TOO_CONCENTRATED")
