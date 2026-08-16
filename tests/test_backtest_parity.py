"""T-09 — the backtest must score the SHIPPED model, with no leakage.

The audited harness scored a substitute: `_RowCtx` returned ml.py's fixture
multiplier (clip 0.6-1.7, no gamma) and a different clean-sheet formula, with
base_*, DEFCON and availability all zeroed. These tests pin the replacement to
the production code path.
"""

from __future__ import annotations

import pandas as pd
import pytest

from gaffer import backtest, histdata, leakage
from gaffer.model import features as F
from gaffer.model import projection

# A frozen decision point. Nothing here is derived from a live API or the clock.
FROZEN_PLAYER = {
    "position": "MID",
    "minutes": 900.0,
    "starts": 10.0,
    "base_minutes": 2400.0,
    "base_starts": 28.0,
    "base_xg90": 0.31,
    "base_xa90": 0.22,
    "price": 75.0,
    "xg_per_90": 0.42,
    "xa_per_90": 0.18,
    "defcon_per_90": 4.5,
    "team_id": 1,
}
FROZEN_RATINGS = {
    "att_home": {1: 1200.0, 2: 1050.0},
    "att_away": {1: 1150.0, 2: 1000.0},
    "def_home": {1: 1180.0, 2: 1020.0},
    "def_away": {1: 1120.0, 2: 980.0},
}


# --------------------------------------------------------------------------
# Parity: the backtest's call reproduces the live projection exactly
# --------------------------------------------------------------------------

def _frozen_ctx() -> F.TeamContext:
    return F.TeamContext.from_ratings(**FROZEN_RATINGS, team_xgc={1: 1.25, 2: 1.55})


def test_backtest_reproduces_the_live_projection_exactly():
    """Same inputs through both entry points must agree to floating point."""
    ctx = _frozen_ctx()
    fx = F.Fixture(gw=12, opponent_id=2, at_home=True, fdr=3)
    avail = projection._availability("a", None)

    live = projection._project_one_fixture(FROZEN_PLAYER, fx, ctx, avail, 11)

    frame = pd.DataFrame([{
        "pos": FROZEN_PLAYER["position"], "GW": 12, "opponent_team": 2,
        "was_home": True, "value": FROZEN_PLAYER["price"],
        "min_td": FROZEN_PLAYER["minutes"], "starts_td": FROZEN_PLAYER["starts"],
        "xg90_td": FROZEN_PLAYER["xg_per_90"], "xa90_td": FROZEN_PLAYER["xa_per_90"],
        "defcon90_td": FROZEN_PLAYER["defcon_per_90"],
        "base_minutes": FROZEN_PLAYER["base_minutes"],
        "base_starts": FROZEN_PLAYER["base_starts"],
        "base_xg90": FROZEN_PLAYER["base_xg90"],
        "base_xa90": FROZEN_PLAYER["base_xa90"],
        "team_id": 1,
    }])
    via_backtest = backtest.project_rows(frame, ctx, fixtures_played=11)

    assert via_backtest.iloc[0] == pytest.approx(live["exp_points"], abs=1e-12)
    assert live["exp_points"] > 0


def test_backtest_uses_the_real_team_context_not_a_stand_in():
    """The old _RowCtx returned a constant; the real class applies gamma+clamp."""
    ctx = _frozen_ctx()
    assert isinstance(ctx, F.TeamContext)
    # Different opponents and venues must give different multipliers.
    a = ctx.attack_multiplier(2, at_home=True)
    b = ctx.attack_multiplier(1, at_home=False)
    assert a != b
    # And they must respect the shipped clamp.
    assert F.STRENGTH_CLAMP[0] <= a <= F.STRENGTH_CLAMP[1]


