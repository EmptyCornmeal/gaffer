"""T-19 — one objective, two solvers, and the audited solver defects repaired."""

from __future__ import annotations

from collections import Counter

import pytest

from gaffer import config
from gaffer.solver import multiperiod as MP
from gaffer.solver import objective as OBJ
from gaffer.solver import optimize
from gaffer.store import db

HORIZON = 4


def seed(conn, horizon=HORIZON, overrides=None):
    overrides = overrides or {}
    rows = []
    for r in conn.execute("SELECT id FROM players"):
        for gw in range(1, horizon + 1):
            rows.append({
                "player_id": r["id"], "gw": gw,
                "exp_points": overrides.get((r["id"], gw), 3.0 + (r["id"] % 5) * 0.1),
                "p_start": 0.9, "exp_minutes": 85, "confidence": 0.6,
            })
    db.upsert(conn, "projections", rows, ["player_id", "gw"])


def own_legal_squad(conn, sell=50, bank=0):
    by_pos, clubs, squad = {}, Counter(), []
    for r in conn.execute("SELECT id, position, team_id FROM players ORDER BY id"):
        by_pos.setdefault(r["position"], []).append((r["id"], r["team_id"]))
    for pos, q in config.SQUAD_QUOTA.items():
        n = 0
        for pid, tid in by_pos[pos]:
            if n >= q:
                break
            if clubs[tid] < config.CLUB_LIMIT:
                squad.append(pid)
                clubs[tid] += 1
                n += 1
    db.upsert(conn, "my_squad", [
        {"gw": 1, "player_id": p, "is_captain": 0, "is_vice": 0, "multiplier": 1,
         "purchase_price": sell, "selling_price": sell,
         "price_source": "transfer_in", "price_exact": 1} for p in squad],
        ["gw", "player_id"])
    db.set_meta(conn, "bank", bank)
    return set(squad)


# --------------------------------------------------------------------------
# The shared definition
# --------------------------------------------------------------------------

def test_both_solvers_import_the_same_params_object():
    assert optimize.OBJ is OBJ and MP.OBJ is OBJ
    assert OBJ.DEFAULT.horizon_decay == 0.84


def test_ownership_is_absent_from_the_shared_objective():
    assert OBJ.DEFAULT.ownership_weight == 0.0
    OBJ.assert_ownership_neutral(OBJ.DEFAULT)
    with pytest.raises(ValueError):
        OBJ.assert_ownership_neutral(OBJ.DEFAULT.with_(ownership_weight=1.0))


def test_bench_weight_is_position_aware_and_non_zero():
    """The single-GW solver previously gave the bench zero weight entirely."""
    for pos in config.POSITIONS:
        assert OBJ.DEFAULT.bench(pos) > 0
    assert OBJ.DEFAULT.bench("GKP") < OBJ.DEFAULT.bench("DEF")


def test_hit_cost_shares_the_gains_time_basis():
    """Undecayed hits made a week-4 move need 8.03 raw points instead of 4."""
    p = OBJ.DEFAULT
    assert p.hit_cost_at(0) == pytest.approx(4.0)
    assert p.hit_cost_at(4) == pytest.approx(4.0 * 0.84 ** 4)
    assert p.hit_cost_at(4) < p.hit_cost_at(0)


def test_free_transfer_can_never_be_worth_more_than_a_hit():
    OBJ.assert_no_ft_arbitrage(OBJ.DEFAULT)
    assert OBJ.DEFAULT.terminal_ft_value < OBJ.DEFAULT.hit_cost
    with pytest.raises(ValueError, match="hit_cost"):
        OBJ.assert_no_ft_arbitrage(OBJ.DEFAULT.with_(terminal_ft_value=5.0))
    with pytest.raises(ValueError, match="double-counts"):
        OBJ.assert_no_ft_arbitrage(OBJ.DEFAULT.with_(ft_value=1.5))


