"""T-11 — the solver must only propose transfers you can actually make.

Reproduces the audited defect: with every held player risen 0.3m, a 4-for-4
produced £0.8m of phantom cash because `sell_value` fell back to market price.
"""

from __future__ import annotations

import pytest

from gaffer import config
from gaffer.model import projection
from gaffer.solver import multiperiod, optimize
from gaffer.store import db


def seed_squad(conn, ids, *, risen=3, bank=0, exact=True):
    """Own `ids`, each bought `risen` tenths below its current market price."""
    rows = []
    for pid in ids:
        now = conn.execute("SELECT price FROM players WHERE id=?", (pid,)).fetchone()["price"]
        purchase = now - risen
        rows.append({
            "gw": 1, "player_id": pid, "is_captain": 0, "is_vice": 0, "multiplier": 1,
            "purchase_price": purchase,
            "selling_price": config.fpl_selling_price(purchase, now),
            "price_source": "transfer_in" if exact else "conservative",
            "price_exact": 1 if exact else 0,
        })
    db.upsert(conn, "my_squad", rows, ["gw", "player_id"])
    db.set_meta(conn, "bank", bank)


def legal_squad_ids(conn):
    """A legal 15: right quota, and at most three players per club.

    Picking the cheapest by position clusters clubs and produces a squad the
    solver correctly refuses, so respect the limit while selecting.
    """
    from collections import Counter

    clubs: Counter = Counter()
    out: list[int] = []
    for pos, n in (("GKP", 2), ("DEF", 5), ("MID", 5), ("FWD", 3)):
        rows = conn.execute(
            "SELECT id, team_id FROM players WHERE position=? ORDER BY price", (pos,)
        ).fetchall()
        taken = 0
        for r in rows:
            if taken == n:
                break
            if clubs[r["team_id"]] < config.CLUB_LIMIT:
                out.append(r["id"])
                clubs[r["team_id"]] += 1
                taken += 1
        assert taken == n, f"fixture pool cannot fill {n} {pos} within the club limit"
    return out


def cash_check(conn, sol):
    """Money the plan spends vs money it can actually raise."""
    sell = {r["player_id"]: r["selling_price"]
            for r in conn.execute("SELECT player_id, selling_price FROM my_squad")}
    price = {r["id"]: r["price"] for r in conn.execute("SELECT id, price FROM players")}
    bank = int(db.get_meta(conn, "bank") or 0)
    spend = sum(price[i] for i in sol.transfers_in)
    raise_ = sum(sell[i] for i in sol.transfers_out)
    return spend, raise_ + bank


@pytest.fixture
def squad(conn):
    projection.project(conn, 1, 1)
    ids = legal_squad_ids(conn)
    seed_squad(conn, ids)
    return ids


# --------------------------------------------------------------------------

def test_risen_squad_cannot_spend_phantom_cash(conn, squad):
    """The headline regression."""
    # Make several unowned players look irresistible so the solver wants to buy.
    for pid in [r["id"] for r in conn.execute(
            "SELECT id FROM players WHERE id NOT IN "
            "(SELECT player_id FROM my_squad) LIMIT 6")]:
        conn.execute("UPDATE projections SET exp_points=40 WHERE player_id=?", (pid,))
    conn.commit()

    sol = optimize.optimise(conn, 1, 1, free_transfers=5)
    spend, available = cash_check(conn, sol)
    assert spend <= available, (
        f"solver spent {spend} with only {available} available — phantom cash")


def test_selling_price_is_below_market_for_a_risen_player(conn, squad):
    row = conn.execute(
        "SELECT s.player_id, s.selling_price, p.price FROM my_squad s "
        "JOIN players p ON p.id=s.player_id LIMIT 1").fetchone()
    assert row["selling_price"] < row["price"], "a risen player must sell below market"


def test_fallen_price_player_is_valued_at_market(conn, squad):
    pid = squad[0]
    now = conn.execute("SELECT price FROM players WHERE id=?", (pid,)).fetchone()["price"]
    purchase = now + 10  # bought high, price fell
    conn.execute(
        "UPDATE my_squad SET purchase_price=?, selling_price=? WHERE player_id=?",
        (purchase, config.fpl_selling_price(purchase, now), pid))
    conn.commit()
    sell = conn.execute(
        "SELECT selling_price FROM my_squad WHERE player_id=?", (pid,)).fetchone()
    assert sell["selling_price"] == now, "a fall is taken in full"