def test_from_ratings_matches_build_semantics():
    """The new constructor must produce the same regime logic as build()."""
    fine = F.TeamContext.from_ratings(**FROZEN_RATINGS)
    assert fine.coarse is False  # 1000-scale ratings
    coarse = F.TeamContext.from_ratings(
        att_home={1: 3, 2: 4}, att_away={1: 3, 2: 4},
        def_home={1: 3, 2: 4}, def_away={1: 3, 2: 4},
    )
    assert coarse.coarse is True  # 1-5 scale
    # mean over att_home + att_away = (1200+1050+1150+1000)/4
    assert fine.league_att == pytest.approx(1100.0)
    assert fine.league_def == pytest.approx((1180 + 1020 + 1120 + 980) / 4)


def test_protected_fixture_strength_constants_are_untouched():
    """T-12 shipped NO change to these: the sweep found no evidence to move the
    clamp, and gamma is inert in the in-season regime the data covers."""
    assert F.STRENGTH_GAMMA == 1.7
    assert F.STRENGTH_CLAMP == (0.5, 1.85)


def test_model_version_reflects_the_scoring_change():
    """The rule, not the number: when shipped projection behaviour changes the
    version changes with it, so a stored artifact can never be mistaken for one
    produced by different arithmetic.

    0.2 added goals conceded, saves, cards, OG and penalties (T-13). 0.3 stopped
    reading an unmeasurable zero in the prior-season baseline as a measurement
    (M3). 0.4 fixed the start-rate denominator to count fixtures rather than
    gameweeks (M3b). Each moves the projection for real players.
    """
    assert projection.MODEL_VERSION == "heuristic-0.4"
    assert projection.MODEL_VERSION not in (
        "heuristic-0.1", "heuristic-0.2", "heuristic-0.3")


def test_availability_path_is_the_real_one():
    assert projection._availability("a", None) == 1.0
    assert projection._availability("i", None) == 0.0
    assert projection._availability("d", None) == 0.5
    assert projection._availability("a", 25) == 0.25


# --------------------------------------------------------------------------
# Leakage
# --------------------------------------------------------------------------

@pytest.mark.parametrize("col", [
    "minutes", "total_points", "bps", "goals_scored", "assists",
    "expected_goals", "expected_assists", "clean_sheets", "bonus",
    "expected_goals_conceded", "ict_index", "saves", "yellow_cards",
    # T-26: the archive's own expected-points column moves with the result, not
    # with the fixture. It is a target at best. See tests/test_ml_removed.py.
    "xP",
])
def test_post_match_columns_are_rejected_as_features(col):
    assert leakage.is_post_match(col) is True
    with pytest.raises(leakage.LeakageError) as exc:
        leakage.assert_no_leakage(["element", "GW", col])
    assert col in str(exc.value)


@pytest.mark.parametrize("col", [
    "element", "GW", "value", "was_home", "opponent_team", "position",
    "selected", "ep_next", "min_td", "starts_td", "xg90_td", "xa90_td",
    "defcon90_td", "pts_td", "r_min", "r_pts", "base_xg90", "base_minutes",
])
def test_pre_deadline_columns_are_allowed(col):
    assert leakage.is_post_match(col) is False
    leakage.assert_no_leakage(["element", col])  # must not raise


def test_adapter_feature_set_is_leak_free():
    """The exact column list the backtest consumes."""
    assert leakage.check_features(histdata.FEATURE_COLUMNS) == []
    histdata.assert_features_leak_free(histdata.FEATURE_COLUMNS)


def test_target_columns_are_recognised_as_post_match():
    for col in histdata.TARGET_COLUMNS:
        assert leakage.is_post_match(col) is True


