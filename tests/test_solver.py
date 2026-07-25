"""Solver legality + behaviour tests."""

from collections import Counter

from gaffer import config
from gaffer.model import projection
from gaffer.solver import optimize
from gaffer.store import db


def _solve(conn):
    projection.project(conn, from_gw=1, horizon=1)
    return optimize.optimise(conn, from_gw=1, horizon=1)


def _seed_owned(conn, bank=0, selling=None):
    """Make the build-optimal squad the *owned* squad so we can test transfers.

    ``selling`` (dict pid->tenths) sets each player's FPL selling price; unset
    players fall back to market price. ``bank`` sets available cash (tenths).
    """
    projection.project(conn, from_gw=1, horizon=1)
    build = optimize.optimise(conn, from_gw=1, horizon=1)
    rows = [
        {"gw": 1, "player_id": pid, "is_captain": 0, "is_vice": 0,
         "multiplier": 1, "purchase_price": None,
         "selling_price": (selling or {}).get(pid)}
        for pid in build.squad
    ]
    db.upsert(conn, "my_squad", rows, ["gw", "player_id"])
    db.set_meta(conn, "bank", bank)
    return build


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


# --- transfer path ("make it work for money") -----------------------------

def test_selling_price_rule():
    # you get purchase back + half the *rise*, rounded down; falls taken in full
    assert config.fpl_selling_price(60, 65) == 62   # +0.5 rise -> +0.2
    assert config.fpl_selling_price(60, 61) == 60   # +0.1 rise -> +0.0
    assert config.fpl_selling_price(100, 111) == 105
    assert config.fpl_selling_price(60, 57) == 57   # fall taken in full
    assert config.fpl_selling_price(60, 60) == 60


def test_transfer_respects_cash_from_selling_price(conn):
    # own the build squad; simulate every owned player having dropped 1.0m so its
    # selling value is below market -> the solver must not spend money it lacks.
    build = _seed_owned(conn, bank=5, selling=None)
    prices = {r["id"]: r["price"] for r in conn.execute("SELECT id, price FROM players")}
    sell = {pid: prices[pid] - 10 for pid in build.squad}
    _seed_owned(conn, bank=5, selling=sell)
    sol = optimize.optimise(conn, from_gw=1, horizon=1, free_transfers=1)

    assert sol.status == "Optimal"
    assert len(sol.squad) == config.SQUAD_SIZE
    spent = sum(prices[i] for i in sol.transfers_in)
    recouped = sum(sell[i] for i in sol.transfers_out)
    assert spent <= 5 + recouped  # never recommends an unaffordable move
    # club limit still respected after transfers
    club = {r["id"]: r["team_id"] for r in conn.execute("SELECT id, team_id FROM players")}
    assert max(Counter(club[i] for i in sol.squad).values()) <= config.CLUB_LIMIT


def test_beneficial_affordable_transfer_is_taken(conn):
    # a cheap unowned player made hugely valuable + plenty of bank -> gets bought
    build = _seed_owned(conn, bank=1000)
    owned = set(build.squad)
    target = next(
        r["id"] for r in conn.execute("SELECT id FROM players WHERE position='MID'")
        if r["id"] not in owned
    )
    conn.execute("UPDATE projections SET exp_points=50 WHERE player_id=? AND gw=1", (target,))
    sol = optimize.optimise(conn, from_gw=1, horizon=1, free_transfers=1)
    assert target in sol.squad
    assert target in sol.transfers_in
    # one free transfer used -> no points hit
    assert sol.hits == max(0, len(sol.transfers_in) - 1)


def test_hits_accounting_matches_free_transfers(conn):
    # two attractive unowned targets but only 1 FT -> a 2nd transfer costs a hit
    build = _seed_owned(conn, bank=1000)
    owned = set(build.squad)
    targets = [
        r["id"] for r in conn.execute("SELECT id FROM players WHERE position='MID'")
        if r["id"] not in owned
    ][:2]
    for t in targets:
        conn.execute("UPDATE projections SET exp_points=80 WHERE player_id=? AND gw=1", (t,))
    sol = optimize.optimise(conn, from_gw=1, horizon=1, free_transfers=1)
    assert sol.hits == max(0, len(sol.transfers_in) - 1)


def test_infeasible_solve_degrades_without_crashing(conn):
    # £1.0m can't buy a legal 15 -> infeasible; must degrade, not IndexError
    projection.project(conn, from_gw=1, horizon=1)
    sol = optimize.optimise(conn, from_gw=1, horizon=1, budget=10)
    assert sol.status != "Optimal"
    assert sol.squad == []          # nothing held in build mode
    assert sol.captain == 0         # no crash on empty starting XI
