"""Tastytrade execution — SPXW long-option spine.

Migrated from upstream-execution/bot/tastytrade_broker.py. This is the V4.1
option subsystem extracted on its own: single-leg SPXW call/put orders plus the
OCO TP/SL bracket. The futures / equity / iron-condor / credit-spread builders
were dropped — WinthorpeBot trades one instrument.

The fill-detection / status-normalization / fill-count logic is carried VERBATIM
because it encodes hard-won incident fixes (the 2026-04-28 GLD routed-and-filled
case, the 2026-05-06 COST phantom-fill, the 2026-06-08 KO cancel-before-replace).
Do not "clean up" those blocks — every branch is load-bearing.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from winthorpe.broker.models import TradeDecision
from winthorpe.broker.session import _run_coro, get_session_and_account

logger = logging.getLogger(__name__)

# Tastytrade order-status strings (lowercased + enum-prefix-stripped via
# ``_normalize_status``) that count as a confirmed fill / reject / cancel.
# The SDK surfaces ``OrderStatus.<NAME>``; ``str()`` yields
# ``"OrderStatus.FILLED"`` → ``"orderstatus.filled"``. ``_normalize_status``
# strips the prefix so both the Enum and plain-string forms hit the same set.
_FILLED_STATUSES = {"filled"}
_REJECTED_STATUSES = {"rejected"}
_CANCELLED_STATUSES = {"cancelled", "canceled"}


def _normalize_status(s: Any) -> str:
    """Return a bare status name, lowercased and enum-prefix-stripped.

    Handles every shape the tastytrade SDK has surfaced over 12.x:
      * ``OrderStatus.FILLED`` (Enum)     → ``"filled"``
      * ``"OrderStatus.FILLED"`` (string) → ``"filled"``
      * ``"Filled"`` (plain string)       → ``"filled"``
      * ``None`` / ``""``                  → ``""``
    """
    if s is None:
        return ""
    text = str(s).strip().lower()
    if not text:
        return ""
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _now_iso() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


def _extract_fill_count_and_qty(tt_order) -> tuple[int | None, int | None]:
    """Best-effort (fill_count, total_filled_qty) from a tastytrade
    OrderResponse.order. Returns (None, None) when the shape doesn't expose them.

    Precedence:
      1. Sum of leg-level ``fills`` (most reliable on SDK 12.x — a Routed order
         with ``leg.fills == []`` on every leg is working but unfilled).
      2. Order-level ``filled_number_of_contracts`` / ``filled_quantity``.

    NEVER falls back to ``tt_order.size`` — that's the ORDERED quantity and
    equals ``decision.size`` by definition; treating it as filled-qty caused the
    2026-05-06 COST phantom-fill (routed-not-filled recorded as filled).
    """
    if tt_order is None:
        return None, None

    legs = getattr(tt_order, "legs", None) or []
    if legs:
        any_legs_have_fills_list = False
        total_fill_events = 0
        total_leg_fill_qty = 0
        for leg in legs:
            leg_fills = getattr(leg, "fills", None)
            if isinstance(leg_fills, list):
                any_legs_have_fills_list = True
                total_fill_events += len(leg_fills)
                for f in leg_fills:
                    fq = getattr(f, "quantity", 0) or 0
                    try:
                        total_leg_fill_qty += int(fq)
                    except (TypeError, ValueError):
                        continue
        if any_legs_have_fills_list:
            n_legs = len(legs)
            spread_fill_qty = total_leg_fill_qty // n_legs if n_legs else 0
            return total_fill_events, spread_fill_qty

    qty = None
    for attr in ("filled_number_of_contracts", "filled_quantity"):
        val = getattr(tt_order, attr, None)
        if val is not None:
            try:
                qty = int(val)
                break
            except (TypeError, ValueError):
                continue
    fills = getattr(tt_order, "fills", None)
    count = len(fills) if isinstance(fills, list) else None
    return count, qty


# ---------------------------------------------------------------------------
# Order builders (verbatim from the V4.1 option path)
# ---------------------------------------------------------------------------
def _build_option_order(decision: TradeDecision):
    """Build a single-leg SPXW outright call/put order.

    Entry via OPTION_MARKET (immediate fill) or OPTION_LIMIT (standing LIMIT).
    Exit (TP / manual close / EOD) via SELL_TO_CLOSE.

    Symbol comes from ``decision.metadata["occ_symbol"]`` (caller resolves it).
    """
    from tastytrade.instruments import InstrumentType
    from tastytrade.order import (
        Leg, NewOrder, OrderAction, OrderTimeInForce, OrderType,
    )

    direction = (decision.direction or "").upper()
    action_map = {
        "BUY_TO_OPEN_CALL": OrderAction.BUY_TO_OPEN,
        "BUY_TO_OPEN_PUT":  OrderAction.BUY_TO_OPEN,
        "BUY_TO_OPEN":      OrderAction.BUY_TO_OPEN,
        "SELL_TO_OPEN":     OrderAction.SELL_TO_OPEN,
        "SELL_TO_CLOSE":    OrderAction.SELL_TO_CLOSE,
        "BUY_TO_CLOSE":     OrderAction.BUY_TO_CLOSE,
    }
    if direction not in action_map:
        raise ValueError(
            f"option direction must be one of {sorted(action_map)}, got {direction!r}"
        )
    action = action_map[direction]

    meta = decision.metadata or {}
    occ = meta.get("occ_symbol")
    if not occ:
        raise ValueError(
            f"option order requires metadata.occ_symbol (strategy={decision.strategy})"
        )
    qty = int(decision.size)
    if qty <= 0:
        raise ValueError(f"non-positive size: {decision.size}")

    leg = Leg(
        instrument_type=InstrumentType.EQUITY_OPTION,
        symbol=occ,
        action=action,
        quantity=qty,
    )

    entry_type = (decision.entry_type or "").upper()
    if entry_type == "OPTION_MARKET":
        return NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.MARKET,
            legs=[leg],
        )
    if entry_type == "OPTION_LIMIT":
        if decision.entry_price is None:
            raise ValueError(
                f"OPTION_LIMIT requires entry_price (strategy={decision.strategy})"
            )
        return NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            price=Decimal(str(round(float(decision.entry_price), 2))),
            legs=[leg],
        )
    raise ValueError(
        f"unsupported option entry_type={entry_type!r} (strategy={decision.strategy})"
    )


def _build_oco_bracket(occ_symbol: str, contracts: int, tp_price: float, sl_price: float):
    """Build an OCO complex order: [SELL_TO_CLOSE LIMIT @ tp, SELL_TO_CLOSE STOP @ sl].

    Tastytrade atomically cancels the unfilled leg when either fills — the
    safety property the managed exit needs (vs separate orders that can race).
    tp_price = fill × (1 + tp_pct); sl_price = fill × (1 + sl_pct) (sl_pct < 0).

    Time-stop is NOT part of this bracket (it's time-based, not price-based);
    the engine handles a time-stop by cancel_complex_order then MARKET close.
    """
    from tastytrade.instruments import InstrumentType
    from tastytrade.order import (
        ComplexOrderType, Leg, NewComplexOrder, NewOrder,
        OrderAction, OrderTimeInForce, OrderType,
    )
    if contracts <= 0:
        raise ValueError(f"non-positive contracts: {contracts}")
    if tp_price <= 0 or sl_price <= 0:
        raise ValueError(f"non-positive bracket prices: tp={tp_price}, sl={sl_price}")
    if tp_price <= sl_price:
        raise ValueError(
            f"OCO bracket inverted: tp={tp_price} must be > sl={sl_price} "
            f"(TP above entry, SL below entry for long-option close)"
        )

    # Each NewOrder gets its own Leg instance (server-side OCO doesn't share legs).
    def _leg() -> "Leg":
        return Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=occ_symbol,
            action=OrderAction.SELL_TO_CLOSE,
            quantity=int(contracts),
        )

    tp_order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        price=Decimal(str(round(float(tp_price), 2))),
        legs=[_leg()],
    )
    sl_order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.STOP,
        stop_trigger=Decimal(str(round(float(sl_price), 2))),
        legs=[_leg()],
    )
    return NewComplexOrder(type=ComplexOrderType.OCO, orders=[tp_order, sl_order])


# ---------------------------------------------------------------------------
# Broker adapter
# ---------------------------------------------------------------------------
class TastytradeBroker:
    """Live SPXW option adapter for tastytrade.

    Construct via :func:`winthorpe.broker.create_broker` — it gates on
    ``config.is_live()`` and returns a DryRunBroker otherwise.
    """

    def __init__(self) -> None:
        # Fail fast if credentials are missing — never a live adapter that
        # silently can't place orders.
        if not os.environ.get("TT_SECRET") or not os.environ.get("TT_REFRESH"):
            raise RuntimeError(
                "TastytradeBroker: TT_SECRET / TT_REFRESH not set in env. "
                "Fill WinthorpeBot's .env before constructing a live adapter."
            )

    def place_order(self, decision: TradeDecision) -> dict[str, Any]:
        """Place a single-leg SPXW option order. Option path only — any other
        entry_type is rejected pre-submit."""
        entry_type_upper = (decision.entry_type or "").upper()
        if entry_type_upper not in ("OPTION_LIMIT", "OPTION_MARKET"):
            msg = (
                f"WinthorpeBot trades SPXW options only; unsupported entry_type="
                f"{decision.entry_type!r}"
            )
            logger.error("tastytrade broker: %s", msg)
            return {
                "order_id": None, "status": "rejected", "order_status": "rejected",
                "reject_reason": msg, "order_placed_at": _now_iso(),
                "order_filled_at": None, "error": msg,
            }
        try:
            built_order = _build_option_order(decision)
        except ValueError as exc:
            logger.error("tastytrade broker (option): %s", exc)
            return {
                "order_id": None, "status": "rejected", "order_status": "rejected",
                "reject_reason": str(exc), "order_placed_at": _now_iso(),
                "order_filled_at": None, "error": str(exc),
            }

        async def _submit() -> dict[str, Any]:
            session, account = await get_session_and_account()
            # Stamp order_placed_at IMMEDIATELY before the network round-trip.
            order_placed_at = _now_iso()
            response = await account.place_order(session, built_order, dry_run=False)
            tt_order = getattr(response, "order", None)
            raw_status = getattr(tt_order, "status", "unknown")
            status_lc = str(raw_status or "").lower()
            status_norm = _normalize_status(raw_status)
            # ``order_filled_at`` is stamped at place_order return time ONLY when
            # the broker reports a full fill in the initial response. Working /
            # routed / live states leave it null for a later back-stamp pass.
            order_filled_at: str | None = None
            reject_reason: str | None = None
            cancel_reason: str | None = None
            if status_norm in _FILLED_STATUSES:
                order_filled_at = _now_iso()
            elif status_norm in _REJECTED_STATUSES:
                errors_list = getattr(response, "errors", []) or []
                reject_reason = "; ".join(str(e) for e in errors_list) or "broker rejected"
            elif status_norm in _CANCELLED_STATUSES:
                cancel_reason = "broker cancelled"
            fill_count, total_filled_qty = _extract_fill_count_and_qty(tt_order)
            # Belt-and-suspenders (2026-04-28 GLD): SDK returned
            # status="OrderStatus.ROUTED" AND filled_number_of_contracts == size
            # in the SAME response. The status check misses it; the fill count
            # proves it. Treat full-quantity fill count as a fill.
            if (
                order_filled_at is None
                and total_filled_qty is not None
                and decision.size > 0
                and total_filled_qty >= decision.size
                and status_norm not in _REJECTED_STATUSES
                and status_norm not in _CANCELLED_STATUSES
            ):
                order_filled_at = _now_iso()
            result: dict[str, Any] = {
                "order_id": getattr(tt_order, "id", None),
                "status": raw_status,
                "order_status": status_lc or None,
                "order_placed_at": order_placed_at,
                "order_filled_at": order_filled_at,
                "warnings": [str(w) for w in (getattr(response, "warnings", []) or [])],
                "errors": [str(e) for e in (getattr(response, "errors", []) or [])],
                "broker": "tastytrade",
                "symbol": decision.product,
                "instrument_type": "EQUITY_OPTION",
                "entry_type": decision.entry_type,
                "entry_price": decision.entry_price,
                "size": decision.size,
            }
            if reject_reason is not None:
                result["reject_reason"] = reject_reason
            if cancel_reason is not None:
                result["cancel_reason"] = cancel_reason
            if fill_count is not None:
                result["fill_count"] = fill_count
            if total_filled_qty is not None:
                result["total_filled_qty"] = total_filled_qty
            return result

        try:
            return _run_coro(_submit)
        except Exception as exc:
            logger.exception("tastytrade broker: place_order failed")
            return {
                "order_id": None, "status": "error", "order_status": "error",
                "reject_reason": str(exc), "order_placed_at": _now_iso(),
                "order_filled_at": None, "error": str(exc), "broker": "tastytrade",
            }

    def wait_for_fill(self, order_id, occ_symbol, max_attempts: int = 5,
                      delay_sec: float = 1.0) -> dict[str, Any] | None:
        """Resolve the real fill price after an OPTION_MARKET entry.

        Ported from alert_consumer._v41_wait_for_fill — the workaround for orders
        that return status=Routed/Filled before fill_price is populated, AND for
        the SDK returning FILLED with an EMPTY .fills array (live-observed 2026-05-29).
        Two sources, in order:
          1. get_order(.fills) weighted-average fill price (preferred)
          2. get_positions() average_open_price for the OCC (fallback when .fills
             is empty but status is filled)
        Returns {fill_price, total_filled_qty, source} or None on timeout.
        """
        import time as _time
        if not order_id:
            return None

        async def _fetch_order():
            session, account = await get_session_and_account()
            return await account.get_order(session, order_id)

        for attempt in range(max_attempts):
            try:
                o = _run_coro(_fetch_order)
            except Exception as exc:
                logger.debug("wait_for_fill poll #%d failed: %s", attempt + 1, exc)
                _time.sleep(delay_sec)
                continue
            status = _normalize_status(getattr(o, "status", ""))
            fills = list(getattr(o, "fills", []) or [])
            if fills:
                qty = sum(float(getattr(f, "quantity", 0) or 0) for f in fills)
                wpx = sum(float(getattr(f, "fill_price", 0) or 0)
                          * float(getattr(f, "quantity", 0) or 0) for f in fills)
                if qty > 0:
                    return {"fill_price": round(wpx / qty, 2),
                            "total_filled_qty": int(qty), "source": "get_order_fills"}
            if status == "filled" and occ_symbol:
                positions = self.get_positions()
                pos = next((p for p in positions
                            if str(p.get("symbol", "")).strip() == occ_symbol), None)
                if pos and float(pos.get("average_price", 0)) > 0 and int(pos.get("quantity", 0)) > 0:
                    return {"fill_price": float(pos["average_price"]),
                            "total_filled_qty": int(pos["quantity"]),
                            "source": "positions_fallback"}
            _time.sleep(delay_sec)
        logger.warning("wait_for_fill timed out on order_id=%s occ=%s", order_id, occ_symbol)
        return None

    def get_positions(self) -> list[dict[str, Any]]:
        async def _fetch() -> list[dict[str, Any]]:
            session, account = await get_session_and_account()
            positions = await account.get_positions(session)
            return [
                {
                    "symbol": str(getattr(p, "symbol", "")),
                    "instrument_type": str(getattr(p, "instrument_type", "")),
                    "quantity": int(getattr(p, "quantity", 0)),
                    "direction": str(getattr(p, "quantity_direction", "")),
                    "average_price": float(getattr(p, "average_open_price", 0) or 0),
                    "mark": float(getattr(p, "mark", 0) or 0),
                }
                for p in positions
            ]

        try:
            return _run_coro(_fetch)
        except Exception:
            logger.exception("tastytrade broker: get_positions failed")
            return []

    def cancel_order(self, order_id: str) -> bool:
        async def _cancel() -> bool:
            session, account = await get_session_and_account()
            try:
                await account.delete_order(session, order_id)
                return True
            except Exception as exc:
                logger.warning("tastytrade broker: cancel %s failed: %s", order_id, exc)
                return False

        try:
            return _run_coro(_cancel)
        except Exception:
            logger.exception("tastytrade broker: cancel_order failed")
            return False

    def cancel_working_orders_for(self, underlying: str) -> list[str]:
        """Cancel any WORKING (unfilled) orders touching ``underlying``; return
        the cancelled order IDs.

        Broker-truth cancel-before-replace: the manager calls this before
        routing a new close so a stuck working order is cleared first.
        tastytrade rejects a second closing order with
        ``cannot_close_against_more_than_existing_position`` while the first is
        still live (the 2026-06-08 KO cap-orphan). Reads live orders rather than
        trusting an audited order_id, so a manual/stale working order is caught.
        Fail-soft: returns ``[]`` on any error.
        """
        und = (underlying or "").upper().strip()
        if not und:
            return []
        _terminal = {"FILLED", "CANCELLED", "CANCELED", "EXPIRED", "REJECTED"}

        async def _cancel_all() -> list[str]:
            session, account = await get_session_and_account()
            try:
                orders = await account.get_live_orders(session)
            except Exception as exc:
                logger.warning("tastytrade broker: get_live_orders failed: %s", exc)
                return []
            cancelled: list[str] = []
            for o in orders or []:
                status = str(getattr(o, "status", "")).rsplit(".", 1)[-1].upper()
                if status in _terminal:
                    continue
                touches = False
                for leg in (getattr(o, "legs", None) or []):
                    sym = str(getattr(leg, "symbol", "") or "")
                    m = re.match(r"^/?([A-Z]{1,6})", sym)
                    if m and m.group(1) == und:
                        touches = True
                        break
                if not touches:
                    continue
                oid = getattr(o, "id", None)
                if oid is None:
                    continue
                try:
                    await account.delete_order(session, oid)
                    cancelled.append(str(oid))
                except Exception as exc:
                    logger.warning(
                        "tastytrade broker: cancel working order %s failed: %s", oid, exc
                    )
            return cancelled

        try:
            return _run_coro(_cancel_all)
        except Exception:
            logger.exception("tastytrade broker: cancel_working_orders_for failed")
            return []

    def place_complex_order(self, complex_order) -> dict[str, Any]:
        """Submit a NewComplexOrder (OCO) — the bracket TP+SL exit. Returns an
        audit-shaped dict with complex_order_id, status, and the linked legs'
        order IDs."""
        async def _submit() -> dict[str, Any]:
            session, account = await get_session_and_account()
            order_placed_at = _now_iso()
            try:
                response = await account.place_complex_order(
                    session, complex_order, dry_run=False
                )
            except Exception as exc:
                logger.exception("tastytrade broker: place_complex_order failed")
                return {
                    "complex_order_id": None, "status": "error",
                    "order_placed_at": order_placed_at, "error": str(exc),
                    "reject_reason": str(exc), "broker": "tastytrade",
                }
            tt_co = getattr(response, "complex_order", None)
            co_id = getattr(tt_co, "id", None)
            leg_order_ids = [getattr(sub, "id", None)
                             for sub in (getattr(tt_co, "orders", []) or [])]
            raw_status = getattr(tt_co, "status", "unknown")
            status_lc = str(raw_status or "").lower()
            status_norm = _normalize_status(raw_status)
            errors_list = [str(e) for e in (getattr(response, "errors", []) or [])]
            warnings_list = [str(w) for w in (getattr(response, "warnings", []) or [])]
            reject_reason = None
            if status_norm in _REJECTED_STATUSES:
                reject_reason = "; ".join(errors_list) or "broker rejected complex order"
            return {
                "complex_order_id": co_id,
                "leg_order_ids": leg_order_ids,
                "status": raw_status,
                "order_status": status_lc or None,
                "order_placed_at": order_placed_at,
                "errors": errors_list,
                "warnings": warnings_list,
                "reject_reason": reject_reason,
                "broker": "tastytrade",
                "complex_type": str(getattr(complex_order, "type", "")),
            }

        try:
            return _run_coro(_submit)
        except Exception as exc:
            logger.exception("tastytrade broker: place_complex_order outer failed")
            return {
                "complex_order_id": None, "status": "error",
                "order_placed_at": _now_iso(), "error": str(exc),
                "reject_reason": str(exc), "broker": "tastytrade",
            }

    def cancel_complex_order(self, complex_order_id: str) -> bool:
        """Cancel an OCO bracket. Idempotent — True if cancelled, False on any
        error (e.g. already filled). Caller treats False as 'no longer
        cancellable; re-check position state before market-close.'"""
        async def _cancel() -> bool:
            session, account = await get_session_and_account()
            try:
                await account.delete_complex_order(session, complex_order_id)
                return True
            except Exception as exc:
                logger.warning(
                    "tastytrade broker: cancel complex %s failed: %s",
                    complex_order_id, exc,
                )
                return False

        try:
            return _run_coro(_cancel)
        except Exception:
            logger.exception("tastytrade broker: cancel_complex_order failed")
            return False

    def get_account_balance(self) -> dict[str, Any]:
        """Live balance snapshot. ``maintenance_requirement`` is tastytrade's
        authoritative committed-BP number. Fail-safe: all-None on error."""
        async def _fetch() -> dict[str, Any]:
            session, account = await get_session_and_account()
            bal = await account.get_balances(session)

            def _f(x):
                if x is None:
                    return None
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return None

            return {
                "net_liquidating_value": _f(getattr(bal, "net_liquidating_value", None)),
                "equity_buying_power": _f(getattr(bal, "equity_buying_power", None)),
                "maintenance_requirement": _f(getattr(bal, "maintenance_requirement", None)),
                "used_buying_power": _f(getattr(bal, "used_derivative_buying_power", None)),
            }

        try:
            return _run_coro(_fetch)
        except Exception:
            logger.exception("tastytrade broker: get_account_balance failed")
            return {
                "net_liquidating_value": None, "equity_buying_power": None,
                "maintenance_requirement": None, "used_buying_power": None,
            }

    def get_today_fills(self) -> list[dict[str, Any]]:
        """Today's executed transactions (used to resolve OCO exit prices — the
        broker's get_order doesn't reliably populate ``.fills`` on closed SPXW
        legs). Fail-safe: ``[]`` on error."""
        from datetime import date

        async def _txs():
            session, account = await get_session_and_account()
            return await account.get_history(session, start_date=date.today())

        try:
            txs = _run_coro(_txs)
        except Exception:
            logger.exception("tastytrade broker: get_today_fills failed")
            return []
        out: list[dict[str, Any]] = []
        for t in txs:
            price = getattr(t, "price", None)
            if price is None:
                continue  # non-fill ledger entries (fees, dividends, etc.)
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            out.append({
                "symbol": str(getattr(t, "symbol", "") or ""),
                "action": str(getattr(t, "action", "") or ""),
                "price": price,
                "quantity": float(getattr(t, "quantity", 0) or 0),
                "executed_at": str(getattr(t, "executed_at", "") or ""),
            })
        return out
