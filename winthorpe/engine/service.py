"""DeskService — the single stateful session object the MCP layer drives.

Owns the persistent stream, the engine, risk, journal, and broker, and runs ONE
signed plan at a time in a background thread (the human/agent is locked out of a
live trade). Read methods give the agent eyes every heartbeat; the only live
intervention is the kill switch.

This is the seam between the deterministic core and any harness (MCP, CLI). It
holds no LLM — the harness supplies the intelligence.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from winthorpe.broker import create_broker
from winthorpe.config import is_live
from winthorpe.data.market_stream import MarketStore, MarketStream
from winthorpe.engine.engine import Engine
from winthorpe.engine.market import StreamMarketView
from winthorpe.journal.journal import Journal
from winthorpe.plan.schema import PlanStatus, TradePlan
from winthorpe.risk.limits import SessionRisk

logger = logging.getLogger(__name__)


class DeskService:
    def __init__(self, *, start_stream: bool = True):
        self.store = MarketStore()
        self.stream: Optional[MarketStream] = None
        self.market = StreamMarketView(self.store)
        self.risk = SessionRisk()
        self.journal = Journal()
        self.broker = create_broker()
        self.engine = Engine(self.market, self.broker, self.risk, self.journal)
        self._plan: Optional[TradePlan] = None
        self._thread: Optional[threading.Thread] = None
        if start_stream:
            self.start_stream()

    def start_stream(self) -> None:
        if self.stream and self.stream.is_alive():
            return
        self.stream = MarketStream(self.store)
        self.stream.start()

    # -- actions -----------------------------------------------------------
    def submit_signed_plan(self, plan: TradePlan) -> dict:
        """Sign (if needed) and arm a plan in a background thread. One at a time.

        Returns an acceptance dict; rejects if a play is already live or the
        session can't open.
        """
        if self._thread and self._thread.is_alive():
            return {"accepted": False, "reason": "a plan is already running"}
        ok, why = self.risk.can_open()
        if not ok:
            return {"accepted": False, "reason": why}
        errs = plan.validate()
        if errs:
            return {"accepted": False, "reason": "invalid plan", "errors": errs}
        if plan.status is not PlanStatus.SIGNED:
            plan.sign()
        self._plan = plan
        self._thread = threading.Thread(
            target=self.engine.run_plan, args=(plan,), name=f"plan-{plan.plan_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("plan %s armed", plan.plan_id)
        return {"accepted": True, "plan_id": plan.plan_id, "status": plan.status.value,
                "live": is_live()}

    def engage_kill(self, reason: str) -> dict:
        self.risk.engage_kill(reason)
        return {"killed": True, "reason": reason}

    # -- reads (the agent's eyes) ------------------------------------------
    def status(self) -> dict:
        running = bool(self._thread and self._thread.is_alive())
        return {
            "live": is_live(),
            "stream_connected": self.store.connected,
            "plan_running": running,
            "current_plan": self._plan.plan_id if self._plan else None,
            "current_status": self._plan.status.value if self._plan else None,
            "risk": self.session_risk(),
        }

    def session_risk(self) -> dict:
        ok, why = self.risk.can_open()
        return {
            "realized_pnl": self.risk.realized_pnl,
            "remaining_budget": self.risk.remaining_budget(),
            "open_position": self.risk.open_position,
            "halted": self.risk.is_halted(),
            "killed": self.risk.killed,
            "closed_trades": self.risk.closed_trades,
            "can_open": ok,
            "can_open_reason": why,
        }

    def market_state(self) -> dict:
        return self.store.snapshot()

    def position_state(self) -> Optional[dict]:
        """Live telemetry for the open position, or None when flat. Read-only —
        the agent sees everything, touches nothing (except engage_kill)."""
        st = self.engine.open_state
        if st is None or self._plan is None:
            return None
        mark = self.market.option_mark(st.streamer_symbol)
        spot = self.market.spot(self._plan.trigger.symbol)
        unrealized = (round((mark - st.entry_premium) * 100 * st.contracts, 2)
                      if mark is not None else None)
        inv = self._plan.invalidation
        dist_to_inv = (round(abs(spot - inv.level), 2)
                       if (inv and spot is not None) else None)
        return {
            "plan_id": self._plan.plan_id,
            "occ_symbol": st.occ_symbol,
            "contracts": st.contracts,
            "entry_premium": st.entry_premium,
            "current_mark": mark,
            "unrealized_pnl": unrealized,
            "spot": spot,
            "distance_to_invalidation": dist_to_inv,
            "time_stop_et": self._plan.time_stop_et,
            "oco_id": st.oco_id,
            "status": self._plan.status.value,
        }
