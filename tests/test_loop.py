"""Phase 7 -- the loop, and the three things it refuses to do.

The refusals are the substance. A post-gameweek review that scores hindsight,
fits a threshold it has no power to fit, or answers "do my overrides beat the
model?" on a handful of weeks would look like all of this and teach the
opposite of it.
"""
from __future__ import annotations

import pytest

from gaffer import loop


def _snap(action="roll", delta=-16.4, p=0.08, bar=1.0, prob_bar=0.55, **over):
    d = {
        "action": action,
        "comparison": {"delta": delta, "p_move_beats_hold": p,
                       "delta_range_p10_p90": [-6.0, 10.0]},
        "thresholds": {"min_actionable_points": bar,
                       "min_actionable_probability": prob_bar},
    }
    d.update(over)
    return {"gameweek": 2, "decision": d}


def _review(percentile=0.7, **over):
    q = {"outcome_percentile": percentile, "realised": 60.0,
         "expected_at_decision": 50.0}
    q.update(over)
    return {"quality": q}


# --------------------------------------------------------------------------
# 7.2 -- the axes are separate, and hindsight never touches the first
# --------------------------------------------------------------------------

def test_a_correct_call_that_lost_is_still_a_correct_call():
    """The most common shape of a good decision, and the one a results-only
    review punishes. If this cell collapses into "wrong", the loop teaches the
    reader to chase variance."""
    got = loop.classify(_snap(), _review(percentile=0.1))
    assert got["cell"] == loop.RIGHT_CALL_BAD_RESULT
    assert got["decision_was_right"] is True
    assert got["outcome_was_good"] is False


def test_getting_away_with_it_is_named_as_such():
    """A transfer that missed both bars and worked anyway. The most dangerous
    cell, because nothing else in the product would flag it."""
    got = loop.classify(
        _snap(action="transfer", delta=0.2, p=0.51), _review(percentile=0.9))
    assert got["cell"] == loop.WRONG_CALL_GOOD_RESULT
    assert "rewards the wrong habit" in got["meaning"]


def test_the_outcome_never_reaches_the_decision_axis():
    """Same decision, opposite weeks. The decision verdict must not move."""
    good = loop.classify(_snap(), _review(percentile=0.95))
    bad = loop.classify(_snap(), _review(percentile=0.05))
    assert good["decision_was_right"] == bad["decision_was_right"]
    assert good["decision_basis"] == bad["decision_basis"]
    assert good["cell"] != bad["cell"]


def test_a_transfer_needs_both_bars_not_either():
    """A +2.0 edge that wins 48% of the time is a coin flip with a good mean,
    and the published policy says so. The loop has to agree with the policy it
    is scoring, or it is scoring a different product."""
    assert loop.classify(_snap(action="transfer", delta=2.0, p=0.48),
                         _review())["decision_was_right"] is False
    assert loop.classify(_snap(action="transfer", delta=0.5, p=0.9),
                         _review())["decision_was_right"] is False
    assert loop.classify(_snap(action="transfer", delta=2.0, p=0.9),
                         _review())["decision_was_right"] is True


def test_an_unplayed_gameweek_is_unresolved_rather_than_neutral():
    got = loop.classify(_snap(), {})
    assert got["cell"] == loop.UNRESOLVED
    # ...but the decision half is still computed, because it is knowable now.
    assert got["decision_was_right"] is True


def test_a_snapshot_with_no_comparison_is_unresolved_not_wrong():
    got = loop.classify({"decision": {"action": "roll"}}, _review())
    assert got["cell"] == loop.UNRESOLVED


# --------------------------------------------------------------------------
# The matrix and its floor
# --------------------------------------------------------------------------

def test_the_rate_is_withheld_below_the_reporting_floor():
    """7.5. The counts are shown, because hiding the record is worse. The rate
    is marked unreadable, because at n=1 it is not a measurement of anything."""
    m = loop.matrix([loop.classify(_snap(), _review())])
    assert m["resolved"] == 1
    assert m["reportable"] is False
    assert "not a measurement" in m["caveat"]


