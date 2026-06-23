"""Structural price levels — the anchors a discretionary 0DTE trader reads.

Phase 1: price-extreme levels (need no volume), so they come exact from SPX cash
candles, except the overnight high/low which the cash index can't provide (it
trades RTH only) — those come from ES futures, basis-adjusted to SPX terms.

  PDH / PDL   prior RTH session high / low      (SPX cash)
  PDC         prior session close               (prev_close REST — exact)
  ONH / ONL   overnight (globex) high / low     (ES futures + basis)
  OR15 / OR30 opening-range high/low (15/30m)   (SPX cash)
  RTH hi/lo   today's session extremes so far   (SPX cash)

These feed level CONFLUENCE in the agent's deliberation: a proposed trigger/stop
that lines up with PDH or ONH is a stronger level than one floating in space.

Volume-based structure (VWAP, VPOC/VAH/VAL, profile shape) is Phase 2 — SPX cash
has no volume, so those need a traded-instrument source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
GLOBEX_OPEN = time(18, 0)


@dataclass
class StructuralLevels:
    asof: str = ""
    basis: float = 0.0                       # SPX_spot - ES_spot (ES→SPX adjust)
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    pdc: Optional[float] = None
    onh: Optional[float] = None
    onl: Optional[float] = None
    or15_high: Optional[float] = None
    or15_low: Optional[float] = None
    or30_high: Optional[float] = None
    or30_low: Optional[float] = None
    rth_high: Optional[float] = None
    rth_low: Optional[float] = None
    session_vwap: Optional[float] = None     # today RTH, SPY-derived → SPX terms
    weekly_vwap: Optional[float] = None      # this week RTH, Monday-anchored
    prior_session_date: Optional[str] = None

    def as_named(self) -> dict[str, float]:
        """Non-null levels as {label: price}, for confluence scanning."""
        names = {
            "PDH": self.pdh, "PDL": self.pdl, "PDC": self.pdc,
            "ONH": self.onh, "ONL": self.onl,
            "OR15H": self.or15_high, "OR15L": self.or15_low,
            "OR30H": self.or30_high, "OR30L": self.or30_low,
            "RTH_HIGH": self.rth_high, "RTH_LOW": self.rth_low,
            "VWAP": self.session_vwap, "WVWAP": self.weekly_vwap,
        }
        return {k: v for k, v in names.items() if v is not None}

    def confluence(self, level: float, tolerance: float = 5.0) -> list[dict]:
        """Structural levels within ``tolerance`` points of ``level``, nearest
        first. Each: {name, price, distance}."""
        hits = [{"name": n, "price": round(p, 2), "distance": round(abs(p - level), 2)}
                for n, p in self.as_named().items() if abs(p - level) <= tolerance]
        return sorted(hits, key=lambda h: h["distance"])

    def to_dict(self) -> dict:
        return {**self.__dict__}


def _in_rth(ts: datetime) -> bool:
    return RTH_OPEN <= ts.timetz().replace(tzinfo=None) < RTH_CLOSE


def _vwap(bars: list) -> Optional[float]:
    """Volume-weighted average of typical price (H+L+C)/3. None if no volume."""
    num = den = 0.0
    for c in bars:
        v = float(c.volume or 0)
        if v <= 0:
            continue
        typical = (float(c.high) + float(c.low) + float(c.close)) / 3.0
        num += typical * v
        den += v
    return (num / den) if den > 0 else None


def basis_from_matched_bars(
    spx_bars: list, es_bars: list, *, max_skew_s: float = 120.0
) -> Optional[float]:
    """True SPX−ES basis from a same-timestamp bar pair at the latest minute the
    cash index actually traded.

    The cash index trades RTH only, so outside RTH the live SPX mark is frozen at
    the prior close. Pairing that stale mark with a *live* ES bar folds the whole
    overnight move into the "basis" — and since fair value is ``ES + basis``, that
    just regurgitates PDC (a flat open) no matter how far ES has travelled. Pairing
    SPX and ES at the same wall-clock minute instead isolates the real
    financing/dividend spread, which is stable overnight.

    Returns None if either side is empty or no ES bar lands within ``max_skew_s``
    of the latest SPX bar — caller falls back to the live-mark estimate.
    """
    if not spx_bars or not es_bars:
        return None
    last_spx = max(spx_bars, key=lambda c: c.ts)
    es_match = min(es_bars, key=lambda c: abs((c.ts - last_spx.ts).total_seconds()))
    if abs((es_match.ts - last_spx.ts).total_seconds()) > max_skew_s:
        return None
    return float(last_spx.close) - float(es_match.close)


def levels_from_bars(
    *,
    spx_bars: list,
    es_bars: list,
    prev_close: Optional[float],
    now_et: datetime,
    basis: float = 0.0,
    spy_bars: Optional[list] = None,
    spy_to_spx: float = 10.0,
) -> StructuralLevels:
    """Pure level extraction from candle lists. Candles are namedtuples with
    ``.ts`` (ET datetime), ``.high``, ``.low``, ``.close``, ``.volume``. No I/O.

    VWAP is derived from ``spy_bars`` (SPX cash has no volume) and scaled to SPX
    terms by ``spy_to_spx`` (the live SPX/SPY ratio, ~10)."""
    today = now_et.date()
    out = StructuralLevels(asof=now_et.isoformat(), basis=round(basis, 2),
                           pdc=prev_close)

    # --- prior RTH session (SPX cash): the latest RTH date before today -----
    rth_by_date: dict[date, list] = {}
    today_rth: list = []
    for c in spx_bars:
        if not _in_rth(c.ts):
            continue
        d = c.ts.date()
        if d < today:
            rth_by_date.setdefault(d, []).append(c)
        elif d == today:
            today_rth.append(c)
    if rth_by_date:
        prior = max(rth_by_date)
        bars = rth_by_date[prior]
        out.prior_session_date = prior.isoformat()
        out.pdh = round(max(c.high for c in bars), 2)
        out.pdl = round(min(c.low for c in bars), 2)

    # --- today's RTH extremes + opening range (SPX cash) --------------------
    if today_rth:
        out.rth_high = round(max(c.high for c in today_rth), 2)
        out.rth_low = round(min(c.low for c in today_rth), 2)
        or15 = [c for c in today_rth if c.ts.time() < time(9, 45)]
        or30 = [c for c in today_rth if c.ts.time() < time(10, 0)]
        if or15:
            out.or15_high = round(max(c.high for c in or15), 2)
            out.or15_low = round(min(c.low for c in or15), 2)
        if or30:
            out.or30_high = round(max(c.high for c in or30), 2)
            out.or30_low = round(min(c.low for c in or30), 2)

    # --- overnight (ES futures, basis-adjusted to SPX) ----------------------
    # Window: yesterday 18:00 ET → today 09:30 ET.
    on_start = datetime.combine(today, GLOBEX_OPEN, tzinfo=ET) - timedelta(days=1)
    on_end = datetime.combine(today, RTH_OPEN, tzinfo=ET)
    on_bars = [c for c in es_bars if on_start <= c.ts < on_end]
    if on_bars:
        out.onh = round(max(c.high for c in on_bars) + basis, 2)
        out.onl = round(min(c.low for c in on_bars) + basis, 2)

    # --- VWAP (SPY RTH, scaled to SPX terms) --------------------------------
    # Session = today's RTH; weekly = this ISO week (Monday-anchored) RTH.
    if spy_bars:
        week_start = today - timedelta(days=today.weekday())  # Monday
        sess = [c for c in spy_bars if _in_rth(c.ts) and c.ts.date() == today]
        week = [c for c in spy_bars if _in_rth(c.ts) and week_start <= c.ts.date() <= today]
        sv = _vwap(sess)
        wv = _vwap(week)
        if sv is not None:
            out.session_vwap = round(sv * spy_to_spx, 2)
        if wv is not None:
            out.weekly_vwap = round(wv * spy_to_spx, 2)

    return out


def fetch_structural_levels(now_et: Optional[datetime] = None) -> StructuralLevels:
    """Live: backfill SPX + ES candles, pull prev_close + basis, compute levels."""
    import asyncio

    from winthorpe.broker.session import market_data_mark
    from winthorpe.data.bars import TastytradeBarSource

    now = now_et or datetime.now(ET)
    # Go back 4 calendar days from midnight so the prior RTH session AND the
    # overnight window are captured even across a weekend.
    start = datetime.combine(now.date(), time(0, 0), tzinfo=ET) - timedelta(days=4)

    async def _pull(symbol):
        src = TastytradeBarSource((symbol,))
        return await src.backfill(interval="1m", start_time=start,
                                  extended_trading_hours=True)

    spx_bars = asyncio.run(_pull("SPX"))
    es_bars = asyncio.run(_pull("ES"))
    spy_bars = asyncio.run(_pull("SPY"))    # has volume → VWAP source

    prev_close = _spx_prev_close()          # dedicated field, not the live mark
    # Basis = SPX−ES at the latest minute SPX cash actually traded (matched bar
    # pair). NOT live_SPX_mark − live_ES: the cash index is frozen at its prior
    # close outside RTH, so that estimate folds the overnight move into the basis
    # and fair value (ES + basis) just spits back PDC. Fall back to the live-mark
    # estimate only if no matched pair is available (e.g. SPX bars missing).
    spx_spot = market_data_mark("SPX", "indices")
    spy_spot = market_data_mark("SPY", "equities")
    basis = basis_from_matched_bars(spx_bars, es_bars)
    if basis is None:
        last_es = max(es_bars, key=lambda c: c.ts).close if es_bars else None
        basis = (spx_spot - last_es) if (spx_spot and last_es) else 0.0
    spy_to_spx = (spx_spot / spy_spot) if (spx_spot and spy_spot) else 10.0

    return levels_from_bars(spx_bars=spx_bars, es_bars=es_bars,
                            prev_close=prev_close, now_et=now, basis=basis,
                            spy_bars=spy_bars, spy_to_spx=spy_to_spx)


def _spx_prev_close() -> Optional[float]:
    """SPX prior-session close via the dedicated market-data field (yfinance
    daily lags a session; prev_close is exact)."""
    import tastytrade.market_data as md

    from winthorpe.broker.session import _run_coro
    from winthorpe.config import require_creds

    secret, refresh = require_creds()

    async def _fetch():
        from tastytrade import Session
        session = Session(provider_secret=secret, refresh_token=refresh, is_test=False)
        data = await md.get_market_data_by_type(session, indices=["SPX"])
        return data[0] if data else None

    try:
        m = _run_coro(_fetch)
        v = getattr(m, "prev_close", None)
        return float(v) if v is not None else None
    except Exception:
        logger.warning("prev_close fetch failed", exc_info=True)
        return None
