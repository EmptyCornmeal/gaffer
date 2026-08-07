"""T-23 — post-gameweek review: decision quality separated from luck.

The failure mode this file exists to prevent is a review that reads the results
and then decides what the right answer had been. Every judgemental number here
must trace back to the immutable pre-deadline snapshot; the hindsight column is
computed, shown, labelled unknowable, and never used to score anything.

The second property is symmetric fairness: a good decision that lost must not be
called a mistake, and a bad decision that won must not be praised.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gaffer import review, snapshots
from gaffer.store import db

ENTRY = 1066421
DEADLINE = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)
DL_ISO = DEADLINE.isoformat().replace("+00:00", "Z")
BEFORE = DEADLINE - timedelta(hours=2)
AFTER = DEADLINE + timedelta(days=3)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.db")
    db.init_schema(c)
    yield c
    c.close()


def snap_payload(*, move=60.0, hold=55.0, starting=None, captain=1,
                 transfers_in=None, dist=None):
    return {
        "decision": {
            "action": "transfer",
            "starting": starting or list(range(1, 12)),
            "captain": captain,
            "transfers_in": transfers_in or [12],
            "transfers_out": [5],
            "comparison": {"move_expected": move, "hold_expected": hold},
        },
        "outcome_distribution": dist,
    }


def store_snapshot(conn, **kw):
    snapshots.record(conn, entry_id=ENTRY, target_event=1, deadline=DL_ISO,
                     payload=snap_payload(**kw), now=BEFORE)


def actual(total=58, starting=None, captain=1, transfers_in=None, **kw):
    return {
        "total_points": total,
        "starting": starting or list(range(1, 12)),
        "bench": [12, 13, 14, 15],
        "captain": captain, "multiplier": 2,
        "transfers_in": transfers_in if transfers_in is not None else [12],
        "transfers_out": [5], "hits": 0, **kw,
    }


PTS = {p: 5 for p in range(1, 16)}


# ==========================================================================
# No hindsight leakage
# ==========================================================================

def test_the_review_module_never_reads_a_post_deadline_projection():
    """A structural check: `review.py` must not query live projections.

    Reading `projections` (which the pipeline rewrites every run) would silently
    substitute today's model for the one that produced the advice.
    """
    src = Path(inspect.getfile(review)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    sql = [n.value for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "SELECT" in n.value.upper()]
    for q in sql:
        assert "FROM projections" not in q, (
            "reviews must read the immutable snapshot, not the live projection "
            f"table: {q!r}")
        assert "player_gw" not in q


def test_decision_numbers_come_from_the_snapshot_not_the_result(conn):
    store_snapshot(conn, move=60.0, hold=55.0)
    r = conn.execute("SELECT payload FROM decision_snapshots").fetchone()
    rev = review.build(conn, entry_id=ENTRY, event=1, actual=actual(total=20),
                       points=PTS, now=AFTER)
    assert rev.quality.expected_at_decision == 60.0, "the pre-deadline EV"
    assert rev.comparison.hold_points == 55.0
    assert rev.snapshot_as_of is not None
    assert r is not None


def test_the_hindsight_column_is_labelled_and_never_scores_the_decision(conn):
    store_snapshot(conn, move=60.0, hold=55.0, dist=[50.0] * 100)
    rev = review.build(conn, entry_id=ENTRY, event=1, actual=actual(total=58),
                       points=PTS, now=AFTER, hindsight_points=140.0)
    d = rev.as_dict()
    assert d["comparison"]["hindsight_points"] == 140.0
    assert d["comparison"]["hindsight_is_unknowable"] is True
    # 140 in hindsight must not turn a +EV decision into a bad one.
    assert d["quality"]["positive_ev"] is True
    assert any("not knowable before the deadline" in x for x in d["limitations"])


def test_a_missing_snapshot_yields_an_unassessable_decision_not_a_guess(conn):
    rev = review.build(conn, entry_id=ENTRY, event=1, actual=actual(),
                       points=PTS, now=AFTER)
    assert rev.quality.verdict == review.VERDICT_UNKNOWN
    assert rev.snapshot_as_of is None
    assert rev.as_dict()["has_snapshot"] is False
    assert any("No pre-deadline snapshot" in x for x in rev.limitations)


def test_a_review_cannot_alter_the_snapshot_it_scores(conn):
    store_snapshot(conn, move=60.0)
    before = snapshots.final_pre_deadline(conn, ENTRY, 1)
    review.save(conn, review.build(conn, entry_id=ENTRY, event=1,
                                   actual=actual(), points=PTS, now=AFTER))
    after = snapshots.final_pre_deadline(conn, ENTRY, 1)
    assert after.as_of == before.as_of
    assert after.content_hash == before.content_hash
    assert after.payload == before.payload


# ==========================================================================
# Outcome percentile
# ==========================================================================

def test_the_percentile_uses_the_pre_deadline_distribution():
    dist = list(range(0, 100))          # 0..99
    assert review.outcome_percentile(dist, 50) == pytest.approx(0.505)
    assert review.outcome_percentile(dist, 0) == pytest.approx(0.005)
    assert review.outcome_percentile(dist, 99) == pytest.approx(0.995)


def test_no_distribution_means_no_percentile_not_a_default():
    assert review.outcome_percentile(None, 50) is None
    assert review.outcome_percentile([], 50) is None
    assert review.outcome_percentile([1, 2, 3], None) is None


def test_ties_are_handled_at_the_midpoint():
    assert review.outcome_percentile([5, 5, 5, 5], 5) == pytest.approx(0.5)


# ==========================================================================
# Decision quality vs luck — the four quadrants
# ==========================================================================

def test_a_good_decision_with_a_bad_result_is_not_a_mistake():
    q = review.assess(expected=60.0, realised=30.0, percentile=0.05,
                      hold_expected=55.0)
    assert q.verdict == review.VERDICT_GOOD_UNLUCKY
    assert q.positive_ev is True
    assert "still a good decision" in q.explanation


def test_a_bad_decision_with_a_lucky_result_is_not_praised():
    q = review.assess(expected=50.0, realised=90.0, percentile=0.97,
                      hold_expected=55.0)
    assert q.verdict == review.VERDICT_BAD_LUCKY
    assert q.positive_ev is False
    assert "still a bad decision" in q.explanation


def test_a_good_decision_with_a_good_result():
    q = review.assess(expected=60.0, realised=85.0, percentile=0.9,
                      hold_expected=55.0)
    assert q.verdict == review.VERDICT_GOOD_LUCKY
    assert q.positive_ev is True


def test_a_normal_outcome_is_not_called_lucky_either_way():
    q = review.assess(expected=60.0, realised=61.0, percentile=0.5,
                      hold_expected=55.0)
    assert q.verdict == review.VERDICT_GOOD_NORMAL
    assert "luck is not the story" in q.explanation


def test_a_bad_decision_with_a_bad_result():
    q = review.assess(expected=50.0, realised=20.0, percentile=0.05,
                      hold_expected=55.0)
    assert q.verdict == review.VERDICT_BAD_UNLUCKY
    assert q.positive_ev is False


def test_luck_cannot_be_measured_without_a_distribution():
    q = review.assess(expected=60.0, realised=30.0, percentile=None,
                      hold_expected=55.0)
    assert q.percentile is None
    assert "cannot be measured" in q.explanation
    assert q.positive_ev is True, "EV is still assessable"


def test_ev_is_judged_against_holding_not_against_the_result():
    better = review.assess(expected=60.0, realised=10.0, percentile=0.5,
                           hold_expected=55.0)
    worse = review.assess(expected=50.0, realised=99.0, percentile=0.5,
                          hold_expected=55.0)
    assert better.positive_ev is True and worse.positive_ev is False


def test_the_boundary_percentiles_are_exact():
    at_lucky = review.assess(expected=60, realised=1, percentile=review.LUCKY_ABOVE,
                             hold_expected=50)
    inside = review.assess(expected=60, realised=1,
                           percentile=review.LUCKY_ABOVE - 0.01, hold_expected=50)
    assert at_lucky.verdict == review.VERDICT_GOOD_LUCKY
    assert inside.verdict == review.VERDICT_GOOD_NORMAL


# ==========================================================================
# Attribution
# ==========================================================================

def test_captaincy_is_the_extra_copy_only():
    a = review.attribute(xi=[1, 2], bench=[], captain=1, vice_used=False,
                         points={1: 12, 2: 4}, multiplier=2)
    assert a.captaincy == 12, "the armband added one more copy of 12"
    assert a.starting_xi == 16


def test_triple_captain_attributes_two_extra_copies():
    a = review.attribute(xi=[1], bench=[], captain=1, vice_used=False,
                         points={1: 10}, multiplier=3, chip="3xc")
    assert a.captaincy == 20
    assert a.chip == 10


def test_bench_points_are_attributed_as_a_cost_of_the_lineup():
    a = review.attribute(xi=[1], bench=[2, 3], captain=1, vice_used=False,
                         points={1: 5, 2: 9, 3: 3})
    assert a.bench == 12


def test_bench_boost_attributes_the_bench_to_the_chip():
    a = review.attribute(xi=[1], bench=[2, 3], captain=1, vice_used=False,
                         points={1: 5, 2: 9, 3: 3}, chip="bboost")
    assert a.chip == 12


def test_transfers_are_the_delta_between_in_and_out():
    a = review.attribute(xi=[1], bench=[], captain=None, vice_used=False,
                         points={1: 5, 9: 12, 5: 2},
                         transfers_in=[9], transfers_out=[5])
    assert a.transfers == 10


def test_a_hit_is_recorded_as_its_own_line():
    a = review.attribute(xi=[1], bench=[], captain=None, vice_used=False,
                         points={1: 5}, hits=8)
    assert a.hit_cost == 8


def test_autosubs_are_credited_separately():
    a = review.attribute(xi=[1, 2], bench=[], captain=None, vice_used=False,
                         points={1: 5, 2: 7}, subs_in=[2], subs_out=[3])
    assert a.autosubs == 7


def test_attribution_serialises_every_line():
    a = review.attribute(xi=[1], bench=[], captain=1, vice_used=False,
                         points={1: 5})
    assert set(a.as_dict()) == {"captaincy", "hit_cost", "transfers", "bench",
                                "chip", "starting_xi", "autosubs"}


# ==========================================================================
# The learning loop
# ==========================================================================

def test_one_gameweek_is_never_a_pattern():
    lesson = review.lesson_from_history([{"event": 1, "zero_minute_starters": 5}])
    assert lesson["key"] == review.LESSON_NONE
    assert "not enough" in lesson["text"].lower()


def test_a_repeated_minutes_problem_becomes_the_lesson():
    hist = [{"event": e, "zero_minute_starters": 3} for e in (3, 2, 1)]
    lesson = review.lesson_from_history(hist)
    assert lesson["key"] == review.LESSON_MINUTES
    assert "zero minutes" in lesson["text"]
    assert lesson["occurrences"] == 3


def test_a_repeated_unprofitable_hit_becomes_the_lesson():
    hist = [{"event": e, "hits": 4, "transfer_delta": 1} for e in (3, 2)]
    lesson = review.lesson_from_history(hist)
    assert lesson["key"] == review.LESSON_HITS
    assert "-4" in lesson["text"]


def test_a_repeated_bench_problem_becomes_the_lesson():
    hist = [{"event": e, "bench_points": 15} for e in (3, 2)]
    lesson = review.lesson_from_history(hist)
    assert lesson["key"] == review.LESSON_BENCH


def test_normal_variance_produces_an_honest_no_pattern():
    hist = [{"event": e, "zero_minute_starters": 0, "bench_points": 2,
             "hits": 0} for e in (4, 3, 2, 1)]
    lesson = review.lesson_from_history(hist)
    assert lesson["key"] == review.LESSON_NONE
    assert "variance" in lesson["text"]


def test_every_lesson_key_is_from_the_declared_vocabulary():
    for hist in ([{"event": e, "zero_minute_starters": 3} for e in (2, 1)],
                 [{"event": e, "bench_points": 20} for e in (2, 1)],
                 [{"event": 1}]):
        assert review.lesson_from_history(hist)["key"] in review.ALL_LESSONS


def test_a_lesson_always_cites_its_evidence():
    hist = [{"event": e, "zero_minute_starters": 3, "summary": f"gw{e}"}
            for e in (3, 2)]
    lesson = review.lesson_from_history(hist)
    assert lesson["evidence"] and all("event" in x for x in lesson["evidence"])


def test_no_lesson_is_free_form_prose():
    """Every lesson maps to a key; there is no path that invents encouragement."""
    src = Path(inspect.getfile(review)).read_text(encoding="utf-8")
    assert "Well done" not in src and "Keep it up" not in src
    hist = [{"event": e, "zero_minute_starters": 3} for e in (3, 2)]
    assert review.lesson_from_history(hist)["key"] != ""


# ==========================================================================
# Persistence: idempotent, season-aware, correctable
# ==========================================================================

def test_a_review_is_stored_and_read_back(conn):
    store_snapshot(conn)
    rev = review.build(conn, entry_id=ENTRY, event=1, actual=actual(),
                       points=PTS, now=AFTER)
    review.save(conn, rev)
    loaded = review.load(conn, ENTRY, 1)
    assert loaded["event"] == 1 and loaded["schema_version"] == 1


def test_saving_twice_updates_rather_than_duplicates(conn):
    store_snapshot(conn)
    rev = review.build(conn, entry_id=ENTRY, event=1, actual=actual(),
                       points=PTS, now=AFTER)
    review.save(conn, rev)
    review.save(conn, rev)
    assert conn.execute("SELECT COUNT(*) c FROM gw_reviews").fetchone()["c"] == 1


def test_fpl_revising_points_corrects_the_review_in_place(conn):
    store_snapshot(conn)
    first = review.build(conn, entry_id=ENTRY, event=1, actual=actual(total=58),
                         points=PTS, now=AFTER)
    review.save(conn, first)
    # Bonus is finalised three hours later and the total changes.
    corrected = review.build(conn, entry_id=ENTRY, event=1,
                             actual=actual(total=61), points=PTS,
                             now=AFTER + timedelta(hours=3))
    review.save(conn, corrected)
    assert conn.execute("SELECT COUNT(*) c FROM gw_reviews").fetchone()["c"] == 1
    assert review.load(conn, ENTRY, 1)["comparison"]["actual_points"] == 61


def test_reviews_are_season_aware(conn):
    store_snapshot(conn)
    rev = review.build(conn, entry_id=ENTRY, event=1, actual=actual(),
                       points=PTS, now=AFTER, season="2025-26")
    review.save(conn, rev)
    assert review.load(conn, ENTRY, 1, season="2025-26") is not None
    assert review.load(conn, ENTRY, 1, season="2026-27") is None


def test_load_all_returns_most_recent_event_first(conn):
    for ev in (1, 2, 3):
        rev = review.build(conn, entry_id=ENTRY, event=ev, actual=actual(),
                           points=PTS, now=AFTER)
        review.save(conn, rev)
    assert [r["event"] for r in review.load_all(conn, ENTRY)] == [3, 2, 1]


# ==========================================================================
# Assembly
# ==========================================================================

def test_following_the_advice_is_detected(conn):
    store_snapshot(conn, transfers_in=[12])
    rev = review.build(conn, entry_id=ENTRY, event=1,
                       actual=actual(transfers_in=[12]), points=PTS, now=AFTER)
    assert rev.comparison.followed_advice is True
    assert "made the recommended move" in rev.comparison.note


def test_diverging_from_the_advice_is_detected_without_judgement(conn):
    store_snapshot(conn, transfers_in=[12])
    rev = review.build(conn, entry_id=ENTRY, event=1,
                       actual=actual(transfers_in=[99]), points=PTS, now=AFTER)
    assert rev.comparison.followed_advice is False
    assert "different" in rev.comparison.note


def test_the_review_reports_what_the_recommendation_would_have_scored(conn):
    store_snapshot(conn, starting=[1, 2, 3], captain=1)
    rev = review.build(conn, entry_id=ENTRY, event=1, actual=actual(),
                       points={1: 10, 2: 4, 3: 6}, now=AFTER)
    assert rev.comparison.recommended_points == 10 + 4 + 6 + 10


def test_the_full_payload_carries_versions_and_identity(conn):
    store_snapshot(conn)
    d = review.build(conn, entry_id=ENTRY, event=1, actual=actual(),
                     points=PTS, now=AFTER).as_dict()
    assert d["review_version"] == review.REVIEW_VERSION
    assert d["entry_id"] == ENTRY and d["event"] == 1
    assert "facts" in d and "attribution" in d and "quality" in d
    assert d["generated_at"].endswith("+00:00")
