"""T-13 — the remaining FPL scoring rules.

Six elements were unmodelled: goals conceded, saves, cards, own goals, penalty
saves and penalty misses. Every one except saves is negative, and all of them
load onto defensive assets at weak clubs — the cohort the appearance-dominated
model already inflated.
"""

from __future__ import annotations

import math

import pytest

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection as P

COMPONENTS = (
    "exp_appearance", "exp_goal_pts", "exp_assist_pts", "exp_cs_pts",
    "exp_defcon_pts", "exp_bonus_pts", "exp_conceded_pts", "exp_saves_pts",
    "exp_cards_pts", "exp_misc_pts",
)


def player(pos="DEF", **kw):
    base = {
        "position": pos, "minutes": 2700, "starts": 30, "price": 55,
        "base_minutes": 2700, "base_starts": 30, "base_xg90": 0.10,
        "base_xa90": 0.08, "xg_per_90": 0.10, "xa_per_90": 0.08,
        "defcon_per_90": 0.0, "team_id": 1,
        "saves_per_90": 0.0, "yellow_per_90": 0.0, "red_per_90": 0.0,
        "og_per_90": 0.0, "pen_save_per_90": 0.0, "pen_miss_per_90": 0.0,
        "bonus_per_90": 0.0,
    }
    base.update(kw)
    return base


def ctx(conceded=1.3):
    class C:
        def attack_multiplier(self, opp, at_home):
            return 1.0

        def expected_conceded(self, team, opp, at_home):
            return conceded
    return C()


def project(p, conceded=1.3):
    fx = F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3)
    return P._project_one_fixture(p, fx, ctx(conceded), 1.0, 10)


# --------------------------------------------------------------------------
# The counting maths
# --------------------------------------------------------------------------

def test_expected_floor_div_is_not_the_floor_of_the_expectation():
    """E[floor(X/2)] != floor(E[X]/2) — the naive shortcut is wrong."""
    lam = 1.3
    got = F.expected_floor_div(lam, 2)
    # P(X>=2) + P(X>=4) + ... = 0.3732 + 0.0430 + 0.0020 = 0.4182
    assert got == pytest.approx(0.4182, abs=1e-3)
    assert math.floor(lam / 2) == 0          # the shortcut would say zero
    assert got > 0


@pytest.mark.parametrize("lam,divisor", [(0, 2), (0, 3), (-1, 2)])
def test_expected_floor_div_degenerate(lam, divisor):
    assert F.expected_floor_div(lam, divisor) == 0.0


def test_expected_floor_div_grows_with_lambda():
    vals = [F.expected_floor_div(x, 2) for x in (0.5, 1.0, 2.0, 4.0)]
    assert vals == sorted(vals)


def test_expected_floor_div_matches_a_direct_sum():
    lam, d = 2.4, 3
    direct = sum(
        (k // d) * F.poisson_pmf(k, lam) for k in range(0, 60)
    )
    assert F.expected_floor_div(lam, d) == pytest.approx(direct, abs=1e-9)


# --------------------------------------------------------------------------
# Goals conceded
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pos,expect_penalty", [
    ("GKP", True), ("DEF", True), ("MID", False), ("FWD", False),
])
def test_goals_conceded_applies_to_the_right_positions(pos, expect_penalty):
    r = project(player(pos), conceded=2.0)
    if expect_penalty:
        assert r["exp_conceded_pts"] < 0
    else:
        assert r["exp_conceded_pts"] == 0.0


def test_goals_conceded_scales_with_the_fixture():
    easy = project(player("DEF"), conceded=0.6)["exp_conceded_pts"]
    hard = project(player("DEF"), conceded=2.6)["exp_conceded_pts"]
    assert hard < easy < 0


def test_clean_sheet_and_conceded_come_from_one_lambda():
    """They must never disagree about how leaky the fixture is."""
    for lam in (0.5, 1.4, 2.6):
        r = project(player("DEF"), conceded=lam)
        assert r["exp_cs_pts"] > 0
        assert r["exp_conceded_pts"] < 0
        # A leakier fixture lowers the clean sheet AND deepens the penalty.
        r2 = project(player("DEF"), conceded=lam + 0.6)
        assert r2["exp_cs_pts"] < r["exp_cs_pts"]
        assert r2["exp_conceded_pts"] < r["exp_conceded_pts"]


def test_conceded_penalty_is_bounded_by_the_rule():
    """-1 per 2 conceded: even a 3-goal expectation cannot cost a whole match."""
    r = project(player("DEF"), conceded=3.0)
    assert -2.0 < r["exp_conceded_pts"] < 0


# --------------------------------------------------------------------------
# Saves
# --------------------------------------------------------------------------

def test_saves_score_only_for_keepers():
    for pos in ("DEF", "MID", "FWD"):
        assert project(player(pos, saves_per_90=4.0))["exp_saves_pts"] == 0.0
    assert project(player("GKP", saves_per_90=4.0))["exp_saves_pts"] > 0


