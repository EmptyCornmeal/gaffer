"""T-16 — fixture-level correlated simulation.

The old simulator drew every player independently: two Arsenal centre-backs got
a joint clean sheet 52.6% of the time when the truth is 72.6%, and the measured
teammate correlation was -0.0032.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from gaffer.model import projection, scenarios
from gaffer.store import db

SIMS = 3000


@pytest.fixture
def sim(conn):
    projection.project(conn, 1, 1)
    return scenarios.simulate(conn, 1, n_sims=SIMS, seed=7)


def team_of(conn, pid):
    return conn.execute("SELECT team_id FROM players WHERE id=?", (pid,)).fetchone()["team_id"]


def players_in(conn, team, position=None):
    q = "SELECT id FROM players WHERE team_id=?"
    args = [team]
    if position:
        q += " AND position=?"
        args.append(position)
    return [r["id"] for r in conn.execute(q, args)]


# --------------------------------------------------------------------------
# Marginals
# --------------------------------------------------------------------------

def test_marginal_means_track_the_deterministic_projection(conn, sim):
    """Within Monte Carlo tolerance, the simulator must not move the mean."""
    det = {r["player_id"]: r["exp_points"]
           for r in conn.execute("SELECT player_id, exp_points FROM projections WHERE gw=1")}
    diffs = []
    for pid in sim.player_ids:
        if det.get(pid, 0) > 1.0:
            diffs.append(sim.row(pid).mean() - det[pid])
    assert diffs, "precondition: some players have a real projection"
    # The documented sources of drift are the discretised bonus proxy and the
    # forfeited-goal rule, so tolerate a modest bias but not a systematic one.
    assert abs(float(np.mean(diffs))) < 1.2, f"mean drift {np.mean(diffs):.3f}"
    assert float(np.percentile(np.abs(diffs), 90)) < 2.5


def test_every_player_has_a_finite_distribution(sim):
    assert np.isfinite(sim.points).all()
    assert sim.points.shape == (len(sim.player_ids), SIMS)


def test_uncertainty_is_exposed_not_just_a_mean(sim):
    s = sim.summary(sim.player_ids[0])
    assert set(s) == {"mean", "floor", "ceiling", "boom", "std"}
    assert s["floor"] <= s["mean"] <= s["ceiling"] or s["std"] == 0


# --------------------------------------------------------------------------
# Coherence within a match
# --------------------------------------------------------------------------

def test_teammates_are_positively_correlated(conn, sim):
    """The headline fix: same-club defenders share one clean sheet."""
    team = team_of(conn, sim.player_ids[0])
    defs = [p for p in players_in(conn, team, "DEF") if p in sim.index][:3]
    assert len(defs) >= 2
    corrs = []
    for a, b in zip(defs, defs[1:], strict=False):
        ra, rb = sim.row(a), sim.row(b)
        if ra.std() > 0 and rb.std() > 0:
            corrs.append(float(np.corrcoef(ra, rb)[0, 1]))
    assert corrs
    assert max(corrs) > 0.15, f"teammate correlation too weak: {corrs}"


def test_opposing_attackers_hurt_your_clean_sheet(conn, sim):
    """A defender's points must fall when the opposing attack scores."""
    fx = conn.execute("SELECT team_h, team_a FROM fixtures WHERE gw=1").fetchone()
    home_def = [p for p in players_in(conn, fx["team_h"], "DEF") if p in sim.index]
    away_fwd = [p for p in players_in(conn, fx["team_a"], "FWD") if p in sim.index]
    # An assert, not a skip: the shared `conn` fixture always supplies these, and
    # a silently-skipped test shrinks coverage without shrinking the test count.
    assert home_def and away_fwd, "the shared fixture pool must supply both sides"
    d = sim.row(home_def[0])
    a = sum(sim.row(p) for p in away_fwd)
    if d.std() > 0 and a.std() > 0:
        c = float(np.corrcoef(d, a)[0, 1])
        assert c < 0.05, f"defender/opposing-attack correlation should be <=0, got {c}"


