"""MCP server integration — in-memory FastMCP client, no real stream/network.

Run under .venv (has fastmcp + tastytrade via system-site):
    .venv/bin/python -m pytest tests/test_mcp.py
"""

import pytest

fastmcp = pytest.importorskip("fastmcp")
from fastmcp import Client

import winthorpe.mcp.server as srv
from winthorpe.engine.service import DeskService


@pytest.fixture
def desk(monkeypatch):
    # Inject a no-stream, dry-run service so tools don't open DXLink.
    s = DeskService(start_stream=False)
    monkeypatch.setattr(srv, "_service", s)
    return s


async def _call(tool, args=None):
    async with Client(srv.mcp) as c:
        res = await c.call_tool(tool, args or {})
        return res.data


@pytest.mark.asyncio
async def test_tools_are_registered():
    async with Client(srv.mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"get_gex", "propose_plan", "get_status", "get_session_risk",
            "get_market_state", "get_position_state", "sign_and_arm_plan",
            "engage_kill_switch"} <= names


@pytest.mark.asyncio
async def test_status_and_risk_reads(desk):
    st = await _call("get_status")
    assert st["plan_running"] is False
    risk = await _call("get_session_risk")
    assert risk["can_open"] is True
    assert await _call("get_position_state") is None


@pytest.mark.asyncio
async def test_sign_and_arm_rejects_invalid_plan(desk):
    bad = {"thesis": "x", "side": "PUT",
           "trigger": {"symbol": "SPX", "comparator": "touch", "level": 7500.0},
           "strike": 7495.0, "expiry": "2026-06-22",
           "tp_pct": 0.30, "sl_pct": -0.25, "time_stop_et": ""}  # missing time-stop
    r = await _call("sign_and_arm_plan", {"plan": bad})
    assert r["accepted"] is False


@pytest.mark.asyncio
async def test_kill_switch_then_blocked(desk):
    r = await _call("engage_kill_switch", {"reason": "broker glitch"})
    assert r["killed"] is True
    risk = await _call("get_session_risk")
    assert risk["killed"] is True
    assert risk["can_open"] is False


# --- read / inspect kit -----------------------------------------------------
@pytest.mark.asyncio
async def test_read_inspect_kit_registered():
    async with Client(srv.mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"get_journal", "get_bars", "get_option_quote", "validate_plan"} <= names


@pytest.mark.asyncio
async def test_get_journal_reads_written_events(desk):
    desk.journal.event("plan-x", "armed")
    desk.journal.closed("plan-x", pnl=123.0, reason="oco_filled")
    rows = await _call("get_journal")
    kinds = [r["kind"] for r in rows]
    assert "event" in kinds and "closed" in kinds


@pytest.mark.asyncio
async def test_get_bars_rejects_unknown_symbol(desk):
    rows = await _call("get_bars", {"symbol": "TSLA"})
    assert rows[0]["error"].startswith("unsupported symbol")


@pytest.mark.asyncio
async def test_validate_plan_previews_sizing(desk, monkeypatch):
    # Resolve without network; seed the store so option_mark reads locally.
    fake = {"occ_symbol": "SPXW  260622P07495000",
            "streamer_symbol": ".SPXW260622P7495",
            "strike": 7495.0, "expiry": "2026-06-22", "right": "P"}
    monkeypatch.setattr("winthorpe.broker.options.resolve_spxw_option",
                        lambda *a, **k: fake)
    desk.store.set_quote(".SPXW260622P7495", 7.9, 8.1)   # mid 8.0
    plan = {"thesis": "fade", "side": "PUT",
            "trigger": {"symbol": "SPX", "comparator": "touch", "level": 7500.0,
                        "from_side": "below"},
            "strike": 7495.0, "expiry": "2026-06-22",
            "tp_pct": 0.30, "sl_pct": -0.25, "time_stop_et": "15:45"}
    r = await _call("validate_plan", {"plan": plan})
    assert r["valid"] is True
    sp = r["sizing_preview"]
    assert sp["entry_mark"] == 8.0
    assert sp["feasible"] is True
    assert sp["contracts"] == 10            # $8→$6 stop = $200/ctr, $5k budget


@pytest.mark.asyncio
async def test_validate_plan_reports_errors(desk):
    plan = {"thesis": "x", "side": "PUT",
            "trigger": {"symbol": "SPX", "comparator": "touch", "level": 7500.0},
            "strike": 7495.0, "expiry": "2026-06-22",
            "tp_pct": 0.30, "sl_pct": -0.25, "time_stop_et": ""}   # missing time-stop
    r = await _call("validate_plan", {"plan": plan})
    assert r["valid"] is False
    assert any("time_stop" in e for e in r["errors"])
