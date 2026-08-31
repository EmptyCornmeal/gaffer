"""A13 — the point estimate and the distribution are one model, not two.

``projection._project_one_fixture`` and ``simulate._sample_fixture`` build from
the same ``projection.fixture_rates`` bundle. They disagreed by up to 1.25 points
on the live 2026/27 GW3 artifact — Davis (DEF) published at 1.11 beside a
simulated mean of 2.36, half a clean sheet apart — because the sampler drew six
of the projection's eleven components, invented a second bonus formula for the
seventh, and gated two more on starting where the projection gates them on the
hour. Three keepers therefore shipped an expectation ABOVE their own simulated
90th percentile.

These tests do not check that the model is right. They check that the two
readings of it cannot drift apart again.
"""
import math

import numpy as np
import pytest

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection, simulate

N_TIGHT = 200_000       # enough that this tests the identity, not the noise
N_SHIPPED = 3000        # what `simulate_next_gw` actually runs

FIXTURE = F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3)


def _ctx(xgc=1.4):
    """Two average teams, so the fixture effect is neutral and only the rate
    bundle can move a number. ``xgc`` sets how leaky they are, which is what
    drives both the clean sheet and the goals conceded."""
    ids = (1, 2)
    return F.TeamContext.from_ratings(
        att_home={i: 1100.0 for i in ids}, att_away={i: 1050.0 for i in ids},
        def_home={i: 1100.0 for i in ids}, def_away={i: 1050.0 for i in ids},
        team_xgc={i: xgc for i in ids})


def _player(**over):
    base = {
        "position": "DEF", "price": 55, "team_id": 1, "status": "a",
        "chance_playing": None, "minutes": 2500, "starts": 30,
        "base_minutes": 2500, "base_starts": 30, "base_xg90": 0.10,
        "base_xa90": 0.10, "base_season": "2025/26", "base_defcon90": 9.0,
        "xg_per_90": 0.10, "xa_per_90": 0.10, "defcon_per_90": 9.0,
        "saves_per_90": 0.0, "yellow_per_90": 0.20, "red_per_90": 0.02,
        "og_per_90": 0.03, "pen_save_per_90": 0.0, "pen_miss_per_90": 0.02,
        "bonus_per_90": 0.30,
    }
    base.update(over)
    return base


def _pair(ctx=None, fx=FIXTURE, **over):
    """The two readings of one rate bundle, for the same player-fixture."""
    p = _player(**over)
    ctx = ctx or _ctx()
    avail = projection._availability(p["status"], p["chance_playing"])
    return (projection._project_one_fixture(p, fx, ctx, avail, 30),
            projection.fixture_rates(p, fx, ctx, avail, 30))


def _keeper(**over):
    over.setdefault("saves_per_90", 3.1)
    over.setdefault("pen_save_per_90", 0.03)
    return _pair(position="GKP", price=45, **over)


def _sample(rates, n=N_TIGHT, seed=11):
    return simulate._sample_fixture(rates, n, np.random.default_rng(seed))


def _bare(rates, *keep):
    """The bundle with every scoring rate switched off except the ones named, so
    a single term can be read out of the sampled mean on its own. Appearance
    cannot be switched off — it is the minutes gate — so it is always in."""
    off = dict(rates, exp_goals=0.0, exp_assists=0.0, p_cs=0.0, defcon_p_hit=0.0,
               conceded_lam=0.0, saves_lam=0.0, yellow_rate=0.0, red_rate=0.0,
               og_rate=0.0, pen_miss_rate=0.0, pen_save_rate=0.0, bonus_rate=0.0)
    for key in keep:
        off[key] = rates[key]
    return off


def _appearance(r):
    return (config.APPEARANCE_LONG * r["p60"]
            + config.APPEARANCE_SHORT * (r["p_play"] - r["p60"]))


