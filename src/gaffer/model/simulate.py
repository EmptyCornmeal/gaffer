"""Monte-Carlo layer: turn the projection's point estimate into a distribution.

The market's #1 gap is that everyone outputs a single expected-points number and
so under-sells hauls. We sample the *match* (Poisson goals/assists, Bernoulli
clean sheet, NegBin-driven DEFCON, minutes scenarios) from the SAME per-fixture
rates the deterministic projection sums — so mean(sims) ≈ the shipped xP, but we
also get floor, ceiling and boom% for free.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from gaffer.model import features as F
from gaffer.model import projection
from gaffer.model.features import TeamContext

# Fixed seed → reproducible artifacts (and no wall-clock/RNG surprises in CI).
_SEED = 20260727
_START_MIN = 82.0
_CAMEO_MIN = 20.0


def _sample_fixture(r: dict[str, float], n: int, rng: np.random.Generator) -> np.ndarray:
    """Vectorised points sample for one fixture across ``n`` universes."""
    p_start, p_play = r["p_start"], r["p_play"]
    u = rng.random(n)
    started = u < p_start
    cameo = (~started) & (u < p_play)
    played = started | cameo

    sim_min = np.where(started, _START_MIN, np.where(cameo, _CAMEO_MIN, 0.0))
    exp_min = r["exp_minutes"] or 1.0
    scale = sim_min / exp_min  # keeps the goal/assist mean aligned with the point est.

    goals = rng.poisson(np.maximum(r["exp_goals"] * scale, 0.0))
    assists = rng.poisson(np.maximum(r["exp_assists"] * scale, 0.0))
    goal_pts = goals * r["goal_pts_per"]
    assist_pts = assists * r["assist_pts_per"]

    appearance = np.where(started, 2.0, np.where(cameo, 1.0, 0.0))

    if r["cs_pts_per"] > 0:
        cs_flag = started & (rng.random(n) < r["p_cs"])
    else:
        cs_flag = np.zeros(n, dtype=bool)
    cs_pts = cs_flag * r["cs_pts_per"]

    if r["defcon_pts"] and r["defcon_p_hit"] > 0:
        dc_flag = played & (rng.random(n) < r["defcon_p_hit"])
    else:
        dc_flag = np.zeros(n, dtype=bool)
    dc_pts = dc_flag * r["defcon_pts"]

    # bonus proxy, correlated with the sim's own returns so hauls carry bonus
    bonus = np.clip(np.round(0.9 * goals + 0.6 * assists + 0.4 * cs_flag + 0.3 * dc_flag), 0, 3)

    return appearance + goal_pts + assist_pts + cs_pts + dc_pts + bonus


def _summarise(totals: np.ndarray) -> dict[str, float]:
    return {
        "mean": round(float(totals.mean()), 2),
        "floor": round(float(np.percentile(totals, 25)), 1),   # a bad-but-plausible week
        "ceiling": round(float(np.percentile(totals, 90)), 1),  # the upside you captain for
        "boom": round(float((totals >= 10).mean()) * 100, 1),   # P(double-digit haul)
        "std": round(float(totals.std()), 2),
    }


def simulate_next_gw(
    conn: sqlite3.Connection, from_gw: int, n: int = 3000
) -> dict[int, dict[str, float]]:
    """Per-player next-GW points distribution (handles doubles/blanks)."""
    ctx = TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, from_gw, 1)
    players = conn.execute("SELECT * FROM players").fetchall()
    lf = conn.execute("SELECT value FROM meta WHERE key='last_finished_gw'").fetchone()
    games_played = int(lf["value"]) if lf and str(lf["value"]).isdigit() else 0
    rng = np.random.default_rng(_SEED)

    out: dict[int, dict[str, float]] = {}
    for p in players:
        gw_fx = [fx for fx in fixtures.get(p["team_id"], []) if fx.gw == from_gw]
        if not gw_fx:  # blank
            out[p["id"]] = {"mean": 0.0, "floor": 0.0, "ceiling": 0.0, "boom": 0.0, "std": 0.0}
            continue
        avail = projection._availability(p["status"], p["chance_playing"])
        totals = np.zeros(n)
        for fx in gw_fx:
            r = projection.fixture_rates(p, fx, ctx, avail, games_played)
            totals = totals + _sample_fixture(r, n, rng)
        out[p["id"]] = _summarise(totals)
    return out