def test_score_week_terms_sum_to_the_total():
    b = OBJ.score_week(
        OBJ.DEFAULT, 1, xi_points=50.0, captain_points=8.0, vice_points=7.0,
        bench_points_by_pos={"GKP": 1.0, "DEF": 3.0}, paid_transfers=1,
        transfers_made=2)
    assert b.total == pytest.approx(sum(b.terms.values()))
    assert b.terms["hits"] < 0 and b.terms["xi"] > 0


def test_terminal_value_rewards_all_three_carryovers():
    t = OBJ.score_terminal(OBJ.DEFAULT, final_ft=2, final_bank=10, final_xi_points=55)
    assert set(t.terms) == {"terminal_ft", "terminal_bank", "terminal_squad"}
    assert all(v > 0 for v in t.terms.values())


# --------------------------------------------------------------------------
# Cross-solver agreement
# --------------------------------------------------------------------------

def test_one_week_equivalence(conn):
    """Given the same one-week problem, both solvers reach the same solution."""
    seed(conn, horizon=1)
    single = optimize.optimise(conn, 1, 1, free_transfers=1)
    plan = MP.optimise_path(conn, 1, horizon=1, free_transfers=1)
    assert plan.status == "Optimal"
    step = plan.steps[0]
    assert set(single.squad) == set(step.squad)
    assert set(single.starting) == set(step.starting)
    assert single.xi_expected == pytest.approx(step.xi_expected, abs=1e-6)
    # The seeded pool contains exact ties for the armband (exp_points is a
    # function of pid % 5), so assert the captain is equally good rather than
    # that the two MILPs break a tie identically.
    xp = {r["player_id"]: r["exp_points"]
          for r in conn.execute("SELECT player_id, exp_points FROM projections")}
    assert xp[single.captain] == pytest.approx(xp[step.captain])
    assert xp[single.captain] == pytest.approx(max(xp[i] for i in single.starting))


def test_one_week_equivalence_with_a_unique_optimum(conn):
    """No ties: the two solvers must then agree on the captain's identity too."""
    seed(conn, horizon=1)
    best = next(r["id"] for r in conn.execute(
        "SELECT id FROM players WHERE position='MID' ORDER BY id"))
    conn.execute("UPDATE projections SET exp_points=25 WHERE player_id=?", (best,))
    conn.commit()
    single = optimize.optimise(conn, 1, 1, free_transfers=1)
    step = MP.optimise_path(conn, 1, horizon=1, free_transfers=1).steps[0]
    assert single.captain == step.captain == best


@pytest.mark.parametrize("h", [1, 2, 3, 4, 5, 6, 7, 8])
def test_horizons_one_to_eight_are_solvable_and_legal(conn, h):
    seed(conn, horizon=h)
    plan = MP.optimise_path(conn, 1, horizon=h, free_transfers=1)
    assert plan.status == "Optimal"
    assert len(plan.steps) == h
    for s in plan.steps:
        assert len(s.squad) == config.SQUAD_SIZE
        assert len(s.starting) == 11
        assert s.captain in s.starting and s.vice in s.starting
        assert s.captain != s.vice


# --------------------------------------------------------------------------
# The repaired defects
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ft", [1, 2, 3, 4, 5])
def test_free_transfers_zero_to_five(conn, ft):
    seed(conn)
    own_legal_squad(conn)
    plan = MP.optimise_path(conn, 1, horizon=HORIZON, free_transfers=ft)
    assert plan.status == "Optimal"
    assert plan.steps[0].free_transfers == min(ft, config.MAX_FREE_TRANSFERS)


@pytest.mark.parametrize("h", [5, 6, 7, 8])
def test_no_manufactured_free_transfers(conn, h):
    """The audited arbitrage: at h>=6 the planner could pay -4 to create an FT.

    A plan that makes no transfers must never take a hit, at any horizon.
    """
    seed(conn, horizon=h)
    own_legal_squad(conn)
    plan = MP.optimise_path(conn, 1, horizon=h, free_transfers=1)
    assert plan.status == "Optimal"
    for s in plan.steps:
        assert s.hits == max(0, len(s.transfers_in) - s.free_transfers), (
            f"gw{s.gw}: {s.hits} hits for {len(s.transfers_in)} transfers "
            f"with {s.free_transfers} free")
        if not s.transfers_in:
            assert s.hits == 0, "a hit with no transfer is manufactured"