def _expected(r):
    """The projection's sum, rebuilt from the rate bundle alone.

    A deliberate second opinion rather than a call into
    `_project_one_fixture`: a test that reads the code under test cannot notice
    the code under test dropping a term, which is the defect being guarded. It
    also works on a doctored bundle, so a single component can be isolated by
    zeroing the rest — the bonus proxy included, since the proxy reads the other
    returns and has no rate key of its own to switch off.
    """
    mf = r["mins_frac"]
    goals = r["exp_goals"] * r["goal_pts_per"]
    assists = r["exp_assists"] * r["assist_pts_per"]
    cs = r["p_cs"] * r["cs_pts_per"] * r["p60"]
    dc = r["defcon_p_hit"] * r["defcon_pts"]
    hist = r["bonus_rate"] * mf
    total = (
        _appearance(r) + goals + assists + cs + dc
        + projection.bonus_points(r["pos"], r["exp_goals"], r["exp_assists"],
                                  dc, cs, r["cs_pts_per"], hist, hist > 0)
        + F.expected_floor_div(r["conceded_lam"], config.CONCEDED_PER_PENALTY)
        * config.CONCEDED_PENALTY
        + F.expected_floor_div(r["saves_lam"], config.SAVES_PER_POINT)
        * config.SAVE_POINTS
        + r["yellow_rate"] * mf * config.YELLOW_POINTS
        + r["red_rate"] * mf * config.RED_POINTS
        + r["og_rate"] * mf * config.OWN_GOAL_POINTS
        + r["pen_miss_rate"] * mf * config.PENALTY_MISS_POINTS
    )
    if r["pos"] == "GKP":
        total += r["pen_save_rate"] * mf * config.PENALTY_SAVE_POINTS
    return total


# --------------------------------------------------------------------------
# the identity itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pos,price,saves", [
    ("GKP", 45, 3.1), ("DEF", 55, 0.0), ("MID", 70, 0.0), ("FWD", 80, 0.0)])
def test_the_sampled_mean_is_the_point_estimate(pos, price, saves):
    """One bundle, two readings, one answer. This is the whole defect."""
    model, rates = _pair(position=pos, price=price, saves_per_90=saves)
    totals = _sample(rates)
    tol = simulate.sampling_tolerance(float(totals.std()), N_TIGHT)
    assert abs(totals.mean() - model["exp_points"]) < tol, (
        f"{pos}: model {model['exp_points']:.4f} vs sim {totals.mean():.4f}")


@pytest.mark.parametrize("pos,price", [
    ("GKP", 45), ("DEF", 55), ("MID", 70), ("FWD", 80)])
def test_a_rotation_risk_agrees_too(pos, price):
    """The minutes gate is where the two parted company hardest, so the identity
    is asserted again on a player only half likely to feature."""
    model, rates = _pair(position=pos, price=price, status="d",
                         minutes=600, starts=6, base_minutes=900, base_starts=9,
                         saves_per_90=3.1 if pos == "GKP" else 0.0)
    assert 0.0 < rates["p_play"] < 0.5, "this must be a real rotation risk"
    totals = _sample(rates)
    tol = simulate.sampling_tolerance(float(totals.std()), N_TIGHT)
    assert abs(totals.mean() - model["exp_points"]) < tol


def test_a_defender_at_a_leaky_club_does_not_read_high_in_the_simulation():
    """The Davis case. Goals conceded is a term only the projection carried, and
    it is NEGATIVE, so leaving it out of the sampler read a whole back four the
    better part of a clean sheet too high."""
    ctx = F.TeamContext.from_ratings(
        att_home={1: 1000.0, 2: 1400.0}, att_away={1: 950.0, 2: 1350.0},
        def_home={1: 900.0, 2: 1300.0}, def_away={1: 850.0, 2: 1250.0},
        team_xgc={1: 2.1, 2: 0.9})
    fx = F.Fixture(gw=1, opponent_id=2, at_home=False, fdr=5)
    model, rates = _pair(ctx=ctx, fx=fx, position="DEF")
    assert rates["conceded_lam"] > 0.5, "this fixture is meant to be leaky"
    assert model["exp_conceded_pts"] < -0.2, "the projection must charge for it"
    totals = _sample(rates)
    tol = simulate.sampling_tolerance(float(totals.std()), N_TIGHT)
    assert abs(totals.mean() - model["exp_points"]) < tol


def test_a_keeper_is_not_published_above_his_own_ceiling():
    """The Dubravka / Sánchez / Martinez case in miniature. Saves are a term only
    the projection carried and for a keeper they outweigh the goals conceded, so
    his expectation sat above his own simulated 90th percentile — which is close
    to arithmetically impossible, and three keepers did it on the live GW3
    artifact."""
    model, rates = _keeper()
    assert model["exp_saves_pts"] > 0.3, "this keeper is meant to make saves"
    d = simulate._summarise(_sample(rates, n=N_SHIPPED))
    assert round(model["exp_points"], 2) <= d["ceiling"]


# --------------------------------------------------------------------------
# every component, not six of eleven
# --------------------------------------------------------------------------

class _Spy(dict):
    """A rate bundle that remembers which rates were actually read."""

    def __init__(self, d):
        super().__init__(d)
        self.read: set[str] = set()

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)


