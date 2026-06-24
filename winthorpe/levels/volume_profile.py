"""Volume-at-price profile — POC / value area from OHLCV bars.

SPX cash has no volume, so the profile is computed from the traded instrument
(ES) and converted to SPX terms via the basis (see structural.py). A true profile
needs tick-level volume-at-price; from OHLCV we approximate, and the approximation
is only good on FINE bars — coarse bars smear a high-volume bar across its wide
range and push the POC toward overlap zones.

Validated 2026-06-24 against Karl's charting platform (1hr volume profile, ES):
his POC 7439 / VAL 7431 / VAH 7468 vs this module on 1m bars + ``mode="typical"``
+ the standard expand-from-POC value area: POC 7438.5 / VAL 7435 / VAH 7470 —
POC within ~1, value area within ~4. (An earlier ad-hoc calc on 5m bars with a
uniform-across-range smear put the POC at 7466 — ~27 off; that method is wrong.)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass
class VolumeProfile:
    poc: float          # point of control — most-traded price (bin midpoint)
    val: float          # value-area low  (lower edge of the value band)
    vah: float          # value-area high (upper edge of the value band)
    total_volume: float
    bin_size: float

    def to_dict(self) -> dict:
        return {"poc": self.poc, "val": self.val, "vah": self.vah,
                "total_volume": self.total_volume, "bin_size": self.bin_size}


def volume_profile(
    bars,
    *,
    bin_size: float = 1.0,
    mode: str = "typical",
    value_pct: float = 0.70,
) -> Optional[VolumeProfile]:
    """POC + value area from OHLCV ``bars`` (need .high .low .close .volume).

    mode:
      "typical" — put a bar's whole volume at its typical price (H+L+C)/3. Best
                  POC match vs a real platform on 1m bars (default).
      "uniform" — spread a bar's volume evenly across [low, high]. Smoother value
                  area but biases the POC toward overlap zones.

    Value area = the contiguous band around the POC holding ``value_pct`` of total
    volume, grown by the standard rule: repeatedly add whichever adjacent side
    (looking two bins out) carries more volume. Returns None if there's no volume.

    Use the FINEST bars available (1m); coarse bars misplace the POC.
    """
    prof: dict[int, float] = defaultdict(float)
    for b in bars:
        v = float(getattr(b, "volume", 0) or 0)
        if v <= 0:
            continue
        hi_p, lo_p, c = float(b.high), float(b.low), float(b.close)
        if mode == "uniform":
            lo_k = int(lo_p // bin_size)
            hi_k = int(hi_p // bin_size)
            n = hi_k - lo_k + 1
            for k in range(lo_k, hi_k + 1):
                prof[k] += v / n
        else:  # "typical"
            prof[int(((hi_p + lo_p + c) / 3.0) // bin_size)] += v
    if not prof:
        return None

    lo_k, hi_k = min(prof), max(prof)
    g = [prof.get(k, 0.0) for k in range(lo_k, hi_k + 1)]   # contiguous, gaps=0
    tot = sum(g)
    poc_i = g.index(max(g))
    inc = g[poc_i]
    lo = hi = poc_i
    target = value_pct * tot
    while inc < target and (lo > 0 or hi < len(g) - 1):
        up = (g[hi + 1] + (g[hi + 2] if hi + 2 < len(g) else 0.0)) if hi < len(g) - 1 else -1.0
        dn = (g[lo - 1] + (g[lo - 2] if lo - 2 >= 0 else 0.0)) if lo > 0 else -1.0
        if up >= dn and hi < len(g) - 1:
            hi += 1
            inc += g[hi]
        elif lo > 0:
            lo -= 1
            inc += g[lo]
        else:
            hi += 1
            inc += g[hi]
    return VolumeProfile(
        poc=round((lo_k + poc_i) * bin_size + bin_size / 2.0, 2),
        val=round((lo_k + lo) * bin_size, 2),
        vah=round((lo_k + hi + 1) * bin_size, 2),
        total_volume=round(tot, 1),
        bin_size=bin_size,
    )


def fetch_volume_profile(session: str = "prior", *, mode: str = "typical",
                         bin_size: float = 1.0, now_et=None) -> dict:
    """Live: pull 1m ES bars and compute the prior or today RTH volume profile.
    Returns ES-native POC/VAL/VAH AND SPX-converted (via the matched-bar basis).
    session: 'prior' (last completed RTH day) or 'today' (developing)."""
    import asyncio
    from datetime import datetime, time, timedelta

    from winthorpe.data.bars import TastytradeBarSource
    from winthorpe.levels.structural import (
        ET, RTH_CLOSE, RTH_OPEN, basis_from_matched_bars,
    )

    now = now_et or datetime.now(ET)
    start = datetime.combine(now.date(), time(0, 0), tzinfo=ET) - timedelta(days=4)

    def _pull(sym):
        return asyncio.run(TastytradeBarSource((sym,)).backfill(
            interval="1m", start_time=start, extended_trading_hours=True))

    es_bars = _pull("ES")
    spx_bars = _pull("SPX")

    def _et(b):
        return b.ts.astimezone(ET)

    def _in_rth(b):
        t = _et(b).timetz().replace(tzinfo=None)
        return RTH_OPEN <= t < RTH_CLOSE

    rth_dates = sorted({_et(b).date() for b in es_bars if _in_rth(b)})
    today = now.date()
    if session == "today":
        sess_date = today
    else:
        prior = [d for d in rth_dates if d < today]
        sess_date = prior[-1] if prior else (rth_dates[-1] if rth_dates else today)

    sess_bars = [b for b in es_bars if _in_rth(b) and _et(b).date() == sess_date]
    vp = volume_profile(sess_bars, bin_size=bin_size, mode=mode)
    basis = basis_from_matched_bars(spx_bars, es_bars) or 0.0
    if vp is None:
        return {"session": session, "session_date": str(sess_date),
                "n_bars": len(sess_bars), "error": "no volume in session window"}
    return {
        "session": session,
        "session_date": str(sess_date),
        "n_bars": len(sess_bars),
        "basis": round(basis, 2),
        "mode": mode,
        "es": vp.to_dict(),
        "spx": {"poc": round(vp.poc + basis, 2),
                "val": round(vp.val + basis, 2),
                "vah": round(vp.vah + basis, 2)},
    }
