"""T-12 — historical team ratings must use only pre-deadline information.

Batch 2's backtest applied the dataset's season-END ratings to every gameweek,
so a GW3 projection knew how the season finished. That is the largest optimism
in the Batch 2 numbers and it had to go before any parameter could be fitted.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from gaffer import histdata
from gaffer.model import features as F


def _frame(rows):
    return pd.DataFrame(rows)


def _match(gw, fixture, team_id, opp, home, gf, ga, pos="DEF", minutes=90):
    return {
        "GW": gw, "fixture": fixture, "team_id": team_id, "opponent_team": opp,
        "was_home": home, "minutes": minutes, "pos": pos,
        "team_h_score": gf if home else ga,
        "team_a_score": ga if home else gf,
        "expected_goals_conceded": 1.0, "element": team_id * 100,
    }


class FakeHistory(histdata.SeasonHistory):
    """A SeasonHistory over a hand-built frame, so every result is controlled."""

    def __init__(self, frame, team_ids=(1, 2, 3, 4)):
        teams = pd.DataFrame({"id": list(team_ids),
                              "name": [f"Club{t}" for t in team_ids]})
        super().__init__(season="2024-25", frame=frame, teams=teams,
                         name_to_id=dict(zip(teams["name"], teams["id"], strict=False)))

    def prior_rates(self):  # deterministic prior, no CSV dependency
        return histdata.PriorRates(1.4, 1.4, {}, set())


# --------------------------------------------------------------------------
# The core property
# --------------------------------------------------------------------------

def test_future_results_cannot_change_an_earlier_rating():
    """The property that makes the whole backtest honest."""
    early = [
        _match(1, 101, 1, 2, True, 3, 0), _match(1, 101, 2, 1, False, 0, 3),
        _match(2, 102, 1, 3, False, 1, 1), _match(2, 102, 3, 1, True, 1, 1),
    ]
    later = early + [
        # Team 1 collapses later in the season.
        _match(9, 109, 1, 4, True, 0, 6), _match(9, 109, 4, 1, False, 6, 0),
        _match(10, 110, 1, 2, False, 0, 5), _match(10, 110, 2, 1, True, 5, 0),
    ]
    a = FakeHistory(_frame(early)).team_form_ratings(5)
    b = FakeHistory(_frame(later)).team_form_ratings(5)
    assert a == b, "a rating for GW5 changed when later gameweeks were appended"


def test_rating_at_gw1_is_the_prior_only():
    h = FakeHistory(_frame([_match(1, 101, 1, 2, True, 4, 0)]))
    r = h.team_form_ratings(1)
    # No matches played before GW1, so every team sits on the shared prior.
    assert len({round(v, 6) for v in r["def_home"].values()}) == 1


def test_ratings_respond_to_results_already_played():
    rows = []
    for gw in range(1, 6):
        # Team 1 concedes nothing at home; team 2 ships four away.
        rows += [_match(gw, 100 + gw, 1, 2, True, 2, 0),
                 _match(gw, 100 + gw, 2, 1, False, 0, 2)]
    r = FakeHistory(_frame(rows)).team_form_ratings(6)
    # Higher defence rating = harder to score against.
    assert r["def_home"][1] > r["def_away"][2]
    assert r["att_home"][1] > r["att_away"][2]


def test_home_and_away_are_kept_separate():
    rows = [
        _match(1, 101, 1, 2, True, 5, 0), _match(1, 101, 2, 1, False, 0, 5),
        _match(2, 102, 1, 3, False, 0, 4), _match(2, 102, 3, 1, True, 4, 0),
    ]
    r = FakeHistory(_frame(rows)).team_form_ratings(3)
    # Team 1: dominant at home, dreadful away. The ratings must not merge.
    assert r["att_home"][1] > r["att_away"][1]
    assert r["def_home"][1] > r["def_away"][1]


def test_orientation_matches_teamcontext_expectations():
    """A stronger defence must make attacking against it HARDER."""
    rows = []
    for gw in range(1, 8):
        rows += [_match(gw, 100 + gw, 1, 2, False, 0, 0),   # team 1 concedes 0 away
                 _match(gw, 100 + gw, 2, 1, True, 0, 0)]
    rows += [_match(8, 200, 3, 4, True, 0, 5), _match(8, 200, 4, 3, False, 5, 0)]
    r = FakeHistory(_frame(rows)).team_form_ratings(9)
    ctx = F.TeamContext.from_ratings(
        att_home=r["att_home"], att_away=r["att_away"],
        def_home=r["def_home"], def_away=r["def_away"])
    # Facing team 1 away (they defend at home... team 1's away record here) vs
    # facing team 3, who ship five at home.
    tough = ctx.attack_multiplier(1, at_home=True)   # opponent defends away
    soft = ctx.attack_multiplier(3, at_home=False)   # opponent defends at home
    assert soft > tough, "the leakier defence must be the easier fixture"


def test_promoted_team_gets_a_documented_prior_not_a_fabricated_record():
    prior = histdata.PriorRates(1.4, 1.4, {1: (2.0, 0.8)}, promoted={2})
    assert prior.for_team(1) == (2.0, 0.8)
    gf, ga = prior.for_team(2)
    assert gf == pytest.approx(1.4 * histdata.PROMOTED_GF_FACTOR)
    assert ga == pytest.approx(1.4 * histdata.PROMOTED_GA_FACTOR)
    assert gf < 1.4 and ga > 1.4, "promoted sides score less and concede more"
    # An unknown team that is not flagged promoted falls back to the league mean.
    assert prior.for_team(99) == (1.4, 1.4)


def test_shrinkage_damps_a_single_freak_result():
    one = [_match(1, 101, 1, 2, True, 9, 0), _match(1, 101, 2, 1, False, 0, 9)]
    r = FakeHistory(_frame(one)).team_form_ratings(2, shrink_k=5.0)
    hard = FakeHistory(_frame(one)).team_form_ratings(2, shrink_k=0.0)
    # With shrinkage the 9-0 moves the rating far less than without.
    assert r["att_home"][1] < hard["att_home"][1]


def test_unplayed_rows_are_ignored():
    rows = [_match(1, 101, 1, 2, True, 3, 0, minutes=0),
            _match(1, 101, 2, 1, False, 0, 3, minutes=0)]
    r = FakeHistory(_frame(rows)).team_form_ratings(2)
    assert len({round(v, 6) for v in r["att_home"].values()}) == 1


# --------------------------------------------------------------------------
# Comparison with the leaky baseline
# --------------------------------------------------------------------------

def test_backtest_defaults_to_pre_deadline_ratings():
    import inspect

    from gaffer import backtest

    sig = inspect.signature(backtest.build_evaluation)
    assert sig.parameters["season_end_ratings"].default is False


def test_season_end_ratings_are_available_only_as_an_explicit_opt_in():
    import inspect

    from gaffer import backtest

    src = inspect.getsource(backtest._context_for)
    assert "season_end_ratings" in src
    assert "leak" in src.lower()


def test_ratings_are_on_the_fine_scale_so_gamma_is_not_applied():
    """The in-season regime must not accidentally trip the coarse branch."""
    rows = [_match(1, 101, 1, 2, True, 2, 1), _match(1, 101, 2, 1, False, 1, 2)]
    r = FakeHistory(_frame(rows)).team_form_ratings(2)
    ctx = F.TeamContext.from_ratings(
        att_home=r["att_home"], att_away=r["att_away"],
        def_home=r["def_home"], def_away=r["def_away"])
    assert ctx.coarse is False
    assert ctx.league_att > 100


def test_ratings_are_finite_and_positive():
    rows = []
    for gw in range(1, 4):
        rows += [_match(gw, 100 + gw, 1, 2, True, 0, 0),
                 _match(gw, 100 + gw, 2, 1, False, 0, 0)]
    r = FakeHistory(_frame(rows)).team_form_ratings(4)
    for key in ("att_home", "att_away", "def_home", "def_away"):
        for v in r[key].values():
            assert math.isfinite(v) and v > 0, f"{key} produced {v}"
