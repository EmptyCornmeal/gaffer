"""Near-optimal margins (G-O): what each individual squad slot is actually worth.

Every pool here is specified outright — prices, expected points, clubs — and the
projections are inserted directly rather than modelled. These tests are about the
solver's margin arithmetic; routing them through the projection model would make
them fail for reasons that have nothing to do with margins, and would make the
expected numbers un-derivable by hand.
"""

from __future__ import annotations

import pytest

from gaffer.export import artifacts
from gaffer.solver import objective as OBJ
from gaffer.solver import optimize
from gaffer.store import db

CLUBS = 12  # enough that the 3-per-club limit never binds by accident


def _pool(tmp_path, spec, *, horizon=1, name="margins.db"):
    """A bespoke pool. ``spec`` maps position -> [(price_tenths, xp_per_gw), ...]."""
    conn = db.connect(tmp_path / name)
    db.init_schema(conn)
    db.upsert(conn, "teams",
              [{"id": t, "name": f"Club{t}", "short": f"C{t}"}
               for t in range(1, CLUBS + 1)], ["id"])
    players, proj, pid = [], [], 1
    for pos, entries in spec.items():
        for k, (price, xp) in enumerate(entries):
            players.append({"id": pid, "web_name": f"{pos}{k}",
                            "team_id": (pid % CLUBS) + 1, "position": pos,
                            "price": price, "status": "a", "selected_by_pct": 5.0})
            proj += [{"player_id": pid, "gw": gw, "exp_points": xp}
                     for gw in range(1, horizon + 1)]
            pid += 1
    db.upsert(conn, "players", players, ["id"])
    db.upsert(conn, "projections", proj, ["player_id", "gw"])
    db.set_meta(conn, "current_gw", 1)
    return conn


#: One superstar with no near substitute (MID0), a pair of exactly interchangeable
#: defenders at the squad's fifth-defender slot (DEF4/DEF5), and enough depth
#: everywhere else that nothing is forced.
STANDARD = {
    "GKP": [(45, 3.0), (45, 2.8), (45, 2.6)],
    "DEF": [(45, 5.0), (45, 4.8), (45, 4.6), (45, 4.4), (45, 4.2), (45, 4.2)],
    "MID": [(60, 20.0), (55, 4.0), (55, 3.9), (55, 3.8), (55, 3.7), (55, 3.6)],
    "FWD": [(50, 6.0), (50, 5.8), (50, 5.6), (50, 5.4)],
}

#: Exactly two goalkeepers exist and the squad must hold two, so neither can be
#: dropped: forcing either one out has no legal answer at all.
ONLY_TWO_KEEPERS = {**STANDARD, "GKP": [(45, 3.0), (45, 2.8)]}

#: One cheap defender is the only way the squad fits under the cap. Cheapest
#: legal 15: 2*45 (GKP) + 40 + 4*120 (DEF) + 5*50 (MID) + 3*50 (FWD) = 1010.
#: DEF6 at £90.0m exists purely so there is something that cannot be forced IN.
BUDGET_CRITICAL = {
    "GKP": [(45, 3.0), (45, 2.8), (45, 2.6)],
    "DEF": [(120, 5.0), (120, 4.9), (120, 4.8), (120, 4.7), (120, 4.6),
            (40, 1.0), (900, 0.5)],
    "MID": [(50, 4.0)] * 6,
    "FWD": [(50, 4.0)] * 4,
}
BUDGET_CRITICAL_CAP = 1010


def _by_name(conn):
    return {r["web_name"]: r["id"] for r in conn.execute("SELECT id, web_name FROM players")}


def _solve_and_measure(conn, **kw):
    budget = kw.pop("budget", None)
    sol = optimize.optimise(conn, 1, kw.pop("horizon", 1), budget=budget, **kw)
    return sol, optimize.squad_margins(conn, sol)


# ---------------------------------------------------------------------------
# shape and coverage
# ---------------------------------------------------------------------------

def test_every_squad_member_gets_a_margin(tmp_path):
    conn = _pool(tmp_path, STANDARD)
    sol, rep = _solve_and_measure(conn)
    assert sol.status == "Optimal"
    assert rep.status == "ok"
    assert set(rep.margins) == set(sol.squad)
    assert len(rep.margins) == 15
    for m in rep.margins.values():
        assert m.status in {"optimal", "required"}
        assert m.points is None or m.points >= 0.0


def test_the_margin_baseline_reproduces_the_shipped_objective(tmp_path):
    """The replay must be the SAME model, ceiling term included."""
    conn = _pool(tmp_path, STANDARD)
    ids = _by_name(conn)
    dists = {ids["MID1"]: {"mean": 4.0, "ceiling": 11.0},
             ids["FWD0"]: {"mean": 6.0, "ceiling": 14.0}}
    sol = optimize.optimise(conn, 1, 1, distributions=dists)
    rep = optimize.squad_margins(conn, sol, distributions=dists)
    assert rep.baseline_objective == pytest.approx(sol.meta["objective"], abs=1e-6)
    assert rep.baseline_matches_solution is True


