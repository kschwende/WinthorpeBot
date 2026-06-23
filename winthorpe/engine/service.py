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
        self.journal = Journal()
        # Resume today's persisted risk (budget, halt, open-position context) if a
        # prior process ran this session; fresh on a new session date.
        self.risk = SessionRisk.load_or_new(self.journal.session_date)
        self.broker = create_broker(self.market)
        self.engine = Engine(self.market, self.broker, self.risk, self.journal)
        self._plan: Optional[TradePlan] = None
        self._thread: Optional[threading.Thread] = None
        self.needs_reconcile: bool = False
        self.reconcile_note: str = ""
        if self.risk.live_position is not None:
            self._reconcile_open_position(self.risk.live_position)
        if start_stream:
            self.start_stream()

    def _reconcile_open_position(self, lp: dict) -> None:
        """A persisted open position survived a restart. Recover against broker
        truth: if still held, re-attach the management loop; if gone (live) flag
        for human resolution rather than guessing P&L."""
        from winthorpe.engine.engine import OpenState

        osd = lp.get("open_state", {})
        st = OpenState(osd.get("occ_symbol", ""), osd.get("streamer_symbol", ""),
                       int(osd.get("contracts", 0)), float(osd.get("entry_premium", 0)),
                       osd.get("oco_id"), float(osd.get("high_water", 0) or 0),
                       bool(osd.get("trail_armed", False)))
        try:
            plan = TradePlan(**lp["plan"])
        except Exception as exc:
            self.needs_reconcile = True
            self.reconcile_note = f"could not rebuild plan from persisted state: {exc}"
            logger.error(self.reconcile_note)
            return
        plan.status = PlanStatus.OPEN
        self._plan = plan

        if is_live():
            positions = self.broker.get_positions()
            held = any(str(p.get("symbol", "")).strip() == st.occ_symbol
                       and int(p.get("quantity", 0)) != 0 for p in positions)
            if not held:
                # Closed out-of-band while we were down — can't trust P&L. Block
                # new entries until the human reconciles (broker-truth doctrine).
                self.needs_reconcile = True
                self.reconcile_note = (
                    f"persisted position {st.occ_symbol} not held at broker after "
                    f"restart — manual reconcile required (check fills, set realized P&L)"
                )
                self.journal.event(plan.plan_id, "reconcile_needed", occ=st.occ_symbol,
                                   note=self.reconcile_note)
                logger.warning(self.reconcile_note)
                return
        # Live-and-held, or dry-run: re-attach the management loop.
        self._thread = threading.Thread(
            target=self.engine.resume_management, args=(plan, st),
            name=f"resume-{plan.plan_id}", daemon=True,
        )
        self._thread.start()
        logger.info("resumed management of %s after restart", st.occ_symbol)

    def start_stream(self) -> None:
        if self.stream and self.stream.is_alive():
            return
        self.stream = MarketStream(self.store)
        # Mark the warm-up clock synchronously so a read between start() and the
        # thread entering run() reports "warming", never a spurious "down".
        self.store.mark_starting()
        self.stream.start()

    # -- actions -----------------------------------------------------------
    def submit_signed_plan(self, plan: TradePlan) -> dict:
        """Sign (if needed) and arm a plan in a background thread. One at a time.

        Returns an acceptance dict; rejects if a play is already live or the
        session can't open.
        """
        if self.needs_reconcile:
            return {"accepted": False, "reason": f"reconcile required: {self.reconcile_note}"}
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

    def flatten_position(self, reason: str) -> dict:
        """Graceful, NON-latching close — close an open position or cancel an
        armed-but-unfilled plan, without engaging the kill latch. The engine acts
        on its next tick (~1s); poll get_status until flat."""
        if not (self._thread and self._thread.is_alive()):
            return {"flattened": False, "reason": "no plan running (nothing to flatten)"}
        self.engine.flatten_requested = True
        had_position = self.engine.open_state is not None
        return {"flattened": True,
                "action": "closing open position" if had_position else "cancelling armed plan",
                "note": f"flatten requested ({reason}); engine acts on its next tick — "
                        f"poll get_status until plan_running is false"}

    def reset_kill(self) -> dict:
        """Clear a kill-switch latch after reconciling (requires flat). Lets new
        plans arm again without restarting the desk. Does NOT clear the daily-loss
        halt (a separate, real risk limit)."""
        if self._thread and self._thread.is_alive():
            return {"reset": False, "reason": "a plan is still running — flatten first"}
        if self.engine.open_state is not None or self.risk.open_position:
            return {"reset": False, "reason": "a position is open — flatten/reconcile before resetting"}
        if not self.risk.clear_kill():
            return {"reset": False, "reason": "kill switch is not engaged"}
        return {"reset": True,
                "note": "kill switch cleared; new plans can be armed. "
                        "(Daily-loss halt, if any, is unaffected.)"}

    # -- reads (the agent's eyes) ------------------------------------------
    def status(self) -> dict:
        running = bool(self._thread and self._thread.is_alive())
        st = {
            "live": is_live(),
            "stream_connected": self.store.connected,
            "stream_state": self.store.stream_state(),
            "plan_running": running,
            "current_plan": self._plan.plan_id if self._plan else None,
            "current_status": self._plan.status.value if self._plan else None,
            "needs_reconcile": self.needs_reconcile,
            "reconcile_note": self.reconcile_note,
            "risk": self.session_risk(),
        }
        # Diagnostic: if the stream is down AND other winthorpe.mcp.server
        # processes exist, a stale orphan is likely squatting the DXLink slot.
        # Surface it (read-only) so the degraded state isn't a silent mystery.
        if not self.store.connected:
            from winthorpe.data.process_guard import find_sibling_servers
            others = find_sibling_servers()
            if others:
                st["stream_warning"] = (
                    f"stream down and {len(others)} other winthorpe.mcp.server "
                    f"process(es) detected (pids {others}) — a stale orphan may be "
                    f"squatting the DXLink slot. Kill it; the stream auto-retries."
                )
        return st

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
        trail = None
        if self._plan.trail_pct is not None:
            trail = {
                "high_water": round(st.high_water, 2),
                "armed": st.trail_armed,
                "trail_stop": round(st.high_water * (1 - self._plan.trail_pct), 2),
                "activate_at": round(st.entry_premium * (1 + (self._plan.trail_activate_pct or 0.0)), 2),
            }
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
            "trail": trail,
            "oco_id": st.oco_id,
            "status": self._plan.status.value,
        }
