"""SPXW option symbol resolution.

Turns a (right, strike, expiry) request into the tastytrade OCC symbol the order
builder needs, plus the DXLink streamer symbol for live option pricing. Resolved
against the live option chain (``get_option_chain``) rather than string-built, so
a non-existent strike/expiry fails loudly instead of routing a bad symbol — the
same chain call the GEX engine already relies on.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from winthorpe.broker.session import _run_coro

logger = logging.getLogger(__name__)


def _nearest_expiry(chain: dict, target: date) -> date:
    """Pick the chain expiry == target, else the soonest expiry on/after target."""
    if target in chain:
        return target
    future = sorted(d for d in chain if d >= target)
    if not future:
        raise ValueError(f"no SPXW expiry on/after {target} in chain")
    return future[0]


def resolve_spxw_option(right: str, strike: float, expiry: date | str,
                        underlying: str = "SPX") -> dict:
    """Resolve one SPXW option to {occ_symbol, streamer_symbol, strike, expiry, right}.

    right:   "C"/"P" (or "CALL"/"PUT").
    strike:  exact strike on the SPXW $5 grid.
    expiry:  date or YYYY-MM-DD; snapped to the nearest listed expiry >= it.
    Raises ValueError if the strike isn't listed for the resolved expiry.
    """
    r = (right or "").upper()[:1]
    if r not in ("C", "P"):
        raise ValueError(f"right must be C/P, got {right!r}")
    if isinstance(expiry, str):
        expiry = date.fromisoformat(expiry)

    async def _resolve() -> dict:
        from tastytrade import Session
        from tastytrade.instruments import get_option_chain
        from winthorpe.config import require_creds

        secret, refresh = require_creds()
        session = Session(provider_secret=secret, refresh_token=refresh, is_test=False)
        chain = await get_option_chain(session, underlying)
        exp = _nearest_expiry(chain, expiry)
        for opt in chain[exp]:
            opt_right = str(getattr(opt, "option_type", "")).rsplit(".", 1)[-1][:1].upper()
            if opt_right != r:
                continue
            if abs(float(getattr(opt, "strike_price", 0)) - float(strike)) < 1e-6:
                return {
                    "occ_symbol": getattr(opt, "symbol", None),
                    "streamer_symbol": getattr(opt, "streamer_symbol", None),
                    "strike": float(strike),
                    "expiry": exp.isoformat(),
                    "right": r,
                }
        listed = sorted({float(getattr(o, "strike_price", 0)) for o in chain[exp]
                         if str(getattr(o, "option_type", "")).rsplit(".", 1)[-1][:1].upper() == r})
        raise ValueError(
            f"strike {strike} {r} not listed for {underlying} {exp}; "
            f"nearest listed: {min(listed, key=lambda s: abs(s - strike)) if listed else 'none'}"
        )

    return _run_coro(_resolve)
