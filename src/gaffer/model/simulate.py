"""Monte-Carlo layer: turn the projection's point estimate into a distribution.

The market's #1 gap is that everyone outputs a single expected-points number and
so under-sells hauls. We sample the *match* (Poisson goals/assists, Bernoulli
clean sheet, NegBin-driven DEFCON, minutes scenarios) from the SAME per-fixture
rates the deterministic projection sums — so mean(sims) IS the shipped xP up to
sampling error, and floor, ceiling and boom% come for free.

A13 — that used to say "≈", and it was hiding a real disagreement. The sampler
read SIX of the projection's eleven components. Goals conceded, saves, yellows,
reds, own goals and penalty miss/save were all sitting in the rate bundle and
were simply never drawn, and the bonus term was a second, different formula. Two
readings of one set of rates differed by up to 1.25 points on the live 2026/27
GW3 artifact — Davis (DEF) published at 1.11 beside a simulated mean of 2.36,
half a clean sheet apart.

The sign was positional, because the omitted terms do not cancel: they are net
NEGATIVE for a defender (goals conceded, roughly -0.2 to -1.3) and net POSITIVE
for a keeper (saves outweigh them). So defenders read high in the simulation and
keepers read high in the projection, and three keepers published an expectation
ABOVE their own simulated 90th percentile — Dubravka 2.58 vs 2.2, Sánchez 2.21 vs
2.2, Martinez 2.02 vs 2.0 — which is close to arithmetically impossible and was
the entire residual left after A0 removed the `ep_next` blend.

Every component is now drawn, and each in the form that makes its mean the
projection's own term EXACTLY rather than approximately. Three kinds:

  * LINEAR IN MINUTES — goals, assists, cards, own goals, penalties, and the
    historical half of the bonus blend. Scaled by ``scale = sim_min/exp_minutes``,
    whose mean is 1 by construction, so a benched universe scores zero and the
    marginal is untouched.

  * GATED ON AN EVENT THE PROJECTION NAMES — the appearance point and the clean
    sheet. Both are gated on ``p60`` there, not on starting, so the sampler now
    draws a 60-minute event nested inside the start/cameo split and reproduces
    ``p60`` exactly, rare hour-long substitute included. Gating the clean sheet
    on ``started`` was worth up to +0.15 a player on its own.

  * THRESHOLDED, AND THEREFORE NOT LINEAR IN MINUTES — goals conceded, saves and
    DEFCON. Each is drawn at the projection's own unconditional per-fixture rate
    and deliberately NOT re-gated on the appearance draw. That rate is already
    scaled by ``mins_frac``, an expectation across the whole fixture that
    includes the chance of not playing at all; gating it a second time counts the
    bench universe twice and puts the disagreement straight back — it is what the
    old ``played &`` DEFCON gate was doing, worth up to -0.13.

    The cost is a bounded incoherence: a universe in which the player never
    appears can still be charged a goal conceded. It is self-limiting, because
    ``mins_frac`` is small exactly when that universe is likely — for a fringe
    defender the lambda is a third of a goal, so it lands in about 4% of his
    bench universes and moves the floor, not the mean. Removing it properly means
    making the projection's conceded/saves/DEFCON terms minutes-CONDITIONAL
    (E[floor(X/d)] over the minutes mixture rather than at mean minutes — the
    same Jensen argument ``features.expected_floor_div`` already makes for the
    divisor). That moves every published number for every keeper and defender and
    belongs behind a backtest, not behind a divergence fix.

What is NOT sampled here: correlation. Two centre-backs draw independent clean
sheets in this module, which is exactly the defect ``model.scenarios`` exists to
fix. This layer answers "what is one player's own range", scenarios answers "what
is a squad's", and they are different questions.
"""
from __future__ import annotations

import math
import sqlite3

import numpy as np

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection
from gaffer.model.features import TeamContext

# Fixed seed → reproducible artifacts (and no wall-clock/RNG surprises in CI).
_SEED = 20260727
_START_MIN = 82.0
_CAMEO_MIN = 20.0

#: Standard errors of daylight allowed between ``mean(sims)`` and the
#: deterministic ``exp_points`` before the two are disagreeing rather than
#: sampling. Two-sided, so a false alarm on any one player is about 6e-7.
XP_SIM_SIGMAS = 5.0


def sampling_tolerance(std: float, n: int, sigmas: float = XP_SIM_SIGMAS) -> float:
    """The most ``mean(sims)`` may miss the point estimate by and still be noise.

    Both readings are exact in expectation after A13, so the only difference left
    between them is Monte-Carlo error — ``std/sqrt(n)``, about 0.09 at the shipped
    n=3000 for the most volatile forward in the game and under 0.02 for a
    defender.

    A FLAT tolerance would be wrong in both directions, and wrong in the
    dangerous one. Loose enough for a 5-std forward is loose enough to wave
    through the defect this replaced, which landed hardest on defenders — Davis
    was 1.25 points adrift on a distribution whose whole spread is 2.5. Scaling
    with the player's own spread is what makes the alarm as tight on him as on
    Haaland.

    The floor covers the degenerate case: an unavailable player's distribution is
    a point mass at zero, std is 0, and any difference at all is a real one.
    """
    return max(sigmas * std / math.sqrt(max(n, 1)), 0.02)