#: Every rate in the bundle that carries a scoring component. A term the sampler
#: never reads is invisible to any mean test on a player for whom it happens to
#: be small, which is how six of these stayed missing through a live season.
_SCORING_RATES = {
    "p_start", "p_play", "exp_minutes",      # minutes and the appearance point
    "exp_goals", "exp_assists",              # attacking returns
    "p_cs",                                  # clean sheet
    "defcon_p_hit",                          # DEFCON
    "conceded_lam", "saves_lam",             # T-13 goals conceded and saves
    "yellow_rate", "red_rate", "og_rate",    # discipline
    "pen_miss_rate", "pen_save_rate",        # penalties
    "bonus_rate",                            # the historical half of the bonus
}


def test_the_sampler_reads_every_rate_that_carries_points():
    _, rates = _keeper()
    spy = _Spy(rates)
    simulate._sample_fixture(spy, 64, np.random.default_rng(3))
    missing = _SCORING_RATES - spy.read
    assert not missing, f"the sampler never reads {sorted(missing)}"


def test_the_sampler_reads_the_lambda_the_projection_integrates():
    """`conceded_units` and `save_units` are E[floor(X/d)]. A floor is not
    linear, so the lambda cannot be recovered from the expectation — the bundle
    has to carry it, or the sampler is drawing a different X."""
    _, rates = _keeper()
    assert F.expected_floor_div(
        rates["conceded_lam"], config.CONCEDED_PER_PENALTY
    ) == pytest.approx(rates["conceded_units"], abs=1e-12)
    assert F.expected_floor_div(
        rates["saves_lam"], config.SAVES_PER_POINT
    ) == pytest.approx(rates["save_units"], abs=1e-12)


def test_a_bundle_without_the_new_lambdas_raises_rather_than_scoring_zero():
    _, rates = _keeper()
    stripped = {k: v for k, v in rates.items() if k != "saves_lam"}
    with pytest.raises(KeyError):
        simulate._sample_fixture(stripped, 16, np.random.default_rng(1))


def test_the_rebuilt_sum_is_the_projections_sum():
    """The second opinion agrees with `_project_one_fixture`, so the isolation
    tests below are measuring the sampler and not each other."""
    for over in ({}, {"position": "GKP", "price": 45, "saves_per_90": 3.1},
                 {"position": "FWD", "price": 80}):
        model, rates = _pair(**over)
        assert _expected(rates) == pytest.approx(model["exp_points"], abs=1e-9)


@pytest.mark.parametrize("term,keep", [
    ("exp_conceded_pts", "conceded_lam"),
    ("exp_saves_pts", "saves_lam"),
])
def test_the_terms_the_sampler_used_to_omit_carry_their_own_weight(term, keep):
    """Read each formerly-missing term out of the sampled mean on its own and
    check it against the projection's value for the same term."""
    model, rates = _keeper()
    assert abs(model[term]) > 0.15, f"{term} must be material to be testable"
    bare = _bare(rates, keep)
    totals = _sample(bare)
    assert totals.mean() == pytest.approx(_expected(bare), abs=0.02)
    assert _expected(bare) - _expected(_bare(rates)) == pytest.approx(
        model[term], abs=1e-9)


def test_cards_and_rare_events_are_sampled_too():
    """Four small negative terms that were also missing. Grouped because the
    projection groups them, and because each on its own is inside the noise."""
    model, rates = _pair(position="DEF", yellow_per_90=0.30, red_per_90=0.05,
                         og_per_90=0.05, pen_miss_per_90=0.04)
    penalty = model["exp_cards_pts"] + model["exp_misc_pts"]
    assert penalty < -0.15, "these must be material to be testable"
    keys = ("yellow_rate", "red_rate", "og_rate", "pen_miss_rate")
    assert all(rates[k] > 0 for k in keys)
    bare = _bare(rates, *keys)
    assert _sample(bare).mean() == pytest.approx(_expected(bare), abs=0.02)
    assert _expected(bare) - _expected(_bare(rates)) == pytest.approx(
        penalty, abs=1e-9)


# --------------------------------------------------------------------------
# the specific mechanisms that were wrong
# --------------------------------------------------------------------------

