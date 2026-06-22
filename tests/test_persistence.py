"""SessionRisk persistence + DeskService restart reconciliation."""

from unittest.mock import patch

from winthorpe.engine.service import DeskService
from winthorpe.plan.schema import Comparator, Condition, Side, TradePlan
from winthorpe.risk.limits import SessionRisk, session_path

_FAKE_RESOLVE = {"occ_symbol": "SPXW  260622P07525000",
                 "streamer_symbol": ".SPXW260622P7525",
                 "strike": 7525.0, "expiry": "2026-06-22", "right": "P"}


def _plan(**over):
    base = dict(
        thesis="fade call wall", side=Side.PUT,
        trigger=Condition("SPX", Comparator.TOUCH, 7530.0, from_side="below"),
        strike=7525.0, expiry="2026-06-22", tp_pct=0.30, sl_pct=-0.25,
        time_stop_et="15:45", invalidation=Condition("SPX", Comparator.GTE, 7536.0),
        plan_id="persist-1",
    )
    base.update(over)
    return TradePlan(**base)


# --- SessionRisk persistence ------------------------------------------------
def test_loss_survives_restart_same_day():
    r = SessionRisk.load_or_new("2026-06-22")
    r.record_close(-1500.0)
    assert r.remaining_budget() == 3500.0
    # New process, same day → resumes the spent budget.
    r2 = SessionRisk.load_or_new("2026-06-22")
    assert r2.realized_pnl == -1500.0
    assert r2.remaining_budget() == 3500.0
    assert r2.closed_trades == 1


def test_halt_latch_survives_restart():
    r = SessionRisk.load_or_new("2026-06-22")
    r.record_close(-5000.0)
    r2 = SessionRisk.load_or_new("2026-06-22")
    assert r2.is_halted()
    assert not r2.can_open()[0]


def test_new_day_resets():
    r = SessionRisk.load_or_new("2026-06-22")
    r.record_close(-4000.0)
    fresh = SessionRisk.load_or_new("2026-06-23")   # different date
    assert fresh.realized_pnl == 0.0
    assert fresh.remaining_budget() == 5000.0


def test_kill_survives_restart():
    r = SessionRisk.load_or_new("2026-06-22")
    r.engage_kill("broker glitch")
    r2 = SessionRisk.load_or_new("2026-06-22")
    assert r2.killed
    assert not r2.can_open()[0]


# --- DeskService reconcile --------------------------------------------------
def _seed_open_position_state(date):
    """Run a play to OPEN, persist it, then leave the position 'live' by writing
    a live_position into the session file directly."""
    r = SessionRisk.load_or_new(date)
    r.mark_opened()
    r.set_live_position({
        "open_state": {"occ_symbol": "SPXW  260622P07525000",
                       "streamer_symbol": ".SPXW260622P7525",
                       "contracts": 10, "entry_premium": 8.0, "oco_id": "oco-1"},
        "plan": _plan().to_dict(),
    })
    return r


def test_dryrun_restart_resumes_management(monkeypatch):
    s0 = _seed_open_position_state("2026-06-22")
    assert s0.open_position is True
    # New DeskService (dry-run) sees the persisted open position and re-attaches.
    s = DeskService(start_stream=False)
    if s._thread:
        s._thread.join(timeout=5)
    # Dry-run resume_management: get_positions() == [] → oco_filled → closes flat.
    assert s.risk.open_position is False
    assert s.needs_reconcile is False


def test_live_restart_flags_reconcile_when_position_gone(monkeypatch):
    _seed_open_position_state("2026-06-22")
    # Force the live branch; the dry-run broker reports no positions → "gone".
    monkeypatch.setattr("winthorpe.engine.service.is_live", lambda: True)
    s = DeskService(start_stream=False)
    assert s.needs_reconcile is True
    r = s.submit_signed_plan(_plan(plan_id="new-after-reconcile"))
    assert r["accepted"] is False
    assert "reconcile" in r["reason"].lower()


def test_session_file_is_written():
    r = SessionRisk.load_or_new("2026-06-22")
    r.record_close(-100.0)
    assert session_path("2026-06-22").exists()
