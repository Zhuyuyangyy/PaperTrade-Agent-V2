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
from research.memehunter import Desk, Token, score, veto  # noqa: E402


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