def test_the_clean_sheet_is_gated_on_the_hour_not_on_starting():
    """FPL pays a clean sheet for 60 minutes and the projection gates it on
    `p60`. The sampler gated it on `started`, a different and larger number — a
    starting defender is hooked before the hour about 5% of the time."""
    _, rates = _pair(ctx=_ctx(xgc=0.7), position="DEF")
    assert rates["p60"] < rates["p_start"], "the two must actually differ"

    # Differenced against the same bundle with the clean sheet switched off, so
    # what is measured is the clean sheet and nothing else. Both samples run the
    # same seed and draw the same number of variates, so the minutes draws are
    # identical and cancel exactly — which matters, because a wrong appearance
    # gate and a wrong clean-sheet gate are errors of the same size in opposite
    # directions and an undifferenced mean lets them hide each other.
    with_cs = _bare(rates, "p_cs")
    without = _bare(rates)
    delta = _sample(with_cs).mean() - _sample(without).mean()
    on_the_hour = _expected(with_cs) - _expected(without)
    # the same difference with the gate the sampler used to use, and ONLY that
    on_starting = (_expected(dict(with_cs, p60=with_cs["p_start"]))
                    - _expected(dict(without, p60=without["p_start"])))
    assert abs(on_starting - on_the_hour) > 0.05, "the hypotheses must differ"
    assert abs(delta - on_the_hour) < 0.02
    assert abs(delta - on_the_hour) < abs(delta - on_starting)


def test_the_long_appearance_point_is_gated_on_the_hour_too():
    """Same rule, same gate: 2 points for 60 minutes, 1 for anything less."""
    _, rates = _pair(position="MID", price=70)
    bare = _bare(rates)
    totals = _sample(bare)
    assert _expected(bare) == pytest.approx(_appearance(rates), abs=1e-12)
    assert abs(totals.mean() - _appearance(rates)) < 0.02
    assert abs(totals.mean() - rates["p_start"] * 2) > 0.02, (
        "gating on `started` would pay every starter the long appearance point")
    assert set(np.unique(totals)) <= {0.0, 1.0, 2.0}


def test_defcon_is_not_gated_twice():
    """`defcon_p_hit` is already built on minutes-scaled volume, so it is an
    unconditional per-fixture probability. The sampler used to multiply it by a
    `played` draw as well, charging the bench universe to it twice."""
    _, rates = _pair(position="DEF", status="d")   # 50% availability
    assert rates["p_play"] < 0.7, "this must be a real rotation risk"
    # p_hit is forced rather than modelled: a rotation risk's own DEFCON
    # probability is too small for the double gate to show above the noise, and
    # the gate is what is on trial here, not the rate behind it.
    bare = dict(_bare(rates), defcon_p_hit=0.40)
    mean = _sample(bare).mean()
    once = _expected(bare)
    twice = _expected(dict(bare, defcon_p_hit=0.40 * rates["p_play"]))
    assert abs(once - twice) > 0.2, "the hypotheses must differ"
    assert abs(mean - once) < 0.02
    assert abs(mean - once) < abs(mean - twice)


def test_bonus_is_one_formula_read_twice():
    """There is a single `projection.bonus_points`. The point estimate is what
    you get by feeding it expectations; the sampler feeds it draws."""
    model, rates = _pair(position="DEF")
    hist = rates["bonus_rate"] * rates["mins_frac"]
    assert hist > 0, "the history blend must be live for this to test it"
    rebuilt = projection.bonus_points(
        rates["pos"], rates["exp_goals"], rates["exp_assists"],
        rates["defcon_p_hit"] * rates["defcon_pts"],
        rates["p_cs"] * rates["cs_pts_per"] * rates["p60"], rates["cs_pts_per"],
        hist, True)
    assert rebuilt == pytest.approx(model["exp_bonus_pts"], abs=1e-12)


def test_the_sampled_bonus_has_the_published_bonus_as_its_mean():
    """`bonus_points` is linear in all four returns, which is the entire reason
    one function can serve both an expectation and a draw: feed it draws whose
    means are the expectations and it returns the expectation.

    It used to be two functions. The sampler carried `round(0.9*goals +
    0.6*assists + 0.4*cs + 0.3*defcon)` capped at 3, and the second half of this
    test is what that cost — the largest term left once goals conceded and saves
    were drawn, worth up to 0.41 a player on the live artifact.
    """
    rng = np.random.default_rng(5)
    n, lam_g, lam_a = 200_000, 0.60, 0.30
    goals = rng.poisson(lam_g, n)
    assists = rng.poisson(lam_a, n)
    cs = rng.random(n) < 0.0        # a forward keeps no clean sheets
    dc = rng.random(n) < 0.0
    args = (config.DEFCON_POINTS, float(config.CS_POINTS["FWD"]), 0.0, False)
    sampled = projection.bonus_points(
        "FWD", goals, assists, dc * args[0], cs * args[1], 1.0, *args[2:]).mean()
    expected = projection.bonus_points(
        "FWD", lam_g, lam_a, 0.0, 0.0, 1.0, *args[2:])
    assert sampled == pytest.approx(expected, abs=0.01)

    superseded = np.clip(np.round(0.9 * goals + 0.6 * assists), 0, 3)
    assert abs(superseded.mean() - expected) > 0.15, (
        "the rule this replaced must actually have disagreed, or the fix was "
        "cosmetic")


