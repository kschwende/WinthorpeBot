# WinthorpeBot

Agent-in-the-loop autonomous SPX trade executor.

**The thesis is human. The discipline is the machine.** You and the agent deliberate
a trade plan together; once you sign it, you are deliberately locked out of the trigger
and the stop. The agent fires when the agreed condition hits (not when you get itchy)
and stops you out at the agreed level (when you'd be rationalizing "give it room").

## Scope (v1)

- **Triggers / analysis:** SPX, SPY, ES.
- **Execution instrument:** SPXW options only, 5–10 contracts (size *derived* from stop distance).
- **Hard daily max loss:** $5,000 — agent flattens and goes dark for the session on touch.
- Standalone: all required market-data and execution code is **migrated into this repo**,
  not imported from upstream. No runtime dependency on the upstream projects.

## The control surface

- **Human-in-the-loop at plan time** — deliberate thesis, trigger, levels, stop, target,
  invalidation. Signing the plan *is* the authorization.
- **Agent-only at execution time** — you have no vote on entry or stop once armed.
- **Kill switch** — mechanical/plumbing emergencies only (broker glitch, unfillable leg,
  cascade). High-friction, logged. Never a "this hurts, get me out" button — every
  market-based exit is already a pre-agreed invalidation rule the agent monitors.

## Layout

| Package            | Responsibility |
|--------------------|----------------|
| `winthorpe/data`   | Market-data plane — SPX/SPY/ES bars, spot, gamma/GEX levels (migrated) |
| `winthorpe/broker` | tastytrade execution path — order build, natural-debit close, guards (migrated) |
| `winthorpe/plan`   | Trade-plan schema + deliberation loop (new) |
| `winthorpe/risk`   | Daily loss limit, derived sizing, kill switch (new) |
| `winthorpe/engine` | Autonomous arm → fire → manage loop (new) |
| `winthorpe/journal`| Thesis → plan → outcome log (new; the only way to evaluate discretionary trades) |
| `winthorpe/agent`  | Orchestration / agent loop (new) |

## Status

All four planes built, standalone, 42 tests green. Execution defaults to **DRY-RUN**
(`WINTHORPE_LIVE=0`); the live lock stays off until the management loop has been
watched fire on paper for a session.

- **data** — GEX engine, live-verified bit-identical to upstream. ✅
- **broker** — SPXW option path migrated verbatim (GLD/COST/KO fixes), 12 guards. ✅
- **plan / risk** — signable plan + coupled sizing + $5k latching halt + kill switch. ✅
- **engine / journal / agent** — arm→fire→manage loop, journal, level-correction. ✅
- **stream / service / mcp** — persistent DXLink stream, DeskService, FastMCP server. ✅

### Harness (MCP)

Single process in `.venv` (has tastytrade + fastmcp). Any MCP harness — Claude Code,
OpenClaw, a custom agent — drives the desk through 8 tools (reads: `get_gex`,
`get_status`, `get_session_risk`, `get_market_state`, `get_position_state`; reason:
`propose_plan`; act: `sign_and_arm_plan`, `engage_kill_switch`). The harness supplies
the LLM; this process holds the deterministic core + the persistent stream + the risk
floor. No `winthorpe/` module imports an LLM SDK — fully model-agnostic.

```bash
.venv/bin/python -m winthorpe.mcp.server                      # stdio (local harness)
WINTHORPE_MCP_HTTP=1 .venv/bin/python -m winthorpe.mcp.server # http :8190
```

Agent role is position-state-dependent by construction: while a plan runs there is no
"adjust live trade" tool (read telemetry + kill only); between plans the agent proposes
and arms, but signing always passes validation + the risk gate it can't bypass.

### Known follow-ups before live
- A trading-hours shadow (DRY-RUN) session before the live lock comes off.
- ES is not streamed (REST-fallback only); add a `/ES` front-month subscription if a
  plan ever needs to trigger on ES.

Done: live fill-price resolution (`wait_for_fill`), and `SessionRisk` + open-position
persistence with broker-truth reconciliation on restart.
