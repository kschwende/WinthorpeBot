"""The autonomous engine — arm → fire → manage, human locked out until flat.

Drives ONE signed plan at a time (one-at-a-time concurrency). Dependency-injected
(market, broker, risk, journal, clock) so the state machine is unit-testable with
a scripted market and runs live with tastytrade in production.

Lifecycle:
  SIGNED → arm() → [poll trigger] → enter() → [poll exits] → close() → CLOSED

Exits are layered: the broker-side OCO bracket is the mechanical floor; the
engine additionally monitors the underlying invalidation rule and the time-stop
and will cancel the bracket and market-close when either fires.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from winthorpe.broker.models import TradeDecision
from winthorpe.broker.options import resolve_spxw_option
from winthorpe.broker.tastytrade_broker import _build_oco_bracket
from winthorpe.config import EXEC_INSTRUMENT, OPTION_MULTIPLIER
from winthorpe.engine.market import MarketView
from winthorpe.journal.journal import Journal
from winthorpe.plan.schema import Comparator, Condition, PlanStatus, TradePlan
from winthorpe.risk.limits import SessionRisk
from winthorpe.risk.sizing import (
    derive_contracts,
    stop_premium_from_sl_pct,
    tp_premium_from_tp_pct,
)

logger = logging.getLogger(__name__)


def condition_met(cond: Condition, market: MarketView) -> bool:
    """Instantaneous evaluation of a spot condition. Hold timing is tracked by
    the engine, not here."""
    spot = market.spot(cond.symbol)
    if spot is None:
        return False
    c = cond.comparator
    if c in (Comparator.GTE, Comparator.CROSS_ABOVE):
        return spot >= cond.level
    if c in (Comparator.LTE, Comparator.CROSS_BELOW):
        return spot <= cond.level
    # TOUCH — directional if from_side given, else level reached either way.
    if cond.from_side == "below":
        return spot >= cond.level
    if cond.from_side == "above":
        return spot <= cond.level
    return spot >= cond.level


@dataclass
class OpenState:
    occ_symbol: str
    streamer_symbol: str
    contracts: int
    entry_premium: float
    oco_id: Optional[str]


class Engine:
    def __init__(self, market: MarketView, broker, risk: SessionRisk,
                 journal: Journal):
        self.market = market
        self.broker = broker
        self.risk = risk
        self.journal = journal
        self.open_state: Optional[OpenState] = None  # live position (None when flat)

    # -- live orchestration ------------------------------------------------
    def run_plan(self, plan: TradePlan, poll_interval: float = 1.0,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        """Drive one SIGNED plan through its whole life. Blocks until flat.

        Glue over the tested units (enter/manage_step/close). The human is
        locked out for the duration — the only intervention is the kill switch.
        """
        if plan.status is not PlanStatus.SIGNED:
            raise ValueError(f"run_plan needs a SIGNED plan, got {plan.status}")
        self.journal.plan_signed(plan.to_dict())

        # ARM — poll the trigger (honoring hold_seconds) until fire / expiry / kill.
        plan.status = PlanStatus.ARMED
        self.journal.event(plan.plan_id, "armed", trigger=plan.trigger.__dict__)
        first_met: Optional[float] = None
        while True:
            if self.risk.killed:
                self.journal.event(plan.plan_id, "kill_before_entry", reason=self.risk.kill_reason)
                return
            if plan.valid_until_et and self.market.now_et().strftime("%H:%M") >= plan.valid_until_et:
                plan.status = PlanStatus.EXPIRED
                self.journal.event(plan.plan_id, "expired", valid_until=plan.valid_until_et)
                return
            if condition_met(plan.trigger, self.market):
                first_met = first_met if first_met is not None else self.market.now_et().timestamp()
                if self.market.now_et().timestamp() - first_met >= plan.trigger.hold_seconds:
                    break
            else:
                first_met = None
            sleep_fn(poll_interval)

        plan.status = PlanStatus.TRIGGERED
        st = self.enter(plan)
        if st is None:
            return  # rejected/infeasible — enter() already journaled why

        self._manage_loop(plan, st, poll_interval, sleep_fn)

    def resume_management(self, plan: TradePlan, st: OpenState,
                          poll_interval: float = 1.0,
                          sleep_fn: Callable[[float], None] = time.sleep) -> None:
        """Re-attach the management loop to a position recovered after a restart
        (skips arm/enter — the position is already open and the OCO already live)."""
        plan.status = PlanStatus.OPEN
        self.open_state = st
        self.journal.event(plan.plan_id, "management_resumed", occ=st.occ_symbol)
        self._manage_loop(plan, st, poll_interval, sleep_fn)

    def _manage_loop(self, plan: TradePlan, st: OpenState,
                     poll_interval: float, sleep_fn: Callable[[float], None]) -> None:
        """Poll the exits until one fires. Shared by run_plan and resume_management."""
        while True:
            if self.risk.killed:
                self.close(plan, st, "kill_switch")
                return
            reason = self.manage_step(plan, st)
            if reason:
                self.close(plan, st, reason)
                return
            sleep_fn(poll_interval)

    # -- entry -------------------------------------------------------------
    def enter(self, plan: TradePlan) -> Optional[OpenState]:
        """Resolve the option, size against remaining budget, buy to open, and
        place the OCO bracket. Returns OpenState, or None if infeasible/rejected."""
        ok, why = self.risk.can_open()
        if not ok:
            logger.warning("enter blocked: %s", why)
            self.journal.event(plan.plan_id, "entry_blocked", reason=why)
            plan.status = PlanStatus.REJECTED
            return None

        resolved = resolve_spxw_option(plan.side.occ_right, plan.strike, plan.expiry)
        occ = resolved["occ_symbol"]
        streamer = resolved["streamer_symbol"]

        entry_premium = self.market.option_mark(streamer)
        if not entry_premium or entry_premium <= 0:
            self.journal.event(plan.plan_id, "entry_no_mark", streamer=streamer)
            plan.status = PlanStatus.REJECTED
            return None

        # Coupled sizing: budget = tighter of remaining-daily and plan cap.
        budget = self.risk.remaining_budget()
        if plan.max_play_loss is not None:
            budget = min(budget, plan.max_play_loss)
        stop_prem = stop_premium_from_sl_pct(entry_premium, plan.sl_pct)
        sizing = derive_contracts(entry_premium, stop_prem, budget,
                                  plan.min_contracts, plan.max_contracts)
        if not sizing.feasible:
            logger.warning("plan %s infeasible: %s", plan.plan_id, sizing.reason)
            self.journal.event(plan.plan_id, "infeasible",
                               reason=sizing.reason, entry_premium=entry_premium,
                               stop_premium=stop_prem, budget=budget)
            plan.status = PlanStatus.REJECTED
            return None

        decision = TradeDecision(
            strategy=plan.plan_id, product=EXEC_INSTRUMENT,
            direction=plan.side.open_direction, size=sizing.contracts,
            entry_type=plan.entry_type, entry_price=plan.entry_limit,
            metadata={"occ_symbol": occ, "streamer_symbol": streamer},
        )
        result = self.broker.place_order(decision)
        if result.get("status") in ("rejected", "error"):
            self.journal.event(plan.plan_id, "entry_rejected", occ=occ, order=result)
            plan.status = PlanStatus.REJECTED
            return None

        # Resolve the REAL fill price (ported _v41_wait_for_fill); the OCO bracket
        # and P&L key off this, not the pre-trade mark. Falls back to the mark only
        # if the broker can't confirm a fill price in time.
        fill = result.get("fill_price")
        waiter = getattr(self.broker, "wait_for_fill", None)
        if not fill and waiter:
            enriched = waiter(result.get("order_id"), occ)
            if enriched:
                fill = enriched.get("fill_price")
                self.journal.event(plan.plan_id, "fill_resolved", fill_price=fill,
                                   source=enriched.get("source"))
        fill = fill or result.get("entry_price") or entry_premium

        self.journal.event(plan.plan_id, "entered", occ=occ, contracts=sizing.contracts,
                           entry_premium=fill, worst_case_loss=sizing.worst_case_loss,
                           order=result)
        self.risk.mark_opened()
        plan.status = PlanStatus.OPEN

        # Mechanical OCO bracket off the fill premium.
        tp = tp_premium_from_tp_pct(fill, plan.tp_pct)
        sl = stop_premium_from_sl_pct(fill, plan.sl_pct)
        oco_id = None
        try:
            bracket = _build_oco_bracket(occ, sizing.contracts, tp, sl)
            oco_res = self.broker.place_complex_order(bracket)
            oco_id = oco_res.get("complex_order_id")
            self.journal.event(plan.plan_id, "oco_placed", tp=tp, sl=sl, oco=oco_res)
        except Exception:
            logger.exception("OCO bracket placement failed for %s", plan.plan_id)
            self.journal.event(plan.plan_id, "oco_failed", tp=tp, sl=sl)

        self.open_state = OpenState(occ, streamer, sizing.contracts, fill, oco_id)
        # Persist enough to recover this live trade if the process restarts.
        self.risk.set_live_position({
            "open_state": {"occ_symbol": occ, "streamer_symbol": streamer,
                           "contracts": sizing.contracts, "entry_premium": fill,
                           "oco_id": oco_id},
            "plan": plan.to_dict(),
        })
        return self.open_state

    # -- management --------------------------------------------------------
    def manage_step(self, plan: TradePlan, st: OpenState) -> Optional[str]:
        """One management tick. Returns a close-reason string if the position
        should be flattened now, else None.

        Precedence: invalidation (thesis broke) > time-stop > let the OCO ride.
        """
        # 1. Underlying invalidation — the pre-agreed bail rule.
        if plan.invalidation and condition_met(plan.invalidation, self.market):
            return "invalidation"
        # 2. Time-stop.
        now = self.market.now_et().strftime("%H:%M")
        if plan.time_stop_et and now >= plan.time_stop_et:
            return "time_stop"
        # 3. OCO filled out-of-band → broker no longer shows the position.
        positions = self.broker.get_positions()
        held = any(str(p.get("symbol", "")).strip() == st.occ_symbol
                   and int(p.get("quantity", 0)) != 0 for p in positions)
        if not held:
            return "oco_filled"
        return None

    # -- exit --------------------------------------------------------------
    def close(self, plan: TradePlan, st: OpenState, reason: str) -> float:
        """Flatten and record. Cancel-before-replace, then market sell-to-close
        (unless the OCO already did it). Returns realized P&L."""
        if reason != "oco_filled":
            # Cancel the bracket + any stuck working order before closing.
            if st.oco_id:
                self.broker.cancel_complex_order(st.oco_id)
            self.broker.cancel_working_orders_for(EXEC_INSTRUMENT)
            close_decision = TradeDecision(
                strategy=plan.plan_id, product=EXEC_INSTRUMENT,
                direction="SELL_TO_CLOSE", size=st.contracts,
                entry_type="OPTION_MARKET",
                metadata={"occ_symbol": st.occ_symbol},
            )
            res = self.broker.place_order(close_decision)
            exit_premium = (res.get("fill_price")
                            or self.market.option_mark(st.streamer_symbol)
                            or st.entry_premium)
        else:
            # OCO already closed it — resolve the exit price from the option mark.
            exit_premium = self.market.option_mark(st.streamer_symbol) or st.entry_premium

        pnl = round((exit_premium - st.entry_premium) * OPTION_MULTIPLIER * st.contracts, 2)
        self.risk.record_close(pnl)
        plan.status = PlanStatus.CLOSED
        self.journal.closed(plan.plan_id, pnl=pnl, reason=reason,
                            entry_premium=st.entry_premium, exit_premium=exit_premium,
                            contracts=st.contracts,
                            session_realized=self.risk.realized_pnl,
                            remaining_budget=self.risk.remaining_budget())
        logger.info("closed %s reason=%s pnl=$%.0f", plan.plan_id, reason, pnl)
        self.open_state = None
        return pnl