def test_saves_use_the_players_own_rate_when_known():
    low = project(player("GKP", saves_per_90=1.5))["exp_saves_pts"]
    high = project(player("GKP", saves_per_90=5.0))["exp_saves_pts"]
    assert high > low


def test_keeper_without_history_falls_back_to_the_fixture():
    """No saves rate must not mean 'never saves'."""
    r = project(player("GKP", saves_per_90=0.0), conceded=1.6)
    assert r["exp_saves_pts"] > 0


def test_saves_rule_is_one_point_per_three():
    """A keeper averaging 3 saves banks well under a point per match.

    floor(3/3) = 1 is the naive answer; the true expectation of floor(X/3) for
    a Poisson mean near 3 is ~0.56, because two-save games score nothing.
    """
    r = project(player("GKP", saves_per_90=3.0, starts=38, minutes=3420))
    assert 0.45 <= r["exp_saves_pts"] <= 0.70
    # Six saves a game is worth roughly twice as much, not six times.
    r6 = project(player("GKP", saves_per_90=6.0, starts=38, minutes=3420))
    assert 1.3 <= r6["exp_saves_pts"] <= 1.9


# --------------------------------------------------------------------------
# Discipline and rare events
# --------------------------------------------------------------------------

def test_yellow_and_red_cards_cost_points():
    clean = project(player("MID"))["exp_cards_pts"]
    booked = project(player("MID", yellow_per_90=0.30))["exp_cards_pts"]
    sent_off = project(player("MID", red_per_90=0.05))["exp_cards_pts"]
    assert clean == 0.0
    assert booked < 0 and sent_off < 0


def test_a_red_costs_three_times_a_yellow_at_equal_rates():
    """Rates sit inside both positional priors on purpose (M11).

    This asserts a fact about the scoring table -- RED_POINTS is three times
    YELLOW_POINTS -- through the projection. Since M11 the projection shrinks a
    rate that exceeds its positional prior, and a MID's red prior is 0.0067
    against a yellow prior of 0.216, so 0.1 is *ordinary* for a yellow and
    *implausible* for a red. Feeding both 0.1 stopped comparing like with like:
    one side was shrunk and the other was not. 0.005 is below both priors, so
    neither is touched and the invariant is tested rather than the shrinkage.
    """
    y = project(player("MID", yellow_per_90=0.005))["exp_cards_pts"]
    r = project(player("MID", red_per_90=0.005))["exp_cards_pts"]
    assert r == pytest.approx(3 * y, rel=1e-9)


def test_own_goals_and_penalty_misses_cost_two():
    """0.005 is below the DEF own-goal prior (0.0097) and the FWD penalty-miss
    prior (0.0067), so M11's shrinkage leaves both alone and the two -2 point
    rules are compared at genuinely equal modelled rates. At the old 0.05 both
    were shrunk, by different amounts, and the equality became a coincidence."""
    og = project(player("DEF", og_per_90=0.005))["exp_misc_pts"]
    miss = project(player("FWD", pen_miss_per_90=0.005))["exp_misc_pts"]
    assert og == pytest.approx(miss, rel=1e-9)
    assert og < 0


def test_penalty_saves_score_only_for_keepers():
    gk = project(player("GKP", pen_save_per_90=0.05))["exp_misc_pts"]
    outfield = project(player("DEF", pen_save_per_90=0.05))["exp_misc_pts"]
    assert gk > 0
    assert outfield == 0.0


def test_rare_events_are_small():
    """Conservative: a booking rate must not swamp a goal."""
    r = project(player("MID", yellow_per_90=0.25, og_per_90=0.02))
    assert abs(r["exp_cards_pts"]) < 0.5
    assert abs(r["exp_misc_pts"]) < 0.2


# --------------------------------------------------------------------------
# Bonus
# --------------------------------------------------------------------------

def test_bonus_uses_history_when_available():
    none = project(player("MID"))["exp_bonus_pts"]
    lots = project(player("MID", bonus_per_90=1.2))["exp_bonus_pts"]
    assert lots > none


def test_bonus_never_uses_realised_bps():
    """BPS is post-match; only a per-90 rate may inform the projection."""
    from gaffer import leakage

    read_keys = []

    class Spy(dict):
        def __getitem__(self, k):
            read_keys.append(k)
            return super().__getitem__(k)

    p = Spy(player("MID", bonus_per_90=0.4))
    project(p)
    # No realised bonus/BPS field may be read — only the per-90 rate.
    assert "bps" not in read_keys and "bonus" not in read_keys
    assert "bonus_per_90" in read_keys, "the pre-deadline rate should be used"
    # `minutes`/`starts` in the player dict are season-TO-DATE aggregates, not
    # this gameweek's outcome, so they are legitimately pre-deadline. Everything
    # else the model reads must be outside the post-match denylist.
    to_date = {"minutes", "starts"}
    leaked = [k for k in read_keys
              if k not in to_date and leakage.is_post_match(str(k))]
    assert leaked == [], f"projection read post-match fields: {leaked}"


