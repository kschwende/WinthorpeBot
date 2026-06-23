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


class DryRunBroker:
    """No-op broker. Logs intent, returns simulated-shaped dicts, sends nothing."""

    def place_order(self, decision) -> dict[str, Any]:
        logger.info(
            "[DRY RUN] place_order %s %s x%s %s occ=%s",
            decision.strategy, decision.direction, decision.size,
            decision.entry_type, (decision.metadata or {}).get("occ_symbol"),
        )
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
        return {
            "complex_order_id": f"DRYRUN-OCO-{_now_iso()}",
            "status": "simulated", "order_placed_at": _now_iso(),
            "broker": "dryrun", "dry_run": True,
        }

    def place_protective_stop(self, occ_symbol, contracts, sl_price) -> dict[str, Any]:
        logger.info("[DRY RUN] place_protective_stop %s x%s @ %s",
                    occ_symbol, contracts, sl_price)
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
        return []

    def get_account_balance(self) -> dict[str, Any]:
        return {
            "net_liquidating_value": None, "equity_buying_power": None,
            "maintenance_requirement": None, "used_buying_power": None,
        }

    def get_today_fills(self) -> list[dict[str, Any]]:
        return []


def create_broker():
    """Return a live broker iff WINTHORPE_LIVE=1, else a DryRunBroker."""
    if is_live():
        from winthorpe.broker.tastytrade_broker import TastytradeBroker
        logger.warning("create_broker: LIVE tastytrade adapter (WINTHORPE_LIVE=1)")
        return TastytradeBroker()
    logger.info("create_broker: DryRunBroker (WINTHORPE_LIVE != 1)")
    return DryRunBroker()
