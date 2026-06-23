"""The trade-plan object — the contract between human thesis and autonomous execution.

This is the keystone of WinthorpeBot. You and the agent deliberate the fields;
``sign()`` is the authorization boundary. After a plan is SIGNED, the engine owns
the trigger and the exits — the human has no vote until the position is flat.

A plan carries TWO independent protections, by design:
  * an OCO premium bracket (tp_pct / sl_pct) — mechanical, broker-side, fires even
    if the engine process dies;
  * an underlying invalidation rule — engine-monitored ("if SPX reclaims 7535 and
    holds 60s, thesis void → flatten"), so the bail decision is a pre-agreed rule,
    not a gut call.

Sizing is NOT stored as a free number — it is derived at fill time from the stop
distance and the remaining daily budget (see winthorpe.risk.sizing).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    """Which option we BUY (this is a long-premium book)."""
    PUT = "PUT"
    CALL = "CALL"

    @property
    def occ_right(self) -> str:
        return "P" if self is Side.PUT else "C"

    @property
    def open_direction(self) -> str:
        return "BUY_TO_OPEN_PUT" if self is Side.PUT else "BUY_TO_OPEN_CALL"


class Comparator(str, Enum):
    CROSS_ABOVE = "cross_above"   # spot crosses up through level
    CROSS_BELOW = "cross_below"   # spot crosses down through level
    TOUCH = "touch"              # spot reaches level from either side
    GTE = "gte"
    LTE = "lte"


class PlanStatus(str, Enum):
    DRAFT = "draft"          # under deliberation, editable
    SIGNED = "signed"        # authorized; human locked out
    ARMED = "armed"          # engine watching the trigger
    TRIGGERED = "triggered"  # condition hit, entering
    OPEN = "open"            # position live, managed
    CLOSED = "closed"        # flat — terminal
    REJECTED = "rejected"    # infeasible / vetoed — terminal
    EXPIRED = "expired"      # trigger never hit before valid_until — terminal


@dataclass
class Condition:
    """A spot-based condition on a trigger/analysis symbol (SPX/SPY/ES)."""
    symbol: str                       # "SPX" | "SPY" | "ES"
    comparator: Comparator
    level: float
    from_side: Optional[str] = None   # "below" | "above" (for TOUCH provenance)
    hold_seconds: int = 0             # must hold the condition this long to fire

    def __post_init__(self):
        self.comparator = Comparator(self.comparator)


@dataclass
class TradePlan:
    """A single deliberated, signable SPXW option play."""

    # --- the human read ---
    thesis: str
    side: Side

    # --- the trigger (when to enter) ---
    trigger: Condition

    # --- the instrument ---
    strike: float
    expiry: str                          # YYYY-MM-DD (0DTE = today)

    # --- entry mechanics ---
    entry_type: str = "OPTION_MARKET"    # OPTION_MARKET | OPTION_LIMIT
    entry_limit: Optional[float] = None  # required if OPTION_LIMIT

    # --- exits: mechanical OCO bracket (premium %, off fill) ---
    tp_pct: float = 0.0                  # +0.30 = take profit at fill × 1.30
    sl_pct: float = 0.0                  # -0.25 = stop at fill × 0.75 (negative)

    # --- exits: optional trailing stop (engine-monitored on the option mark) ---
    # Lets a winner run instead of capping at the fixed tp_pct. Both-or-neither;
    # absent = today's fixed-OCO behavior. The sl_pct OCO stays as the mechanical
    # floor; tp_pct stays as a hard ceiling. The trail exits BELOW that ceiling on
    # a pullback off the high-water mark.
    trail_activate_pct: Optional[float] = None  # arm once up this much (+0.20)
    trail_pct: Optional[float] = None           # exit on this pullback off the high (0.25)

    # --- exits: engine-monitored underlying invalidation (the agreed bail rule) ---
    invalidation: Optional[Condition] = None

    # --- exits: time-stop (REQUIRED for 0DTE; no global EOD backstop) ---
    time_stop_et: str = ""               # "HH:MM" ET — flatten at/after this

    # --- sizing band + optional tighter per-play cap ---
    min_contracts: int = 5
    max_contracts: int = 10
    max_play_loss: Optional[float] = None  # None → use remaining daily budget

    # --- lifecycle / provenance ---
    plan_id: str = ""
    status: PlanStatus = PlanStatus.DRAFT
    valid_until_et: str = ""             # "HH:MM" ET — arm window expiry
    created_ts: float = field(default_factory=time.time)
    signed_ts: Optional[float] = None
    notes: str = ""                      # agent corrections / deliberation trail

    def __post_init__(self):
        self.side = Side(self.side)
        self.status = PlanStatus(self.status)
        if isinstance(self.trigger, dict):
            self.trigger = Condition(**self.trigger)
        if isinstance(self.invalidation, dict):
            self.invalidation = Condition(**self.invalidation)

    # -- validation: everything that makes a plan executable ----------------
    def validate(self) -> list[str]:
        """Return a list of problems; empty list = ready to sign."""
        errs: list[str] = []
        if not self.thesis.strip():
            errs.append("thesis is empty — the human read is the whole point")
        if self.entry_type == "OPTION_LIMIT" and self.entry_limit is None:
            errs.append("OPTION_LIMIT requires entry_limit")
        if not (self.tp_pct > 0):
            errs.append("tp_pct must be > 0")
        if not (-1.0 < self.sl_pct < 0):
            errs.append("sl_pct must be negative and > -1.0 (a fractional loss)")
        if (self.trail_pct is None) != (self.trail_activate_pct is None):
            errs.append("trail_pct and trail_activate_pct must be set together (or both omitted)")
        if self.trail_pct is not None and not (0 < self.trail_pct < 1):
            errs.append("trail_pct must be a fraction in (0, 1)")
        if self.trail_activate_pct is not None and self.trail_activate_pct < 0:
            errs.append("trail_activate_pct must be >= 0")
        if not self.time_stop_et:
            errs.append("time_stop_et is REQUIRED for 0DTE (no global EOD backstop)")
        if self.min_contracts < 1 or self.max_contracts < self.min_contracts:
            errs.append("contract band invalid (need 1 <= min <= max)")
        if self.strike <= 0:
            errs.append("strike must be positive")
        return errs

    def sign(self) -> "TradePlan":
        """The authorization boundary. Raises if the plan isn't executable."""
        errs = self.validate()
        if errs:
            raise ValueError("cannot sign plan:\n  - " + "\n  - ".join(errs))
        self.status = PlanStatus.SIGNED
        self.signed_ts = time.time()
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        # Enums → their values for clean JSON.
        d["side"] = self.side.value
        d["status"] = self.status.value
        d["trigger"]["comparator"] = self.trigger.comparator.value
        if self.invalidation:
            d["invalidation"]["comparator"] = self.invalidation.comparator.value
        return d
