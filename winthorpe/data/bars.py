"""Shared tastytrade DXLink OHLCV bar source for ES/NQ futures.

Drop-in replacement for the Databento GLBX.MDP3 `ohlcv` feed used by
`agent/level_service.py` and `bot/pattern_distribution/live/bar_sources.py`
(budget trim: this is what lets those services leave the GLBX subscription —
VP parity validated 2026-06-11, `backtests/vp_parity_databento_vs_tastytrade.py`).

Why a wrapper and not raw `subscribe_candle`
--------------------------------------------
Databento `ohlcv-1s`/`ohlcv-1m` emits each bar ONCE, already closed. dxfeed
`Candle` instead streams the *forming* bar repeatedly (snapshot + updates) as
trades arrive, delivers the history snapshot **newest-first**, and re-sends
history on every (re)connect. A consumer that *accumulates* volume per event
(like level_service's VP builder) would badly double-count, and a naive
forward-order assumption drops the whole history. So this source folds events
order-independently — `bars[symbol][bar_open]` keyed by bucket, latest OHLCV
wins — and emits **closed bars only, once each**:

* `max_open[symbol]` is the highest bar-open seen = the still-forming bar;
  anything below it is closed;
* an `emitted` set suppresses re-emission of already-seen bars after a
  reconnect / history replay.

That gives Databento-ohlcv semantics — one closed bar per interval — so the VP
builder and the pattern-distribution aggregator work unchanged.

Symbols are the parent roots ``"ES"`` / ``"NQ"``; the tastytrade continuous
front-month streamer symbols (``/ES:XCME`` / ``/NQ:XCME``) are an internal
detail. Bars come back as ``Candle(symbol, ts, open, high, low, close, volume)``
with ``ts`` the bar-open in ET and prices in dollars.
"""

from __future__ import annotations

import asyncio
import logging
from collections import namedtuple
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("tastytrade_bars")

ET = ZoneInfo("America/New_York")

# Parent root -> tastytrade continuous front-month streamer symbol.
SYMBOL_MAP = {"ES": "/ES:XCME", "NQ": "/NQ:XCME"}

Candle = namedtuple("Candle", ["symbol", "ts", "open", "high", "low", "close", "volume"])


def _parent_of(event_symbol: str) -> str | None:
    """`/ES:XCME{=1m,tho=true}` -> 'ES'. Reverse of SYMBOL_MAP."""
    base = event_symbol.split("{", 1)[0]  # drop the candle attributes
    for parent, stx in SYMBOL_MAP.items():
        if base == stx:
            return parent
    return None


