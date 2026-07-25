"""Solver legality + behaviour tests."""

from gaffer import config
from gaffer.model import projection
from gaffer.solver import optimize


def _solve(conn):
    projection.project(conn, from_gw=1, horizon=1)
    return optimize.optimise(conn, from_gw=1, horizon=1)


def test_build_squad_is_legal(conn):
    sol = _solve(conn)
    assert sol.status == "Optimal"
    assert len(sol.squad) == config.SQUAD_SIZE
    assert len(sol.starting) == 11
    assert len(sol.bench) == 4

    players = {r["id"]: r for r in conn.execute("SELECT id, position, team_id, price FROM players")}
    # position quotas
    from collections import Counter
    pos = Counter(players[i]["position"] for i in sol.squad)
    assert pos == {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
    # club limit
    clubs = Counter(players[i]["team_id"] for i in sol.squad)
    assert max(clubs.values()) <= config.CLUB_LIMIT
    # budget
    assert sum(players[i]["price"] for i in sol.squad) <= config.BUDGET_TENTHS


def test_formation_is_valid(conn):
    sol = _solve(conn)
    players = {r["id"]: r["position"] for r in conn.execute("SELECT id, position FROM players")}
    from collections import Counter
    xi = Counter(players[i] for i in sol.starting)
    assert xi["GKP"] == 1
    assert xi["DEF"] >= 3
    assert xi["MID"] >= 2
    assert xi["FWD"] >= 1


def test_captain_is_a_starter(conn):
    sol = _solve(conn)
    assert sol.captain in sol.starting
    assert sol.vice in sol.starting
    assert sol.captain != sol.vice
