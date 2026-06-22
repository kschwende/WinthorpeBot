"""DeskService — gating + a full background-thread plan run via the store."""

from unittest.mock import patch

from winthorpe.engine.service import DeskService
from winthorpe.plan.schema import Comparator, Condition, Side, TradePlan

_FAKE_RESOLVE = {"occ_symbol": "SPXW  260622P07525000",
                 "streamer_symbol": ".SPXW260622P7525",
                 "strike": 7525.0, "expiry": "2026-06-22", "right": "P"}


def _plan(**over):
    base = dict(
        thesis="fade call wall", side=Side.PUT,
        trigger=Condition("SPX", Comparator.TOUCH, 7530.0, from_side="below"),
        strike=7525.0, expiry="2026-06-22",
        tp_pct=0.30, sl_pct=-0.25, time_stop_et="15:45",
        invalidation=Condition("SPX", Comparator.GTE, 7536.0),
        plan_id="svc-plan-1",
    )
    base.update(over)
    return TradePlan(**base)


def _svc():
    return DeskService(start_stream=False)   # no real DXLink


def test_status_and_risk_shapes():
    s = _svc()
    st = s.status()
    assert st["plan_running"] is False
    assert st["risk"]["can_open"] is True
    assert s.market_state()["connected"] is False
    assert s.position_state() is None


def test_submit_rejected_when_halted():
    s = _svc()
    s.risk.record_close(-5000.0)
    r = s.submit_signed_plan(_plan())
    assert r["accepted"] is False
    assert "loss limit" in r["reason"]


def test_submit_rejected_when_invalid_plan():
    s = _svc()
    r = s.submit_signed_plan(_plan(time_stop_et=""))
    assert r["accepted"] is False
    assert any("time_stop" in e for e in r["errors"])


def test_full_run_through_service():
    s = _svc()
    # Seed the store so the trigger is already true and the option has a mark.
    s.store.set_spot("SPX", 7530.0)
    s.store.set_quote(".SPXW260622P7525", 7.9, 8.1)   # mid 8.0

    with patch("winthorpe.engine.engine.resolve_spxw_option", return_value=_FAKE_RESOLVE):
        r = s.submit_signed_plan(_plan())
        assert r["accepted"] is True
        s._thread.join(timeout=5)

    assert not s._thread.is_alive()
    assert s._plan.status.value == "closed"
    assert s.risk.closed_trades == 1
    assert s.position_state() is None          # flat again


def test_kill_switch_blocks_subsequent_submit():
    s = _svc()
    s.engage_kill("broker glitch")
    r = s.submit_signed_plan(_plan())
    assert r["accepted"] is False
    assert "kill" in r["reason"].lower()
