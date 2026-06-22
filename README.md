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

### Known follow-ups before live
- Port `_v41_wait_for_fill` for precise live fill-price (entry currently falls back
  to the option mark when the broker response lacks `fill_price`).
- Persist `SessionRisk` across process restarts (today it's in-memory per session).
- A conversational runner/CLI entrypoint (deliberate → sign → `run_plan`).
- A trading-hours shadow (DRY-RUN) session before the live lock comes off.
