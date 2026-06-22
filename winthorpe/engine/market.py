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
        from winthorpe.data.bars import SYMBOL_MAP
        sym = symbol.upper()
        stx = SYMBOL_MAP.get(sym, sym)
        if stx.startswith("/"):
            # Futures (ES/NQ): NOT an index — the "indices" market-data type
            # won't resolve them (the bug this fixes). Pull a real-time last
            # trade off DXLink using the same streamer symbol the bar source +
            # live stream use, with yfinance as the fail-soft fallback.
            return self._future_spot(sym, stx)
        kind = "equities" if sym == "SPY" else "indices"
        return market_data_mark(sym, kind)

    def _future_spot(self, sym: str, streamer_symbol: str) -> Optional[float]:
        from winthorpe.broker.session import _run_coro, get_session_and_account
        from winthorpe.data.spot_source import (
            fetch_spot_via_dxlink,
            fetch_spot_via_yfinance,
        )

        async def _pull() -> Optional[float]:
            from tastytrade.streamer import DXLinkStreamer
            session, _ = await get_session_and_account()
            async with DXLinkStreamer(session) as streamer:
                return await fetch_spot_via_dxlink(streamer, streamer_symbol)

        try:
            v = _run_coro(_pull)
            if v is not None:
                return v
        except Exception:
            logger.warning("future spot DXLink pull failed for %s", sym, exc_info=True)
        return fetch_spot_via_yfinance(sym)

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


class StreamMarketView:
    """MarketView backed by the persistent MarketStore.

    Reads in-memory streamed values (no per-tick network). Falls back to the
    REST/one-shot LiveMarketView for any symbol the stream doesn't carry (e.g.
    ES) or whenever a streamed value is stale — so the stream is an optimization,
    never a hard dependency.
    """

    def __init__(self, store, fallback: Optional["LiveMarketView"] = None):
        self.store = store
        self.fallback = fallback or LiveMarketView()

    def spot(self, symbol: str) -> Optional[float]:
        v = self.store.spot(symbol)
        if v is not None:
            return v
        return self.fallback.spot(symbol)

    def option_mark(self, streamer_symbol: str) -> Optional[float]:
        self.store.request_option(streamer_symbol)   # ensure it's subscribed
        v = self.store.option_mid(streamer_symbol)
        if v is not None:
            return v
        return self.fallback.option_mark(streamer_symbol)

    def now_et(self) -> datetime:
        return datetime.now(ET)