def test_dropping_the_distributions_is_caught_rather_than_silently_rescored(tmp_path):
    """Measuring against a different objective is the mistake worth catching."""
    conn = _pool(tmp_path, STANDARD)
    ids = _by_name(conn)
    dists = {ids["MID1"]: {"mean": 4.0, "ceiling": 30.0}}
    sol = optimize.optimise(conn, 1, 1, distributions=dists)
    rep = optimize.squad_margins(conn, sol)          # distributions forgotten
    assert rep.baseline_matches_solution is False


def test_margins_refuse_a_solve_scored_with_different_objective_params(tmp_path):
    conn = _pool(tmp_path, STANDARD)
    sol = optimize.optimise(conn, 1, 1)
    rep = optimize.squad_margins(
        conn, sol, params=OBJ.DEFAULT.with_(ceiling_weight=0.9))
    assert rep.status == "unavailable"
    assert "objective" in rep.note
    assert rep.margins == {}


# ---------------------------------------------------------------------------
# the numbers themselves
# ---------------------------------------------------------------------------

def test_an_interchangeable_pick_is_worth_nothing(tmp_path):
    """DEF4 and DEF5 are identical in price, points and eligibility.

    Exactly one of them makes the squad, and swapping him for his twin costs the
    objective nothing — so his margin must be zero, not merely small.
    """
    conn = _pool(tmp_path, STANDARD)
    ids = _by_name(conn)
    sol, rep = _solve_and_measure(conn)
    twins = {ids["DEF4"], ids["DEF5"]}
    picked = twins & set(sol.squad)
    assert len(picked) == 1, "exactly one of the identical pair should be picked"
    assert rep.get(picked.pop()).points == pytest.approx(0.0, abs=1e-6)


def test_a_player_with_no_substitute_is_worth_a_lot(tmp_path):
    """MID0 projects 20.0 against a next-best midfielder on 4.0, and captains."""
    conn = _pool(tmp_path, STANDARD)
    ids = _by_name(conn)
    sol, rep = _solve_and_measure(conn)
    assert ids["MID0"] in sol.squad
    assert rep.get(ids["MID0"]).points > 10.0


