"""Market view — the read interface the engine depends on.

Defined as a small Protocol so the engine can be driven by a scripted fake in
tests and by live tastytrade data in production. Three reads:
  * spot(symbol)         — SPX/SPY/ES index/equity mark
  * option_mark(streamer)— current premium for a streamer option symbol
  * now_et()             — current ET time (clock injection for time-stops)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class MarketView(Protocol):
    def spot(self, symbol: str) -> Optional[float]: ...
    def option_mark(self, streamer_symbol: str) -> Optional[float]: ...
    def now_et(self) -> datetime: ...


class LiveMarketView:
    """Production MarketView backed by the independent tastytrade session."""

    def spot(self, symbol: str) -> Optional[float]:
        from winthorpe.broker.session import market_data_mark
        kind = "equities" if symbol.upper() == "SPY" else "indices"
        return market_data_mark(symbol.upper(), kind)

    def option_mark(self, streamer_symbol: str) -> Optional[float]:
        """Mid of bid/ask for one option via a short DXLink Quote pull."""
        from winthorpe.broker.session import _run_coro, get_session_and_account

        async def _pull() -> Optional[float]:
            import asyncio as _aio
            import time as _t
            from tastytrade.dxfeed import Quote
            from tastytrade.streamer import DXLinkStreamer
            session, _ = await get_session_and_account()
            async with DXLinkStreamer(session) as streamer:
                await streamer.subscribe(Quote, [streamer_symbol])
                deadline = _t.time() + 5.0
                while _t.time() < deadline:
                    try:
                        q = await _aio.wait_for(streamer.get_event(Quote), timeout=1.0)
                    except TimeoutError:
                        continue
                    if q and q.event_symbol == streamer_symbol:
                        bid = float(getattr(q, "bid_price", 0) or 0)
                        ask = float(getattr(q, "ask_price", 0) or 0)
                        if bid > 0 and ask > 0:
                            return round((bid + ask) / 2, 2)
            return None

        try:
            return _run_coro(_pull)
        except Exception:
            logger.warning("option_mark(%s) failed", streamer_symbol, exc_info=True)
            return None

    def now_et(self) -> datetime:
        return datetime.now(ET)
