"""Append-only play journal — thesis → plan → events → outcome.

The only way to evaluate discretionary trades: you can't backtest "I expect the
holiday drift to clean up." So every plan, every agent correction, every fill and
exit is logged. After 30-50 plays you can ask whether the agent's *corrections*
add value even when the thesis itself can't be scored.

One JSONL file per session date under JOURNAL_DIR. Each line is one event.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from winthorpe.config import JOURNAL_DIR

logger = logging.getLogger(__name__)


class Journal:
    def __init__(self, session_date: Optional[str] = None, journal_dir: Path = JOURNAL_DIR):
        self.dir = Path(journal_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session_date = session_date or datetime.now(UTC).date().isoformat()
        self.path = self.dir / f"plays-{self.session_date}.jsonl"

    def _write(self, kind: str, payload: dict[str, Any]) -> None:
        row = {"ts": datetime.now(UTC).isoformat(), "kind": kind, **payload}
        with self.path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        logger.debug("journal %s: %s", kind, payload.get("plan_id", ""))

    def plan_signed(self, plan_dict: dict) -> None:
        self._write("plan_signed", {"plan_id": plan_dict.get("plan_id"), "plan": plan_dict})

    def event(self, plan_id: str, event: str, **data: Any) -> None:
        self._write("event", {"plan_id": plan_id, "event": event, **data})

    def closed(self, plan_id: str, pnl: float, reason: str, **data: Any) -> None:
        self._write("closed", {"plan_id": plan_id, "pnl": pnl, "reason": reason, **data})
