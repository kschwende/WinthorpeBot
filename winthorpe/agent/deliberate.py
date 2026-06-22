"""Deliberation helpers — turn a human thesis into a corrected draft plan.

This is the agent's reasoning scaffold for the exact exchange from the original
brief:

  the user: "if SPX reaches the call wall at 7530, buy puts"
  Agent: corrects 7530 → the real call wall, proposes entry a few points below it
         with a stop above the wall, and a managed target.

The agent (Claude, in a session) calls ``propose_plan`` with the live GEX result
from ``winthorpe.data.gex_engine``, reviews the corrections and the derived draft
with the user, edits as needed, then ``plan.sign()`` hands it to the engine. The
authoritative-wall choice is explicit here: we trust the *live* GEX walls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from winthorpe.plan.schema import Comparator, Condition, Side, TradePlan

STRIKE_GRID = 5.0  # SPXW strike increment


def _round_to_grid(x: float, *, down: bool) -> float:
    """Snap to the $5 SPXW strike grid — floor if down else ceil."""
    f = math.floor if down else math.ceil
    return f(x / STRIKE_GRID) * STRIKE_GRID


@dataclass
class Proposal:
    plan: TradePlan
    corrections: list[str]


def propose_plan(
    *,
    thesis: str,
    side: Side,
    proposed_level: float,
    gex: dict,
    expiry: str,
    entry_offset: float = 5.0,    # enter this many pts inside the wall
    wall_buffer: float = 5.0,     # invalidation this many pts beyond the wall
    tp_pct: float = 0.30,
    sl_pct: float = -0.25,
    time_stop_et: str = "15:45",
    valid_until_et: str = "15:30",
) -> Proposal:
    """Build a DRAFT TradePlan from a thesis + live GEX, recording corrections.

    For a PUT fade the reference wall is the call wall (resistance above); for a
    CALL play it's the put wall (support below). Levels are snapped to the $5 grid.
    """
    side = Side(side)
    corrections: list[str] = []

    if side is Side.PUT:
        wall = float(gex["call_wall"]["strike"])
        wall_label = "call wall"
        # Trigger: SPX rising into the wall from below.
        trigger = Condition("SPX", Comparator.TOUCH, wall, from_side="below")
        # Enter puts a few points below the wall; strike on the grid below.
        strike = _round_to_grid(wall - entry_offset, down=True)
        # Invalidation: SPX pushes through and holds above the wall → thesis void.
        invalidation = Condition("SPX", Comparator.GTE, wall + wall_buffer, hold_seconds=60)
    else:
        wall = float(gex["put_wall"]["strike"])
        wall_label = "put wall"
        trigger = Condition("SPX", Comparator.TOUCH, wall, from_side="above")
        strike = _round_to_grid(wall + entry_offset, down=False)
        invalidation = Condition("SPX", Comparator.LTE, wall - wall_buffer, hold_seconds=60)

    if abs(proposed_level - wall) >= 0.5:
        corrections.append(
            f"You said the {wall_label} is {proposed_level:g}; live GEX puts it at "
            f"{wall:g}. Using {wall:g}."
        )
    corrections.append(
        f"Trigger set at the {wall_label} ({wall:g}); entry strike {strike:g} "
        f"({entry_offset:g}pt inside), invalidation if SPX holds beyond "
        f"{wall + wall_buffer if side is Side.PUT else wall - wall_buffer:g}."
    )

    plan = TradePlan(
        thesis=thesis, side=side, trigger=trigger, strike=strike, expiry=expiry,
        tp_pct=tp_pct, sl_pct=sl_pct, invalidation=invalidation,
        time_stop_et=time_stop_et, valid_until_et=valid_until_et,
        notes=" ".join(corrections),
    )
    return Proposal(plan=plan, corrections=corrections)
