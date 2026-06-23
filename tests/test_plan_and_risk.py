"""Tests for the trade-plan schema and the risk envelope.

The coupled sizing constraint and the daily-loss gate are the parts where a
silent bug costs real money, so they get the most coverage.
"""

import pytest

from winthorpe.config import MAX_DAILY_LOSS
from winthorpe.plan.schema import Comparator, Condition, PlanStatus, Side, TradePlan
from winthorpe.risk.limits import SessionRisk
from winthorpe.risk.sizing import (
    derive_contracts,
    stop_premium_from_sl_pct,
    tp_premium_from_tp_pct,
)


def _good_plan(**over):
    base = dict(
        thesis="fade the call wall after the holiday drift",
        side=Side.PUT,
        trigger=Condition("SPX", Comparator.TOUCH, 7530.0, from_side="below"),
        strike=7525.0,
        expiry="2026-06-22",
        tp_pct=0.30,
        sl_pct=-0.25,
        time_stop_et="15:45",
    )
    base.update(over)
    return TradePlan(**base)


# --- plan lifecycle ---------------------------------------------------------
def test_plan_signs_when_valid():
    p = _good_plan()
    assert p.status is PlanStatus.DRAFT
    p.sign()
    assert p.status is PlanStatus.SIGNED
    assert p.signed_ts is not None


def test_plan_requires_time_stop():
    """0DTE footgun guard: no time_stop_et must block signing."""
    p = _good_plan(time_stop_et="")
    assert any("time_stop" in e for e in p.validate())
    with pytest.raises(ValueError, match="time_stop"):
        p.sign()


def test_plan_rejects_non_negative_sl_pct():
    assert any("sl_pct" in e for e in _good_plan(sl_pct=0.25).validate())


def test_plan_rejects_empty_thesis():
    assert any("thesis" in e for e in _good_plan(thesis="  ").validate())


def test_clear_kill_resets_latch():
    r = SessionRisk()
    r.engage_kill("validation cleanup")
    assert r.killed and not r.can_open()[0]
    assert r.clear_kill() is True
    assert not r.killed and r.can_open()[0]   # arming re-enabled, no restart needed
    assert r.clear_kill() is False            # idempotent: nothing left to clear


def test_clear_kill_does_not_touch_daily_halt():
    r = SessionRisk()
    r.record_close(-abs(r.max_daily_loss))    # trip the daily-loss halt
    r.engage_kill("also killed")
    r.clear_kill()
    assert r.killed is False
    assert r.is_halted() is True              # the real risk limit stands
    assert not r.can_open()[0]


def test_trailing_stop_validation():
    # Both-or-neither.
    assert any("set together" in e for e in _good_plan(trail_pct=0.25).validate())
    assert any("set together" in e for e in _good_plan(trail_activate_pct=0.2).validate())
    # Range checks.
    assert any("trail_pct" in e for e in
               _good_plan(trail_pct=1.5, trail_activate_pct=0.2).validate())
    assert any("trail_activate_pct" in e for e in
               _good_plan(trail_pct=0.25, trail_activate_pct=-0.1).validate())
    # A well-formed trail is clean and signs.
    p = _good_plan(trail_pct=0.25, trail_activate_pct=0.20)
    assert p.validate() == []
    p.sign()
    # Default plans have no trail (backward compatible).
    assert _good_plan().trail_pct is None


def test_plan_limit_entry_needs_price():
    assert any("entry_limit" in e for e in
               _good_plan(entry_type="OPTION_LIMIT").validate())


def test_plan_round_trips_to_dict():
    d = _good_plan().to_dict()
    assert d["side"] == "PUT"
    assert d["trigger"]["comparator"] == "touch"


def test_side_maps_to_occ_and_direction():
    assert Side.PUT.occ_right == "P"
    assert Side.CALL.occ_right == "C"
    assert Side.PUT.open_direction == "BUY_TO_OPEN_PUT"


# --- sizing: the coupled constraint ----------------------------------------
def test_sizing_caps_at_max_when_stop_is_tight():
    # $8 entry, $7 stop → $100/contract risk. Budget 5000 affords 50 → cap at 10.
    r = derive_contracts(8.0, 7.0, 5000.0)
    assert r.feasible
    assert r.contracts == 10
    assert r.per_contract_risk == 100.0
    assert r.worst_case_loss == 1000.0


def test_sizing_clamps_between_min_and_max():
    # $8 entry, $6 stop → $200/contract. Budget 1500 affords 7 → between 5 and 10.
    r = derive_contracts(8.0, 6.0, 1500.0)
    assert r.feasible
    assert r.contracts == 7
    assert "clamped" in r.reason


def test_sizing_infeasible_when_even_min_exceeds_budget():
    # $10 entry, $5 stop → $500/contract. 5 contracts = $2500 > $2000 budget.
    r = derive_contracts(10.0, 5.0, 2000.0)
    assert not r.feasible
    assert r.contracts == 0
    assert "infeasible" in r.reason.lower()


def test_sizing_rejects_stop_above_entry():
    assert not derive_contracts(5.0, 6.0, 5000.0).feasible


def test_sizing_rejects_zero_budget():
    assert not derive_contracts(8.0, 6.0, 0.0).feasible


def test_premium_helpers():
    assert stop_premium_from_sl_pct(8.0, -0.25) == 6.0
    assert tp_premium_from_tp_pct(8.0, 0.30) == 10.4


# --- risk envelope ----------------------------------------------------------
def test_fresh_session_can_open_full_budget():
    s = SessionRisk()
    ok, _ = s.can_open()
    assert ok
    assert s.remaining_budget() == MAX_DAILY_LOSS


def test_one_at_a_time_blocks_second_open():
    s = SessionRisk()
    s.mark_opened()
    ok, why = s.can_open()
    assert not ok
    assert "one at a time" in why


def test_remaining_budget_shrinks_after_loss():
    s = SessionRisk()
    s.record_close(-1500.0)
    assert s.remaining_budget() == MAX_DAILY_LOSS - 1500.0
    ok, _ = s.can_open()
    assert ok  # still room, and slot freed


def test_daily_loss_limit_halts_and_latches():
    s = SessionRisk()
    s.record_close(-5000.0)
    assert s.is_halted()
    ok, why = s.can_open()
    assert not ok
    assert "loss limit" in why
    # Latches: a later green close cannot re-arm the halted session.
    s.record_close(+800.0)
    assert s.is_halted()
    assert not s.can_open()[0]


def test_green_day_keeps_trading_no_win_stop():
    s = SessionRisk()
    s.record_close(+2000.0)
    s.record_close(+3000.0)
    ok, _ = s.can_open()
    assert ok  # no win-stop — keep going


def test_kill_switch_blocks_open():
    s = SessionRisk()
    s.engage_kill("broker returned malformed chain")
    ok, why = s.can_open()
    assert not ok
    assert "kill" in why.lower()
