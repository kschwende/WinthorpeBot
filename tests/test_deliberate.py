"""Tests for the agent deliberation layer — the 'correct my call wall' logic."""

from winthorpe.agent.deliberate import propose_plan
from winthorpe.plan.schema import Comparator, Side

# the user's example: they say 7530; live GEX says the call wall is 7500.
GEX = {
    "spot": 7487.0,
    "call_wall": {"strike": 7500.0, "oi": 4258},
    "put_wall": {"strike": 7475.0, "oi": 1378},
}


def test_put_fade_corrects_call_wall_and_structures_trade():
    p = propose_plan(
        thesis="fade the holiday drift into the call wall",
        side=Side.PUT, proposed_level=7530.0, gex=GEX, expiry="2026-06-22",
    )
    # The correction must call out 7530 → 7500.
    assert any("7530" in c and "7500" in c for c in p.corrections)
    # Trigger at the real wall, from below.
    assert p.plan.trigger.level == 7500.0
    assert p.plan.trigger.from_side == "below"
    # Entry strike 5pt inside the wall, on the grid.
    assert p.plan.strike == 7495.0
    # Invalidation above the wall, with a hold.
    assert p.plan.invalidation.comparator is Comparator.GTE
    assert p.plan.invalidation.level == 7505.0
    assert p.plan.invalidation.hold_seconds == 60
    # Draft is signable.
    assert p.plan.validate() == []


def test_call_play_uses_put_wall():
    p = propose_plan(
        thesis="bounce off the put wall", side=Side.CALL,
        proposed_level=7475.0, gex=GEX, expiry="2026-06-22",
    )
    assert p.plan.trigger.level == 7475.0
    assert p.plan.trigger.from_side == "above"
    assert p.plan.strike == 7480.0          # 5pt above wall, grid ceil
    assert p.plan.invalidation.level == 7470.0


def test_no_correction_when_level_matches():
    p = propose_plan(
        thesis="x", side=Side.PUT, proposed_level=7500.0, gex=GEX,
        expiry="2026-06-22",
    )
    # No "you said X, GEX says Y" line when they agree.
    assert not any("Using" in c for c in p.corrections)


def test_confluence_note_when_levels_align():
    from winthorpe.levels.structural import StructuralLevels
    # Call wall 7500 lines up with PDH 7503 and ONH 7498.
    lv = StructuralLevels(pdh=7503.0, onh=7498.0, pdc=7475.0)
    p = propose_plan(
        thesis="fade", side=Side.PUT, proposed_level=7530.0, gex=GEX,
        expiry="2026-06-22", levels=lv,
    )
    assert any("Confluence" in c for c in p.corrections)
    names = {h["name"] for h in p.confluence}
    assert {"PDH", "ONH"} <= names


def test_confluence_absent_note_when_no_levels_near():
    from winthorpe.levels.structural import StructuralLevels
    lv = StructuralLevels(pdh=7400.0, onh=7390.0)   # far from the 7500 wall
    p = propose_plan(
        thesis="fade", side=Side.PUT, proposed_level=7500.0, gex=GEX,
        expiry="2026-06-22", levels=lv,
    )
    assert p.confluence == []
    assert any("standing alone" in c for c in p.corrections)
