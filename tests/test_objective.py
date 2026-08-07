"""T-14 — the base objective optimises expected points, not global popularity.

At the shipped `balanced` weight the ownership term was ~70% of the objective:
it gave away 2.11 xP on the armband and changed 11 of 15 squad players versus
the pure-points solve.
"""

from __future__ import annotations

import pytest

from gaffer.model import projection
from gaffer.solver import optimize
from gaffer.store import db


@pytest.fixture
def seeded(conn):
    projection.project(conn, 1, 1)
    return conn


def set_xp(conn, pid, xp):
    conn.execute("UPDATE projections SET exp_points=? WHERE player_id=?", (xp, pid))
    conn.commit()


def set_owned(conn, pid, pct):
    conn.execute("UPDATE players SET selected_by_pct=? WHERE id=?", (pct, pid))
    conn.commit()


def a_mid(conn, offset=0):
    rows = conn.execute(
        "SELECT id FROM players WHERE position='MID' ORDER BY id").fetchall()
    return rows[offset]["id"]


# --------------------------------------------------------------------------
# The weights themselves
# --------------------------------------------------------------------------

def test_ownership_weighting_is_neutralised():
    assert set(optimize.RISK_WEIGHTS.values()) == {0.0}
    assert optimize.NEUTRAL_RISK_WEIGHT == 0.0


def test_every_stance_is_currently_identical():
    """Kept for artifact shape; they must not silently differ."""
    assert len(set(optimize.RISK_WEIGHTS.values())) == 1


def test_the_reason_is_documented_for_users():
    assert "league" in optimize.RISK_NOTE.lower()
    assert "T-17" in optimize.RISK_NOTE


# --------------------------------------------------------------------------
# Captain
# --------------------------------------------------------------------------

def test_captain_is_the_highest_expected_points_starter(seeded):
    """The headline defect: a lower-xP but higher-owned player took the armband."""
    conn = seeded
    star, crowd = a_mid(conn, 0), a_mid(conn, 1)
    set_xp(conn, star, 12.0)
    set_xp(conn, crowd, 8.0)
    set_owned(conn, star, 3.0)      # a differential
    set_owned(conn, crowd, 85.0)    # a near-must-own

    sol = optimize.optimise(conn, 1, 1, free_transfers=1,
                            template_weight=optimize.RISK_WEIGHTS["balanced"])
    xp = {r["player_id"]: r["exp_points"]
          for r in conn.execute("SELECT player_id, exp_points FROM projections")}
    best = max(sol.starting, key=lambda i: xp[i])
    assert sol.captain == best
    assert xp[sol.captain] == pytest.approx(12.0)


def test_a_heavily_owned_lower_xp_player_no_longer_wins_the_armband(seeded):
    conn = seeded
    star, crowd = a_mid(conn, 0), a_mid(conn, 1)
    set_xp(conn, star, 9.0)
    set_xp(conn, crowd, 8.5)        # only a small gap
    set_owned(conn, star, 1.0)
    set_owned(conn, crowd, 95.0)
    sol = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert sol.captain == star, "a 0.5 xP edge must beat a 94-point ownership gap"


def test_the_old_weight_would_have_flipped_it(seeded):
    """Demonstrates the defect the neutralisation removes."""
    conn = seeded
    star, crowd = a_mid(conn, 0), a_mid(conn, 1)
    set_xp(conn, star, 9.0)
    set_xp(conn, crowd, 8.5)
    set_owned(conn, star, 1.0)
    set_owned(conn, crowd, 95.0)
    old = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=8.0)
    new = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert old.captain == crowd, "the old 8.0 weight captained the popular player"
    assert new.captain == star
    assert old.captain != new.captain


def test_vice_is_the_next_best_and_distinct(seeded):
    conn = seeded
    a, b = a_mid(conn, 0), a_mid(conn, 1)
    set_xp(conn, a, 12.0)
    set_xp(conn, b, 11.0)
    sol = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert sol.captain != sol.vice
    assert sol.captain in sol.starting and sol.vice in sol.starting


# --------------------------------------------------------------------------
# Squad selection
# --------------------------------------------------------------------------

def test_large_projection_gaps_pick_the_higher_scorer(seeded):
    conn = seeded
    star, crowd = a_mid(conn, 0), a_mid(conn, 1)
    set_xp(conn, star, 20.0)
    set_xp(conn, crowd, 2.0)
    set_owned(conn, star, 0.5)
    set_owned(conn, crowd, 99.0)
    sol = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert star in sol.starting


def test_ownership_data_is_ignored_entirely_by_the_base_objective(seeded):
    """Same projections, wildly different ownership -> identical squad."""
    conn = seeded
    before = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    conn.execute("UPDATE players SET selected_by_pct = 90.0 WHERE id % 2 = 0")
    conn.execute("UPDATE players SET selected_by_pct = 0.1 WHERE id % 2 = 1")
    conn.commit()
    after = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert set(before.squad) == set(after.squad)
    assert before.captain == after.captain


def test_missing_ownership_data_is_harmless(seeded):
    conn = seeded
    conn.execute("UPDATE players SET selected_by_pct = NULL")
    conn.commit()
    sol = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert sol.status == "Optimal"
    assert len(sol.squad) == 15


def test_template_squad_and_differential_squad_now_coincide(seeded):
    """With the dial at zero the three stances must produce one answer."""
    conn = seeded
    sols = {
        name: optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=w)
        for name, w in optimize.RISK_WEIGHTS.items()
    }
    squads = [frozenset(s.squad) for s in sols.values()]
    assert len(set(squads)) == 1
    caps = {s.captain for s in sols.values()}
    assert len(caps) == 1


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def test_generic_build_mode_still_works(seeded):
    conn = seeded
    conn.execute("DELETE FROM my_squad")
    conn.commit()
    sol = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert sol.meta.get("mode") == "build"
    assert len(sol.squad) == 15


def test_personalised_transfer_mode_still_works(seeded):
    conn = seeded
    ids = [r["id"] for r in conn.execute("SELECT id FROM players LIMIT 15")]
    db.upsert(conn, "my_squad", [
        {"gw": 1, "player_id": p, "is_captain": 0, "is_vice": 0, "multiplier": 1,
         "purchase_price": 50, "selling_price": 50, "price_source": "transfer_in",
         "price_exact": 1} for p in ids], ["gw", "player_id"])
    sol = optimize.optimise(conn, 1, 1, free_transfers=1, template_weight=0.0)
    assert sol.meta.get("mode") == "transfer"