def _sample_fixture(r: dict[str, float], n: int, rng: np.random.Generator) -> np.ndarray:
    """Vectorised points sample for one fixture across ``n`` universes.

    Every term is drawn so its mean is the matching term in
    ``projection._project_one_fixture``. See the module docstring for the three
    forms that takes and why each one is the form it is.
    """
    pos = str(r["pos"])
    p_start, p_play = r["p_start"], r["p_play"]

    u = rng.random(n)
    started = u < p_start
    cameo = (~started) & (u < p_play)
    played = started | cameo

    sim_min = np.where(started, _START_MIN, np.where(cameo, _CAMEO_MIN, 0.0))
    exp_min = r["exp_minutes"] or 1.0
    scale = sim_min / exp_min  # keeps the goal/assist mean aligned with the point est.
    mins_90 = sim_min / 90.0   # == r["mins_frac"] * scale, in mean r["mins_frac"]

    # --- the hour ---------------------------------------------------------
    # The projection pays the long appearance point and the clean sheet on
    # `p60`, and `p60` is NOT `p_start`: a starter is hooked before the hour
    # about 5% of the time (11% for a midfielder), and a substitute reaches it
    # occasionally. Nesting the draw inside the start/cameo split with the same
    # `features.P60_GIVEN_*` rates reproduces `p60` to the digit.
    v = rng.random(n)
    hour = np.where(
        started, v < F.P60_GIVEN_START.get(pos, 1.0),
        np.where(cameo, v < F.P60_GIVEN_SUB.get(pos, 0.0), False),
    )

    appearance = np.where(
        hour, float(config.APPEARANCE_LONG),
        np.where(played, float(config.APPEARANCE_SHORT), 0.0),
    )

    # --- attacking --------------------------------------------------------
    goals = rng.poisson(np.maximum(r["exp_goals"] * scale, 0.0))
    assists = rng.poisson(np.maximum(r["exp_assists"] * scale, 0.0))
    goal_pts = goals * r["goal_pts_per"]
    assist_pts = assists * r["assist_pts_per"]

    # --- clean sheet ------------------------------------------------------
    if r["cs_pts_per"] > 0:
        cs_flag = hour & (rng.random(n) < r["p_cs"])
    else:
        cs_flag = np.zeros(n, dtype=bool)
    cs_pts = cs_flag * r["cs_pts_per"]

    # --- DEFCON -----------------------------------------------------------
    # No `played` gate: `defcon_p_hit` is already an unconditional per-fixture
    # probability built on minutes-scaled volume. See the module docstring.
    if r["defcon_pts"] and r["defcon_p_hit"] > 0:
        dc_flag = rng.random(n) < r["defcon_p_hit"]
    else:
        dc_flag = np.zeros(n, dtype=bool)
    dc_pts = dc_flag * r["defcon_pts"]

    # --- goals conceded and saves (T-13) ----------------------------------
    # Drawn from the very Poisson `features.expected_floor_div` integrates, so
    # E[floor(X/d)] is the projection's `conceded_units` / `save_units` exactly
    # instead of a second approximation to it.
    # Indexed, not `.get`-with-a-default: a bundle that has lost the key must
    # raise, because scoring it as zero is exactly the failure this replaced.
    conceded_pts: np.ndarray | float = 0.0
    if r["conceded_lam"] > 0:
        conceded = rng.poisson(r["conceded_lam"], n)
        conceded_pts = ((conceded // config.CONCEDED_PER_PENALTY)
                        * config.CONCEDED_PENALTY)
    saves_pts: np.ndarray | float = 0.0
    if r["saves_lam"] > 0:
        saves = rng.poisson(r["saves_lam"], n)
        saves_pts = (saves // config.SAVES_PER_POINT) * config.SAVE_POINTS

    # --- discipline and rare events (T-13) --------------------------------
    # Linear in minutes, so a per-90 rate times the minutes actually played has
    # the projection's `rate * mins_frac` as its mean and pays a bench universe
    # nothing. Drawn as counts, like goals, rather than as a clipped Bernoulli —
    # clipping a rate above 1 would silently shave the mean.
    def _rare(rate: float) -> np.ndarray | float:
        if rate <= 0:
            return 0.0
        return rng.poisson(np.maximum(rate * mins_90, 0.0))

    other_pts = (
        _rare(r["yellow_rate"]) * config.YELLOW_POINTS
        + _rare(r["red_rate"]) * config.RED_POINTS
        + _rare(r["og_rate"]) * config.OWN_GOAL_POINTS
        + _rare(r["pen_miss_rate"]) * config.PENALTY_MISS_POINTS
    )
    if pos == "GKP":
        other_pts = other_pts + _rare(r["pen_save_rate"]) * config.PENALTY_SAVE_POINTS

    # --- bonus ------------------------------------------------------------
    # `projection.bonus_points` is the projection's OWN formula, evaluated here
    # on the drawn returns instead of on their expectations. It is linear in all
    # four inputs, so the mean of what comes back is the published bonus.
    #
    # Then randomised-rounded, because FPL bonus is a whole number and the
    # artifact's floor/ceiling read as whole points: floor(b) plus a Bernoulli on
    # the fraction is integer-valued AND mean-preserving, where a plain round is
    # neither. There is no 0-3 cap: capping would bias the mean downward, and the
    # blend only clears 3 on a universe where the player has scored five.
    #
    # What this gives up, honestly: the old sampler paid 3 bonus for a hat-trick
    # and this pays about 1, because 0.55 a goal is an EXPECTED bonus
    # contribution and not a realised one. A proxy admitting it is a proxy is
    # worse-looking and better-behaved than a distribution that does not centre
    # on the number printed beside it. The real fix is a BPS model both paths
    # derive from, not two disagreeing guesses at the same unmeasurable thing.
    bonus_lam = projection.bonus_points(
        pos, goals, assists, dc_pts, cs_pts, r["cs_pts_per"],
        r["bonus_rate"] * mins_90,
        # The projection switches the history blend on a per-PLAYER quantity
        # (`bonus_rate * mins_frac > 0`), so the sampler must switch on the same
        # one — not on the per-universe draw, which would flip the branch for a
        # benched universe and lose the mean.
        bool(r["bonus_rate"] * r["mins_frac"] > 0),
    )
    lo = np.floor(bonus_lam)
    bonus = lo + (rng.random(n) < (bonus_lam - lo))

    return (appearance + goal_pts + assist_pts + cs_pts + dc_pts
            + conceded_pts + saves_pts + other_pts + bonus)


def _summarise(totals: np.ndarray) -> dict[str, float]:
    mean = round(float(totals.mean()), 2)
    floor = round(float(np.percentile(totals, 25)), 1)  # a bad-but-plausible week
    ceiling = round(float(np.percentile(totals, 90)), 1)  # the upside you captain for
    # For low-minutes players the percentiles collapse to a point and rounding can
    # leave floor>mean or mean>ceiling; keep the invariant floor ≤ mean ≤ ceiling.
    return {
        "mean": mean,
        "floor": round(min(floor, mean), 1),
        "ceiling": round(max(ceiling, mean), 1),
        "boom": round(float((totals >= 10).mean()) * 100, 1),  # P(double-digit haul)
        "std": round(float(totals.std()), 2),
    }


def simulate_next_gw(
    conn: sqlite3.Connection, from_gw: int, n: int = 3000
) -> dict[int, dict[str, float]]:
    """Per-player next-GW points distribution (handles doubles/blanks)."""
    ctx = TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, from_gw, 1)
    players = conn.execute("SELECT * FROM players").fetchall()
    # Per TEAM, how many fixtures it has completed -- the same quantity
    # `projection.project` passes. `meta.last_finished_gw` counts EVENTS, and the
    # two part company at the first double gameweek or blank.
    played_by_team = F.played_fixtures_by_team(conn)
    rng = np.random.default_rng(_SEED)
    out: dict[int, dict[str, float]] = {}
        # 2A -- the recency map MUST be passed here too. `fixture_rates`
        # now reads per-fixture start recency, and a reading of it that
        # omits the input computes a DIFFERENT p_start from the one the
        # projection published. That is the A13/A17 divergence the
        # sampling-tolerance invariant exists to catch, and it caught
        # this: Armstrong published a point estimate of 2.17 above his
        # own simulated ceiling of 2.0 before the map was threaded.
    recency_by_player = F.start_recency_by_player(conn)
    for p in players:
        gw_fx = [fx for fx in fixtures.get(p["team_id"], []) if fx.gw == from_gw]
        if not gw_fx:  # blank
            out[p["id"]] = {"mean": 0.0, "floor": 0.0, "ceiling": 0.0, "boom": 0.0, "std": 0.0}
            continue
        avail = projection._availability(p["status"], p["chance_playing"])
        totals = np.zeros(n)
        for fx in gw_fx:
            r = projection.fixture_rates(
                p, fx, ctx, avail, played_by_team.get(p["team_id"], 0),
                recency_by_player.get(p["id"]))
            totals = totals + _sample_fixture(r, n, rng)
        out[p["id"]] = _summarise(totals)
    return out