def test_player_inputs_carry_no_same_gameweek_outcome():
    """Every value fed to the model must be an as-of-decision-time aggregate."""
    row = pd.DataFrame([{
        "pos": "MID", "min_td": 900.0, "starts_td": 10.0, "xg90_td": 0.4,
        "xa90_td": 0.2, "defcon90_td": 4.0, "base_minutes": 2000.0,
        "base_starts": 25.0, "base_xg90": 0.3, "base_xa90": 0.2,
        "value": 75.0, "team_id": 1,
    }]).itertuples(index=False)
    inputs = backtest._player_inputs(next(row))
    # The model dict's keys are all season-to-date / prior-season / static.
    assert set(inputs) == {
        "position", "minutes", "starts", "base_minutes", "base_starts",
        "base_xg90", "base_xa90", "base_season", "price", "xg_per_90",
        "xa_per_90", "defcon_per_90", "team_id",
    }
    assert inputs["minutes"] == 900.0  # to-date, not this gameweek's minutes
    # The row above carries no `base_season`, as an older frame would not. It
    # must come back as unrecorded rather than raise, and unrecorded must never
    # be mistaken for a season that could not report a statistic.
    assert inputs["base_season"] == ""


def test_the_backtest_passes_the_baseline_season_through():
    """Otherwise the harness silently takes a more forgiving zero-vs-missing
    branch than the code that ships, and measures something that is not the
    model. `base_season` names a season that finished before this one began, so
    it is pre-deadline by construction."""
    assert "base_season" in histdata.FEATURE_COLUMNS
    assert not leakage.check_features(histdata.FEATURE_COLUMNS)
    row = pd.DataFrame([{
        "pos": "MID", "min_td": 0.0, "starts_td": 0.0, "xg90_td": 0.0,
        "xa90_td": 0.0, "defcon90_td": 0.0, "base_minutes": 2000.0,
        "base_starts": 0.0, "base_xg90": 0.0, "base_xa90": 0.0,
        "base_season": "2021/22", "value": 75.0, "team_id": 1,
    }]).itertuples(index=False)
    assert backtest._player_inputs(next(row))["base_season"] == "2021/22"


def test_season_to_date_never_includes_the_current_row():
    df = pd.DataFrame({
        "element": [1, 1, 1], "GW": [1, 2, 3], "fixture": [1, 2, 3],
        "minutes": [90, 90, 90], "total_points": [2, 6, 10],
        "starts": [1, 1, 1], "expected_goals": [0.1, 0.2, 0.3],
        "expected_assists": [0.0, 0.1, 0.2],
    })
    out = histdata._season_to_date(df)
    # GW1 sees nothing; GW3 sees GW1+GW2 only.
    assert out.loc[out["GW"] == 1, "min_td"].iloc[0] == 0
    assert out.loc[out["GW"] == 3, "min_td"].iloc[0] == 180
    assert out.loc[out["GW"] == 3, "pts_td"].iloc[0] == 8  # 2 + 6, not 18


# --------------------------------------------------------------------------
# M1 — the pre-season decision is actually evaluated
# --------------------------------------------------------------------------

def test_the_evaluation_starts_at_gameweek_one():
    """For five schema versions this constant was 2 while the comment beside it
    said GW1 was included. Every accuracy number the project has quoted about
    itself therefore came from a harness that skipped the one decision made with
    no season-to-date information at all — the evening a whole squad is picked
    from scratch. The constant is the claim; assert it."""
    assert backtest.FIRST_DECISION_GW == 1


def _ev_frame(naive_values):
    """Minimal evaluation frame: GW1 at h=1, plus an in-season row to be excluded."""
    n = len(naive_values)
    rows = {
        "element": list(range(n)) + [900],
        "decision_gw": [1] * n + [7],
        "target_gw": [1] * n + [7],       # at h=1 the target IS the decision gw
        "horizon": [1] * n + [1],
        "pred": [0.5 * i for i in range(n)] + [3.0],
        "actual": [float(i % 4) for i in range(n)] + [9.0],
        "minutes": [90 if i % 2 else 0 for i in range(n)] + [90],
        "naive": list(naive_values) + [2.0],
        "value": [50] * n + [50],
        "team_id": [1 + (i % 4) for i in range(n)] + [1],
        "pos": ["MID"] * n + ["MID"],
    }
    return pd.DataFrame(rows)