class TastytradeBarSource:
    """ES/NQ OHLCV bars off tastytrade DXLink, emitting closed bars only.

    One interval per call. Use ``stream()`` for a live feed (history-on-subscribe
    first, then live) or ``backfill()`` for a one-shot session pull.
    """

    def __init__(self, symbols: tuple[str, ...] = ("ES", "NQ")) -> None:
        self.symbols = tuple(symbols)
        self._stx = [SYMBOL_MAP[s] for s in self.symbols]

    @staticmethod
    def _fold(c, bars, max_open):
        """Upsert one dxfeed Candle event into ``bars[symbol][bar_open]``.

        Order-independent: dxfeed delivers the history snapshot newest-first and
        re-updates the forming bar, so we key by bar-open and keep the latest
        OHLCV per bucket. Returns the parent symbol whose state changed (or None).
        ``max_open[symbol]`` tracks the highest bar-open seen = the still-forming
        bar; anything below it is closed."""
        if c.time is None or c.close is None:
            return None
        parent = _parent_of(getattr(c, "event_symbol", "") or "")
        if parent is None:
            return None
        bo = int(c.time) // 1000  # ms -> s; dxfeed time = bar open
        o = float(c.open) if c.open is not None else float(c.close)
        bars.setdefault(parent, {})[bo] = [
            o, float(c.high or o), float(c.low or o), float(c.close), float(c.volume or 0)]
        if bo > max_open.get(parent, -1):
            max_open[parent] = bo
        return parent

    # ── public: resilient live stream (reconnects, dedupes) ─────────────────
    async def stream(self, *, interval: str = "1s", start_time: datetime | None = None,
                     extended_trading_hours: bool = True):
        """Async-iterate closed ``Candle``s. Reconnects with backoff; already-seen
        bars are not re-emitted after a reconnect, so accumulating consumers stay
        correct. ``start_time`` backfills history on (re)subscribe."""
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Candle as DxCandle

        from winthorpe.broker.session import get_session_and_account

        bars: dict[str, dict[int, list]] = {}
        max_open: dict[str, int] = {}
        emitted: set[tuple[str, int]] = set()
        backoff = 2.0
        while True:
            try:
                session, _ = await get_session_and_account()
                async with DXLinkStreamer(session) as st:
                    await st.subscribe_candle(self._stx, interval=interval,
                                              start_time=start_time,
                                              extended_trading_hours=extended_trading_hours)
                    logger.info("tastytrade_bars: streaming %s @ %s from %s",
                                self.symbols, interval, start_time)
                    backoff = 2.0
                    async for c in st.listen(DxCandle):
                        sym = self._fold(c, bars, max_open)
                        if sym is None:
                            continue
                        mx = max_open[sym]
                        # Emit every bar below the forming bar that we haven't yet.
                        for bo in sorted(bars[sym]):
                            if bo < mx and (sym, bo) not in emitted:
                                emitted.add((sym, bo))
                                yield Candle(sym, datetime.fromtimestamp(bo, tz=ET), *bars[sym][bo])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("tastytrade_bars: stream error (%s); reconnect in %.0fs",
                               exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── public: one-shot session pull (replaces db.Historical.get_range) ────
    async def backfill(self, *, interval: str = "1m", start_time: datetime,
                       extended_trading_hours: bool = True, include_forming: bool = True,
                       settle: float = 4.0, timeout: float = 60.0) -> list[Candle]:
        """Drain the history-on-subscribe burst into a sorted list of closed
        bars. Returns once no event has arrived for ``settle`` seconds after the
        burst started, or ``timeout`` is hit.

        ``include_forming`` (default True): include the still-forming bar per
        symbol. Set False when handing off to a live ``stream()`` so the forming
        minute is fed exactly once — by the stream, when it actually closes —
        avoiding double-counting in accumulating consumers."""
        from tastytrade import DXLinkStreamer
        from tastytrade.dxfeed import Candle as DxCandle

        from winthorpe.broker.session import get_session_and_account

        bars: dict[str, dict[int, list]] = {}
        max_open: dict[str, int] = {}

        async def collect(st):
            async for c in st.listen(DxCandle):
                self._fold(c, bars, max_open)

        session, _ = await get_session_and_account()
        async with DXLinkStreamer(session) as st:
            await st.subscribe_candle(self._stx, interval=interval,
                                      start_time=start_time,
                                      extended_trading_hours=extended_trading_hours)
            task = asyncio.ensure_future(collect(st))
            # Poll until the burst settles (total bar count stops growing).
            elapsed, last_n = 0.0, -1
            try:
                while elapsed < timeout:
                    await asyncio.sleep(settle)
                    elapsed += settle
                    n = sum(len(v) for v in bars.values())
                    if n == last_n and n > 0:
                        break
                    last_n = n
            finally:
                task.cancel()

        # Return every bucket, deduped. With include_forming=False, drop each
        # symbol's still-forming (max-open) bar so a live stream feeds it on close.
        out = [Candle(sym, datetime.fromtimestamp(bo, tz=ET), *row)
               for sym, sb in bars.items() for bo, row in sb.items()
               if include_forming or bo < max_open.get(sym, -1)]
        out.sort(key=lambda c: (c.ts, c.symbol))
        return out
