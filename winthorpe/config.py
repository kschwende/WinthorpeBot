"""WinthorpeBot configuration — env loading and the frozen risk envelope.

Single source of truth for the hard limits the user set. These are *structural*
constants, not per-plan knobs: the agent can size and shape a trade within
this envelope but can never agree its way past it. They live in code (not the
agent's reasoning) precisely so emotion can't talk past them mid-session.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root = parent of the winthorpe/ package.
ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Idempotent; safe to call from scripts, tests, and the engine alike.
load_dotenv(ENV_PATH)


# ---------------------------------------------------------------------------
# Frozen risk envelope (v1) — see README "Scope".
# ---------------------------------------------------------------------------
MAX_DAILY_LOSS = 5_000.0          # USD. On touch: flatten + dark for the session.
MIN_CONTRACTS = 5                 # SPXW contracts, lower bound.
MAX_CONTRACTS = 10                # SPXW contracts, upper bound.
EXEC_INSTRUMENT = "SPXW"          # only instrument WinthorpeBot trades.
TRIGGER_SYMBOLS = ("SPX", "SPY", "ES")   # analysis / trigger inputs only.

OPTION_MULTIPLIER = 100           # SPX/SPXW contract multiplier.


# ---------------------------------------------------------------------------
# Live gate. DRY-RUN unless BOTH conditions hold. Belt and suspenders so a
# stray env var alone can't arm execution.
# ---------------------------------------------------------------------------
def is_live() -> bool:
    """True only when WINTHORPE_LIVE=1 in the loaded env. Default: DRY-RUN."""
    return os.environ.get("WINTHORPE_LIVE", "0").strip() == "1"


def require_creds() -> tuple[str, str]:
    """Return (TT_SECRET, TT_REFRESH) or raise with a clear message."""
    secret = os.environ.get("TT_SECRET")
    refresh = os.environ.get("TT_REFRESH")
    if not secret or not refresh:
        raise RuntimeError(
            "TT_SECRET / TT_REFRESH not set. Copy .env.example to .env and fill "
            "in WinthorpeBot's own tastytrade OAuth credentials."
        )
    return secret, refresh


# Runtime state / journal locations (created lazily by their owners).
STATE_DIR = ROOT / "state"
JOURNAL_DIR = ROOT / "journal"
