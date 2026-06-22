"""Independent tastytrade session for WinthorpeBot.

Canonical auth used by BOTH the data plane (index spot, option chains) and the
broker plane (orders). One isolated session source — no credentials or session
state shared with the upstream live stack.

Mirrors the proven upstream pattern: a trading Session+Account, plus a separate
cached market-data Session for index-spot lookups that resets and retries once
on failure (the OAuth token can expire on a long-lived process).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from winthorpe.config import require_creds

logger = logging.getLogger(__name__)


def _run_coro(factory):
    """Run an async factory whether or not a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


async def get_session_and_account():
    """Build a trading Session + first Account. Raises if creds are missing."""
    from tastytrade import Account, Session

    secret, refresh = require_creds()
    session = Session(provider_secret=secret, refresh_token=refresh, is_test=False)
    accounts = await Account.get(session)
    return session, accounts[0]


# Cached market-data session (auth ~1s; reused). Reset to None on any failure so
# the next call rebuilds it — handles token expiry on long-lived services.
_MD_SESSION: Optional[Any] = None


def market_data_mark(symbol: str, market_kind: str) -> Optional[float]:
    """Real-time ``mark`` (mid), falling back to ``last``, for one symbol.

    ``market_kind`` is the ``get_market_data_by_type`` keyword — ``"indices"``
    for SPX/VIX, ``"equities"`` for SPY. Returns a float or ``None`` on failure
    so callers can fall back. Uses the brokerage feed already connected to — no
    per-call cost, no third-party data-license cutoff.
    """
    global _MD_SESSION
    import tastytrade.market_data as md
    from tastytrade import Session

    secret, refresh = require_creds()

    async def _fetch():
        return await md.get_market_data_by_type(_MD_SESSION, **{market_kind: [symbol]})

    data = None
    for attempt in (1, 2):
        try:
            if _MD_SESSION is None:
                _MD_SESSION = Session(
                    provider_secret=secret, refresh_token=refresh, is_test=False
                )
            data = _run_coro(_fetch)
            break
        except Exception:
            _MD_SESSION = None  # force a fresh session on retry / next call
            if attempt == 2:
                logger.warning(
                    "market_data_mark(%s, %s) failed", symbol, market_kind,
                    exc_info=True,
                )
                return None

    if not data:
        return None
    m = data[0]
    for attr in ("mark", "last"):
        v = getattr(m, attr, None)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None
