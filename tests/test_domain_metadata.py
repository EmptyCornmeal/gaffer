"""1.1 -- every published number states its domain.

SCOPE, from the §0.3 contract: no layer may widen the domain over which the
value beneath it was computed. That is only enforceable if the domain travels
WITH the value. Before this, `next_gw_xp` and `horizon_xp` were distinguishable
only by their names, the horizon behind the second was never published at all,
and `delta_ci95` did not say which of four different uncertainties it described.

A number whose domain is unstated is a number the reader will assume the domain
of -- and on 2026-09-01 that produced `p_first` (a next-gameweek probability)
under a tile reading "Win it".

These assert what the CODE PRODUCES, never what is committed in `data/`.
The first version of this file read the committed artifacts, which in CI are
the PREVIOUS run's -- tests run before the pipeline -- so a correct change
failed its own test until the artifact it had not yet written caught up. That
bootstrapping error blocked publishing, which is precisely the class of failure
P0 exists to prevent, so the lesson is recorded here rather than only fixed:
an artifact test that reads `data/` is testing the last run, not this change.
"""
from __future__ import annotations

from gaffer import config
from gaffer.export import artifacts


def test_meta_states_what_each_projection_is_a_projection_of(conn):
    meta = artifacts.build_meta(conn, "test-1", settings=config.Settings())
    dom = meta.get("projection_domain")
    assert dom, "build_meta must publish projection_domain"
    for key in ("next_gw_xp", "horizon_xp", "solver_value"):
        assert key in dom, f"{key} has no stated domain"
        assert isinstance(dom[key].get("horizon_gameweeks"), int)
        assert dom[key].get("measures")


def test_the_one_week_and_horizon_projections_do_not_claim_the_same_domain(conn):
    dom = artifacts.build_meta(
        conn, "test-1", settings=config.Settings())["projection_domain"]
    assert dom["next_gw_xp"]["horizon_gameweeks"] == 1
    assert dom["horizon_xp"]["horizon_gameweeks"] > 1


def test_the_solver_value_admits_it_is_decayed_and_selects_the_xi(conn):
    """The XI-selection refusal is only honest if the artifact says which
    estimator picked the eleven."""
    dom = artifacts.build_meta(
        conn, "test-1", settings=config.Settings())["projection_domain"]
    sv = dom["solver_value"]
    assert "decayed" in sv["weighting"]
    assert "starting XI" in sv["measures"]


def test_the_decision_margin_names_which_uncertainty_it_is():
    """`delta_ci95` is Monte-Carlo error on the mean difference -- how much of
    the edge is simulation noise. It is not the spread of possible football
    outcomes, which is far wider, and not parameter uncertainty."""
    from gaffer import decision

    cmp_ = decision.Comparison(
        move_expected=50.0, hold_expected=48.0, delta=2.0,
        delta_ci95=(1.0, 3.0), p_move_beats_hold=0.6, n_sims=2000,
        short_term_delta=2.0, horizon_delta=None, hit_cost=0,
    ).as_dict()
    assert cmp_["delta_ci95_interval_type"] == "monte_carlo", (
        "an interval must say whether it is simulation error, the spread of "
        "football outcomes, parameter uncertainty or sampling error")
    assert cmp_["domain"]["delta"]
    assert "2000" in cmp_["domain"]["measured_in"]


def test_placing_probabilities_carry_their_gameweek():
    """The horizon belongs in the KEY, not only in prose."""
    from gaffer.league import PlacingResult

    d = PlacingResult(
        p_first=0.62, p_target=0.62, target=1, expected_position=1.5,
        n_sims=2000, ci_halfwidth=0.02, basis="shared fixture scenarios",
        coverage_pct=100.0, gameweek=3,
    ).as_dict()
    assert "p_first_after_gw" in d
    assert d["domain"]["horizon"] == "next_gameweek"
    assert d["domain"]["gameweek"] == 3


def test_the_horizonless_names_are_gone_rather_than_aliased():
    """Keeping the old key beside the new one would leave the ambiguous number
    in the product, which is the thing being fixed."""
    from gaffer.league import PlacingResult

    d = PlacingResult(
        p_first=0.62, p_target=0.62, target=1, expected_position=1.5,
        n_sims=2000, ci_halfwidth=0.02, basis="b", coverage_pct=100.0,
        gameweek=3,
    ).as_dict()
    for gone in ("p_first", "p_target", "expected_position"):
        assert gone not in d, (
            f"{gone} reads as a season-end quantity and is a next-gameweek "
            "one; it was renamed, not aliased")
