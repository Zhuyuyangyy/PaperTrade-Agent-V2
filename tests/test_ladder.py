"""The ladder's four promises, tested:
1. halt outranks everything (a broken feed stops trading, politely or not)
2. demotion is automatic -- the machine can lose confidence on its own
3. promotion requires a human -- the machine never gains it
4. every verdict carries the reason that produced it
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.live.ladder import HALT_RULES, Ladder  # noqa: E402


def _steady(ladder: Ladder, n: int, win: float = 0.004, loss: float = -0.002) -> None:
    """n alternating trades with a comfortable edge."""
    for i in range(n):
        ladder.record(win if i % 2 else loss)


def test_a_new_strategy_starts_at_the_bottom_with_nothing_proven():
    lad = Ladder(name="x")
    assert lad.tier == "replay"
    assert lad.stats()["n"] == 0
    assert lad.evaluate().action == "hold"


def test_halt_outranks_every_statistic():
    """Eight straight losses stops the book even if everything else is fine."""
    lad = Ladder(name="x")
    _steady(lad, 60)                       # a good book...
    for _ in range(HALT_RULES["max_consecutive_losses"]):
        lad.record(-0.001)                 # ...then a bad run
    verdict = lad.evaluate()
    assert verdict.action == "halt"
    assert lad.halted is True
    assert "连续" in verdict.reason


def test_a_reconciliation_gap_halts_before_any_statistic_is_read():
    """An unmatched fill is not a strategy problem, it is a stop-trading problem."""
    lad = Ladder(name="x")
    _steady(lad, 60)
    verdict = lad.evaluate(reconciliation_gap=2)
    assert verdict.action == "halt"
    assert "对账不平" in verdict.reason


def test_a_data_gap_halts_the_book():
    """No fresh market data for five minutes is a reason to stop, not to wait."""
    lad = Ladder(name="x")
    _steady(lad, 60)
    verdict = lad.evaluate(data_age_s=HALT_RULES["max_data_gap_s"] + 30)
    assert verdict.action == "halt"
    assert "中断" in verdict.reason


def test_demotion_is_automatic_when_the_tier_rules_are_broken():
    """The machine can lose confidence on its own.

    Alternating wins and losses with a losing skew: PF below the tier floor
    but no eight-loss streak, so the halt rule stays out of the way and the
    demotion rule is what fires. (A hundred straight losses halts first --
    halt outranks demotion, and that has its own test.)
    """
    lad = Ladder(name="x")
    lad.tier = "dryrun"                     # pretend it got there
    for i in range(100):
        lad.record(0.001 if i % 2 else -0.004)
    verdict = lad.evaluate()
    assert verdict.action == "demote"
    assert verdict.stats["profit_factor"] < 1.0
    assert verdict.stats["worst_streak"] < HALT_RULES["max_consecutive_losses"]
    assert lad.demote() == "replay", "and the step down is immediate"


def test_a_bad_unfilled_rate_demotes_a_maker_strategy():
    """Signals that never become trades are a fact about execution, and they count.

    The filled trades are healthy on purpose: this is the case where ONLY the
    unfilled rate fails, so the demotion can be attributed to it.
    """
    lad = Ladder(name="x")
    lad.tier = "dryrun"
    for i in range(100):
        lad.record(0.004 if i % 2 else -0.002, filled=(i % 4 != 0))  # 25% unfilled
    verdict = lad.evaluate()
    assert verdict.stats["unfilled_rate"] == pytest.approx(0.25, abs=0.01)
    assert verdict.stats["profit_factor"] > 1.0, "the fills themselves are fine"
    assert verdict.action == "demote"
    assert "未成交率" in verdict.reason


def test_promotion_requires_a_human_no_matter_how_good_the_book_looks():
    """The machine never gains confidence on its own."""
    lad = Ladder(name="x")
    _steady(lad, 65)
    verdict = lad.evaluate()
    assert verdict.action == "promote", "it reports eligibility..."
    ok, why = lad.promote()
    assert ok is False, "...but cannot act on it"
    assert "人工确认" in why
    assert lad.tier == "replay"


def test_promotion_with_confirmation_steps_up_one_tier():
    lad = Ladder(name="x")
    _steady(lad, 65)
    ok, tier = lad.promote(confirm=True)
    assert ok is True
    assert tier == "dryrun"
    assert lad.tier == "dryrun"


def test_a_halted_ladder_cannot_be_promoted():
    lad = Ladder(name="x")
    _steady(lad, 65)
    lad.halt()
    ok, why = lad.promote(confirm=True)
    assert ok is False
    assert "停机" in why


def test_below_the_sample_minimum_nothing_is_decided():
    """Ten trades decide nothing, whatever they say."""
    lad = Ladder(name="x")
    for _ in range(10):
        lad.record(0.05)                    # absurdly good, and still nothing
    verdict = lad.evaluate()
    assert verdict.action == "hold"
    assert "继续积累" in verdict.reason


def test_the_ladder_round_trips_through_disk():
    lad = Ladder(name="x")
    _steady(lad, 70)
    lad.promote(confirm=True)
    path = Path("runtime") / "live" / "test_ladder.json"
    try:
        lad.save(path)
        back = Ladder.load(path, name="x")
        assert back.tier == lad.tier
        assert back.stats()["n"] == lad.stats()["n"]
        assert back.stats()["expectancy"] == pytest.approx(lad.stats()["expectancy"])
    finally:
        path.unlink(missing_ok=True)


def test_every_verdict_is_recorded_with_its_reason():
    """An audit that cannot say why it acted is theatre."""
    lad = Ladder(name="x")
    lad.evaluate()
    lad.record(0.001)
    lad.evaluate()
    assert len(lad.history) == 2
    assert all(h["reason"] for h in lad.history)
    assert all("at_ms" in h for h in lad.history)