def test_bonus_falls_back_to_the_returns_proxy():
    r = project(player("FWD", xg_per_90=0.8, base_xg90=0.8))
    assert r["exp_bonus_pts"] > 0


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pos", ["GKP", "DEF", "MID", "FWD"])
def test_components_sum_to_total(pos):
    r = project(player(pos, saves_per_90=3.0, yellow_per_90=0.2, og_per_90=0.02,
                       pen_miss_per_90=0.02, pen_save_per_90=0.02,
                       bonus_per_90=0.3, defcon_per_90=8.0), conceded=1.5)
    total = sum(r[c] for c in COMPONENTS)
    assert total == pytest.approx(r["exp_points"], abs=1e-9)


def test_every_component_is_present_for_every_position():
    for pos in ("GKP", "DEF", "MID", "FWD"):
        r = project(player(pos))
        for c in COMPONENTS:
            assert c in r, f"{c} missing for {pos}"
            assert isinstance(r[c], float)


def test_no_component_is_nan_or_infinite():
    for pos in ("GKP", "DEF", "MID", "FWD"):
        for lam in (0.0, 0.3, 1.4, 4.0):
            r = project(player(pos, saves_per_90=2.0, yellow_per_90=0.3), conceded=lam)
            for c in COMPONENTS + ("exp_points",):
                assert math.isfinite(r[c]), f"{c} not finite for {pos} lam={lam}"


def test_an_unavailable_player_scores_nothing():
    fx = F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3)
    r = P._project_one_fixture(player("DEF"), fx, ctx(), 0.0, 10)
    assert r["exp_points"] == pytest.approx(0.0, abs=1e-9)
    # And no component may be non-zero when the player cannot feature.
    for c in COMPONENTS:
        assert abs(r[c]) < 1e-9


def test_missing_rate_columns_do_not_raise():
    """Historical frames and old fixtures lack the new rate columns."""
    minimal = {
        "position": "DEF", "minutes": 900, "starts": 10, "price": 50,
        "base_minutes": 0, "base_starts": 0, "base_xg90": 0.0, "base_xa90": 0.0,
        "xg_per_90": 0.1, "xa_per_90": 0.05, "defcon_per_90": 0.0, "team_id": 1,
    }
    r = project(minimal)
    assert math.isfinite(r["exp_points"])
    assert r["exp_cards_pts"] == 0.0


def test_scoring_constants_match_the_rules():
    assert config.CONCEDED_PENALTY == -1 and config.CONCEDED_PER_PENALTY == 2
    assert config.SAVE_POINTS == 1 and config.SAVES_PER_POINT == 3
    assert config.YELLOW_POINTS == -1 and config.RED_POINTS == -3
    assert config.OWN_GOAL_POINTS == -2
    assert config.PENALTY_SAVE_POINTS == 5 and config.PENALTY_MISS_POINTS == -2
    assert set(config.CONCEDED_POSITIONS) == {"GKP", "DEF"}


def test_no_double_counting_of_defensive_value():
    """DEFCON and goals-conceded are different rules and must both apply."""
    r = project(player("DEF", defcon_per_90=12.0), conceded=1.8)
    assert r["exp_defcon_pts"] > 0
    assert r["exp_conceded_pts"] < 0
    assert r["exp_defcon_pts"] != -r["exp_conceded_pts"]


def test_weak_club_defender_is_now_penalised():
    """The structural point of T-13: a leaky club's defender was over-projected."""
    strong = project(player("DEF"), conceded=0.7)["exp_points"]
    weak = project(player("DEF"), conceded=2.6)["exp_points"]
    assert weak < strong


# --- M11: a cameo cannot manufacture a rate ---------------------------------

def test_a_cameo_red_card_does_not_ship_a_sending_off_rate():
    """D.Essugo shipped `other = -2.25` off one red card in ~13 minutes.

    Raw, that is a `red_per_90` of 6.9 against a league rate of 0.0067 -- a
    thousand times the prior, from a single event.
    """
    cameo = project(player("MID", minutes=13, red_per_90=90.0 / 13.0))
    assert cameo["exp_cards_pts"] > -0.25, (
        "one red card in a cameo is still being read as a sending-off habit")


def test_real_evidence_outranks_a_cameo():
    """The shrinkage must not simply flatten everything to the prior: a player
    sent off twice across a full season has to end up *above* a one-cameo
    player, which is the ordering the raw rate got backwards."""
    cameo = project(player("MID", minutes=13, red_per_90=90.0 / 13.0))
    repeat = project(player("MID", minutes=3000, red_per_90=2 * 90.0 / 3000))
    assert repeat["exp_cards_pts"] < cameo["exp_cards_pts"]


def test_shrinkage_is_one_sided():
    """A player with no cards keeps zero, rather than inheriting the prior.

    Pulling zero rates up is the purer estimator and a different change; this
    pins the scope so it cannot arrive unnoticed.
    """
    clean = project(player("MID"))
    assert clean["exp_cards_pts"] == 0.0
