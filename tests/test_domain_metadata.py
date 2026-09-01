"""1.1 -- every published number that is not season-wide states its domain.

SCOPE, from the §0.3 contract: no layer may widen the domain over which the
value beneath it was computed. That is only enforceable if the domain travels
WITH the value. Before this, `next_gw_xp` and `horizon_xp` were distinguishable
only by their names, the horizon behind the second was never published at all,
and `delta_ci95` did not say which of four different uncertainties it described.

A number whose domain is unstated is a number the reader will assume the domain
of -- and on 2026-09-01 that produced `p_first` (a next-gameweek probability)
under a tile reading "Win it".
"""
from __future__ import annotations

import json

import pytest

from gaffer import config

DATA = config.REPO_ROOT / "data"


def _load(name: str):
    path = DATA / name
    if not path.exists():
        pytest.skip(f"no {name} artifact")
    return json.loads(path.read_text(encoding="utf-8"))


def test_meta_states_what_each_projection_is_a_projection_of():
    dom = _load("meta.json").get("projection_domain")
    assert dom, "meta.json must publish projection_domain"
    for key in ("next_gw_xp", "horizon_xp", "solver_value"):
        assert key in dom, f"{key} has no stated domain"
        assert isinstance(dom[key].get("horizon_gameweeks"), int)
        assert dom[key].get("measures")


def test_the_one_week_and_horizon_projections_do_not_claim_the_same_domain():
    dom = _load("meta.json")["projection_domain"]
    assert dom["next_gw_xp"]["horizon_gameweeks"] == 1
    assert dom["horizon_xp"]["horizon_gameweeks"] > 1


def test_the_solver_value_admits_it_is_decayed_and_selects_the_xi():
    """The XI-selection refusal is only honest if the artifact says which
    estimator picked the eleven."""
    sv = _load("meta.json")["projection_domain"]["solver_value"]
    assert "decayed" in sv["weighting"]
    assert "starting XI" in sv["measures"]


def test_the_decision_margin_names_which_uncertainty_it_is():
    dec = _load("decision.json").get("decision") or {}
    cmp_ = dec.get("comparison") or {}
    if not cmp_:
        pytest.skip("no comparison in this decision")
    assert cmp_.get("delta_ci95_interval_type") == "monte_carlo", (
        "an interval must say whether it is simulation error, the spread of "
        "football outcomes, parameter uncertainty or sampling error")
    assert (cmp_.get("domain") or {}).get("delta")


def test_placing_probabilities_carry_their_gameweek():
    strat = _load("strategy.json")
    leagues = strat.get("leagues") or []
    if not leagues:
        pytest.skip("no leagues configured")
    for lg in leagues:
        placing = lg.get("placing") or {}
        assert "p_first_after_gw" in placing, (
            "the horizon belongs in the KEY, not only in prose")
        assert (placing.get("domain") or {}).get("horizon") == "next_gameweek"


def test_no_artifact_still_publishes_the_horizonless_names():
    """The old names must be gone, not aliased. Keeping one beside the new
    name would leave the ambiguous number in the product."""
    strat = _load("strategy.json")
    for lg in strat.get("leagues") or []:
        placing = lg.get("placing") or {}
        for gone in ("p_first", "p_target", "expected_position"):
            assert gone not in placing, (
                f"{gone} reads as a season-end quantity and is a next-gameweek "
                "one; it was renamed, not aliased")
