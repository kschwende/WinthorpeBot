"""WinthorpeBot MCP server — the desk's hands, exposed as tools.

Any MCP-capable harness (Claude Code, OpenClaw, Claude Desktop, a custom agent)
drives the desk through these tools. The harness supplies the intelligence; this
process holds the deterministic core, the persistent stream, and the risk floor.

The agent's role is position-state-dependent BY CONSTRUCTION:
  * While a plan runs, the engine owns it — there is no "adjust live trade" tool.
    The agent can only read telemetry and (in emergencies) engage the kill switch.
  * Between plans (flat), the agent proposes and arms new plans — but signing
    still goes through validation + the risk gate, which the agent can't bypass.

Run (single process, .venv has tastytrade + fastmcp):
    .venv/bin/python -m winthorpe.mcp.server            # stdio (local harness)
    WINTHORPE_MCP_HTTP=1 .venv/bin/python -m winthorpe.mcp.server   # http :8190
"""

from __future__ import annotations

import logging
import os

from fastmcp import FastMCP

from winthorpe.agent.deliberate import propose_plan as _propose
from winthorpe.data.gex_engine import compute_gex
from winthorpe.engine.service import DeskService
from winthorpe.plan.schema import Side, TradePlan

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("winthorpe.mcp")

mcp = FastMCP("winthorpe-desk")

# One session-long service. Starts the persistent stream on import.
_service: DeskService | None = None


def service() -> DeskService:
    global _service
    if _service is None:
        _service = DeskService(start_stream=True)
    return _service


# --- reads: the agent's eyes ------------------------------------------------
@mcp.tool
async def get_gex(product: str = "SPX") -> dict:
    """Live GEX: spot, call wall, put wall, dealer regime. The authoritative
    walls for correcting a thesis level."""
    r = await compute_gex(product=product)
    return {k: r[k] for k in ("spot", "call_wall", "put_wall", "net_gex",
                              "gex_regime", "zero_gamma", "flip_point") if k in r}


@mcp.tool
def get_status() -> dict:
    """Desk status: live/dry-run, stream connection, whether a plan is running,
    and the session risk snapshot."""
    return service().status()


@mcp.tool
def get_session_risk() -> dict:
    """Realized P&L, remaining daily budget, halt/kill state, and whether a new
    plan can be opened right now."""
    return service().session_risk()


@mcp.tool
def get_market_state() -> dict:
    """Streamed spot/option snapshot with ages — the heartbeat read."""
    return service().market_state()


@mcp.tool
def get_position_state() -> dict | None:
    """Live telemetry for the open position (unrealized P&L, distance to
    invalidation, time-stop), or null when flat. Read-only."""
    return service().position_state()


@mcp.tool
def get_structural_levels() -> dict:
    """Prior-day/overnight price structure: PDH/PDL/PDC, ONH/ONL (ES basis-adj),
    opening range, today's RTH extremes. The anchors to check a thesis level
    against for confluence."""
    from winthorpe.levels.structural import fetch_structural_levels
    return fetch_structural_levels().to_dict()


# --- deliberation: turn a thesis into a corrected draft ---------------------
@mcp.tool
async def propose_plan(thesis: str, side: str, proposed_level: float,
                       expiry: str, tp_pct: float = 0.30, sl_pct: float = -0.25,
                       time_stop_et: str = "15:45", product: str = "SPX") -> dict:
    """Correct a thesis against live GEX and return a DRAFT plan + the
    corrections (e.g. 'you said 7530, the call wall is 7500'). Does NOT arm —
    review/edit the draft, then call sign_and_arm_plan."""
    gex = await compute_gex(product=product)
    # Structural levels for confluence (best-effort — never block the proposal).
    levels = None
    try:
        from winthorpe.levels.structural import fetch_structural_levels
        levels = fetch_structural_levels()
    except Exception:
        logger.warning("structural levels unavailable for confluence", exc_info=True)
    proposal = _propose(
        thesis=thesis, side=Side(side.upper()), proposed_level=proposed_level,
        gex=gex, expiry=expiry, levels=levels, tp_pct=tp_pct, sl_pct=sl_pct,
        time_stop_et=time_stop_et,
    )
    return {"draft_plan": proposal.plan.to_dict(), "corrections": proposal.corrections,
            "confluence": proposal.confluence,
            "gex": {k: gex[k] for k in ("spot", "call_wall", "put_wall") if k in gex},
            "structural_levels": levels.as_named() if levels else {}}


# --- actions: arm / kill ----------------------------------------------------
@mcp.tool
def sign_and_arm_plan(plan: dict) -> dict:
    """Sign a (reviewed, possibly edited) draft plan and arm it. Runs validation
    + the risk gate first — rejects an invalid plan, a live conflict, or a halted
    session. Once armed, the engine owns the trade; the human is locked out until
    flat."""
    try:
        tp = TradePlan(**plan)
    except Exception as exc:
        return {"accepted": False, "reason": f"could not build plan: {exc}"}
    return service().submit_signed_plan(tp)


@mcp.tool
def engage_kill_switch(reason: str) -> dict:
    """MECHANICAL emergency stop only (broker glitch, unfillable leg, cascade).
    Halts new entries; the engine flattens any open position. Not a 'this trade
    hurts' button — market-based bail is the plan's invalidation rule."""
    return service().engage_kill(reason)


def main() -> None:
    if os.environ.get("WINTHORPE_MCP_HTTP", "0") == "1":
        port = int(os.environ.get("WINTHORPE_MCP_PORT", "8190"))
        mcp.run(transport="http", host="127.0.0.1", port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
