"""End-to-end engine lifecycle on a scripted market + fake broker.

Proves the risk-critical behavior without touching tastytrade: coupled sizing at
entry, infeasible rejection, one-at-a-time, the dual exits (invalidation +
time-stop), and P&L bookkeeping into SessionRisk.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from winthorpe.engine.engine import Engine, condition_met
from winthorpe.journal.journal import Journal
from winthorpe.plan.schema import Comparator, Condition, Side, TradePlan
from winthorpe.risk.limits import SessionRisk

ET = ZoneInfo("America/New_York")


class FakeMarket:
    def __init__(self, spot=7530.0, mark=8.0, now="10:00"):
        self._spot = spot
        self._mark = mark
        self._now = now

    def spot(self, symbol): return self._spot
    def option_mark(self, streamer): return self._mark
    def now_et(self):
        h, m = self._now.split(":")
        return datetime(2026, 6, 22, int(h), int(m), tzinfo=ET)


class FakeBroker:
    """Records orders; reports the position as held until told otherwise."""
    def __init__(self):
        self.orders = []
        self.complex = []
        self.protective = []
        self.cancels = []
        self.swept = 0
        self.held = True
        self.occ = None

    def place_order(self, decision):
        self.orders.append(decision)
        meta = decision.metadata or {}
        if decision.direction.startswith("BUY"):
            self.occ = meta.get("occ_symbol")
        if decision.direction == "SELL_TO_CLOSE":
            self.held = False
        return {"status": "filled", "order_id": "x", "fill_price": None,
                "entry_price": None}

    def place_complex_order(self, co):
        self.complex.append(co)
        return {"complex_order_id": "oco-1", "status": "received"}

    def place_protective_stop(self, occ, contracts, sl_price):
        self.protective.append((occ, contracts, sl_price))
        return {"order_id": "stop-1", "status": "received"}

    def cancel_complex_order(self, oid): self.cancels.append(oid); return True
    def cancel_working_orders_for(self, u): self.swept += 1; return []
    def get_positions(self):
        if self.held and self.occ:
            return [{"symbol": self.occ, "quantity": 5}]
        return []


def _plan(**over):
    base = dict(
        thesis="fade call wall", side=Side.PUT,
        trigger=Condition("SPX", Comparator.TOUCH, 7530.0, from_side="below"),
        strike=7525.0, expiry="2026-06-22",
        tp_pct=0.30, sl_pct=-0.25, time_stop_et="15:45",
        invalidation=Condition("SPX", Comparator.GTE, 7536.0),
        plan_id="plan-1",
    )
    base.update(over)
    return TradePlan(**base).sign()


# Resolve the OCC without hitting tastytrade.
_FAKE_RESOLVE = {"occ_symbol": "SPXW  260622P07525000",
                 "streamer_symbol": ".SPXW260622P7525",
                 "strike": 7525.0, "expiry": "2026-06-22", "right": "P"}


def _engine(market, broker):
    return Engine(market, broker, SessionRisk(), Journal(session_date="test", journal_dir="/tmp/winthorpe_test_journal"))


def test_trigger_evaluation():
    m = FakeMarket(spot=7530.0)
    assert condition_met(Condition("SPX", Comparator.TOUCH, 7530.0, from_side="below"), m)
    m._spot = 7529.0
    assert not condition_met(Condition("SPX", Comparator.TOUCH, 7530.0, from_side="below"), m)


def test_full_lifecycle_invalidation_flatten():
    m = FakeMarket(spot=7530.0, mark=8.0)
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan()

    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        st = eng.enter(plan)
    assert st is not None
    # $8 entry, $6 stop ($2*100=$200/ctr), budget $5000 → cap at 10.
    assert st.contracts == 10
    assert eng.risk.open_position is True
    assert len(b.complex) == 1            # OCO bracket placed

    # No exit yet at 10:00, spot below invalidation.
    assert eng.manage_step(plan, st) is None

    # SPX reclaims 7536 → thesis void.
    m._spot = 7537.0
    assert eng.manage_step(plan, st) == "invalidation"

    # Close at a $5 mark → loss of (5-8)*100*10 = -$3000.
    m._mark = 5.0
    pnl = eng.close(plan, st, "invalidation")
    assert pnl == -3000.0
    assert eng.risk.realized_pnl == -3000.0
    assert eng.risk.open_position is False
    assert "oco-1" in b.cancels           # bracket cancelled before close


def test_time_stop_flattens():
    m = FakeMarket(spot=7530.0, mark=8.0, now="15:45")
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan()
    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        st = eng.enter(plan)
    assert eng.manage_step(plan, st) == "time_stop"


def test_oco_filled_detected_when_position_gone():
    m = FakeMarket(spot=7530.0, mark=8.0)
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan()
    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        st = eng.enter(plan)
    b.held = False    # OCO took it out of band
    assert eng.manage_step(plan, st) == "oco_filled"


def test_trailing_stop_rides_then_exits_on_pullback():
    """Trail arms after +20%, ratchets the high-water up, and exits on a 25%
    pullback — letting the winner run far past the fixed tp_pct ceiling."""
    m = FakeMarket(spot=7530.0, mark=11.0)        # enter at $11
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan(tp_pct=2.0, trail_activate_pct=0.20, trail_pct=0.25)
    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        st = eng.enter(plan)
    assert st.high_water == 11.0 and st.trail_armed is False
    # Trailing plan drops the OCO TP leg → only a protective stop ($11×0.75=$8.25).
    assert len(b.complex) == 0
    assert b.protective == [(_FAKE_RESOLVE["occ_symbol"], st.contracts, 8.25)]

    # Still below activation ($13.20) → no trail, not armed.
    m._mark = 13.0
    assert eng.manage_step(plan, st) is None
    assert st.trail_armed is False

    # Crosses activation and runs to $18.80 → armed, high-water ratchets up.
    m._mark = 18.8
    assert eng.manage_step(plan, st) is None
    assert st.trail_armed is True
    assert st.high_water == 18.8

    # Mild dip holds (trail stop = 18.8 * 0.75 = $14.10) → still in.
    m._mark = 15.0
    assert eng.manage_step(plan, st) is None
    assert st.high_water == 18.8                   # high-water does not fall

    # Pullback through the trail stop → exit.
    m._mark = 14.0
    assert eng.manage_step(plan, st) == "trail_stop"

    pnl = eng.close(plan, st, "trail_stop")
    assert pnl == round((14.0 - 11.0) * 100 * st.contracts, 2)   # captured the run
    assert b.cancels == []          # no OCO to cancel (TP leg was dropped)
    assert b.swept == 1             # protective stop swept via working-orders cancel
    assert b.held is False          # market-closed


def test_no_trail_by_default_is_backward_compatible():
    m = FakeMarket(spot=7530.0, mark=8.0)
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan()                                # no trail fields
    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        st = eng.enter(plan)
    # A big run then a deep pullback must NOT trigger any trail exit.
    m._mark = 40.0
    assert eng.manage_step(plan, st) is None
    m._mark = 1.0
    assert eng.manage_step(plan, st) is None       # only OCO/invalidation/time govern


def test_infeasible_plan_rejected_at_entry():
    # $12 mark, sl_pct -0.25 → stop $9, risk $300/ctr. Budget 1000 → can't afford 5.
    m = FakeMarket(spot=7530.0, mark=12.0)
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan(max_play_loss=1000.0)
    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        st = eng.enter(plan)
    assert st is None
    assert eng.risk.open_position is False
    assert len(b.orders) == 0             # never sent an order


def test_run_plan_arms_enters_and_exits_on_invalidation():
    """Full live-loop glue: trigger fires immediately, position opens, then the
    invalidation flips and the loop closes and returns."""
    m = FakeMarket(spot=7530.0, mark=8.0)
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan()

    ticks = {"n": 0}

    def fake_sleep(_):
        # After entry, flip spot above invalidation so the manage loop exits.
        ticks["n"] += 1
        if ticks["n"] >= 1:
            m._spot = 7537.0

    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        eng.run_plan(plan, poll_interval=0, sleep_fn=fake_sleep)

    assert plan.status.value == "closed"
    assert eng.risk.open_position is False
    assert eng.risk.closed_trades == 1


def test_run_plan_expires_if_trigger_never_hits():
    m = FakeMarket(spot=7000.0, mark=8.0, now="15:50")   # far from level, past window
    b = FakeBroker()
    eng = _engine(m, b)
    plan = _plan(valid_until_et="15:45")
    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        eng.run_plan(plan, poll_interval=0, sleep_fn=lambda _: None)
    assert plan.status.value == "expired"
    assert len(b.orders) == 0


def test_halted_session_blocks_entry():
    m = FakeMarket(spot=7530.0, mark=8.0)
    b = FakeBroker()
    eng = _engine(m, b)
    eng.risk.record_close(-5000.0)        # daily limit hit
    plan = _plan()
    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        st = eng.enter(plan)
    assert st is None
    assert len(b.orders) == 0