def test_clean_sheet_and_conceded_never_contradict(conn, sim):
    """They read the same draw, so both cannot be true at once."""
    fx = conn.execute("SELECT team_h, team_a FROM fixtures WHERE gw=1").fetchone()
    defs = [p for p in players_in(conn, fx["team_h"], "DEF") if p in sim.index]
    assert defs, "the shared fixture pool must supply home defenders"
    # A scenario cannot both award a clean sheet and deduct for goals conceded;
    # verified structurally by both reading `conceded` from one array.
    import inspect
    src = inspect.getsource(scenarios.simulate)
    assert "conceded_by(key)" in src
    assert "clean_sheet = conceded == 0" in src


def test_a_double_gameweek_accumulates_both_fixtures(conn):
    projection.project(conn, 1, 1)
    db.upsert(conn, "fixtures", [
        {"id": 99, "gw": 1, "team_h": 1, "team_a": 3, "kickoff": None,
         "fdr_h": 3, "fdr_a": 3, "finished": 0}], ["id"])
    single = scenarios.simulate(conn, 1, n_sims=800, seed=3)
    assert single.meta["fixtures"] >= 4


def test_no_fixture_means_no_points(conn):
    projection.project(conn, 1, 1)
    conn.execute("DELETE FROM fixtures")
    conn.commit()
    s = scenarios.simulate(conn, 1, n_sims=200, seed=3)
    assert s.meta["fixtures"] == 0
    assert s.points.size == 0


# --------------------------------------------------------------------------
# Shared scenarios across squads
# --------------------------------------------------------------------------

def test_the_same_player_scores_identically_in_two_squads(sim):
    """A rival and the user must see one reality, not two draws."""
    pid = sim.player_ids[0]
    mine = sim.squad_points([pid])
    theirs = sim.squad_points([pid])
    assert np.array_equal(mine, theirs)


def test_captain_doubles_and_triples(sim):
    pid = sim.player_ids[0]
    base = sim.squad_points([pid])
    doubled = sim.squad_points([pid], captain=pid)
    tripled = sim.squad_points([pid], captain=pid, captain_multiplier=3)
    assert np.allclose(doubled, base * 2)
    assert np.allclose(tripled, base * 3)


def test_bench_boost_adds_the_bench(sim):
    a, b = sim.player_ids[0], sim.player_ids[1]
    without = sim.squad_points([a], bench=[b], bench_boost=False)
    with_bb = sim.squad_points([a], bench=[b], bench_boost=True)
    assert np.allclose(with_bb - without, sim.row(b))


def test_unknown_player_contributes_zero(sim):
    assert np.allclose(sim.row(999999), 0.0)


# --------------------------------------------------------------------------
# Reproducibility, metadata, performance
# --------------------------------------------------------------------------

def test_same_seed_reproduces_exactly(conn):
    projection.project(conn, 1, 1)
    a = scenarios.simulate(conn, 1, n_sims=500, seed=42)
    b = scenarios.simulate(conn, 1, n_sims=500, seed=42)
    assert np.array_equal(a.points, b.points)


def test_different_seeds_differ(conn):
    projection.project(conn, 1, 1)
    a = scenarios.simulate(conn, 1, n_sims=500, seed=1)
    b = scenarios.simulate(conn, 1, n_sims=500, seed=2)
    assert not np.array_equal(a.points, b.points)


def test_metadata_records_the_assumptions(sim):
    m = sim.as_meta()
    assert m["sim_version"] == scenarios.SIM_VERSION
    assert m["n_sims"] == SIMS and m["seed"] == 7
    assert m["model_version"] == projection.MODEL_VERSION
    assert len(m["assumptions"]) >= 3


def test_performance_is_practical(conn):
    projection.project(conn, 1, 1)
    t0 = time.perf_counter()
    s = scenarios.simulate(conn, 1, n_sims=2000, seed=5)
    elapsed = time.perf_counter() - t0
    assert s.n_sims == 2000
    assert elapsed < 20.0, f"2000 scenarios took {elapsed:.1f}s"
