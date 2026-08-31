"""T-16 — fixture-level correlated simulation.

The old simulator drew every player independently: two Arsenal centre-backs got
a joint clean sheet 52.6% of the time when the truth is 72.6%, and the measured
teammate correlation was -0.0032.

A17 — and then this module became the THIRD reading of one set of
``projection.fixture_rates``, after ``_project_one_fixture`` and
``model.simulate``, and disagreed with both by up to 2.54 points on the live
2026/27 GW3 artifact. The test that should have caught it is the first one
below: it allowed 1.2 points of mean drift and 2.5 at the 90th percentile, which
is wider than the defect. It now allows what Monte-Carlo error allows and
nothing else, and ``test_no_two_readings_of_the_rate_bundle_disagree`` holds all
three readings against each other rather than two of them.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection, scenarios, simulate
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


def model_xp(conn, gw):
    """The deterministic reading, computed the way ``projection.project`` does.

    Read from the model directly rather than from the ``projections`` table so
    the comparison is against Gaffer's own number and not against whatever the
    ``ep_next`` blend did to it.
    """
    ctx = F.TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, gw, 1)
    played = F.played_fixtures_by_team(conn)
    out = {}
    for p in conn.execute("SELECT * FROM players").fetchall():
        avail = projection._availability(p["status"], p["chance_playing"])
        if avail <= 0:
            continue
        out[p["id"]] = sum(
            projection._project_one_fixture(
                p, fx, ctx, avail, played.get(p["team_id"], 0))["exp_points"]
            for fx in fixtures.get(p["team_id"], []) if fx.gw == gw)
    return out


# --------------------------------------------------------------------------
# Marginals — one rate bundle, three readings, one answer
# --------------------------------------------------------------------------

def test_marginal_means_track_the_deterministic_projection(conn, sim):
    """The simulator must not move the mean by more than sampling error.

    The tolerance is ``simulate.sampling_tolerance``: scaled to each player's own
    spread, because a flat one loose enough for a volatile forward is loose
    enough to wave through the defect this replaced, which landed on defenders.
    """
    det = model_xp(conn, 1)
    assert det, "precondition: some players have a real projection"
    over = []
    for pid in sim.player_ids:
        row = sim.row(pid)
        tol = simulate.sampling_tolerance(float(row.std()), SIMS)
        if abs(float(row.mean()) - det.get(pid, 0.0)) > tol:
            over.append((pid, round(det.get(pid, 0.0), 3),
                         round(float(row.mean()), 3), round(tol, 3)))
    assert not over, f"(player, projection, scenario mean, tolerance): {over}"


def test_no_two_readings_of_the_rate_bundle_disagree(conn):
    """The A17 acceptance test: all THREE readings, pairwise, or none of them.

    ``_project_one_fixture`` sums the rates, ``model.simulate`` samples one
    player from them and this module samples a whole match from them. Any pair
    drifting is the same defect wearing a different hat, so any pair drifting
    fails here — including the pair that does not involve this module, because a
    two-way check is what let a third reading rot unnoticed.
    """
    projection.project(conn, 1, 1)
    det = model_xp(conn, 1)
    dist = simulate.simulate_next_gw(conn, 1, n=SIMS)
    scen = scenarios.simulate(conn, 1, n_sims=SIMS, seed=11)
    assert det, "precondition: some players have a real projection"

    failures = []
    for pid, xp in det.items():
        row = scen.row(pid)
        readings = {
            "projection": (xp, 0.0),
            "simulate": (dist[pid]["mean"], dist[pid]["std"]),
            "scenarios": (float(row.mean()), float(row.std())),
        }
        for a, b in (("projection", "simulate"), ("projection", "scenarios"),
                     ("simulate", "scenarios")):
            # Two sampled readings each carry Monte-Carlo error, so the pair is
            # allowed the larger of the two tolerances, not a doubled one --
            # 5 sigma on the wider spread already dominates.
            tol = max(simulate.sampling_tolerance(readings[a][1], SIMS),
                      simulate.sampling_tolerance(readings[b][1], SIMS))
            gap = abs(readings[a][0] - readings[b][0])
            if gap > tol:
                failures.append((pid, a, b, round(readings[a][0], 3),
                                 round(readings[b][0], 3), round(tol, 3)))
    assert not failures, f"(player, a, b, mean_a, mean_b, tolerance): {failures}"


def test_the_marginal_bias_is_not_positional(conn, sim):
    """The A13/A17 signature was a SIGN that depended on position.

    An aggregate mean drift near zero hides it — the omitted terms are net
    negative for a defender and net positive for a keeper, so a population
    average cancels the very thing worth being told about.
    """
    det = model_xp(conn, 1)
    pos = {r["id"]: r["position"]
           for r in conn.execute("SELECT id, position FROM players")}
    by_pos: dict[str, list[float]] = {}
    for pid in sim.player_ids:
        by_pos.setdefault(pos[pid], []).append(
            float(sim.row(pid).mean()) - det.get(pid, 0.0))
    assert set(by_pos) >= {"GKP", "DEF", "MID", "FWD"}
    worst = {p: round(float(np.mean(d)), 3) for p, d in by_pos.items()}
    # Per position the mean of n players has standard error std/sqrt(n*SIMS);
    # 0.05 is far above that and far below the 0.15-0.18 positional bias A17
    # removed.
    assert all(abs(v) < 0.05 for v in worst.values()), f"positional drift: {worst}"


def test_the_rate_bundle_is_carried_raw(conn):
    """Rates are scaled where they are drawn, once — not on the way in.

    The defect: ``_collect_rates`` pre-multiplied the six per-90 rates by
    ``mins_frac`` and the sampler then gated the draw on ``played``, so the bench
    universe was counted twice. Carrying the rate raw is what makes that
    impossible to write by accident.
    """
    rates, _ = scenarios._collect_rates(conn, 1)
    assert rates
    ctx = F.TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, 1, 1)
    played = F.played_fixtures_by_team(conn)
    checked = 0
    for r in rates[:12]:
        p = conn.execute("SELECT * FROM players WHERE id=?", (r.pid,)).fetchone()
        fx = next(f for f in fixtures[p["team_id"]]
                  if f.gw == 1 and f.opponent_id == r.fixture_key[2])
        bundle = projection.fixture_rates(
            p, fx, ctx, projection._availability(p["status"], p["chance_playing"]),
            played.get(p["team_id"], 0))
        assert r.yellow == bundle["yellow_rate"]
        assert r.red == bundle["red_rate"]
        assert r.og == bundle["og_rate"]
        assert r.pen_save == bundle["pen_save_rate"]
        assert r.pen_miss == bundle["pen_miss_rate"]
        assert r.bonus_rate == bundle["bonus_rate"]
        # And the two lambdas a floor cannot be inverted out of.
        assert r.saves_lam == bundle["saves_lam"]
        assert r.lam_conceded == bundle["lam_conceded"]
        checked += 1
    assert checked, "precondition: the fixture pool must supply player-fixtures"


def test_the_starts_denominator_is_the_teams_own_fixtures(conn, monkeypatch):
    """``p_start`` must be built on the same count ``projection.project`` uses.

    This module passed ``meta.last_finished_gw``, an EVENT count, where the
    projection passes the team's completed FIXTURES. They agree until the first
    double or blank and then silently do not, and a different ``p_start`` is a
    different everything downstream.
    """
    # Give team 1 a completed fixture and nobody else one, so the fixture count
    # and the event count are different numbers and reading the wrong one shows.
    db.upsert(conn, "fixtures", [
        {"id": 900, "gw": 0, "team_h": 1, "team_a": 2, "kickoff": None,
         "fdr_h": 3, "fdr_a": 3, "finished": 1}], ["id"])
    db.set_meta(conn, "last_finished_gw", "7")
    expected = F.played_fixtures_by_team(conn)
    assert expected.get(1) == 1 and 7 not in expected.values(), \
        "precondition: the two counts must differ"

    seen: dict[int, int] = {}
    real = projection.fixture_rates

    def spy(player, fx, ctx, avail, fixtures_played=0):
        seen[player["team_id"]] = fixtures_played
        return real(player, fx, ctx, avail, fixtures_played)

    monkeypatch.setattr(projection, "fixture_rates", spy)
    scenarios._collect_rates(conn, 1)
    assert seen, "precondition: some player-fixture was rated"
    assert seen == {t: expected.get(t, 0) for t in seen}, \
        f"team -> fixtures_played passed: {seen}, expected {expected}"
    assert 7 not in seen.values(), "read meta.last_finished_gw, not the fixtures"


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


def test_clean_sheet_and_conceded_never_contradict():
    """A scenario cannot pay a clean sheet AND deduct for goals conceded.

    Both read one uniform, so this is a statement about two bands of it rather
    than about the code that happens to be written today — which is why it used
    to be tested by grepping the source and now is not. A clean sheet needs
    ``u < exp(-lam)``; a deduction needs the conceded draw to reach
    ``CONCEDED_PER_PENALTY``, i.e. ``u`` above the CDF of one goal at
    ``lam x mins_frac``. That CDF is at its smallest at ``mins_frac = 1``, where
    it is ``exp(-lam)(1 + lam)`` — still above the clean-sheet band. The bands
    cannot overlap, for any lambda and any share of the match played.
    """
    for lam in (0.12, 0.4, 0.9, 1.5, 2.4, 4.0):
        p_cs = F.poisson_p0(lam)
        u = np.linspace(0.0, p_cs, 4000, endpoint=False)
        for mins_frac in (0.02, 0.2, 0.5, 0.85, 1.0):
            conceded = scenarios._poisson_icdf(u, lam * mins_frac)
            assert int(conceded.max()) < config.CONCEDED_PER_PENALTY, (
                f"lam={lam} mins_frac={mins_frac} paid a clean sheet and "
                f"conceded {int(conceded.max())}")


def test_a_clean_sheet_rarely_contradicts_the_goal_draw(sim):
    """The one residual incoherence is measured and published, not assumed away.

    ``p_cs`` comes from the team-strength model and the opposing lambda from the
    sum of that side's expected goals. They are two estimates of one quantity and
    they differ, so a clean sheet occasionally lands in a scenario where the
    drawn goal total is not zero. That band is exactly
    ``E[max(0, p_cs - e^-lambda)]`` and it is measured on every run.
    """
    gap = sim.diagnostics["clean_sheet_contradiction"]
    assert set(gap) == {"mean", "max", "sides"}
    assert gap["sides"] == sim.meta["fixtures"]
    assert 0.0 <= gap["mean"] <= gap["max"] <= 1.0
    assert any("two different estimates" in a for a in sim.meta["assumptions"]), \
        "the residual must be stated in the assumptions, not only in a number"
    # The diagnostic must NOT reach the published meta: `simulation` is echoed
    # wholesale into a byte-capped MCP payload.
    assert "clean_sheet_contradiction" not in sim.as_meta()


def test_poisson_icdf_is_a_poisson_and_is_monotone():
    """The coupling device. If it is not monotone the module is back to
    independent draws; if it is not Poisson every marginal is wrong."""
    rng = np.random.default_rng(3)
    for lam in (0.0, 0.25, 1.3, 3.0):
        u = rng.random(200_000)
        draw = scenarios._poisson_icdf(u, lam)
        assert float(draw.mean()) == pytest.approx(lam, abs=0.02)
        assert float(draw.var()) == pytest.approx(lam, abs=0.05)
        assert float((draw == 0).mean()) == pytest.approx(F.poisson_p0(lam), abs=0.01)
        ordered = np.sort(u)
        assert np.all(np.diff(scenarios._poisson_icdf(ordered, lam)) >= 0)
    # And it broadcasts a per-player lambda against one shared uniform.
    shared = rng.random(50)
    out = scenarios._poisson_icdf(shared[None, :], np.array([[0.5], [2.0]]))
    assert out.shape == (2, 50)
    assert np.all(out[0] <= out[1] + 6)


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


def test_every_player_has_a_finite_distribution(sim):
    assert np.isfinite(sim.points).all()
    assert sim.points.shape == (len(sim.player_ids), SIMS)


def test_uncertainty_is_exposed_not_just_a_mean(sim):
    s = sim.summary(sim.player_ids[0])
    assert set(s) == {"mean", "floor", "ceiling", "boom", "std"}
    assert s["floor"] <= s["mean"] <= s["ceiling"] or s["std"] == 0


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
