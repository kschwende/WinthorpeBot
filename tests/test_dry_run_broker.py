"""DryRunBroker as a paper broker — tracks positions and simulates OCO/stop fills
against the live mark, so dry-run P&L is real instead of an instant $0 round-trip."""

from types import SimpleNamespace

from winthorpe.broker import DryRunBroker, _extract_bracket
from winthorpe.broker.models import TradeDecision


class MarkView:
    """Minimal market view: one option mark, mutable between calls."""
    def __init__(self, mark):
        self.mark = mark

    def option_mark(self, streamer):
        return self.mark


def _buy(occ, streamer, qty=10):
    return TradeDecision(strategy="t", product="SPXW", direction="BUY_TO_OPEN_CALL",
                         size=qty, entry_type="OPTION_MARKET",
                         metadata={"occ_symbol": occ, "streamer_symbol": streamer})


def _sell(occ, qty=10):
    return TradeDecision(strategy="t", product="SPXW", direction="SELL_TO_CLOSE",
                         size=qty, entry_type="OPTION_MARKET",
                         metadata={"occ_symbol": occ})


def _oco(occ, tp, sl):
    """A NewComplexOrder-shaped stub: [LIMIT @ tp, STOP @ sl] sell-to-close."""
    leg = SimpleNamespace(symbol=occ)
    return SimpleNamespace(type="OCO", orders=[
        SimpleNamespace(price=tp, stop_trigger=None, legs=[leg]),
        SimpleNamespace(price=None, stop_trigger=sl, legs=[leg]),
    ])


def test_extract_bracket_reads_tp_and_sl():
    occ, tp, sl = _extract_bracket(_oco("OCC", 14.3, 8.25))
    assert (occ, tp, sl) == ("OCC", 14.3, 8.25)


def test_position_held_at_entry_then_filled_when_mark_crosses_tp():
    mv = MarkView(11.0)
    b = DryRunBroker(market=mv)
    b.place_order(_buy("OCC1", ".OCC1", 10))
    b.place_complex_order(_oco("OCC1", tp=14.3, sl=8.25))
    # At the entry mark the position is HELD — no phantom instant close.
    assert b.get_positions() == [{"symbol": "OCC1", "quantity": 10}]
    # Mild move, still inside the bracket → still held.
    mv.mark = 13.0
    assert b.get_positions() == [{"symbol": "OCC1", "quantity": 10}]
    # Mark runs through the TP → simulated OCO fill → no longer held.
    mv.mark = 14.5
    assert b.get_positions() == []


def test_position_filled_when_mark_breaches_stop():
    mv = MarkView(11.0)
    b = DryRunBroker(market=mv)
    b.place_order(_buy("OCC2", ".OCC2", 10))
    b.place_complex_order(_oco("OCC2", tp=14.3, sl=8.25))
    mv.mark = 8.0                       # below the stop
    assert b.get_positions() == []


def test_protective_stop_only_fills_on_breach_not_on_upside():
    mv = MarkView(11.0)
    b = DryRunBroker(market=mv)
    b.place_order(_buy("OCC3", ".OCC3", 10))
    b.place_protective_stop("OCC3", 10, 8.25)   # trailing plan: stop only, no TP
    mv.mark = 50.0                               # huge upside must NOT fill (no TP leg)
    assert b.get_positions() == [{"symbol": "OCC3", "quantity": 10}]
    mv.mark = 8.0                                # breach the floor → fill
    assert b.get_positions() == []


def test_sell_to_close_clears_position():
    b = DryRunBroker()                  # no market → still tracks positions
    b.place_order(_buy("OCC4", ".OCC4", 5))
    assert b.get_positions() == [{"symbol": "OCC4", "quantity": 5}]
    b.place_order(_sell("OCC4", 5))
    assert b.get_positions() == []


def test_without_market_tracks_position_without_simulating_fills():
    b = DryRunBroker()                  # no market view
    b.place_order(_buy("OCC5", ".OCC5", 7))
    b.place_complex_order(_oco("OCC5", tp=14.3, sl=8.25))
    # No mark stream → can't simulate the OCO, but the position is still HELD
    # (engine-side exits govern); it is NOT a phantom-empty book.
    assert b.get_positions() == [{"symbol": "OCC5", "quantity": 7}]