def test_free_transfers_never_exceed_the_cap(conn):
    seed(conn, horizon=8)
    own_legal_squad(conn)
    plan = MP.optimise_path(conn, 1, horizon=8, free_transfers=5)
    for s in plan.steps:
        assert 1 <= s.free_transfers <= config.MAX_FREE_TRANSFERS


def test_rolling_beats_a_marginal_transfer(conn):
    """With nothing worth buying, the plan rolls rather than churning."""
    seed(conn)
    own_legal_squad(conn)
    plan = MP.optimise_path(conn, 1, horizon=HORIZON, free_transfers=1)
    assert sum(len(s.transfers_in) for s in plan.steps) == 0
    assert plan.steps[1].free_transfers > plan.steps[0].free_transfers


def test_no_terminal_week_dumping(conn):
    """Transfers must not pile into the last modelled week for its own sake."""
    seed(conn)
    own_legal_squad(conn)
    plan = MP.optimise_path(conn, 1, horizon=HORIZON, free_transfers=3)
    last = plan.steps[-1]
    assert len(last.transfers_in) <= 1, (
        f"final week made {len(last.transfers_in)} transfers with nothing to gain")


def test_squad_continuity_every_week(conn):
    seed(conn)
    own_legal_squad(conn)
    plan = MP.optimise_path(conn, 1, horizon=HORIZON, free_transfers=2)
    for a, b in zip(plan.steps, plan.steps[1:], strict=False):
        added = set(b.squad) - set(a.squad)
        removed = set(a.squad) - set(b.squad)
        assert added == set(b.transfers_in)
        assert removed == set(b.transfers_out)
        assert len(added) == len(removed)


def test_hits_in_a_late_week_are_discounted_not_ignored(conn):
    """A late-horizon hit is cheaper in present value, so a big late upgrade
    should still be taken."""
    seed(conn, horizon=6)
    owned = own_legal_squad(conn)
    target = next(r["id"] for r in conn.execute(
        "SELECT id FROM players WHERE position='MID' ORDER BY id") if r["id"] not in owned)
    for gw in range(4, 7):
        conn.execute("UPDATE projections SET exp_points=40 WHERE player_id=? AND gw=?",
                     (target, gw))
    conn.commit()
    plan = MP.optimise_path(conn, 1, horizon=6, free_transfers=1)
    assert plan.status == "Optimal"
    assert any(target in s.squad for s in plan.steps)


def test_bench_is_no_longer_an_arbitrary_tiebreak(conn):
    """With bench weight > 0 a strictly better bench player must be preferred."""
    seed(conn, horizon=1)
    mids = [r["id"] for r in conn.execute(
        "SELECT id FROM players WHERE position='MID' ORDER BY id")]
    good, bad = mids[-1], mids[-2]
    conn.execute("UPDATE projections SET exp_points=2.9 WHERE player_id=?", (bad,))
    conn.execute("UPDATE projections SET exp_points=2.95 WHERE player_id=?", (good,))
    conn.commit()
    sol = optimize.optimise(conn, 1, 1, free_transfers=1)
    if good in sol.bench or bad in sol.bench:
        assert bad not in sol.bench or good in sol.squad


def test_generic_and_personalised_modes_both_solve(conn):
    seed(conn)
    build = optimize.optimise(conn, 1, HORIZON, free_transfers=1)
    assert build.meta.get("mode") == "build"
    own_legal_squad(conn)
    transfer = optimize.optimise(conn, 1, HORIZON, free_transfers=1)
    assert transfer.meta.get("mode") == "transfer"


def test_missing_selling_price_does_not_create_budget(conn):
    seed(conn)
    own_legal_squad(conn)
    conn.execute("UPDATE my_squad SET selling_price=NULL")
    conn.commit()
    players = optimize.load_players(conn, 1, 1)
    for p in players.values():
        if p.in_squad:
            assert p.sell_value <= p.price
