"""Daily loss limit, single-play concurrency, kill switch — persisted per session.

The structural guardrails the agent can never talk past mid-session. Decisions
locked with the user 2026-06-22:
  * Daily max loss $5,000 — on touch the session goes DARK (latching halt).
  * No win-stop — a green day keeps trading agreed plans.
  * One play at a time — no new plan arms while a position is open.

State persists to ``state/session_<date>.json`` so a process restart MID-SESSION
resumes the same $5k budget, halt latch, and the open-position context (so a
crash can't reset the daily counter or lose track of a live trade). A new session
date starts fresh. The kill switch is for MECHANICAL emergencies only.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from winthorpe import config
from winthorpe.config import MAX_DAILY_LOSS
from winthorpe.data.json_store import atomic_write_json

logger = logging.getLogger(__name__)


def session_path(session_date: str) -> Path:
    # STATE_DIR read dynamically so tests can redirect it.
    return config.STATE_DIR / f"session_{session_date}.json"


@dataclass
class SessionRisk:
    """Risk state for one trading session. Self-persists on every mutation."""

    max_daily_loss: float = MAX_DAILY_LOSS
    realized_pnl: float = 0.0
    open_position: bool = False
    killed: bool = False
    kill_reason: str = ""
    closed_trades: int = 0
    _halt_latched: bool = False
    # Context to recover a live trade after a restart (occ/oco/plan). None = flat.
    live_position: Optional[dict] = None
    # Persistence (not serialized into the payload).
    session_date: str = ""
    persist_path: Optional[Path] = field(default=None, repr=False)

    # -- persistence -------------------------------------------------------
    _PAYLOAD_FIELDS = (
        "max_daily_loss", "realized_pnl", "open_position", "killed",
        "kill_reason", "closed_trades", "_halt_latched", "live_position",
        "session_date",
    )

    def _save(self) -> None:
        if self.persist_path is None:
            return
        payload = {k: getattr(self, k) for k in self._PAYLOAD_FIELDS}
        atomic_write_json(self.persist_path, payload)

    @classmethod
    def load_or_new(cls, session_date: str, persist_path: Optional[Path] = None) -> "SessionRisk":
        """Resume today's persisted state, or start fresh for a new session date."""
        path = persist_path or session_path(session_date)
        if path.exists():
            import json
            try:
                data = json.loads(path.read_text())
            except Exception:
                logger.warning("session state unreadable at %s; starting fresh", path)
                data = {}
            if data.get("session_date") == session_date:
                obj = cls(**{k: data[k] for k in cls._PAYLOAD_FIELDS if k in data})
                obj.persist_path = path
                logger.info("resumed session %s: realized=$%.0f open=%s halted=%s",
                            session_date, obj.realized_pnl, obj.open_position, obj.is_halted())
                return obj
        obj = cls(session_date=session_date, persist_path=path)
        obj._save()
        return obj

    # -- the gate the engine checks before arming/entering ------------------
    def can_open(self) -> tuple[bool, str]:
        if self.killed:
            return False, f"kill switch engaged: {self.kill_reason}"
        if self.is_halted():
            return False, (f"daily loss limit hit "
                           f"(realized ${self.realized_pnl:,.0f} ≤ -${self.max_daily_loss:,.0f})")
        if self.open_position:
            return False, "a play is already open (one at a time)"
        return True, ""

    def is_halted(self) -> bool:
        """Latching — once the daily limit is reached it stays dark for the day."""
        return self._halt_latched

    def remaining_budget(self) -> float:
        """Dollars a new play may risk before the daily limit."""
        return max(0.0, self.max_daily_loss + self.realized_pnl)

    # -- mutations the engine calls ----------------------------------------
    def mark_opened(self) -> None:
        self.open_position = True
        self._save()

    def set_live_position(self, ctx: Optional[dict]) -> None:
        """Persist enough to recover a live trade after a restart (or clear it)."""
        self.live_position = ctx
        self._save()

    def record_close(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.open_position = False
        self.live_position = None
        self.closed_trades += 1
        if self.realized_pnl <= -abs(self.max_daily_loss):
            self._halt_latched = True
        self._save()
        logger.info(
            "play closed pnl=$%.0f | session realized=$%.0f | remaining budget=$%.0f%s",
            pnl, self.realized_pnl, self.remaining_budget(),
            "  [HALTED]" if self.is_halted() else "",
        )
        if self.is_halted():
            logger.warning("DAILY LOSS LIMIT REACHED — session dark, no new entries")

    def engage_kill(self, reason: str) -> None:
        self.killed = True
        self.kill_reason = reason
        self._save()
        logger.error("KILL SWITCH ENGAGED: %s", reason)
