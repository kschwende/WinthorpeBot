"""Expected cap / floor — pre-compute where a rally (or selloff) turns, BEFORE
price gets there, so a fade trigger goes at the right level from the start
instead of being discovered after 2-3 tests.

WHY THE CAP SITS BELOW THE CALL WALL (not at it)
------------------------------------------------
A call wall is dealer resistance: dealers are short delta above the strike and
sell futures as price *rises toward* it. So the selling builds on the APPROACH
and price stalls at the LEADING EDGE of the overhead resistance cluster — a few
points under the wall — not at the printed strike. (Karsan / Kai Volatility
dealer-flow mechanics; see OB1 "Dealer Flow & Gamma Mechanics".)

Live cost of getting this wrong, 3x over 6/25-6/26: a fade trigger parked at the
pristine wall/round number missed the fill while the target move happened a few
points below it. 6/26 (clearest): trigger 7397, cap formed ~7392, price rejected
~20 pts — never filled. (See OB1 / memory feedback_reachable_level_over_pristine.)

THE METHOD (confluence — Tier 1)
--------------------------------
The cap is the leading edge of the tightest cluster of overhead levels at/below
the call wall:
  resistance candidates = call wall + ONH + PDH + weekly/session VWAP (if above)
                        + today's VAH + RTH high + gamma flip (if above)
1. Cluster the candidates (greedy, ``cluster_tol`` points).
2. Operative cluster = the LOWEST cluster that is significant — >=2 members
   (real confluence) OR contains the wall (the wall is significant alone).
3. ``trigger`` = the NEAR edge (first the rally touches = cluster low) — this is
   the fix for the repeated misses: fill on first contact, don't park above.
   ``expected_turn`` = cluster center (where it likely stalls).
   ``invalidation_beyond`` = the FAR edge / wall (acceptance past it = thesis dead).

The floor (support, for breakdown/bounce reads) is the mirror: put wall + PDL +
ONL + VAL + RTH low + VWAP/POC below, near edge = cluster HIGH (first touched
coming down).

CONVICTION: dealer defense is mechanical in the charm windows (open 09:30-11:30,
close 15:15-16:00 ET); midday the wall drifts and a computed cap is lower
conviction. Anchor the call wall to the MORNING OI snapshot — intraday wall
migrations track price, they don't predict it.

LIMITATION: this is a CONFLUENCE model. A gamma-weighted cap (the strike where
cumulative dealer gamma-resistance actually peaks) is Tier 3 — it needs the full
per-strike GEX curve, which ``build_gex_result`` already computes (its ``levels``
list) but ``get_gex`` strips to the walls. ``compute_turn`` takes an optional
``gamma_by_strike`` hook so Tier 3 can refine the center without a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Charm re-hedging windows (ET) where dealer wall defense is mechanical.
_CHARM_WINDOWS = ((time(9, 30), time(11, 30)), (time(15, 15), time(16, 0)))

DEFAULT_CLUSTER_TOL = 8.0   # ~0.1% of SPX — points within which levels "cluster"


@dataclass
class TurnZone:
    """One resistance/support zone and the levels that build it."""
    side: str                       # "resistance" | "support"
    trigger: float                  # NEAR edge — first the move touches (fade entry)
    expected_turn: float            # cluster center — where it likely stalls
    invalidation_beyond: float      # FAR edge / wall — acceptance past = thesis dead
    members: list[dict] = field(default_factory=list)   # [{name, price}]
    anchor: Optional[str] = None    # which member is the wall, if any
    significant: bool = True        # >=2 members or contains the wall

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "trigger": round(self.trigger, 2),
            "expected_turn": round(self.expected_turn, 2),
            "invalidation_beyond": round(self.invalidation_beyond, 2),
            "members": self.members,
            "anchor": self.anchor,
            "significant": self.significant,
        }


def _clusters(prices: list[float], tol: float) -> list[list[float]]:
    """Greedy 1-D clustering: sorted ascending, break a cluster when the gap to
    the previous price exceeds ``tol``."""
    out: list[list[float]] = []
    cur: list[float] = []
    for p in sorted(prices):
        if cur and p - cur[-1] > tol:
            out.append(cur)
            cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    return out


def in_charm_window(now_et: datetime) -> bool:
    """True if ``now_et`` is inside an open/close charm window (higher-conviction
    wall defense)."""
    t = now_et.timetz().replace(tzinfo=None)
    return any(lo <= t < hi for lo, hi in _CHARM_WINDOWS)


def compute_turn(
    spot: float,
    candidates: dict[str, float],
    *,
    side: str,
    anchor_name: Optional[str] = None,
    cluster_tol: float = DEFAULT_CLUSTER_TOL,
    gamma_by_strike: Optional[dict[float, float]] = None,
) -> Optional[TurnZone]:
    """Find the operative turn zone from named ``candidates`` (label -> price).

    side="resistance": candidates strictly ABOVE spot; the move rises into them,
        so the NEAR edge (first touch) is the cluster LOW and the trigger.
    side="support": candidates strictly BELOW spot; the move falls into them, so
        the NEAR edge is the cluster HIGH.

    Operative cluster = the one NEAREST spot that is significant (>=2 members, or
    contains ``anchor_name`` — the wall). Lone non-wall levels nearer than that
    are skipped (a rally slices through a single minor level; it stalls at the
    cluster / wall). Falls back to the anchor's own cluster if nothing else
    qualifies, else None.

    ``gamma_by_strike`` (Tier 3 hook): if given, the expected_turn is nudged
    toward the gamma-weighted center of the cluster's strikes instead of the
    plain mean. Unused in Tier 1.
    """
    if side not in ("resistance", "support"):
        raise ValueError(f"side must be 'resistance' or 'support', got {side!r}")
    rising = side == "resistance"

    # Levels on the operative side of spot, with their names.
    named = {n: p for n, p in candidates.items()
             if p is not None and (p > spot if rising else p < spot)}
    if not named:
        return None

    price_to_names: dict[float, list[str]] = {}
    for n, p in named.items():
        price_to_names.setdefault(round(float(p), 4), []).append(n)

    clusters = _clusters(list(price_to_names), cluster_tol)
    # Order clusters by nearness to spot (nearest first).
    clusters.sort(key=lambda c: min(abs(x - spot) for x in c))

    anchor_price = named.get(anchor_name) if anchor_name else None

    def _significant(cluster: list[float]) -> bool:
        n_members = sum(len(price_to_names[round(p, 4)]) for p in cluster)
        has_anchor = anchor_price is not None and any(
            abs(p - anchor_price) < 1e-6 for p in cluster)
        return n_members >= 2 or has_anchor

    operative = next((c for c in clusters if _significant(c)), None)
    if operative is None:
        return None

    members = []
    for p in sorted(operative):
        for n in price_to_names[round(p, 4)]:
            members.append({"name": n, "price": round(p, 2)})
    member_prices = [m["price"] for m in members]

    lo, hi = min(member_prices), max(member_prices)
    center = sum(member_prices) / len(member_prices)
    if gamma_by_strike:  # Tier 3 refinement: gamma-weighted center
        wsum = w = 0.0
        for p in member_prices:
            g = abs(gamma_by_strike.get(round(p), 0.0))
            wsum += p * g
            w += g
        if w > 0:
            center = wsum / w

    near_edge = lo if rising else hi          # first the move touches
    far_edge = hi if rising else lo           # acceptance past here = dead
    anchor_hit = next((m["name"] for m in members
                       if anchor_price is not None
                       and abs(m["price"] - anchor_price) < 1e-6), None)

    return TurnZone(
        side=side,
        trigger=near_edge,
        expected_turn=center,
        invalidation_beyond=far_edge,
        members=members,
        anchor=anchor_hit,
        significant=_significant(operative),
    )


def gamma_resistance_cap(spot: float, levels: list[dict],
                         *, upper: Optional[float] = None) -> Optional[float]:
    """Tier 3 — the call-GEX-weighted CENTROID of overhead strikes in
    ``(spot, upper]``: the centre of mass of dealer sell-resistance, which sits
    BELOW the single max-GEX strike when gamma is spread under the wall.

    ``levels`` is the GEX engine's per-strike list ({strike, call_gex, ...}).
    call_gex is positive (dealers sell into rises). Returns None if no overhead
    call-gex. ``upper`` defaults to no cap (use the call wall to bound it)."""
    hi = upper if upper is not None else float("inf")
    zone = [l for l in levels
            if l.get("strike") is not None and spot < l["strike"] <= hi
            and (l.get("call_gex") or 0) > 0]
    w = sum(l["call_gex"] for l in zone)
    if w <= 0:
        return None
    return round(sum(l["strike"] * l["call_gex"] for l in zone) / w, 2)


def gamma_support_floor(spot: float, levels: list[dict],
                        *, lower: Optional[float] = None) -> Optional[float]:
    """Mirror of :func:`gamma_resistance_cap` for the downside — the
    |put-GEX|-weighted centroid of strikes in ``[lower, spot)`` (put_gex is
    negative in this engine's convention, so weight by magnitude)."""
    lo = lower if lower is not None else float("-inf")
    zone = [l for l in levels
            if l.get("strike") is not None and lo <= l["strike"] < spot
            and abs(l.get("put_gex") or 0) > 0]
    w = sum(abs(l["put_gex"]) for l in zone)
    if w <= 0:
        return None
    return round(sum(l["strike"] * abs(l["put_gex"]) for l in zone) / w, 2)


def structural_sl_pct(
    *,
    near_edge: float,
    far_edge: float,
    option_delta: float,
    option_premium: float,
    buffer: float = 0.20,
    band: tuple[float, float] = (-0.50, -0.20),
) -> Optional[dict]:
    """Derive a premium stop wide enough to SURVIVE a normal push from the fade
    entry (``near_edge``) to the wall (``far_edge``), so the in-zone push doesn't
    stop a good fade out before the structural invalidation (acceptance past the
    wall) can fire.

    A leading-edge entry sits BELOW the wall, so a normal rally through the
    cluster moves against the put by ``|far_edge - near_edge|`` points. The
    first-order premium hit is ``|delta| * adverse_pts / premium`` (using entry
    delta overstates it slightly since the put goes OTM into the wall — a
    conservative, intentionally-not-too-tight estimate). Widen by ``buffer`` and
    clamp to ``band`` (default: never tighter than -20%, never wider than -50%).

    The premium stop is the BACKSTOP; the real kill stays the plan's invalidation
    (accept above the wall / 60s). Returns {sl_pct, adverse_pts,
    est_loss_pct_at_wall, clamped}, or None if premium is non-positive.
    """
    if option_premium <= 0:
        return None
    adverse_pts = abs(far_edge - near_edge)
    loss_at_wall = abs(option_delta) * adverse_pts / option_premium
    raw = -loss_at_wall * (1.0 + buffer)
    lo, hi = band                      # e.g. (-0.50, -0.20)
    sl = max(lo, min(hi, raw))
    return {
        "sl_pct": round(sl, 3),
        "adverse_pts": round(adverse_pts, 2),
        "est_loss_pct_at_wall": round(loss_at_wall, 3),
        "clamped": round(sl, 3) != round(raw, 3),
    }


def expected_levels(
    *,
    spot: float,
    gex: dict,
    structural: dict | None = None,
    vp_spx: dict | None = None,
    now_et: Optional[datetime] = None,
    cluster_tol: float = DEFAULT_CLUSTER_TOL,
) -> dict:
    """Assemble resistance/support candidates from live desk reads and return the
    expected cap + floor.

    ``gex``: the get_gex dict (``call_wall``/``put_wall`` as {strike, ...}, and
        optionally ``gamma_flip``/``flip_point``).
    ``structural``: StructuralLevels.to_dict() (pdh/onh/weekly_vwap/... in SPX).
    ``vp_spx``: the get_volume_profile ``spx`` sub-dict ({poc, val, vah}), SPX terms.

    Returns {"spot", "cap", "floor", "charm_window", "note"} where cap/floor are
    TurnZone dicts (or None if nothing significant on that side).
    """
    s = structural or {}
    vp = vp_spx or {}
    cw = (gex.get("call_wall") or {}).get("strike") if gex else None
    pw = (gex.get("put_wall") or {}).get("strike") if gex else None
    flip = gex.get("gamma_flip") or gex.get("flip_point") if gex else None

    # Resistance candidates (overhead). Only meaningful ones; None auto-dropped.
    resistance = {
        "call_wall": cw,
        "ONH": s.get("onh"),
        "PDH": s.get("pdh"),
        "weekly_vwap": s.get("weekly_vwap"),
        "session_vwap": s.get("session_vwap"),
        "RTH_high": s.get("rth_high"),
        "VAH": vp.get("vah"),
        "POC": vp.get("poc"),
        "gamma_flip": flip,
    }
    support = {
        "put_wall": pw,
        "PDL": s.get("pdl"),
        "ONL": s.get("onl"),
        "weekly_vwap": s.get("weekly_vwap"),
        "session_vwap": s.get("session_vwap"),
        "RTH_low": s.get("rth_low"),
        "VAL": vp.get("val"),
        "POC": vp.get("poc"),
        "gamma_flip": flip,
    }
    resistance = {k: v for k, v in resistance.items() if v is not None}
    support = {k: v for k, v in support.items() if v is not None}

    cap = compute_turn(spot, resistance, side="resistance",
                       anchor_name="call_wall", cluster_tol=cluster_tol)
    floor = compute_turn(spot, support, side="support",
                         anchor_name="put_wall", cluster_tol=cluster_tol)

    cap_dict = cap.to_dict() if cap else None
    floor_dict = floor.to_dict() if floor else None

    # Tier 3: if the full per-strike GEX curve is present, attach the
    # gamma-weighted turn (centre of mass of dealer resistance) alongside the
    # confluence turn. Bounded by the wall so it stays in the approach zone.
    curve = gex.get("levels") if gex else None
    if curve:
        strikes = [l for l in curve if l.get("strike") is not None]
        if cap_dict:
            gt = gamma_resistance_cap(spot, curve, upper=cw)
            if gt is not None:
                cap_dict["gamma_turn"] = gt
            # Structural stop for a PUT fade entered at the cap's near edge:
            # size sl_pct to survive a push to the wall, using the near-ATM put.
            atm = min(strikes, key=lambda l: abs(l["strike"] - cap_dict["trigger"]),
                      default=None)
            if atm and (atm.get("put_price") or 0) > 0:
                ssl = structural_sl_pct(
                    near_edge=cap_dict["trigger"],
                    far_edge=cap_dict["invalidation_beyond"],
                    option_delta=atm.get("put_delta") or 0.5,
                    option_premium=atm["put_price"])
                if ssl:
                    ssl["est_strike"] = atm["strike"]
                    cap_dict["suggested_sl_pct"] = ssl
        if floor_dict:
            gf = gamma_support_floor(spot, curve, lower=pw)
            if gf is not None:
                floor_dict["gamma_turn"] = gf

    charm = in_charm_window(now_et) if now_et else None
    note = (
        "Fade trigger = cap.trigger (first touch of the overhead cluster), NOT "
        "the wall above it. expected_turn = confluence centre; gamma_turn (if "
        "present) = GEX centre of mass. Conviction higher inside a charm window."
    )
    if charm is False:
        note += " WARNING: outside the charm window — wall defense is lighter, "\
                "lower conviction."
    return {
        "spot": round(spot, 2),
        "cap": cap_dict,
        "floor": floor_dict,
        "charm_window": charm,
        "note": note,
    }
