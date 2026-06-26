"""Cap-prediction calibration — pure scorer + summary, no network."""

from winthorpe.levels.cap_calibration import (
    append_calibration,
    build_calibration_row,
    score_prediction,
    summarize,
)


def test_score_resistance_filled_with_overshoot():
    # 6/26 with the CORRECT trigger (7388): high 7392 fills it, ran +4 past it,
    # stalled 2 short of the expected turn (7394).
    s = score_prediction(predicted_trigger=7388, predicted_turn=7394,
                         actual_extreme=7392, side="resistance")
    assert s["filled"] is True
    assert s["overshoot"] == 4.0
    assert s["turn_error"] == -2.0


def test_score_resistance_missed_with_bad_trigger():
    # 6/26 as actually traded: trigger parked at 7397, high only 7392 → no fill.
    s = score_prediction(predicted_trigger=7397, predicted_turn=7400,
                         actual_extreme=7392, side="resistance")
    assert s["filled"] is False


def test_build_row_has_offset_and_score():
    row = build_calibration_row(
        date="2026-06-26", side="resistance", morning_wall=7400,
        predicted_trigger=7388, predicted_turn=7394, actual_extreme=7392,
        charm_window=True)
    assert row["wall_minus_extreme"] == 8.0          # the real offset truth
    assert row["score"]["filled"] is True


def test_summarize_offsets_and_small_sample_caveat(tmp_path):
    path = tmp_path / "calib.jsonl"
    for d, wall, high in [("d1", 7400, 7392), ("d2", 7450, 7438), ("d3", 7500, 7491)]:
        append_calibration(build_calibration_row(
            date=d, side="resistance", morning_wall=wall,
            predicted_trigger=high - 4, predicted_turn=high - 1,
            actual_extreme=high, charm_window=True), path=path)
    out = summarize(path, min_n=10)
    assert out["n"] == 3
    assert out["all"]["offset_median"] == 9.0        # offsets: 8, 12, 9 -> median 9
    assert out["all"]["fill_rate"] == 1.0
    assert "need >= 10" in out["note"]                # small-sample caveat fires
