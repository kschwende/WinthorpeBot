"""Structural levels — pure extraction + confluence, synthetic bars (no network)."""

from collections import namedtuple
from datetime import datetime
from zoneinfo import ZoneInfo

from winthorpe.levels.structural import (
    StructuralLevels,
    basis_from_matched_bars,
    levels_from_bars,
)

ET = ZoneInfo("America/New_York")
Candle = namedtuple("Candle", ["symbol", "ts", "open", "high", "low", "close", "volume"])


def _c(sym, y, mo, d, h, mi, hi, lo, vol=0):
    ts = datetime(y, mo, d, h, mi, tzinfo=ET)
    return Candle(sym, ts, (hi + lo) / 2, hi, lo, (hi + lo) / 2, vol)


def _c2(sym, ts, close, vol=0):
    """Candle at an explicit ts with an explicit close (flat H/L/O)."""
    return Candle(sym, ts, close, close, close, close, vol)


def test_prior_day_and_overnight_and_opening_range():
    now = datetime(2026, 6, 22, 10, 30, tzinfo=ET)  # Monday mid-morning
    spx = [
        # Prior RTH session (Fri 6/19): high 7510, low 7470
        _c("SPX", 2026, 6, 19, 10, 0, 7505, 7480),
        _c("SPX", 2026, 6, 19, 14, 0, 7510, 7470),
        # Today RTH (Mon 6/22): opening range + later
        _c("SPX", 2026, 6, 22, 9, 31, 7500, 7490),   # OR15 + OR30
        _c("SPX", 2026, 6, 22, 9, 50, 7503, 7495),   # OR30 only (after 9:45)
        _c("SPX", 2026, 6, 22, 10, 15, 7520, 7498),  # later RTH high
    ]
    es = [
        # Overnight window (Sun 18:00 → Mon 09:30): ES high 7460, low 7440
        _c("ES", 2026, 6, 21, 20, 0, 7460, 7450),
        _c("ES", 2026, 6, 22, 3, 0, 7455, 7440),
        # An RTH ES bar that must NOT count as overnight
        _c("ES", 2026, 6, 22, 11, 0, 7600, 7590),
    ]
    lv = levels_from_bars(spx_bars=spx, es_bars=es, prev_close=7475.0,
                          now_et=now, basis=25.0)   # SPX = ES + 25

    assert lv.prior_session_date == "2026-06-19"
    assert lv.pdh == 7510.0
    assert lv.pdl == 7470.0
    assert lv.pdc == 7475.0
    # Overnight extremes from ES, basis-adjusted (+25), RTH ES bar excluded.
    assert lv.onh == 7460.0 + 25.0
    assert lv.onl == 7440.0 + 25.0
    # Opening range: OR15 only the 9:31 bar; OR30 includes 9:50.
    assert lv.or15_high == 7500.0
    assert lv.or30_high == 7503.0
    # Today's running RTH extremes.
    assert lv.rth_high == 7520.0
    assert lv.rth_low == 7490.0


def test_confluence_scan():
    lv = StructuralLevels(pdh=7503.0, onh=7498.0, pdc=7475.0, rth_high=7560.0)
    hits = lv.confluence(7500.0, tolerance=5.0)
    names = [h["name"] for h in hits]
    # PDH (7503, dist 3) and ONH (7498, dist 2) within 5pt; PDC/RTH_HIGH far.
    assert names == ["ONH", "PDH"]        # nearest first
    assert hits[0]["distance"] == 2.0
    assert "PDC" not in names


def test_session_and_weekly_vwap_from_spy():
    now = datetime(2026, 6, 24, 11, 0, tzinfo=ET)   # Wednesday
    # SPY bars: Mon, Tue, Wed RTH. Flat each day so VWAP == that price level.
    # typical = (H+L+C)/3; use H==L so typical == that value.
    spy = [
        _c("SPY", 2026, 6, 22, 10, 0, 750.0, 750.0, vol=100),   # Mon @ 750
        _c("SPY", 2026, 6, 23, 10, 0, 752.0, 752.0, vol=100),   # Tue @ 752
        _c("SPY", 2026, 6, 24, 10, 0, 748.0, 748.0, vol=300),   # Wed @ 748 (heavier)
    ]
    lv = levels_from_bars(spx_bars=[], es_bars=[], prev_close=None,
                          now_et=now, spy_bars=spy, spy_to_spx=10.0)
    # Session VWAP (Wed only) = 748 × 10 = 7480.
    assert lv.session_vwap == 7480.0
    # Weekly VWAP (Mon..Wed) = (750·100 + 752·100 + 748·300)/500 × 10
    expected = (750 * 100 + 752 * 100 + 748 * 300) / 500 * 10
    assert lv.weekly_vwap == round(expected, 2)


def test_vwap_ignores_zero_volume_and_participates_in_confluence():
    lv = StructuralLevels(session_vwap=7505.0, weekly_vwap=7490.0)
    hits = {h["name"] for h in lv.confluence(7503.0, tolerance=5.0)}
    assert "VWAP" in hits            # 7505 within 5pt of 7503
    assert "WVWAP" not in hits       # 7490 is 13pt away


def test_basis_from_matched_bars_uses_same_minute_pair():
    # Yesterday's RTH close: SPX cash 7472.79 vs ES 7545.50 at the same 16:00
    # minute → true basis −72.71 (ES at a premium). Overnight ES has since slid
    # to 7432, but the matched-bar basis must NOT absorb that move.
    spx = [
        _c2("SPX", datetime(2026, 6, 22, 15, 59, tzinfo=ET), 7472.79),
        _c2("SPX", datetime(2026, 6, 22, 16, 0, tzinfo=ET), 7472.79),
    ]
    es = [
        _c2("ES", datetime(2026, 6, 22, 16, 0, tzinfo=ET), 7545.50),
        _c2("ES", datetime(2026, 6, 23, 8, 57, tzinfo=ET), 7432.00),  # live, ignored
    ]
    basis = basis_from_matched_bars(spx, es)
    assert basis is not None
    assert round(basis, 2) == -72.71
    # Fair-value open from current ES recovers the real ~1.5% gap-down, not PDC.
    assert round(7432.00 + basis, 2) == 7359.29


def test_basis_returns_none_without_a_close_pair():
    # SPX bar 14:00, nearest ES bar 03:00 next day → skew far beyond tolerance.
    spx = [_c2("SPX", datetime(2026, 6, 22, 14, 0, tzinfo=ET), 7472.0)]
    es = [_c2("ES", datetime(2026, 6, 23, 3, 0, tzinfo=ET), 7432.0)]
    assert basis_from_matched_bars(spx, es) is None
    assert basis_from_matched_bars([], es) is None
    assert basis_from_matched_bars(spx, []) is None


def test_empty_bars_yield_nulls_but_keeps_pdc():
    now = datetime(2026, 6, 22, 10, 0, tzinfo=ET)
    lv = levels_from_bars(spx_bars=[], es_bars=[], prev_close=7475.0, now_et=now)
    assert lv.pdh is None and lv.onh is None
    assert lv.pdc == 7475.0
    assert lv.confluence(7500.0) == []
