"""Every shipped contract preset must load through the engine's own validator.

A preset is the first thing a $29 buyer copies. If it fails Spec.load, the
buyer's very first command errors out -- which is exactly what happened with
equity_swing_std.json (venue "equity" is not a venue the engine knows; stocks
are modelled as spot, no funding). This test makes that class of bug a CI
failure instead of a support ticket.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRESETS = ROOT / "skill" / "strategy-falsifier-pro" / "presets" / "contracts"


def _preset_files() -> list[Path]:
    return sorted(PRESETS.glob("*.json"))


def test_presets_exist():
    assert _preset_files(), "no contract presets shipped"


def test_every_preset_loads_and_validates():
    from research.verify.spec import Spec

    for p in _preset_files():
        spec = Spec.load(p)  # from_dict calls validate(); raises SpecError if bad
        assert spec.venue in ("spot", "futures"), f"{p.name}: venue {spec.venue!r}"
        # a preset that declares no falsify_if rules cannot falsify anything;
        # every shipped contract must state its verdict rules up front
        assert spec.falsify_if, f"{p.name}: preset declares no falsify_if rules"
        assert spec.falsify_if.get("min_fills"), f"{p.name}: no min_fills"


def test_equity_preset_models_stocks_as_spot():
    """Stocks have no funding rate; the engine's spot venue is the honest model."""
    from research.verify.spec import Spec

    spec = Spec.load(PRESETS / "equity_swing_std.json")
    assert spec.venue == "spot"
    assert spec.funding is False