def test_the_pre_season_block_reports_gameweek_one_alone():
    ev = _ev_frame([0.0] * 12)
    block = backtest._pre_season_block(ev)
    assert block["decision_gw"] == 1
    assert block["n"] == 12, "the in-season row must not be counted"
    assert block["rank_corr"]["gaffer"] != 0
    assert block["zero_minute_share_pct"] == 50.0


def test_an_undefined_naive_baseline_is_explained_not_scored():
    """Cumulative season-to-date points-per-game is 0 for every player before a
    ball is kicked. Publishing a rank correlation against a constant, or a
    decision made by ranking one, would invent a baseline that does not exist —
    and "no baseline" must not read like "beat the baseline"."""
    block = backtest._pre_season_block(_ev_frame([0.0] * 12))
    assert "naive" not in block["rank_corr"]
    assert "naive" not in block["mae"]
    assert block["naive_baseline"] != "defined"
    assert "predicts 0 for every player" in block["naive_baseline"]


def test_a_defined_naive_baseline_is_still_reported():
    """The omission above is conditional on the data, not hardcoded: a season
    whose GW1 did carry a usable baseline must still be measured against it."""
    block = backtest._pre_season_block(_ev_frame([float(i) for i in range(12)]))
    assert block["naive_baseline"] == "defined"
    assert "naive" in block["rank_corr"] and "naive" in block["mae"]


def test_one_gameweek_of_decisions_is_labelled_as_such():
    """`captain_accuracy_pct` over a single decision is 0 or 100 and is not a
    rate. A page showing "100% captain accuracy" from one evening would be worse
    than showing nothing, so the artifact carries the caveat with the number."""
    block = backtest._pre_season_block(_ev_frame([0.0] * 12))
    assert "ONE gameweek" in block["decisions_caveat"]


def test_missing_history_fails_loudly(monkeypatch, tmp_path):
    """No silent substitution of current-season data."""
    monkeypatch.setattr(histdata.config, "HISTORY_DIR", tmp_path)
    with pytest.raises(histdata.MissingHistoryError) as exc:
        histdata.load_season("2024-25")
    assert "not found" in str(exc.value)
    assert "will not substitute" in str(exc.value)


# --------------------------------------------------------------------------
# Constrained selection
# --------------------------------------------------------------------------

def _pool():
    """A pool whose cheapest legal 15 costs £68.8m, so £100.0m is feasible."""
    rows, pid = [], 1
    for pos, n, price in (("GKP", 4, 40), ("DEF", 10, 40),
                          ("MID", 10, 45), ("FWD", 6, 45)):
        for i in range(n):
            rows.append({
                "element": pid, "pos": pos, "value": price + i * 2,
                "team_id": (pid % 8) + 1, "pred": 10 - i * 0.3,
                "actual": 5.0,
            })
            pid += 1
    return pd.DataFrame(rows).set_index("element")


def test_selected_squad_obeys_budget_quota_and_club_limit():
    g = _pool()
    squad = backtest._select_squad(g, "pred")
    assert squad is not None
    sub = g.loc[squad]
    assert len(squad) == backtest.SQUAD_SIZE
    assert sub["value"].sum() <= backtest.BUDGET
    for pos, n in backtest.QUOTA.items():
        assert (sub["pos"] == pos).sum() == n
    assert sub["team_id"].value_counts().max() <= backtest.CLUB_LIMIT


def test_best_xi_is_a_legal_formation():
    g = _pool()
    squad = backtest._select_squad(g, "pred")
    xi = backtest._best_xi(g, squad, "pred")
    sub = g.loc[xi]
    assert len(xi) == 11
    for pos, lo in backtest.XI_MIN.items():
        assert (sub["pos"] == pos).sum() >= lo
        assert (sub["pos"] == pos).sum() <= backtest.XI_MAX[pos]
    assert set(xi) <= set(squad)


def test_infeasible_budget_returns_none_rather_than_cheating(monkeypatch):
    g = _pool()
    monkeypatch.setattr(backtest, "BUDGET", 10)
    assert backtest._select_squad(g, "pred") is None
