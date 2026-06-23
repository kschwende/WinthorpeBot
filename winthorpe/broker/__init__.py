"""Broker plane — factory + dry-run adapter.

``create_broker()`` is the ONLY supported way to get a broker. It returns a live
TastytradeBroker only when ``config.is_live()`` is true (WINTHORPE_LIVE=1);
otherwise a DryRunBroker that logs orders and never touches the network.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from winthorpe.config import is_live

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _extract_bracket(complex_order) -> tuple[str | None, float | None, float | None]:
    """Best-effort pull of (occ, tp_limit, sl_stop) from a NewComplexOrder so the
    paper broker can simulate the OCO against live marks. Duck-typed — works on a
    real tastytrade NewComplexOrder or any object with the same shape."""
    occ = tp = sl = None
    for order in getattr(complex_order, "orders", []) or []:
        legs = getattr(order, "legs", []) or []
        if legs and occ is None:
            occ = getattr(legs[0], "symbol", None)
        price = getattr(order, "price", None)
        stop = getattr(order, "stop_trigger", None)
        if price is not None:
            tp = float(price)
        if stop is not None:
            sl = float(stop)
    return occ, tp, sl


class DryRunBroker:
    """Paper broker. Sends nothing, but tracks simulated positions and brackets and
    fills them against the live option-mark stream so dry-run P&L is REAL (not an
    instant $0 round-trip). Pass ``market`` (anything with ``option_mark(streamer)``)
    to enable mark-based OCO/stop fill simulation; without it, positions are still
    tracked (so engine-side exits — trail / invalidation / time-stop — fire), only
    the broker-side OCO fills aren't simulated."""

    def __init__(self, market=None):
        self.market = market
        self._open: dict[str, dict[str, Any]] = {}      # occ -> {streamer, qty}
        self._brackets: dict[str, dict[str, Any]] = {}  # occ -> {tp, sl}

    def place_order(self, decision) -> dict[str, Any]:
        meta = decision.metadata or {}
        occ = meta.get("occ_symbol")
        direction = (decision.direction or "").upper()
        logger.info(
            "[DRY RUN] place_order %s %s x%s %s occ=%s",
            decision.strategy, decision.direction, decision.size,
            decision.entry_type, occ,
        )
        if occ and direction.startswith("BUY"):
            self._open[occ] = {"streamer": meta.get("streamer_symbol"),
                               "qty": int(decision.size)}
        elif occ and direction == "SELL_TO_CLOSE":
            self._open.pop(occ, None)
            self._brackets.pop(occ, None)
        return {
            "order_id": f"DRYRUN-{_now_iso()}",
            "status": "simulated",
            "order_status": "simulated",
            "order_placed_at": _now_iso(),
            "order_filled_at": _now_iso(),
            "entry_price": decision.entry_price,
            "size": decision.size,
            "broker": "dryrun",
            "symbol": decision.product,
            "dry_run": True,
        }

    def place_complex_order(self, complex_order) -> dict[str, Any]:
        logger.info("[DRY RUN] place_complex_order %s",
                    getattr(complex_order, "type", "OCO"))
        occ, tp, sl = _extract_bracket(complex_order)
        if occ is not None:
            self._brackets[occ] = {"tp": tp, "sl": sl}
        return {
            "complex_order_id": f"DRYRUN-OCO-{_now_iso()}",
            "status": "simulated", "order_placed_at": _now_iso(),
            "broker": "dryrun", "dry_run": True,
        }

    def place_protective_stop(self, occ_symbol, contracts, sl_price) -> dict[str, Any]:
        logger.info("[DRY RUN] place_protective_stop %s x%s @ %s",
                    occ_symbol, contracts, sl_price)
        if occ_symbol is not None:
            self._brackets[occ_symbol] = {"tp": None, "sl": float(sl_price)}
        return {
            "order_id": f"DRYRUN-STOP-{_now_iso()}",
            "status": "simulated", "order_placed_at": _now_iso(),
            "stop_price": sl_price, "broker": "dryrun", "dry_run": True,
        }

    def wait_for_fill(self, order_id, occ_symbol, max_attempts=5, delay_sec=1.0):
        # Dry run has no real fill — engine falls back to the option mark.
        return None

    def cancel_order(self, order_id: str) -> bool:
        logger.info("[DRY RUN] cancel_order %s", order_id)
        return True

    def cancel_complex_order(self, complex_order_id: str) -> bool:
        logger.info("[DRY RUN] cancel_complex_order %s", complex_order_id)
        return True

    def cancel_working_orders_for(self, underlying: str) -> list[str]:
        logger.info("[DRY RUN] cancel_working_orders_for %s", underlying)
        return []

    def get_positions(self) -> list[dict[str, Any]]:
        """Report held positions, simulating any OCO/stop fill against the live
        mark first. A position whose mark has crossed its TP (>=) or SL (<=) is
        treated as broker-filled and drops out — the engine then sees it 'gone'
        and books the close at the mark (real paper P&L)."""
        if self.market is not None:
            for occ in list(self._open):
                br = self._brackets.get(occ)
                streamer = self._open[occ].get("streamer")
                if not br or not streamer:
                    continue
                mark = self.market.option_mark(streamer)
                if mark is None:
                    continue
                tp, sl = br.get("tp"), br.get("sl")
                if (tp is not None and mark >= tp) or (sl is not None and mark <= sl):
                    self._open.pop(occ, None)
                    self._brackets.pop(occ, None)
        return [{"symbol": occ, "quantity": st["qty"]}
                for occ, st in self._open.items()]

    def get_account_balance(self) -> dict[str, Any]:
        return {
            "net_liquidating_value": None, "equity_buying_power": None,
            "maintenance_requirement": None, "used_buying_power": None,
        }

    def get_today_fills(self) -> list[dict[str, Any]]:
        return []


def create_broker(market=None):
    """Return a live broker iff WINTHORPE_LIVE=1, else a DryRunBroker.

    ``market`` (a view with ``option_mark(streamer)``) is handed to the paper
    broker so dry-run can simulate OCO/stop fills against live marks; the live
    adapter ignores it."""
    if is_live():
        from winthorpe.broker.tastytrade_broker import TastytradeBroker
        logger.warning("create_broker: LIVE tastytrade adapter (WINTHORPE_LIVE=1)")
        return TastytradeBroker()
    logger.info("create_broker: DryRunBroker (WINTHORPE_LIVE != 1)")
    return DryRunBroker(market=market)
