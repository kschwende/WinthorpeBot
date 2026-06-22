"""Derived position sizing — the coupled 5-10 / $5k constraint.

Contract count is NOT a free choice. On long premium the size and the stop are
linked: 10 contracts of an $8 put risk $8,000 to zero, already past the daily
limit. So contracts are DERIVED from the stop distance and the remaining budget:

    per_contract_risk = (entry_premium - stop_premium) * 100
    max_affordable     = floor(budget / per_contract_risk)
    contracts          = clamp(min_contracts, max_contracts, max_affordable)

If even ``min_contracts`` would exceed the budget, the plan is INFEASIBLE — the
engine says so at plan time rather than silently taking a play that can blow the
limit. That rejection is a feature: it forces tighter stops or a smaller thesis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from winthorpe.config import MAX_CONTRACTS, MIN_CONTRACTS, OPTION_MULTIPLIER


@dataclass
class SizingResult:
    feasible: bool
    contracts: int
    per_contract_risk: float        # dollars lost per contract if stop hits
    worst_case_loss: float          # contracts * per_contract_risk
    reason: str = ""                # why infeasible / how it was clamped


def derive_contracts(
    entry_premium: float,
    stop_premium: float,
    budget: float,
    min_contracts: int = MIN_CONTRACTS,
    max_contracts: int = MAX_CONTRACTS,
) -> SizingResult:
    """Size a long-option play within the band such that a stop-out fits budget.

    entry_premium / stop_premium are per-contract option prices (e.g. 8.00, 6.00).
    budget is the dollars the play may risk (remaining daily budget, or a tighter
    per-play cap). Returns an infeasible result rather than raising.
    """
    if entry_premium <= 0:
        return SizingResult(False, 0, 0.0, 0.0, "entry_premium must be > 0")
    if stop_premium < 0:
        return SizingResult(False, 0, 0.0, 0.0, "stop_premium cannot be negative")
    if stop_premium >= entry_premium:
        return SizingResult(
            False, 0, 0.0, 0.0,
            f"stop_premium {stop_premium} >= entry_premium {entry_premium} "
            f"(stop must be below entry for a long option)",
        )
    if budget <= 0:
        return SizingResult(False, 0, 0.0, 0.0, "no budget remaining")

    per_contract_risk = round((entry_premium - stop_premium) * OPTION_MULTIPLIER, 2)
    max_affordable = math.floor(budget / per_contract_risk)

    if max_affordable < min_contracts:
        return SizingResult(
            False, 0, per_contract_risk, 0.0,
            f"infeasible: even {min_contracts} contracts risk "
            f"${min_contracts * per_contract_risk:,.0f} > budget ${budget:,.0f}. "
            f"Tighten the stop or lower the thesis.",
        )

    contracts = min(max_contracts, max_affordable)
    worst = round(contracts * per_contract_risk, 2)
    reason = ""
    if contracts < max_contracts:
        reason = (f"clamped to {contracts} (max {max_contracts} would risk "
                  f"${max_contracts * per_contract_risk:,.0f} > budget ${budget:,.0f})")
    return SizingResult(True, contracts, per_contract_risk, worst, reason)


def stop_premium_from_sl_pct(entry_premium: float, sl_pct: float) -> float:
    """OCO stop premium from a fractional sl_pct (negative). -0.25 → entry × 0.75."""
    return round(entry_premium * (1.0 + sl_pct), 2)


def tp_premium_from_tp_pct(entry_premium: float, tp_pct: float) -> float:
    """OCO take-profit premium from a fractional tp_pct. +0.30 → entry × 1.30."""
    return round(entry_premium * (1.0 + tp_pct), 2)