def test_the_floor_lifts_once_it_is_met():
    rows = [loop.classify(_snap(), _review()) for _ in range(5)]
    m = loop.matrix(rows)
    assert m["reportable"] is True
    assert "caveat" not in m
    assert m["decision_quality_rate"] == 1.0


def test_an_empty_record_says_so_rather_than_scoring_zero():
    m = loop.matrix([])
    assert m["resolved"] == 0
    assert "no gameweek" in m["reading"]
    assert "decision_quality_rate" not in m


def test_the_lucky_cell_is_called_out_in_the_reading():
    rows = [loop.classify(_snap(action="transfer", delta=0.2, p=0.51),
                          _review(percentile=0.9))]
    assert "rewards the wrong habit" in loop.matrix(rows)["reading"]


# --------------------------------------------------------------------------
# 7.3 -- the power gate
# --------------------------------------------------------------------------

def test_the_requirement_is_computed_not_asserted():
    """The MCP said "~6 completed gameweeks" for a season. Six gameweeks is
    six decisions, and fitting two thresholds on six observations is
    overfitting one month and calling it measurement."""
    sigma = loop.sigma_from_range(-6.0, 10.0)
    assert sigma == pytest.approx(6.24, abs=0.02)
    need = loop.required_decisions(sigma)
    assert need > 1000
    assert need == pytest.approx(1224, abs=5)


def test_it_stays_insufficient_however_many_gameweeks_pass():
    """Gameweek count is deliberately not an input. Using it is how "~6
    gameweeks" became a plan in the first place."""
    r = loop.fitting_readiness(discordant=6, sigma_points=6.24)
    assert r["status"] == "insufficient_data"
    assert "gameweeks" in r["unit"]
    assert r["shortfall"] > 1000


def test_only_discordant_decisions_count_and_it_says_why():
    r = loop.fitting_readiness(discordant=0, sigma_points=6.24)
    assert "teaches nothing" in r["why_not_gameweeks"]


def test_the_parameters_are_pre_registered():
    """Fixed before the data was looked at, so the answer cannot be tuned
    after seeing it."""
    r = loop.fitting_readiness(discordant=0, sigma_points=6.24)
    assert r["pre_registered"]["fixed_before_looking"] is True
    assert r["pre_registered"]["power"] == 0.80
    assert r["pre_registered"]["smallest_effect_worth_acting_on_points"] == 0.5


def test_it_can_say_ready_rather_than_never():
    """A gate that could never open is not a gate, it is a refusal wearing
    one's clothes."""
    r = loop.fitting_readiness(discordant=99_999, sigma_points=6.24)
    assert r["status"] == "ready"


def test_no_spread_means_no_requirement_rather_than_a_made_up_one():
    r = loop.fitting_readiness(discordant=10, sigma_points=None)
    assert r["status"] == "insufficient_data"
    assert "no published per-decision spread" in r["reason"]
    assert "required_discordant_decisions" not in r


# --------------------------------------------------------------------------
# 7.4 -- overrides
# --------------------------------------------------------------------------

def test_the_override_question_refuses_a_small_sample():
    """The question a small sample answers most flatteringly, asked by the
    person it flatters."""
    r = loop.override_analysis([{"followed": False}] * 3)
    assert r["status"] == "insufficient_data"
    assert "flatteringly" in r["reason"]
    assert "mean_percentile_when_overridden" not in r


def test_it_answers_once_the_floor_is_met():
    rows = ([{"followed": False, "outcome_percentile": 0.7,
              "override_kind": "captaincy"}] * 8
            + [{"followed": True, "outcome_percentile": 0.4}] * 4)
    r = loop.override_analysis(rows)
    assert r["reportable"] is True
    assert r["mean_percentile_when_overridden"] == 0.7
    assert r["mean_percentile_when_followed"] == 0.4
    assert r["override_kinds"] == {"captaincy": 8}


def test_an_empty_journal_is_not_an_error():
    r = loop.override_analysis([])
    assert r["status"] == "insufficient_data"
    assert r["total"] == 0
