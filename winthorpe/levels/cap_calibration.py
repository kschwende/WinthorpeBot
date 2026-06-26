"""Cap-prediction calibration — accumulate (predicted cap vs actual extreme)
per session so the Tier-1 confluence model can be MEASURED and tuned.

Why this exists: the wall->cap offset can't be calibrated from the existing GEX
history — only 5 days, 2-8 sparse snapshots each, no session-high pairing. The
6/26 proxy read 40.8 pts of offset vs the known truth of 8 (the snapshots missed
the real high). So we log the right pairs forward: once per session near the
close, record the MORNING OI-anchored call wall, the predicted cap, and the
actual RTH high (the realized cap). After ~15-20 sessions, ``summarize`` reports
the real offset distribution and the model's hit/turn-error — split by charm
window. Symmetric for the floor (put wall vs RTH low).

This is the data-collection half of "Tier 2". The pure scorer below is tested;
``record_session`` does the live fetch + append.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from winthorpe.config import STATE_DIR

CALIB_FILE = STATE_DIR / "logs" / "cap_calibration.jsonl"


def score_prediction(
    *, predicted_trigger: float, predicted_turn: float, actual_extreme: float,
    side: str = "resistance",
) -> dict:
    """Did the prediction work? For a resistance cap, ``actual_extreme`` is the
    session RTH high.

    filled       — would a fade trigger at ``predicted_trigger`` have FILLED
                   (did price reach it)? resistance: high >= trigger.
    turn_error   — signed (actual_extreme - predicted_turn): + means price ran
                   PAST where we expected it to stall (cap too low), - means it
                   stalled short (cap too high).
    overshoot    — how far past the trigger price actually ran (resistance:
                   high - trigger); the room a fade entered at the trigger gave up
                   before the turn.
    """
    rising = side == "resistance"
    filled = actual_extreme >= predicted_trigger if rising else actual_extreme <= predicted_trigger
    turn_error = actual_extreme - predicted_turn          # sign is meaningful
    overshoot = (actual_extreme - predicted_trigger) if rising else (predicted_trigger - actual_extreme)
    return {
        "filled": bool(filled),
        "turn_error": round(turn_error, 2),
        "overshoot": round(overshoot, 2),
    }


def build_calibration_row(
    *, date: str, side: str, morning_wall: Optional[float],
    predicted_trigger: Optional[float], predicted_turn: Optional[float],
    actual_extreme: Optional[float], charm_window: Optional[bool],
) -> dict:
    row = {
        "date": date, "side": side, "morning_wall": morning_wall,
        "predicted_trigger": predicted_trigger, "predicted_turn": predicted_turn,
        "actual_extreme": actual_extreme, "charm_window": charm_window,
        "wall_minus_extreme": (round(morning_wall - actual_extreme, 2)
                               if (morning_wall is not None and actual_extreme is not None) else None),
    }
    if None not in (predicted_trigger, predicted_turn, actual_extreme):
        row["score"] = score_prediction(
            predicted_trigger=predicted_trigger, predicted_turn=predicted_turn,
            actual_extreme=actual_extreme, side=side)
    return row


def append_calibration(row: dict, *, path: Path = CALIB_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def summarize(path: Path = CALIB_FILE, *, min_n: int = 10) -> dict:
    """Offset distribution + hit-rate from accumulated rows. Reports a caveat
    until at least ``min_n`` rows exist — small samples don't calibrate."""
    if not Path(path).exists():
        return {"n": 0, "note": "no calibration rows yet — run record_session near each close"}
    rows = [json.loads(l) for l in open(path) if l.strip()]
    caps = [r for r in rows if r.get("side") == "resistance" and r.get("wall_minus_extreme") is not None]

    def _stats(rs):
        offs = sorted(r["wall_minus_extreme"] for r in rs)
        scored = [r["score"] for r in rs if r.get("score")]
        n = len(offs)
        return {
            "n": n,
            "offset_median": offs[n // 2] if n else None,
            "offset_min": offs[0] if n else None,
            "offset_max": offs[-1] if n else None,
            "fill_rate": round(sum(s["filled"] for s in scored) / len(scored), 2) if scored else None,
        }

    charm = _stats([r for r in caps if r.get("charm_window") is True])
    out = {"n": len(caps), "all": _stats(caps), "in_charm_window": charm}
    if len(caps) < min_n:
        out["note"] = (f"only {len(caps)} cap rows — need >= {min_n} before the "
                       "offset is meaningful (small-sample patience rule)")
    return out


def record_session(*, side: str = "resistance", now_et=None) -> dict:
    """Live: pull the MORNING call wall (earliest of today's gex_history), today's
    RTH high (structural), and the predicted cap (expected_levels), then append one
    calibration row. Run once near the close. Best-effort; returns the row."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from winthorpe.levels.expected_cap import in_charm_window

    et = ZoneInfo("America/New_York")
    now = now_et or datetime.now(et)
    today = now.date().isoformat()

    # Morning OI-anchored wall = earliest gex_history snapshot for today.
    morning_wall = None
    hist = STATE_DIR / "logs" / "gex_history.jsonl"
    if hist.exists():
        todays = [json.loads(l) for l in open(hist)
                  if l.strip() and json.loads(l).get("date") == today]
        if todays:
            morning_wall = sorted(todays, key=lambda r: r.get("timestamp", ""))[0].get("call_wall")

    from winthorpe.levels.structural import fetch_structural_levels
    lv = fetch_structural_levels(now)
    actual_extreme = lv.rth_high if side == "resistance" else lv.rth_low

    pred_trigger = pred_turn = None
    try:
        from winthorpe.data.gex_engine import compute_gex
        from winthorpe.levels.expected_cap import expected_levels
        import asyncio
        gex = asyncio.run(compute_gex(product="SPX"))
        if not gex.get("error"):
            el = expected_levels(spot=gex["spot"], gex=gex, structural=lv.to_dict(),
                                 vp_spx=None, now_et=now)
            zone = el.get("cap" if side == "resistance" else "floor")
            if zone:
                pred_trigger, pred_turn = zone["trigger"], zone["expected_turn"]
    except Exception:
        pass

    row = build_calibration_row(
        date=today, side=side, morning_wall=morning_wall,
        predicted_trigger=pred_trigger, predicted_turn=pred_turn,
        actual_extreme=actual_extreme, charm_window=in_charm_window(now))
    append_calibration(row)
    return row


if __name__ == "__main__":
    # Run near the RTH close (cron) to append one calibration row, or with
    # --summary to print the accumulated offset distribution + hit-rate.
    import sys

    if "--summary" in sys.argv[1:]:
        print(json.dumps(summarize(), indent=2, default=str))
    else:
        print(json.dumps(record_session(), indent=2, default=str))
