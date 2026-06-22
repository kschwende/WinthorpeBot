"""Daily loss limit, single-play concurrency, and the kill switch.

The structural guardrails the agent can never talk past mid-session. Decisions
locked with the user 2026-06-22:
  * Daily max loss $5,000 — on touch the session goes DARK (no new entries; any
    open position is flattened by the engine).
  * No win-stop — a green day keeps trading agreed plans.
  * One play at a time — no new plan arms while a position is open.

The kill switch is for MECHANICAL emergencies only (broker glitch, unfillable
leg, cascade). Every market-based reason to bail is already a plan invalidation
rule the engine monitors — so the kill switch should rarely be reached for a
market reason. It is high-friction and logged on purpose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from winthorpe.config import MAX_DAILY_LOSS

logger = logging.getLogger(__name__)


@dataclass
class SessionRisk:
    """In-memory risk state for one trading session.

    realized_pnl accumulates closed-trade P&L (credit received on close minus
    debit paid on open, ×100×contracts). Negative = loss.
    """
    max_daily_loss: float = MAX_DAILY_LOSS
    realized_pnl: float = 0.0
    open_position: bool = False
    killed: bool = False
    kill_reason: str = ""
    closed_trades: int = field(default=0)
    _halt_latched: bool = field(default=False)

    # -- the gate the engine checks before arming/entering ------------------
    def can_open(self) -> tuple[bool, str]:
        """May the engine arm/enter a NEW play right now?"""
        if self.killed:
            return False, f"kill switch engaged: {self.kill_reason}"
        if self.is_halted():
            return False, (f"daily loss limit hit "
                           f"(realized ${self.realized_pnl:,.0f} ≤ -${self.max_daily_loss:,.0f})")
        if self.open_position:
            return False, "a play is already open (one at a time)"
        return True, ""

    def is_halted(self) -> bool:
        """True once realized losses reach the daily limit. Latches for the day —
        a later P&L recovery cannot re-arm the session (and with one-at-a-time
        you can't open a recovering play anyway)."""
        if self.realized_pnl <= -abs(self.max_daily_loss):
            self._halt_latched = True
        return self._halt_latched

    def remaining_budget(self) -> float:
        """Dollars a new play may risk: what's left before the daily limit.

        With no win-stop and one-at-a-time, the per-play budget is simply the
        distance from current realized P&L down to the -$5k floor.
        """
        return max(0.0, self.max_daily_loss + self.realized_pnl)

    # -- mutations the engine calls ----------------------------------------
    def mark_opened(self) -> None:
        self.open_position = True

    def record_close(self, pnl: float) -> None:
        """Record a closed play's realized P&L and free the concurrency slot."""
        self.realized_pnl += pnl
        self.open_position = False
        self.closed_trades += 1
        logger.info(
            "play closed pnl=$%.0f | session realized=$%.0f | remaining budget=$%.0f%s",
            pnl, self.realized_pnl, self.remaining_budget(),
            "  [HALTED]" if self.is_halted() else "",
        )
        if self.is_halted():
            logger.warning("DAILY LOSS LIMIT REACHED — session dark, no new entries")

    def engage_kill(self, reason: str) -> None:
        """Mechanical emergency stop. Logged, latching."""
        self.killed = True
        self.kill_reason = reason
        logger.error("KILL SWITCH ENGAGED: %s", reason)
