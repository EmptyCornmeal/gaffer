"""In-season calibration must be honest about its own sample before anything else.

The failure this module is built against is not an arithmetic one. It is a
correct statistic, computed on one gameweek, published without the one. So the
tests that matter most here are the ones that fail when a figure can be read
without its `n`, when a floor is quietly met, or when two reference classes are
pooled into a single reassuring number.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from gaffer import calibration as C
from gaffer import review as R

SRC = Path(C.__file__)


# --- fixtures ---------------------------------------------------------------

def _dist(centre: float = 60.0, n: int = 200) -> list[float]:
    """A symmetric spread of simulated totals centred on `centre`."""
    return [centre - n / 2 + i for i in range(n)]


def _decision(event: int, as_of: str, *, dist=None, pre: bool = True,
              season: str = "2026-27", entry_id: int = 7,
              move_expected: float | None = 2.5) -> dict:
    comparison = {} if move_expected is None else {"move_expected": move_expected}
    return {
        "season": season, "entry_id": entry_id, "target_event": event,
        "as_of": as_of, "deadline": "2026-08-21T17:30:00+00:00",
        "is_pre_deadline": 1 if pre else 0, "schema_version": 1,
        "content_hash": f"h{event}{as_of}",
        "payload": json.dumps({
            "decision": {"starting": [1, 2, 3], "captain": 1,
                         "comparison": comparison},
            "outcome_distribution": _dist() if dist is None else dist,
        }),
    }


def _review(event: int, realised: float, *, followed: bool | None = True,
            snapshot_as_of: str = "2026-08-21T16:00:00+00:00",
            published: float | None = None, season: str = "2026-27",
            entry_id: int = 7) -> dict:
    quality = {"outcome_percentile": published, "expected_at_decision": None,
               "verdict": R.VERDICT_UNKNOWN}
    return {
        "season": season, "entry_id": entry_id, "event": event,
        "generated_at": "2026-08-25T10:00:00+00:00",
        "snapshot_as_of": snapshot_as_of, "schema_version": 1,
        "payload": json.dumps({
            "event": event, "snapshot_as_of": snapshot_as_of,
            "comparison": {"actual_points": realised, "followed_advice": followed},
            "quality": quality,
        }),
    }


def _projection(gw: int, pid: int, pred: float, *, as_of: str = "2026-08-21T17:00:00+00:00",
                pre: bool = True, season: str = "2026-27") -> dict:
    return {"season": season, "target_gw": gw, "player_id": pid, "as_of": as_of,
            "is_pre_deadline": 1 if pre else 0, "exp_points": pred,
            "p_start": 0.8, "exp_minutes": 70.0, "model_version": "heuristic-0.5"}


def _write(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


# --- reading the record -----------------------------------------------------

def test_a_missing_file_is_empty_not_an_error(tmp_path):
    rows, skipped = C.read_ndjson(tmp_path / "nope.ndjson")
    assert rows == [] and skipped == 0


def test_a_damaged_line_is_counted_rather_than_raised(tmp_path):
    p = tmp_path / "x.ndjson"
    p.write_text('{"a": 1}\nnot json\n[1,2]\n\n{"b": 2}\n', encoding="utf-8")
    rows, skipped = C.read_ndjson(p)
    assert [r for r in rows] == [{"a": 1}, {"b": 2}]
    assert skipped == 2, "a skipped line must be published, not swallowed"


def test_only_the_last_pre_deadline_snapshot_of_an_event_counts():
    rows = [_decision(2, "2026-08-20T09:00:00+00:00"),
            _decision(2, "2026-08-21T16:59:00+00:00"),
            _decision(2, "2026-08-21T18:00:00+00:00", pre=False)]
    finals = C.final_snapshots(rows)
    assert set(finals) == {2}
    assert finals[2]["as_of"] == "2026-08-21T16:59:00+00:00"


def test_another_entry_is_never_merged_in():
    rows = [_decision(1, "2026-08-20T09:00:00+00:00", entry_id=7),
            _decision(1, "2026-08-20T10:00:00+00:00", entry_id=99)]
    assert C.final_snapshots(rows, entry_id=7)[1]["entry_id"] == 7
    assert C.final_snapshots(rows, entry_id=99)[1]["entry_id"] == 99


def test_the_scope_is_inferred_from_the_newest_row_not_merged():
    rows = [_decision(1, "2026-08-20T09:00:00+00:00", entry_id=7),
            _decision(1, "2026-08-21T09:00:00+00:00", entry_id=99)]
    assert C.infer_scope(rows) == ("2026-27", 99)


# --- observations -----------------------------------------------------------

def test_the_percentile_is_recomputed_and_checked_against_the_published_one():
    dec = [_decision(1, "2026-08-21T16:00:00+00:00")]
    expected = R.outcome_percentile(_dist(), 60.0)
    rev = [_review(1, 60.0, published=round(expected, 3))]
    obs = C.observations(dec, rev)
    assert len(obs) == 1
    assert obs[0]["percentile"] == pytest.approx(expected, abs=1e-4)
    assert obs[0]["agrees_with_published"] is True
    assert obs[0]["snapshot_match"] == "named_by_the_review"


def test_a_percentile_that_disagrees_with_the_published_one_is_named():
    dec = [_decision(1, "2026-08-21T16:00:00+00:00")]
    rev = [_review(1, 60.0, published=0.99)]
    obs = C.observations(dec, rev)
    assert obs[0]["agrees_with_published"] is False
    check = C.pit_check(obs)
    assert check["recomputation"]["disagreed_with_the_published_percentile"] == [1]


def test_the_snapshot_the_review_named_is_preferred_over_the_final_one():
    """A later snapshot of the same gameweek must not silently regrade it."""
    named = "2026-08-21T16:00:00+00:00"
    dec = [_decision(1, named, dist=[10.0] * 100),
           _decision(1, "2026-08-21T17:00:00+00:00", dist=[500.0] * 100)]
    obs = C.observations(dec, [_review(1, 60.0, snapshot_as_of=named)])
    assert obs[0]["snapshot_as_of"] == named
    assert obs[0]["percentile"] == 1.0


def test_a_review_with_no_surviving_snapshot_is_a_stated_gap_not_a_number():
    obs = C.observations([], [_review(1, 60.0)])
    assert obs[0]["percentile"] is None
    assert obs[0]["snapshot_match"] == "no_snapshot"
    assert "no pre-deadline snapshot" in obs[0]["note"]


def test_a_snapshot_without_move_expected_is_still_scored():
    """GW1 has no held squad to compare against. That is correct, not corrupt."""
    dec = [_decision(1, "2026-08-21T16:00:00+00:00", move_expected=None)]
    obs = C.observations(dec, [_review(1, 60.0)])
    assert obs[0]["percentile"] is not None
    assert obs[0]["expected_at_decision"] is None


def test_events_with_a_frozen_distribution_and_no_result_are_the_pipeline_of_n():
    dec = [_decision(1, "2026-08-21T16:00:00+00:00"),
           _decision(2, "2026-08-28T16:00:00+00:00"),
           _decision(3, "2026-09-04T16:00:00+00:00")]
    assert C.awaiting_result(dec, [_review(1, 60.0)]) == [2, 3]


# --- PIT statistics ---------------------------------------------------------

def test_every_statistic_carries_its_own_n():
    stats = C.pit_statistics([0.2, 0.4, 0.6])
    assert stats["n"] == 3
    assert C.pit_statistics([])["n"] == 0


def test_one_observation_produces_an_interval_that_says_so():
    stats = C.pit_statistics([0.206])
    lo, hi = stats["mean_ci95"]
    assert lo < 0.0 and hi > 0.5, "n=1 must not look like a measurement"
    assert stats["smallest_detectable_shift"] == 1.0
    assert stats["rejects_uniform_at_95"] is False


def test_a_uniform_sample_reads_as_calibrated():
    values = [(i + 0.5) / 40 for i in range(40)]
    stats = C.pit_statistics(values)
    assert stats["direction"] == "no_detectable_bias"
    assert stats["rejects_uniform_at_95"] is False
    assert stats["mean"] == pytest.approx(0.5, abs=0.01)


def test_a_simulation_running_hot_is_detected_once_n_is_large_enough():
    values = [0.01 + 0.002 * i for i in range(40)]      # everything lands low
    stats = C.pit_statistics(values)
    assert stats["direction"] == "runs_hot"
    assert stats["rejects_uniform_at_95"] is True


def test_a_simulation_running_cold_is_detected_the_same_way():
    values = [0.95 + 0.001 * i for i in range(40)]
    stats = C.pit_statistics(values)
    assert stats["direction"] == "runs_cold"
    assert stats["rejects_uniform_at_95"] is True


def test_the_power_of_the_test_is_published_not_implied():
    assert C.gameweeks_for_shift(0.15) == 83
    assert C.gameweeks_for_shift(0.15) > 38, (
        "the honest fact is that a moderate miscalibration cannot be detected "
        "inside one season; if this ever stops being true, say so on purpose")
    with pytest.raises(ValueError):
        C.gameweeks_for_shift(0)


# --- the sample-too-small path ---------------------------------------------

def test_one_gameweek_is_not_reportable_and_says_the_number_out_loud():
    dec = [_decision(1, "2026-08-21T16:00:00+00:00")]
    check = C.pit_check(C.observations(dec, [_review(1, 60.0)]))
    assert check["status"] == C.STATUS_INSUFFICIENT
    assert check["reportable"] is False
    assert check["gameweeks_short_of_the_floor"] == C.MIN_PIT_GAMEWEEKS - 1
    assert check["verdict"].startswith("n=1.")
    assert "Not enough to report" in check["verdict"]


def test_no_gameweeks_at_all_is_unavailable_rather_than_a_zero():
    check = C.pit_check([])
    assert check["status"] == C.STATUS_UNAVAILABLE
    assert check["followed_the_advice"]["n"] == 0
    assert check["followed_the_advice"]["mean"] is None


def test_the_floor_is_crossed_only_when_the_floor_is_crossed():
    dec, rev = [], []
    for gw in range(1, C.MIN_PIT_GAMEWEEKS + 1):
        stamp = f"2026-08-{gw + 9:02d}T16:00:00+00:00"
        dec.append(_decision(gw, stamp))
        rev.append(_review(gw, 55.0 + gw, snapshot_as_of=stamp))
    short = C.pit_check(C.observations(dec[:-1], rev[:-1]))
    full = C.pit_check(C.observations(dec, rev))
    assert short["reportable"] is False
    assert full["reportable"] is True and full["status"] == C.STATUS_MEASURED
    assert full["followed_the_advice"]["n"] == C.MIN_PIT_GAMEWEEKS


# --- reference classes ------------------------------------------------------

def test_a_gameweek_the_advice_was_not_followed_in_is_kept_out_of_the_headline():
    dec = [_decision(1, "2026-08-21T16:00:00+00:00"),
           _decision(2, "2026-08-28T16:00:00+00:00")]
    rev = [_review(1, 60.0, followed=True, snapshot_as_of="2026-08-21T16:00:00+00:00"),
           _review(2, 60.0, followed=False, snapshot_as_of="2026-08-28T16:00:00+00:00")]
    check = C.pit_check(C.observations(dec, rev))
    assert check["followed_the_advice"]["n"] == 1
    assert check["every_gameweek"]["n"] == 2
    assert check["gameweeks_diverged"] == 1
    assert "mixes reference classes" in check["every_gameweek"]["caveat"]


def test_the_percentiles_reference_class_is_not_restated_in_a_second_place():
    assert C.PIT_BASIS is R.PERCENTILE_BASIS
    assert "NOT a rank against other managers" in C.pit_check([])["basis"]


# --- per-player projection error -------------------------------------------

def _outcomes(gws, n=120, scale=1.0):
    return {gw: {pid: {"total_points": round(scale * (pid % 7)), "minutes": 90 if pid % 3 else 0}
                 for pid in range(1, n + 1)} for gw in gws}


def _projections(gws, n=120):
    return [_projection(gw, pid, (pid % 7) * 0.9,
                        as_of=f"2026-08-{gw + 20:02d}T16:00:00+00:00")
            for gw in gws for pid in range(1, n + 1)]


def test_no_results_supplied_is_unavailable_and_says_why():
    out = C.projection_check(_projections([1]), None)
    assert out["status"] == C.STATUS_UNAVAILABLE
    assert "no network call" in out["unavailable_reason"]


def test_one_gameweek_of_player_rows_is_not_three_gameweeks_of_evidence():
    out = C.projection_check(_projections([1]), _outcomes([1]))
    assert out["pooled"]["n"] == 120
    assert out["status"] == C.STATUS_INSUFFICIENT
    assert out["reportable"] is False
    assert "not" in out["insufficient_reason"]
    assert "independent observations" in out["insufficient_reason"]


def test_enough_gameweeks_and_enough_rows_is_a_measurement():
    gws = [1, 2, 3]
    out = C.projection_check(_projections(gws), _outcomes(gws))
    assert out["status"] == C.STATUS_MEASURED and out["reportable"] is True
    assert out["gameweeks_measured"] == gws
    assert out["pooled"]["n"] == 360
    assert len(out["per_gameweek"]) == 3
    assert all(row["n"] == 120 for row in out["per_gameweek"])


def test_the_curve_is_ordered_by_prediction_and_keeps_every_row():
    gws = [1, 2, 3]
    out = C.projection_check(_projections(gws), _outcomes(gws))
    curve = out["curve"]
    assert curve, "a 360-row sample must produce a curve"
    assert [b["pred"] for b in curve] == sorted(b["pred"] for b in curve)
    assert sum(b["n"] for b in curve) == out["pooled"]["n"]
    assert all("n" in b for b in curve)


def test_the_top_bin_is_summarised_because_a_pooled_mae_hides_it():
    gws = [1, 2, 3]
    out = C.projection_check(_projections(gws), _outcomes(gws))
    summary = out["curve_summary"]
    assert summary["top_bin"]["pred"] == max(b["pred"] for b in out["curve"])
    assert summary["top_bin_direction"] in ("over", "under", "none")


def test_only_the_gameweeks_with_results_are_measured():
    out = C.projection_check(_projections([1, 2, 3]), _outcomes([1]))
    assert out["gameweeks_measured"] == [1]


def test_a_post_deadline_projection_row_is_never_scored():
    rows = [_projection(1, pid, 3.0, pre=False) for pid in range(1, 121)]
    out = C.projection_check(rows, _outcomes([1]))
    assert out["status"] == C.STATUS_UNAVAILABLE


def test_the_newest_pre_deadline_row_wins():
    rows = [_projection(1, 5, 1.0, as_of="2026-08-21T09:00:00+00:00"),
            _projection(1, 5, 9.0, as_of="2026-08-21T17:00:00+00:00")]
    latest = C.latest_projection_rows(rows)
    assert latest[(1, 5)]["exp_points"] == 9.0


def test_the_appeared_subset_is_labelled_as_conditioned_on_a_post_match_fact():
    gws = [1, 2, 3]
    out = C.projection_check(_projections(gws), _outcomes(gws))
    assert "POST-MATCH" in out["appeared"]["caveat"]
    assert out["appeared"]["n"] < out["pooled"]["n"]


def test_the_persistence_baseline_says_not_yet_rather_than_going_quiet():
    one = C.projection_check(_projections([1]), _outcomes([1]))
    assert one["baselines"]["persistence"]["mae"] is None
    assert "two measured gameweeks" in one["baselines"]["persistence"]["unavailable_reason"]
    many = C.projection_check(_projections([1, 2, 3]), _outcomes([1, 2, 3]))
    assert many["baselines"]["persistence"]["n"] > 0
    assert many["baselines"]["persistence"]["gameweeks"] == [2, 3]


def test_a_prediction_outside_the_measured_range_is_told_so():
    gws = [1, 2, 3]
    curve = C.projection_check(_projections(gws), _outcomes(gws))["curve"]
    inside = C.lookup_bin(curve[0]["pred"], curve)
    assert inside["within_the_measured_range"] is True and inside["caveat"] is None
    outside = C.lookup_bin(50.0, curve)
    assert outside["within_the_measured_range"] is False
    assert "outside every bin" in outside["caveat"]
    assert C.lookup_bin(7.3, []) is None


# --- assembly ---------------------------------------------------------------

def test_the_block_builds_from_a_state_directory(tmp_path):
    _write(tmp_path, "decisions.ndjson", [_decision(1, "2026-08-21T16:00:00+00:00")])
    _write(tmp_path, "reviews.ndjson", [_review(1, 60.0)])
    _write(tmp_path, "projections.ndjson", _projections([1]))
    out = C.build_from_state(tmp_path / "state", outcomes=_outcomes([1]))
    assert out["schema_version"] == C.SCHEMA_VERSION
    assert out["calibration_version"] == C.CALIBRATION_VERSION
    assert out["season"] == "2026-27" and out["entry_id"] == 7
    assert out["status"] == C.STATUS_INSUFFICIENT
    assert out["headline"].startswith("n=1.")
    assert out["sources"]["unparseable_lines"]["decisions.ndjson"] == 0


def test_an_empty_state_directory_is_unavailable_not_a_confident_zero(tmp_path):
    (tmp_path / "state").mkdir()
    out = C.build_from_state(tmp_path / "state")
    assert out["status"] == C.STATUS_UNAVAILABLE
    assert out["distribution"]["followed_the_advice"]["n"] == 0
    assert out["projection"]["status"] == C.STATUS_UNAVAILABLE


def test_a_damaged_archive_is_reported_beside_the_numbers(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    (d / "decisions.ndjson").write_text(
        json.dumps(_decision(1, "2026-08-21T16:00:00+00:00")) + "\nbroken\n",
        encoding="utf-8")
    (d / "reviews.ndjson").write_text(json.dumps(_review(1, 60.0)) + "\n",
                                      encoding="utf-8")
    out = C.build_from_state(d)
    assert out["sources"]["unparseable_lines"]["decisions.ndjson"] == 1


def test_the_status_is_measured_only_when_something_is_reportable(tmp_path):
    dec, rev = [], []
    for gw in range(1, C.MIN_PIT_GAMEWEEKS + 1):
        stamp = f"2026-08-{gw + 9:02d}T16:00:00+00:00"
        dec.append(_decision(gw, stamp))
        rev.append(_review(gw, 55.0 + gw, snapshot_as_of=stamp))
    out = C.build(decision_rows=dec, review_rows=rev, season="2026-27", entry_id=7)
    assert out["status"] == C.STATUS_MEASURED
    assert out["distribution"]["reportable"] is True


# --- guards -----------------------------------------------------------------

def test_no_figure_can_be_read_without_its_count(tmp_path):
    """Walk the published block: any dict holding a statistic must hold an `n`."""
    _write(tmp_path, "decisions.ndjson", [_decision(1, "2026-08-21T16:00:00+00:00")])
    _write(tmp_path, "reviews.ndjson", [_review(1, 60.0)])
    _write(tmp_path, "projections.ndjson", _projections([1, 2, 3]))
    out = C.build_from_state(tmp_path / "state", outcomes=_outcomes([1, 2, 3]))
    statistics = {"mean", "mae", "ks_d", "median", "baseline_mae",
                  "skill_vs_pool_mean", "bias"}
    offenders: list[str] = []

    def walk(node, path="$"):
        if isinstance(node, dict):
            if statistics & set(node) and "n" not in node:
                offenders.append(path)
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(out)
    assert offenders == [], f"a statistic published without its n: {offenders}"


def test_the_module_makes_no_network_call_and_opens_no_database():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    banned = {"httpx", "requests", "urllib", "socket", "sqlite3", "subprocess", "os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] not in banned, f"calibration imports {a.name}"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            assert node.module.split(".")[0] not in banned, \
                f"calibration imports from {node.module}"


def test_the_block_is_json_serialisable(tmp_path):
    _write(tmp_path, "decisions.ndjson", [_decision(1, "2026-08-21T16:00:00+00:00")])
    _write(tmp_path, "reviews.ndjson", [_review(1, 60.0)])
    out = C.build_from_state(tmp_path / "state")
    assert json.loads(json.dumps(out))["status"] == out["status"]


# ---------------------------------------------------------------------------
# The publish path: `export.artifacts` turns the record into an artifact block
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402

from gaffer.export import artifacts as A  # noqa: E402
from gaffer.store import db  # noqa: E402


def _db(last_finished: str | None = "1", season: str = "2026-27"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    db.set_meta(conn, "season", season)
    if last_finished is not None:
        db.set_meta(conn, "last_finished_gw", last_finished)
    return conn


def _player_gw(conn, season, gw, pid, fixture, points, minutes):
    conn.execute(
        "INSERT INTO player_gw (season, player_id, gw, fixture, total_points, "
        "minutes) VALUES (?,?,?,?,?,?)",
        (season, pid, gw, fixture, points, minutes))
    conn.commit()


def test_a_gameweek_in_progress_is_never_scored():
    """`last_finished_gw` is the gate. A provisional total is not a result."""
    assert A.finished_events(_db("2")) == [1, 2]
    assert A.finished_events(_db(None)) == []
    assert A.finished_events(_db("")) == []
    assert A.finished_events(_db("0")) == []


def test_a_double_gameweek_is_summed_because_the_projection_covered_both():
    conn = _db("1")
    _player_gw(conn, "2026-27", 1, 5, 101, 6, 90)
    _player_gw(conn, "2026-27", 1, 5, 102, 9, 85)
    out = A.realised_outcomes(conn, [1], season="2026-27")
    assert out[1][5] == {"total_points": 15, "minutes": 175}


def test_another_seasons_results_are_never_joined_in():
    conn = _db("1")
    _player_gw(conn, "2026-27", 1, 5, 101, 6, 90)
    _player_gw(conn, "2025-26", 1, 5, 900, 99, 90)
    assert A.realised_outcomes(conn, [1], season="2026-27")[1][5]["total_points"] == 6


def test_a_missing_results_table_degrades_rather_than_stopping_the_run():
    conn = _db("1")
    conn.execute("DROP TABLE player_gw")
    conn.commit()
    assert A.realised_outcomes(conn, [1], season="2026-27") == {}


def test_no_finished_gameweek_asks_for_no_results():
    assert A.realised_outcomes(_db(None), [], season="2026-27") == {}


def test_the_block_is_built_from_the_record_and_the_results(tmp_path):
    conn = _db("1")
    for pid in range(1, 121):
        _player_gw(conn, "2026-27", 1, pid, 100 + pid, pid % 7, 90 if pid % 3 else 0)
    _write(tmp_path, "decisions.ndjson", [_decision(1, "2026-08-21T16:00:00+00:00")])
    _write(tmp_path, "reviews.ndjson", [_review(1, 60.0)])
    _write(tmp_path, "projections.ndjson", _projections([1]))
    out = A.build_season_calibration(
        conn, state_dir=tmp_path / "state", generated_at="2026-08-31T12:00:00+00:00")
    assert out["schema_version"] == C.SCHEMA_VERSION
    assert out["generated_at"] == "2026-08-31T12:00:00+00:00"
    assert out["distribution"]["followed_the_advice"]["n"] == 1
    assert out["projection"]["pooled"]["n"] == 120
    assert out["projection"]["gameweeks_measured"] == [1]


def test_the_review_being_published_this_run_is_counted_in_it(tmp_path):
    """The pipeline dumps NDJSON *after* it writes the artifacts.

    Without the in-flight review the published block would be exactly one
    gameweek behind the artifact carrying it, every week — a calibration saying
    n=1 printed underneath a review of gameweek 2.
    """
    conn = _db("2")
    _write(tmp_path, "decisions.ndjson",
           [_decision(1, "2026-08-21T16:00:00+00:00"),
            _decision(2, "2026-08-28T16:00:00+00:00")])
    _write(tmp_path, "reviews.ndjson", [_review(1, 60.0)])
    fresh = {"season": "2026-27", "entry_id": 7, "event": 2,
             "generated_at": "2026-08-31T12:00:00+00:00",
             "snapshot_as_of": "2026-08-28T16:00:00+00:00",
             "comparison": {"actual_points": 70.0, "followed_advice": True},
             "quality": {"outcome_percentile": None}}
    without = A.build_season_calibration(conn, state_dir=tmp_path / "state")
    with_it = A.build_season_calibration(
        conn, state_dir=tmp_path / "state", review=fresh)
    assert without["distribution"]["followed_the_advice"]["n"] == 1
    assert with_it["distribution"]["followed_the_advice"]["n"] == 2
    assert with_it["awaiting_result"] == []


def test_a_review_already_in_the_ndjson_is_not_counted_twice(tmp_path):
    conn = _db("1")
    _write(tmp_path, "decisions.ndjson", [_decision(1, "2026-08-21T16:00:00+00:00")])
    _write(tmp_path, "reviews.ndjson", [_review(1, 60.0)])
    again = {"season": "2026-27", "entry_id": 7, "event": 1,
             "generated_at": "2026-09-01T00:00:00+00:00",
             "snapshot_as_of": "2026-08-21T16:00:00+00:00",
             "comparison": {"actual_points": 60.0, "followed_advice": True},
             "quality": {"outcome_percentile": None}}
    out = A.build_season_calibration(conn, state_dir=tmp_path / "state", review=again)
    assert out["distribution"]["followed_the_advice"]["n"] == 1


def test_a_calibration_failure_never_costs_the_review(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret path /Users/somebody/thing")

    monkeypatch.setattr(A, "build_season_calibration", boom)
    out = A._season_calibration_or_reason(_db("1"), tmp_path, None, None)
    assert out["status"] == C.STATUS_UNAVAILABLE
    assert out["unavailable_reason"] == "RuntimeError"
    assert "/Users/" not in json.dumps(out), "an exception message must not ship"


def test_the_review_artifact_carries_the_block(tmp_path):
    """`write_all` attaches it; nothing in the pipeline had to change to do so."""
    import inspect
    src = inspect.getsource(A.write_all)
    assert "season_calibration" in src
    assert "_season_calibration_or_reason" in src