def test_the_spread_across_the_squad_is_the_whole_point(tmp_path):
    """A squad table that implies equal confidence in all fifteen is a lie."""
    conn = _pool(tmp_path, STANDARD)
    rep = _solve_and_measure(conn)[1]
    pts = sorted(m.points for m in rep.margins.values() if m.points is not None)
    assert pts[0] < 0.5 < pts[-1]
    assert pts[-1] > 20 * pts[len(pts) // 2] or pts[len(pts) // 2] < 1e-6


def test_a_margin_equals_the_gap_to_the_best_squad_without_that_player(tmp_path):
    """Cross-check the forced-out constraint against removing him from the pool.

    Two independent mechanisms for the same quantity: one adds ``squad[i] == 0``
    to the built model, the other deletes his projections so he never becomes a
    variable at all. They must agree exactly, or the constraint is not measuring
    what it claims to.
    """
    conn = _pool(tmp_path, STANDARD)
    ids = _by_name(conn)
    sol, rep = _solve_and_measure(conn)
    target = ids["DEF0"]
    assert target in sol.squad
    measured = rep.get(target).points

    conn.execute("DELETE FROM projections WHERE player_id=?", (target,))
    conn.commit()
    without = optimize.optimise(conn, 1, 1)
    assert target not in without.squad
    gap = sol.meta["objective"] - without.meta["objective"]
    assert measured == pytest.approx(gap, abs=1e-3)


def test_forcing_in_an_unowned_candidate_reports_what_owning_him_costs(tmp_path):
    conn = _pool(tmp_path, STANDARD)
    ids = _by_name(conn)
    sol = optimize.optimise(conn, 1, 1)
    outside = [p for p in (ids["MID5"], ids["DEF4"], ids["DEF5"])
               if p not in sol.squad]
    rep = optimize.squad_margins(conn, sol, candidates=outside)
    for pid in outside:
        assert rep.get(pid).status == "optimal"
        assert rep.get(pid).points >= 0.0
    # The excluded twin is a free swap in both directions; the worst midfielder
    # is not — taking him means dropping a better one.
    twin = next(p for p in outside if p in (ids["DEF4"], ids["DEF5"]))
    assert rep.get(twin).points == pytest.approx(0.0, abs=1e-6)
    assert rep.get(ids["MID5"]).points > 0.0


# ---------------------------------------------------------------------------
# infeasibility is an answer, not an error
# ---------------------------------------------------------------------------

def test_a_structurally_required_player_is_reported_as_required_not_as_a_number(tmp_path):
    """Only two keepers exist and the squad must carry two."""
    conn = _pool(tmp_path, ONLY_TWO_KEEPERS)
    ids = _by_name(conn)
    sol, rep = _solve_and_measure(conn)
    for keeper in (ids["GKP0"], ids["GKP1"]):
        assert keeper in sol.squad
        m = rep.get(keeper)
        assert m.status == "required"
        assert m.points is None
        assert "no legal squad" in m.note
    # ...and the rest of the squad is still measured normally.
    assert any(m.status == "optimal" for m in rep.margins.values())


def test_a_budget_critical_enabler_is_required_too(tmp_path):
    """The cheap defender is the only thing that makes the squad affordable."""
    conn = _pool(tmp_path, BUDGET_CRITICAL)
    ids = _by_name(conn)
    sol, rep = _solve_and_measure(conn, budget=BUDGET_CRITICAL_CAP)
    assert sol.status == "Optimal"
    assert ids["DEF5"] in sol.squad
    assert rep.get(ids["DEF5"]).status == "required"
    assert rep.get(ids["DEF5"]).points is None


def test_a_candidate_who_cannot_be_afforded_is_impossible_not_required(tmp_path):
    conn = _pool(tmp_path, BUDGET_CRITICAL)
    ids = _by_name(conn)
    sol = optimize.optimise(conn, 1, 1, budget=BUDGET_CRITICAL_CAP)
    assert ids["DEF6"] not in sol.squad
    rep = optimize.squad_margins(conn, sol, candidates=[ids["DEF6"]])
    m = rep.get(ids["DEF6"])
    assert m.status == "impossible"
    assert m.points is None


def test_no_margins_are_invented_for_a_degraded_solve(tmp_path):
    conn = _pool(tmp_path, STANDARD)
    sol = optimize.optimise(conn, 1, 1, budget=10)
    assert sol.status != "Optimal"
    rep = optimize.squad_margins(conn, sol)
    assert rep.status == "unavailable"
    assert rep.margins == {}


def test_running_out_of_time_is_reported_rather_than_guessed(tmp_path):
    conn = _pool(tmp_path, STANDARD)
    sol = optimize.optimise(conn, 1, 1)
    rep = optimize.squad_margins(conn, sol, budget_s=0.0)
    assert rep.status == "truncated"
    assert len(rep.margins) == 15
    assert all(m.status == "not_computed" and m.points is None
               for m in rep.margins.values())


# ---------------------------------------------------------------------------
# publication
# ---------------------------------------------------------------------------

def test_the_recommendation_publishes_a_margin_on_every_squad_card(tmp_path):
    conn = _pool(tmp_path, STANDARD)
    sol, rep = _solve_and_measure(conn)
    index = [{"id": pid, "name": f"P{pid}"} for pid in sol.squad]
    reco = artifacts.build_recommendation(conn, sol, index, margins=rep)

    cards = reco["starting"] + reco["bench"]
    assert len(cards) == 15
    for c in cards:
        assert "margin" in c, f"{c['id']} has no margin"
        assert set(c["margin"]) >= {"points", "status"}
    assert reco["captain"]["margin"]["status"] in {"optimal", "required"}

    block = reco["margins"]
    assert block["status"] == "ok"
    assert block["method"] == "exact-forced-resolve"
    assert block["objective_version"] == OBJ.OBJECTIVE_VERSION
    assert block["baseline_matches_solution"] is True
    assert set(block["by_player"]) == {str(p) for p in sol.squad}


def test_a_recommendation_without_margins_is_unchanged(tmp_path):
    """The key must be absent, not null-filled: nothing may assume it is there."""
    conn = _pool(tmp_path, STANDARD)
    sol = optimize.optimise(conn, 1, 1)
    index = [{"id": pid, "name": f"P{pid}"} for pid in sol.squad]
    reco = artifacts.build_recommendation(conn, sol, index)
    assert reco["margins"] is None
    assert all("margin" not in c for c in reco["starting"] + reco["bench"])


def test_write_all_can_be_asked_to_skip_the_sweep(tmp_path, monkeypatch):
    """~3s is cheap, but a caller that does not want it must be able to say so."""
    import gaffer.export.artifacts as A

    calls = []
    monkeypatch.setattr(A, "squad_margins",
                        lambda *a, **k: calls.append(1) or optimize.MarginReport())
    conn = _pool(tmp_path, STANDARD)
    sol = optimize.optimise(conn, 1, 1)
    out = tmp_path / "out"
    A.write_all(conn, sol, 1, 1, "test", out_dir=out, verify_paths=False,
                margins=False)
    assert calls == []
    A.write_all(conn, sol, 1, 1, "test", out_dir=out, verify_paths=False)
    assert calls == [1]