def test_sampled_points_are_whole_numbers():
    """FPL scores in whole points and the artifact's floor/ceiling read as whole
    points, so the bonus blend is randomised-rounded rather than left continuous
    — floor(b) plus a Bernoulli on the fraction, which is integer-valued AND
    mean-preserving where a plain round is neither."""
    _, rates = _pair(position="FWD", price=80, base_xg90=0.6, xg_per_90=0.6)
    totals = _sample(rates, n=20_000)
    assert np.all(totals == np.round(totals))


def test_an_unavailable_player_scores_nothing_in_every_universe():
    """Goals conceded, saves and DEFCON are deliberately not re-gated on the
    appearance draw, so the degenerate case is worth pinning: zero availability
    must still be a point mass at zero, not a keeper conceding from the
    treatment table."""
    model, rates = _keeper(status="i")
    assert model["exp_points"] == 0.0
    assert np.all(_sample(rates, n=5_000) == 0.0)


# --------------------------------------------------------------------------
# and across a whole database, at the n that actually ships
# --------------------------------------------------------------------------

def _model_xp(conn, gw):
    ctx = F.TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, gw, 1)
    lf = conn.execute("SELECT value FROM meta WHERE key='last_finished_gw'").fetchone()
    played = int(lf["value"]) if lf and str(lf["value"]).isdigit() else 0
    return {
        p["id"]: sum(
            projection._project_one_fixture(
                p, fx, ctx, projection._availability(
                    p["status"], p["chance_playing"]), played)["exp_points"]
            for fx in fixtures.get(p["team_id"], []) if fx.gw == gw)
        for p in conn.execute("SELECT * FROM players").fetchall()
    }


def test_no_player_in_the_database_disagrees_with_his_own_distribution(conn):
    dist = simulate.simulate_next_gw(conn, 1, n=N_SHIPPED)
    model = _model_xp(conn, 1)
    assert model, "the fixture must project somebody"
    over = [
        (pid, round(xp, 3), dist[pid]["mean"],
         round(simulate.sampling_tolerance(dist[pid]["std"], N_SHIPPED), 3))
        for pid, xp in model.items()
        if abs(dist[pid]["mean"] - xp) > simulate.sampling_tolerance(
            dist[pid]["std"], N_SHIPPED)
    ]
    assert not over, f"(player, model, sim mean, tolerance): {over}"


def test_nobody_is_published_above_his_own_simulated_ceiling(conn):
    """The acceptance condition. `_summarise` guarantees ceiling >= mean and the
    test above guarantees mean == model, so this can only fail if the two models
    part company again — which is the thing worth being told about."""
    dist = simulate.simulate_next_gw(conn, 1, n=N_SHIPPED)
    over = [(pid, round(xp, 2), dist[pid]["ceiling"])
            for pid, xp in _model_xp(conn, 1).items()
            # 0.05 of slack: `model_xp` publishes to 2dp and `ceiling` to 1dp, so
            # a mean of 2.549 shows as 2.55 against a ceiling of 2.5 without
            # anything being wrong.
            if round(xp, 2) > dist[pid]["ceiling"] + 0.05]
    assert not over, f"published above own ceiling: {over}"


def test_the_tolerance_scales_with_the_players_own_spread():
    """A flat tolerance loose enough for a 5-std forward would wave through the
    1.25-point defect this replaced, which landed on a defender with std 2.5."""
    assert simulate.sampling_tolerance(5.0, N_SHIPPED) > \
        simulate.sampling_tolerance(2.5, N_SHIPPED)
    assert simulate.sampling_tolerance(2.5, N_SHIPPED) < 1.25
    assert simulate.sampling_tolerance(0.0, N_SHIPPED) > 0, \
        "a point mass still needs a floor"
    assert simulate.sampling_tolerance(5.0, N_SHIPPED) == pytest.approx(
        simulate.XP_SIM_SIGMAS * 5.0 / math.sqrt(N_SHIPPED))
