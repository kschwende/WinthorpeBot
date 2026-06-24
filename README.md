# WinthorpeBot

Agent-in-the-loop autonomous SPX trade executor.

**The thesis is human. The discipline is the machine.** You and the agent deliberate
a trade plan together; once you sign it, you are deliberately locked out of the trigger
and the stop. The agent fires when the agreed condition hits (not when you get itchy)
and stops you out at the agreed level (when you'd be rationalizing "give it room").

## Overview

WinthorpeBot is an agent-in-the-loop execution system for 0DTE SPX (SPXW) options. The idea
is simple: the human keeps the judgment, and the bot takes the emotion out of the two places
it costs the most — entry timing and stopping out. You write the plan (the thesis, the level
you're trading, your stop, target, and the condition that voids the idea); the system pulls
live dealer-gamma positioning (the call/put "walls") and prior-day/overnight market structure
(PDH/PDL, ONH/ONL, session & weekly VWAP) to sanity-check it, snaps your level to the *real*
live wall, and sizes the position off the stop distance. Once you sign the plan it arms and
runs hands-off — entering on your trigger, managing a mechanical bracket (profit target / stop
/ time-stop), and bailing automatically the moment price invalidates the thesis. It trades one
position at a time behind a hard $5k daily-loss halt, runs entirely on a free real-time
tastytrade feed, and is model-agnostic (the reasoning is supplied by whatever agent/LLM you
point at it, not baked in). It's currently in dry-run/paper mode by default. Importantly, it's
an **execution-and-risk-discipline harness, not a signal service or black box** — there's no
secret alpha hidden inside. The edge is *your* read of the market; the bot's only job is to
execute it without flinching and refuse to let you blow up.

> **Disclaimer.** This is an execution-and-risk-discipline harness for your *own*
> discretionary trading — not financial advice, not a signal service, and not a
> source of alpha. 0DTE options can lose money fast. Run it in **DRY-RUN** until you
> understand exactly what it does, and trade live entirely at your own risk.

## Install & run

Requires **Python ≥ 3.11** and a free [tastytrade](https://tastytrade.com) account.
No paid data subscription — it runs on tastytrade's free real-time DXLink feed.

```bash
# 1. Clone
git clone https://github.com/kschwende/WinthorpeBot.git
cd WinthorpeBot

# 2. Virtualenv + install (the venv needs tastytrade + fastmcp)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"      # omit [dev] to skip the test tooling

# 3. Credentials (DRY-RUN runs without them; live trading needs them)
cp .env.example .env                   # then fill in TT_SECRET / TT_REFRESH
```

`.env` is auto-loaded. Get `TT_SECRET` / `TT_REFRESH` (OAuth) from the
[tastytrade developer portal](https://developer.tastytrade.com). Execution is
**DRY-RUN by default** (`WINTHORPE_LIVE=0`) — orders are logged, never sent. Live
trading requires real credentials **and** `WINTHORPE_LIVE=1` in `.env`.

### Run the desk (an MCP server)

WinthorpeBot is an [MCP](https://modelcontextprotocol.io) server: it holds the
deterministic core, the persistent live stream, and the risk floor; an MCP client
(Claude Code, a custom agent, …) supplies the reasoning.

```bash
.venv/bin/python -m winthorpe.mcp.server                      # stdio (local MCP client)
WINTHORPE_MCP_HTTP=1 .venv/bin/python -m winthorpe.mcp.server # http on :8190
```

To drive it from **Claude Code** (stdio), add it to your MCP servers (e.g. `~/.claude.json`):

```json
{
  "mcpServers": {
    "winthorpe-desk": {
      "type": "stdio",
      "command": "/absolute/path/to/WinthorpeBot/.venv/bin/python",
      "args": ["-m", "winthorpe.mcp.server"]
    }
  }
}
```

Then read structure (`get_volume_profile`, `get_gex`, `get_structural_levels`,
`get_bars`), deliberate and arm (`propose_plan` → `validate_plan` →
`sign_and_arm_plan`), watch (`get_status`, `get_position_state`), and exit
(`flatten_position` for a graceful close, `engage_kill_switch` for emergencies).
After a code change, restart the server — in Claude Code: `/mcp` → reconnect.

### Tests

```bash
.venv/bin/python -m pytest -q          # full suite
```

## Scope (v1)

- **Triggers / analysis:** SPX, SPY, ES.
- **Execution instrument:** SPXW options only, 5–10 contracts (size *derived* from stop distance).
- **Hard daily max loss:** $5,000 — agent flattens and goes dark for the session on touch.
- Standalone: all required market-data and execution code is **migrated into this repo**,
  not imported from any sibling project. No runtime dependency on external projects.

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

All four planes built, standalone, 110 tests green. Execution defaults to **DRY-RUN**
(`WINTHORPE_LIVE=0`); the live lock stays off until the management loop has been
watched fire on paper for a session.

- **data** — GEX engine, live-verified bit-identical to the source implementation. ✅
- **broker** — SPXW option path migrated verbatim (GLD/COST/KO fixes), 12 guards. ✅
- **plan / risk** — signable plan + coupled sizing + $5k latching halt + kill switch. ✅
- **engine / journal / agent** — arm→fire→manage loop, journal, level-correction. ✅
- **stream / service / mcp** — persistent DXLink stream, DeskService, FastMCP server. ✅

### Harness (MCP)

Single process in `.venv` (has tastytrade + fastmcp). Any MCP harness — Claude Code,
OpenClaw, a custom agent — drives the desk through 16 tools (reads: `get_gex`,
`get_status`, `get_session_risk`, `get_market_state`, `get_position_state`,
`get_structural_levels`, `get_volume_profile`, `get_bars`, `get_option_quote`,
`get_journal`; reason: `propose_plan`, `validate_plan`; act: `sign_and_arm_plan`,
`flatten_position`, `reset_kill_switch`, `engage_kill_switch`). The harness supplies
the LLM; this process holds the deterministic core + the persistent stream + the risk
floor. No `winthorpe/` module imports an LLM SDK — fully model-agnostic.

```bash
.venv/bin/python -m winthorpe.mcp.server                      # stdio (local harness)
WINTHORPE_MCP_HTTP=1 .venv/bin/python -m winthorpe.mcp.server # http :8190
```

Agent role is position-state-dependent by construction: while a plan runs there is no
"adjust live trade" tool (read telemetry + kill only); between plans the agent proposes
and arms, but signing always passes validation + the risk gate it can't bypass.
