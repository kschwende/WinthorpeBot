"""Export the play journal as pre-registered calls for VPEBot's research/grade_calls.py.

    python -m winthorpe.journal.export_calls [--journal-dir DIR] [--out calls.csv] [--target-r 1.5]

One row per distinct plan (re-arms of the same trigger/side/day within the session collapse to the
first signing). Signed plans -> taken=1; proposed-but-never-signed drafts -> taken=0 (declined).
Theses that start with "VALIDATION" are engine smoke tests and are skipped.

Columns (the grader's schema):
    ts_et, symbol, dir, trigger, stop, target, horizon_min, tag, conviction, mechanical_set, taken
`stop` is the plan's invalidation level (SPX). The journal has no structured SPX target (targets live in
the thesis text), so `target` = trigger + dir * TARGET_R * |trigger - stop|; pass --target-r to change the
assumed reward multiple. That is a fixed geometry applied to every call, so the comparison between calls
and controls is fair even though it is not the trader's literal target.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from winthorpe import config

ET = ZoneInfo("America/New_York")
COLUMNS = ["ts_et", "symbol", "dir", "trigger", "stop", "target", "horizon_min", "tag", "conviction",
           "mechanical_set", "taken", "cancelled"]


def _rows(journal_dir: Path) -> list[dict]:
    out = []
    for path in sorted(journal_dir.glob("plays-*.jsonl")):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _call(row: dict, taken: int, target_r: float) -> dict | None:
    plan = row.get("plan") or {}
    thesis = (plan.get("thesis") or "").strip()
    if thesis.upper().startswith("VALIDATION"):
        return None
    trig, inv = plan.get("trigger") or {}, plan.get("invalidation") or {}
    if trig.get("level") is None:
        return None
    ts = datetime.fromisoformat(row["ts"]).astimezone(ET)
    d = 1 if str(plan.get("side", "")).upper() == "CALL" else -1
    trigger = float(trig["level"])
    stop = float(inv["level"]) if inv.get("level") is not None else trigger - d * 0.003 * trigger
    risk = abs(trigger - stop)
    hh, mm = (plan.get("time_stop_et") or "15:45").split(":")
    end = ts.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    horizon = max(1, int((end - ts).total_seconds() // 60))
    return {"ts_et": ts.strftime("%Y-%m-%d %H:%M"), "symbol": trig.get("symbol", "SPX"), "dir": d,
            "trigger": trigger, "stop": round(stop, 2), "target": round(trigger + d * target_r * risk, 2),
            "horizon_min": horizon, "tag": thesis[:60].replace("\n", " "),
            "conviction": int(plan.get("min_contracts") or 1), "mechanical_set": "", "taken": taken}


def export(journal_dir: Path, target_r: float = 1.5) -> list[dict]:
    rows = _rows(journal_dir)
    signed = [r for r in rows if r.get("kind") == "plan_signed"]
    proposed = [r for r in rows if r.get("kind") == "plan_proposed"]
    # a plan the human cancelled before the trigger printed: the grader still scores the call as
    # written (it tests the read), but the flag lets you see how often the hand overrode the plan
    cancelled_days: dict[str, int] = {}
    for r in rows:
        if r.get("kind") == "event" and r.get("event") == "cancelled_before_entry":
            day = datetime.fromisoformat(r["ts"]).astimezone(ET).strftime("%Y-%m-%d")
            cancelled_days[day] = cancelled_days.get(day, 0) + 1
    calls: list[dict] = []
    seen: set[tuple] = set()
    for r in signed:
        c = _call(r, 1, target_r)
        if c is None:
            continue
        key = (c["ts_et"][:10], c["symbol"], c["dir"], c["trigger"])
        if key in seen:
            continue              # re-arm of the same plan
        seen.add(key)
        c["cancelled"] = int(cancelled_days.get(c["ts_et"][:10], 0) > 0)
        calls.append(c)
    for r in proposed:
        c = _call(r, 0, target_r)
        if c is None:
            continue
        key = (c["ts_et"][:10], c["symbol"], c["dir"], c["trigger"])
        if key in seen:
            continue              # this draft was signed
        seen.add(key)
        c["cancelled"] = 0
        calls.append(c)
    calls.sort(key=lambda c: c["ts_et"])
    return calls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal-dir", default=str(config.JOURNAL_DIR))
    ap.add_argument("--out", default="calls.csv")
    ap.add_argument("--target-r", type=float, default=1.5)
    args = ap.parse_args()
    calls = export(Path(args.journal_dir), args.target_r)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(calls)
    print(f"{len(calls)} calls -> {args.out} (taken {sum(c['taken'] for c in calls)}, "
          f"declined {sum(1 - c['taken'] for c in calls)})")


if __name__ == "__main__":
    main()