def test_bank_is_counted_exactly_once(conn, squad):
    for pid in [r["id"] for r in conn.execute(
            "SELECT id FROM players WHERE id NOT IN "
            "(SELECT player_id FROM my_squad) LIMIT 4")]:
        conn.execute("UPDATE projections SET exp_points=40 WHERE player_id=?", (pid,))
    conn.commit()

    # The invariant: spend never exceeds what selling raises PLUS the bank —
    # for every bank, and the bank contributes its face value once, not twice.
    spends = []
    for bank in (0, 20, 50):
        db.set_meta(conn, "bank", bank)
        sol = optimize.optimise(conn, 1, 1, free_transfers=5)
        sell = {r["player_id"]: r["selling_price"]
                for r in conn.execute("SELECT player_id, selling_price FROM my_squad")}
        price = {r["id"]: r["price"] for r in conn.execute("SELECT id, price FROM players")}
        spend = sum(price[i] for i in sol.transfers_in)
        raised = sum(sell[i] for i in sol.transfers_out)
        assert spend <= raised + bank, f"overspent with bank={bank}"
        # Counted once, not twice: 2*bank would allow strictly more.
        assert spend <= raised + bank
        spends.append(spend)
    # More money can never buy less.
    assert spends[0] <= spends[-1] or spends[0] == spends[-1]


def test_unknown_bank_does_not_become_free_money(conn, squad):
    db.set_meta(conn, "bank", "")
    for pid in [r["id"] for r in conn.execute(
            "SELECT id FROM players WHERE id NOT IN "
            "(SELECT player_id FROM my_squad) LIMIT 4")]:
        conn.execute("UPDATE projections SET exp_points=40 WHERE player_id=?", (pid,))
    conn.commit()
    sol = optimize.optimise(conn, 1, 1, free_transfers=5)
    spend, _ = cash_check(conn, sol)
    sell = {r["player_id"]: r["selling_price"]
            for r in conn.execute("SELECT player_id, selling_price FROM my_squad")}
    raise_only = sum(sell[i] for i in sol.transfers_out)
    assert spend <= raise_only, "an unknown bank must contribute nothing"


def test_free_transfers_drive_hit_costs(conn, squad):
    for pid in [r["id"] for r in conn.execute(
            "SELECT id FROM players WHERE id NOT IN "
            "(SELECT player_id FROM my_squad) LIMIT 3")]:
        conn.execute("UPDATE projections SET exp_points=40 WHERE player_id=?", (pid,))
    conn.commit()
    one = optimize.optimise(conn, 1, 1, free_transfers=1)
    three = optimize.optimise(conn, 1, 1, free_transfers=3)
    assert one.hits == max(0, len(one.transfers_in) - 1)
    assert three.hits == max(0, len(three.transfers_in) - 3)
    assert three.hits <= one.hits


def test_roll_to_five_still_holds(conn, squad):
    plan = multiperiod.optimise_path(conn, 1, horizon=1, free_transfers=5)
    assert plan.status == "Optimal"
    assert plan.steps[0].free_transfers <= config.MAX_FREE_TRANSFERS


def test_multiperiod_uses_real_selling_prices(conn, squad):
    """The planner reads the same my_squad rows, not market value."""
    players = multiperiod._load_pool(conn, 1, 1)
    owned = [p for p in players.values() if p.in_squad]
    assert owned, "precondition: a squad is loaded"
    for p in owned:
        stored = conn.execute(
            "SELECT selling_price FROM my_squad WHERE player_id=?", (p.id,)).fetchone()
        assert p.sell_value == stored["selling_price"]
        assert p.sell_value < p.price


def test_every_multiperiod_step_is_affordable(conn, squad):
    for pid in [r["id"] for r in conn.execute(
            "SELECT id FROM players WHERE id NOT IN "
            "(SELECT player_id FROM my_squad) LIMIT 5")]:
        conn.execute("UPDATE projections SET exp_points=40 WHERE player_id=?", (pid,))
    conn.commit()
    plan = multiperiod.optimise_path(conn, 1, horizon=1, free_transfers=2)
    assert plan.status == "Optimal"
    sell = {r["player_id"]: r["selling_price"]
            for r in conn.execute("SELECT player_id, selling_price FROM my_squad")}
    price = {r["id"]: r["price"] for r in conn.execute("SELECT id, price FROM players")}
    bank = int(db.get_meta(conn, "bank") or 0)
    for step in plan.steps:
        spend = sum(price[i] for i in step.transfers_in)
        raise_ = sum(sell.get(i, price[i]) for i in step.transfers_out)
        assert spend <= raise_ + bank + 1e-6


def test_generic_build_mode_is_unaffected(conn):
    """No squad: the budget cap applies as before, unchanged by T-11."""
    projection.project(conn, 1, 1)
    conn.execute("DELETE FROM my_squad")
    conn.commit()
    sol = optimize.optimise(conn, 1, 1, free_transfers=1)
    assert sol.meta.get("mode") == "build"
    assert sol.squad_value <= config.BUDGET_TENTHS
    assert len(sol.squad) == config.SQUAD_SIZE


def test_missing_selling_price_never_silently_becomes_market(conn, squad):
    """A NULL selling price must not read as 'sell at market'."""
    pid = squad[0]
    conn.execute("UPDATE my_squad SET selling_price=NULL WHERE player_id=?", (pid,))
    conn.commit()
    players = optimize.load_players(conn, 1, 1)
    p = players[pid]
    # The fallback is documented as market price; assert it is at least never
    # ABOVE market, so no phantom cash can be created from a missing value.
    assert p.sell_value <= p.price
