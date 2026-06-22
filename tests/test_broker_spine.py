"""Regression guards for the migrated broker spine.

Ported from upstream-execution/tests/test_tastytrade_broker.py — these encode
the incident fixes the order path must never regress: the 2026-04-28 GLD
routed-and-filled stamp, the 2026-05-06 COST phantom-fill, partial-fill
handling, and the build-error reject shape. Adapted to WinthorpeBot's
option-only path (entry_type=OPTION_MARKET, metadata.occ_symbol).
"""

from unittest.mock import patch

import pytest

from winthorpe.broker import DryRunBroker, create_broker
from winthorpe.broker import tastytrade_broker
from winthorpe.broker.models import TradeDecision
from winthorpe.broker.tastytrade_broker import (
    _build_oco_bracket,
    _extract_fill_count_and_qty,
    _normalize_status,
)

OCC = "SPXW  260622P07500000"


def _opt_decision(size=5, direction="BUY_TO_OPEN_PUT", entry_type="OPTION_MARKET",
                  entry_price=None, occ=OCC):
    meta = {"occ_symbol": occ} if occ is not None else {}
    return TradeDecision(
        strategy="test_plan", product="SPXW", direction=direction, size=size,
        entry_type=entry_type, entry_price=entry_price, metadata=meta,
    )


def _patch_account(mock_account):
    async def _fake():
        return object(), mock_account
    return patch.object(tastytrade_broker, "get_session_and_account", side_effect=_fake)


# --- status normalization (GLD prefix bug) ---------------------------------
def test_normalize_status_strips_orderstatus_enum_prefix():
    assert _normalize_status("OrderStatus.FILLED") == "filled"
    assert _normalize_status("orderstatus.routed") == "routed"
    assert _normalize_status("Filled") == "filled"
    assert _normalize_status("REJECTED") == "rejected"
    assert _normalize_status("") == ""
    assert _normalize_status(None) == ""

    class _FakeEnum:
        def __str__(self): return "OrderStatus.FILLED"
    assert _normalize_status(_FakeEnum()) == "filled"


# --- fill extraction (COST phantom-fill guard) -----------------------------
def test_extract_fill_does_not_use_size_as_filled_fallback():
    class _MockOrder:
        size = 1
        filled_number_of_contracts = None
        filled_quantity = None
        legs = None
        fills = None
    count, qty = _extract_fill_count_and_qty(_MockOrder())
    assert qty is None
    assert count is None


def test_extract_fill_uses_leg_fills_as_source_of_truth():
    class _Fill:
        def __init__(self, qty): self.quantity = qty

    class _Leg:
        def __init__(self, fills): self.fills = fills

    class _MockOrder:
        legs = [_Leg([_Fill(1)]), _Leg([_Fill(1)])]
        size = 1
        filled_number_of_contracts = None
    count, qty = _extract_fill_count_and_qty(_MockOrder())
    assert qty == 1
    assert count == 2


def test_extract_fill_routed_with_empty_leg_fills_is_unfilled():
    class _Leg:
        def __init__(self): self.fills = []

    class _MockOrder:
        legs = [_Leg()]
        size = 1
        filled_number_of_contracts = None
    count, qty = _extract_fill_count_and_qty(_MockOrder())
    assert qty == 0
    assert count == 0


# --- place_order fill stamping ---------------------------------------------
def _resp(status, **order_attrs):
    class _Resp:
        class order:
            id = "tt-1"
        warnings = []
        errors = []
    _Resp.order.status = status
    for k, v in order_attrs.items():
        setattr(_Resp.order, k, v)

    class _Account:
        async def place_order(self, session, order, dry_run=False):
            return _Resp()
    return _Account()


def test_place_order_stamps_filled_at_for_enum_form_status():
    acct = _resp("OrderStatus.FILLED", filled_number_of_contracts=5)
    with _patch_account(acct):
        result = tastytrade_broker.TastytradeBroker().place_order(_opt_decision(size=5))
    assert result["order_filled_at"] is not None
    assert result["order_status"] == "orderstatus.filled"
    assert result["total_filled_qty"] == 5


def test_place_order_stamps_filled_at_when_fill_count_equals_size_despite_routed():
    """GLD belt-and-suspenders: Routed status + filled count == size still a fill."""
    acct = _resp("OrderStatus.ROUTED", filled_number_of_contracts=9)
    with _patch_account(acct):
        result = tastytrade_broker.TastytradeBroker().place_order(_opt_decision(size=9))
    assert result["order_filled_at"] is not None
    assert result["total_filled_qty"] == 9
    assert result["order_status"] == "orderstatus.routed"


def test_place_order_does_not_stamp_filled_at_for_partial_fill():
    acct = _resp("OrderStatus.WORKING", filled_number_of_contracts=3)
    with _patch_account(acct):
        result = tastytrade_broker.TastytradeBroker().place_order(_opt_decision(size=9))
    assert result["order_filled_at"] is None
    assert result["total_filled_qty"] == 3


# --- pre-submit rejects -----------------------------------------------------
def test_place_order_rejected_on_missing_occ_symbol():
    result = tastytrade_broker.TastytradeBroker().place_order(_opt_decision(occ=None))
    assert result["status"] == "rejected"
    assert "occ_symbol" in result["reject_reason"]
    assert result["order_placed_at"] is not None  # latency segment still stamped


def test_place_order_rejects_non_option_entry_type():
    """WinthorpeBot trades SPXW options only."""
    result = tastytrade_broker.TastytradeBroker().place_order(
        _opt_decision(entry_type="MARKET"))
    assert result["status"] == "rejected"
    assert "options only" in result["reject_reason"]


# --- OCO bracket invariant --------------------------------------------------
def test_oco_bracket_rejects_inverted_tp_below_sl():
    with pytest.raises(ValueError, match="inverted"):
        _build_oco_bracket(OCC, contracts=5, tp_price=4.0, sl_price=6.0)


def test_oco_bracket_rejects_non_positive_contracts():
    with pytest.raises(ValueError, match="non-positive"):
        _build_oco_bracket(OCC, contracts=0, tp_price=6.0, sl_price=4.0)


# --- factory gating ---------------------------------------------------------
def test_factory_returns_dryrun_when_not_live(monkeypatch):
    monkeypatch.setenv("WINTHORPE_LIVE", "0")
    assert isinstance(create_broker(), DryRunBroker)
