"""Expected cap / floor — pure confluence math, no network.

Anchored on the 6/26 live miss: trigger was parked at 7397, the cap formed at
~7392 (ONH 7388 + weekly VWAP 7395 just under the 7400 call wall), price
rejected ~20 pts and we never filled. The calculator must put the trigger at
~7388 (near edge of that cluster) so it would have filled.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from winthorpe.levels.expected_cap import (
    _clusters,
    compute_turn,
    expected_levels,
    in_charm_window,
)

ET = ZoneInfo("America/New_York")


def test_clusters_breaks_on_gap():
    assert _clusters([7388, 7395, 7400, 7419], tol=8.0) == [[7388, 7395, 7400], [7419]]
    assert _clusters([7300], tol=8.0) == [[7300]]


def test_resistance_cap_6_26_worked_example():
    # spot below the shelf; ONH+wVWAP+wall cluster overhead, PDH far above.
    cap = compute_turn(
        7376.0,
        {"ONH": 7388, "weekly_vwap": 7395, "call_wall": 7400, "PDH": 7419},
        side="resistance",
        anchor_name="call_wall",
    )
    assert cap is not None
    # Trigger = NEAR edge (first touch) = cluster low — the fix for the miss.
    assert cap.trigger == 7388
    # Expected turn ~ cluster center ~ where it actually capped (7392-ish).
    assert 7393 <= cap.expected_turn <= 7396
    # Acceptance beyond the wall kills it.
    assert cap.invalidation_beyond == 7400
    assert cap.anchor == "call_wall"
    assert {m["name"] for m in cap.members} == {"ONH", "weekly_vwap", "call_wall"}
    # 7397 (what we used) sits ABOVE the trigger and never filled — the lesson.
    assert cap.trigger < 7397


def test_resistance_skips_lone_minor_level_below_cluster():
    # A single non-wall level nearer spot is sliced through; cap = the cluster.
    cap = compute_turn(
        7376.0,
        {"session_vwap": 7378, "ONH": 7388, "weekly_vwap": 7395, "call_wall": 7400},
        side="resistance",
        anchor_name="call_wall",
    )
    assert cap is not None
    assert cap.trigger == 7388          # not 7378
    assert "session_vwap" not in {m["name"] for m in cap.members}


def test_lone_wall_is_significant_on_its_own():
    cap = compute_turn(
        7376.0,
        {"call_wall": 7400},
        side="resistance",
        anchor_name="call_wall",
    )
    assert cap is not None
    assert cap.trigger == 7400
    assert cap.anchor == "call_wall"


def test_support_floor_is_mirror_near_edge_is_cluster_high():
    floor = compute_turn(
        7376.0,
        {"put_wall": 7350, "session_vwap": 7347, "VAL": 7304},
        side="support",
        anchor_name="put_wall",
    )
    assert floor is not None
    # Falling into support: first touch = cluster HIGH.
    assert floor.trigger == 7350
    assert floor.invalidation_beyond == 7347      # far edge = cluster low
    assert 7348 <= floor.expected_turn <= 7349


def test_no_candidates_on_side_returns_none():
    # Spot above everything overhead → no resistance.
    assert compute_turn(7500.0, {"call_wall": 7400},
                        side="resistance", anchor_name="call_wall") is None


def test_gamma_weighted_center_hook():
    # Tier-3 hook: gamma concentrated at 7400 pulls the center up toward it.
    cap = compute_turn(
        7376.0,
        {"ONH": 7388, "weekly_vwap": 7395, "call_wall": 7400},
        side="resistance",
        anchor_name="call_wall",
        gamma_by_strike={7388: 1.0, 7395: 1.0, 7400: 8.0},
    )
    assert cap.expected_turn > 7396     # vs plain mean 7394.3


def test_charm_window():
    assert in_charm_window(datetime(2026, 6, 26, 10, 0, tzinfo=ET)) is True
    assert in_charm_window(datetime(2026, 6, 26, 15, 30, tzinfo=ET)) is True
    assert in_charm_window(datetime(2026, 6, 26, 12, 30, tzinfo=ET)) is False


def test_expected_levels_end_to_end_6_26():
    out = expected_levels(
        spot=7376.0,
        gex={"call_wall": {"strike": 7400, "oi": 5542},
             "put_wall": {"strike": 7350, "oi": 3114}},
        structural={"onh": 7388, "pdh": 7419, "weekly_vwap": 7395,
                    "rth_high": 7370, "pdl": 7323, "session_vwap": 7334},
        vp_spx={"poc": 7313, "val": 7304, "vah": 7354},
        now_et=datetime(2026, 6, 26, 10, 30, tzinfo=ET),
    )
    assert out["cap"]["trigger"] == 7388        # would have filled (vs our 7397)
    assert out["cap"]["invalidation_beyond"] == 7400
    assert out["floor"]["trigger"] == 7350      # put wall = first support below
    assert out["charm_window"] is True


# --- Tier 3: gamma-weighted cap/floor from the per-strike GEX curve ----------

_CURVE = [
    {"strike": 7385, "call_gex": 1e6, "put_gex": -1e6},
    {"strike": 7390, "call_gex": 3e6, "put_gex": -2e6},
    {"strike": 7395, "call_gex": 5e6, "put_gex": 0},
    {"strike": 7400, "call_gex": 8e6, "put_gex": 0},
    {"strike": 7405, "call_gex": 2e6, "put_gex": 0},   # above the wall → excluded
    {"strike": 7350, "call_gex": 0, "put_gex": -8e6},
    {"strike": 7345, "call_gex": 0, "put_gex": -3e6},
]


def test_gamma_resistance_cap_centroid_below_wall():
    from winthorpe.levels.expected_cap import gamma_resistance_cap
    c = gamma_resistance_cap(7376.0, _CURVE, upper=7400)
    assert 7393 <= c <= 7399          # GEX centre of mass, below the 7400 wall
    assert c < 7400


def test_gamma_support_floor_centroid_above_lower():
    from winthorpe.levels.expected_cap import gamma_support_floor
    f = gamma_support_floor(7376.0, _CURVE, lower=7340)
    assert 7345 <= f <= 7350          # |put-GEX| weighted, near the 7350 wall


def test_expected_levels_attaches_gamma_turn_when_curve_present():
    out = expected_levels(
        spot=7376.0,
        gex={"call_wall": {"strike": 7400}, "put_wall": {"strike": 7350},
             "levels": _CURVE},
        structural={"onh": 7388, "weekly_vwap": 7395, "pdh": 7419,
                    "pdl": 7323, "session_vwap": 7334},
        now_et=datetime(2026, 6, 26, 10, 30, tzinfo=ET),
    )
    assert out["cap"]["trigger"] == 7388            # confluence near-edge unchanged
    assert "gamma_turn" in out["cap"]               # gamma layer attached
    assert out["cap"]["gamma_turn"] < 7400


# --- structural stop: size sl_pct to survive a push to the wall --------------

def test_structural_sl_pct_widens_for_leading_edge_entry():
    from winthorpe.levels.expected_cap import structural_sl_pct
    # Entry 7388, wall 7400 (12 pts), ATM put delta 0.5 @ $20:
    # loss to the wall ~30%, +20% buffer -> ~-36% (NOT -25%, which the push trips).
    s = structural_sl_pct(near_edge=7388, far_edge=7400,
                          option_delta=-0.5, option_premium=20.0)
    assert s["adverse_pts"] == 12.0
    assert s["est_loss_pct_at_wall"] == 0.3
    assert s["sl_pct"] == -0.36
    assert s["sl_pct"] < -0.25          # wider than the old blanket stop


def test_structural_sl_pct_lone_wall_uses_tight_default():
    from winthorpe.levels.expected_cap import structural_sl_pct
    # near == far (entry AT the wall, no zone): no adverse room -> clamp to -0.20.
    s = structural_sl_pct(near_edge=7400, far_edge=7400,
                          option_delta=-0.5, option_premium=20.0)
    assert s["sl_pct"] == -0.20
    assert s["clamped"] is True


def test_structural_sl_pct_clamps_wide():
    from winthorpe.levels.expected_cap import structural_sl_pct
    # Huge adverse room would imply a >50% stop -> clamp to the -0.50 floor.
    s = structural_sl_pct(near_edge=7350, far_edge=7400,
                          option_delta=-0.5, option_premium=20.0)
    assert s["sl_pct"] == -0.50
    assert s["clamped"] is True


def test_expected_levels_attaches_suggested_sl_pct():
    curve = [
        {"strike": 7388, "put_delta": -0.5, "put_price": 20.0,
         "call_gex": 3e6, "put_gex": -1e6},
        {"strike": 7400, "put_delta": -0.4, "put_price": 14.0,
         "call_gex": 8e6, "put_gex": 0},
    ]
    out = expected_levels(
        spot=7376.0,
        gex={"call_wall": {"strike": 7400}, "put_wall": {"strike": 7350},
             "levels": curve},
        structural={"onh": 7388, "weekly_vwap": 7395, "pdh": 7419},
        now_et=datetime(2026, 6, 26, 10, 30, tzinfo=ET),
    )
    sl = out["cap"]["suggested_sl_pct"]
    assert sl["sl_pct"] == -0.36          # entry 7388 -> wall 7400, ~36%
    assert sl["est_strike"] == 7388
