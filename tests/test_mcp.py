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
