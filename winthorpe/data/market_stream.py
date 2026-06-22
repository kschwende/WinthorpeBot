"""Persistent market stream — one DXLink connection, shared in-memory state.

Replaces per-poll REST snapshots and per-call DXLink open/close with a single
long-lived connection (the pattern gex_stream already proves). A background
thread owns the connection and writes the latest spot / option quotes into a
thread-safe MarketStore that the engine and the MCP layer read synchronously.

Scope: streams the hot symbols (SPX, SPY) plus option Quotes subscribed on
demand when a plan arms. Anything not streamed (e.g. ES) is REST-fallback'd by
StreamMarketView — so the stream is an optimization, never a hard dependency.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SPOT_SYMBOLS = ("SPX", "SPY")
STALE_AFTER_SEC = 10.0   # a value older than this is treated as absent
WARMUP_GRACE_SEC = 8.0   # after start, "not yet connected" reads as warming, not dead


class MarketStore:
    """Thread-safe latest-value store. Writers are the stream thread; readers
    are the engine / MCP. Values carry a monotonic timestamp for staleness."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spot: dict[str, tuple[float, float]] = {}        # sym -> (px, mono)
        self._opt: dict[str, tuple[float, float, float]] = {}  # streamer -> (bid, ask, mono)
        self._want_options: set[str] = set()
        self._subscribed_options: set[str] = set()
        self.connected: bool = False
        self._started_mono: Optional[float] = None  # set when the stream thread starts

    def mark_starting(self) -> None:
        """Record the moment the stream thread begins its connect attempt. Lets
        readers tell a cold warm-up apart from a crashed/never-started stream."""
        with self._lock:
            self._started_mono = time.monotonic()

    # -- writers (stream thread) -------------------------------------------
    def set_spot(self, symbol: str, price: float) -> None:
        with self._lock:
            self._spot[symbol.upper()] = (float(price), time.monotonic())

    def set_quote(self, streamer_symbol: str, bid: float, ask: float) -> None:
        with self._lock:
            self._opt[streamer_symbol] = (float(bid), float(ask), time.monotonic())

    def take_pending_option_subs(self) -> list[str]:
        """Return option streamer symbols requested but not yet subscribed."""
        with self._lock:
            pending = list(self._want_options - self._subscribed_options)
            self._subscribed_options |= set(pending)
            return pending

    # -- readers (engine / MCP) --------------------------------------------
    def spot(self, symbol: str, max_age: float = STALE_AFTER_SEC) -> Optional[float]:
        with self._lock:
            v = self._spot.get(symbol.upper())
        if not v:
            return None
        px, mono = v
        if time.monotonic() - mono > max_age:
            return None
        return px

    def option_mid(self, streamer_symbol: str, max_age: float = STALE_AFTER_SEC) -> Optional[float]:
        with self._lock:
            v = self._opt.get(streamer_symbol)
        if not v:
            return None
        bid, ask, mono = v
        if time.monotonic() - mono > max_age:
            return None
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        return None

    def request_option(self, streamer_symbol: str) -> None:
        """Ask the stream to subscribe this option's Quote (idempotent)."""
        with self._lock:
            self._want_options.add(streamer_symbol)

    @staticmethod
    def _derive_state(connected: bool, started: Optional[float],
                      freshest_age: Optional[float], elapsed: Optional[float]) -> str:
        """Map raw flags to a single honest state. A cold warm-up and a dead
        stream both read connected=False with no spots — this separates them:
          down         — stream never started (e.g. start_stream=False)
          warming      — started, handshake not yet complete, within grace
          disconnected — started but not connected past the grace window (crash)
          stale        — connected but no fresh tick (off-hours, or feed gone quiet)
          live         — connected and ticking within STALE_AFTER_SEC
        """
        if started is None:
            return "down"
        if not connected:
            return "warming" if (elapsed is not None and elapsed <= WARMUP_GRACE_SEC) \
                else "disconnected"
        if freshest_age is None or freshest_age > STALE_AFTER_SEC:
            return "stale"
        return "live"

    def stream_state(self) -> str:
        """The single-word stream state (see ``_derive_state``)."""
        now = time.monotonic()
        with self._lock:
            connected, started = self.connected, self._started_mono
            ages = [now - t for _, t in self._spot.values()]
        freshest = min(ages) if ages else None
        elapsed = (now - started) if started is not None else None
        return self._derive_state(connected, started, freshest, elapsed)

    def snapshot(self) -> dict:
        """Observability snapshot (ages in seconds), safe to serialize."""
        now = time.monotonic()
        with self._lock:
            connected, started = self.connected, self._started_mono
            spots = {s: {"price": p, "age_s": round(now - t, 1)}
                     for s, (p, t) in self._spot.items()}
            options = {s: {"mid": round((b + a) / 2, 2) if b > 0 and a > 0 else None,
                           "age_s": round(now - t, 1)}
                       for s, (b, a, t) in self._opt.items()}
            ages = [now - t for _, t in self._spot.values()]
        freshest = min(ages) if ages else None
        elapsed = (now - started) if started is not None else None
        return {
            "connected": connected,
            "state": self._derive_state(connected, started, freshest, elapsed),
            "stream_age_s": round(elapsed, 1) if elapsed is not None else None,
            "spots": spots,
            "options": options,
        }


class MarketStream(threading.Thread):
    """Owns one DXLink connection in a background thread, feeding a MarketStore."""

    def __init__(self, store: MarketStore, spot_symbols=DEFAULT_SPOT_SYMBOLS):
        super().__init__(name="winthorpe-market-stream", daemon=True)
        self.store = store
        self.spot_symbols = list(spot_symbols)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import asyncio
        try:
            asyncio.run(self._main())
        except Exception:
            logger.exception("market stream crashed")
            self.store.connected = False

    async def _main(self) -> None:
        import asyncio

        from tastytrade.dxfeed import Quote, Trade
        from tastytrade.streamer import DXLinkStreamer

        from winthorpe.broker.session import get_session_and_account

        session, _ = await get_session_and_account()
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Trade, self.spot_symbols)
            await streamer.subscribe(Quote, self.spot_symbols)
            self.store.connected = True
            logger.info("market stream connected; spots=%s", self.spot_symbols)

            async def _consume_trades():
                async for t in streamer.listen(Trade):
                    if self._stop.is_set():
                        return
                    px = getattr(t, "price", None)
                    if px:
                        self.store.set_spot(t.event_symbol, float(px))

            async def _consume_quotes():
                async for q in streamer.listen(Quote):
                    if self._stop.is_set():
                        return
                    bid = float(getattr(q, "bid_price", 0) or 0)
                    ask = float(getattr(q, "ask_price", 0) or 0)
                    sym = q.event_symbol
                    if sym in self.spot_symbols and bid > 0 and ask > 0:
                        # Quote mid as a spot fallback when Trade goes quiet.
                        self.store.set_spot(sym, (bid + ask) / 2)
                    else:
                        self.store.set_quote(sym, bid, ask)

            async def _manage_subs():
                while not self._stop.is_set():
                    pending = self.store.take_pending_option_subs()
                    if pending:
                        await streamer.subscribe(Quote, pending)
                        logger.info("market stream subscribed options: %s", pending)
                    await asyncio.sleep(1.0)

            await asyncio.gather(_consume_trades(), _consume_quotes(), _manage_subs())
        self.store.connected = False
