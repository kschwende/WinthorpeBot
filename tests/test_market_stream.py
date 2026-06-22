"""MarketStore + StreamMarketView — without opening a DXLink connection."""

import time

from winthorpe.data.market_stream import MarketStore
from winthorpe.engine.market import StreamMarketView


def test_store_spot_roundtrip_and_staleness():
    s = MarketStore()
    assert s.spot("SPX") is None
    s.set_spot("SPX", 7500.0)
    assert s.spot("SPX") == 7500.0
    # Stale value (max_age tiny) reads as absent.
    time.sleep(0.01)
    assert s.spot("SPX", max_age=0.0) is None


def test_store_option_mid():
    s = MarketStore()
    s.set_quote(".SPXW260622P7500", 5.0, 6.0)
    assert s.option_mid(".SPXW260622P7500") == 5.5
    # Zero/locked book → no mid.
    s.set_quote(".SPXW260622P7500", 0.0, 6.0)
    assert s.option_mid(".SPXW260622P7500") is None


def test_pending_option_subscriptions_are_drained_once():
    s = MarketStore()
    s.request_option(".A")
    s.request_option(".B")
    first = set(s.take_pending_option_subs())
    assert first == {".A", ".B"}
    # Already-subscribed not handed out again.
    assert s.take_pending_option_subs() == []
    s.request_option(".C")
    assert s.take_pending_option_subs() == [".C"]


def test_snapshot_shape():
    s = MarketStore()
    s.mark_starting()
    s.connected = True
    s.set_spot("SPX", 7500.0)
    snap = s.snapshot()
    assert snap["connected"] is True
    assert snap["state"] == "live"          # connected + fresh tick
    assert snap["stream_age_s"] is not None
    assert "SPX" in snap["spots"]
    assert snap["spots"]["SPX"]["price"] == 7500.0


def test_stream_state_distinguishes_warming_from_dead():
    import time as _t

    s = MarketStore()
    # Never started → down, not a false "disconnected".
    assert s.stream_state() == "down"
    assert s.snapshot()["state"] == "down"

    # Started, handshake not yet complete, within grace → warming.
    s.mark_starting()
    assert s.stream_state() == "warming"

    # Past the grace window without connecting → disconnected (crash/never-up).
    s._started_mono = _t.monotonic() - 100.0
    assert s.stream_state() == "disconnected"

    # Connected but no fresh tick (off-hours / feed quiet) → stale.
    s.connected = True
    assert s.stream_state() == "stale"

    # Connected with a fresh tick → live.
    s.set_spot("SPX", 7500.0)
    assert s.stream_state() == "live"

    # Connected but the only tick has aged out → stale again.
    s._spot["SPX"] = (7500.0, _t.monotonic() - 999.0)
    assert s.stream_state() == "stale"


class _FakeFallback:
    def __init__(self): self.spot_calls = []; self.opt_calls = []
    def spot(self, sym): self.spot_calls.append(sym); return 111.0
    def option_mark(self, s): self.opt_calls.append(s); return 9.9
    def now_et(self): import datetime; return datetime.datetime.now()


def test_view_prefers_stream_then_falls_back():
    store = MarketStore()
    fb = _FakeFallback()
    view = StreamMarketView(store, fallback=fb)

    # Not in stream → fallback used (e.g. ES).
    assert view.spot("ES") == 111.0
    assert fb.spot_calls == ["ES"]

    # In stream → fallback NOT used.
    store.set_spot("SPX", 7500.0)
    assert view.spot("SPX") == 7500.0
    assert "SPX" not in fb.spot_calls


def test_view_option_mark_requests_subscription():
    store = MarketStore()
    fb = _FakeFallback()
    view = StreamMarketView(store, fallback=fb)
    # First call: not streamed yet → requests sub + falls back.
    assert view.option_mark(".SPXW260622P7500") == 9.9
    assert ".SPXW260622P7500" in store.take_pending_option_subs()
    # Once streamed, uses the stream.
    store.set_quote(".SPXW260622P7500", 8.0, 8.2)
    assert view.option_mark(".SPXW260622P7500") == 8.1
