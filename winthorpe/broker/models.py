"""Order/position models the broker consumes.

Trimmed from upstream-execution's models.py to the fields the SPXW option path
actually uses. WinthorpeBot trades one instrument — long single-leg SPXW
calls/puts — so the multi-strategy/futures fields are gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TradeDecision:
    """What to do — the broker's input.

    For WinthorpeBot this is always an SPXW option action. ``metadata`` must
    carry ``occ_symbol`` (the resolved OCC) for option orders.

    direction:  BUY_TO_OPEN_CALL / BUY_TO_OPEN_PUT / BUY_TO_OPEN / SELL_TO_CLOSE
    entry_type: OPTION_MARKET (immediate fill) | OPTION_LIMIT (standing limit)
    """

    strategy: str                          # plan id / label (audit only)
    product: str                           # "SPXW"
    direction: str                         # see action map in the broker
    size: int                              # contracts
    entry_type: str                        # OPTION_MARKET | OPTION_LIMIT
    entry_price: Optional[float] = None    # required for OPTION_LIMIT
    stop_price: Optional[float] = None     # option-price stop (informational here)
    target_price: Optional[float] = None   # option-price target (informational)
    reason: str = ""
    confidence: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """A tracked open SPXW option position."""

    id: str = ""
    product: str = "SPXW"
    occ_symbol: str = ""
    right: str = ""              # "C" / "P"
    strike: float = 0.0
    expiry: str = ""            # YYYY-MM-DD
    size: int = 0
    entry_price: float = 0.0   # option premium paid
    entry_time: str = ""
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    oco_complex_order_id: Optional[str] = None
    plan_id: str = ""
    pnl_unrealized: float = 0.0
    status: str = "OPEN"        # OPEN / CLOSED / STOPPED
