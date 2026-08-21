"""T-21 — the weekly decision, and the threshold that stops noise being advice.

The audited home page opened on a solver table, and any positive delta however
small was rendered as a recommendation. These tests pin the two properties that
make the new answer trustworthy: the hold baseline is scored in the *same*
scenarios as the move, and a sub-threshold edge is reported as "too close to
call" rather than dressed up as a decision.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from gaffer import config, decision


class Scen:
    def __init__(self, means: dict[int, float], n=4000, sd=3.0, seed=11):
        rng = np.random.default_rng(seed)
        self.n_sims, self.seed = n, seed
        self.table = {p: rng.normal(m, sd, n) for p, m in means.items()}

    def row(self, pid):
        return self.table.get(pid, np.zeros(self.n_sims))

    def squad_points(self, starting, captain=None, bench=None,
                     captain_multiplier=2, bench_boost=False):
        t = np.zeros(self.n_sims)
        for p in starting:
            t += self.row(p)
        if captain is not None:
            t += self.row(captain) * (captain_multiplier - 1)
        if bench_boost and bench:
            for p in bench:
                t += self.row(p)
        return t


def cmp_for(move_mean, hold_mean, sd=3.0, hit=0, n=8000):
    """A comparison where the move's XI averages `move_mean` more per player."""
    scen = Scen({1: move_mean, 2: hold_mean}, n=n, sd=sd)
    return decision.compare(scen, move_xi=[1], move_captain=None,
                            hold_xi=[2], hold_captain=None, hit_cost=hit)


# --------------------------------------------------------------------------
# Like-for-like comparison
# --------------------------------------------------------------------------

def test_move_and_hold_are_drawn_from_the_same_scenarios():
    """The shared player must contribute identically to both sides."""
    scen = Scen({1: 5.0, 2: 5.0, 3: 6.0})
    c = decision.compare(scen, move_xi=[1, 3], move_captain=None,
                         hold_xi=[2, 3], hold_captain=None)
    # Player 3 is in both XIs, so he cancels exactly; the delta is 1 vs 2 only.
    direct = scen.row(1) - scen.row(2)
    assert c.delta == pytest.approx(float(direct.mean()), abs=1e-9)


def test_an_identical_squad_has_exactly_zero_delta():
    scen = Scen({p: 4.0 for p in range(1, 12)})
    xi = list(range(1, 12))
    c = decision.compare(scen, move_xi=xi, move_captain=1, hold_xi=xi,
                         hold_captain=1)
    assert c.delta == 0.0
    assert c.p_move_beats_hold == 0.0


def test_a_hit_is_charged_in_every_scenario_not_to_the_mean():
    """A -4 is certain, so it must move the win probability too."""
    free = cmp_for(6.0, 5.0, hit=0)
    paid = cmp_for(6.0, 5.0, hit=4)
    assert paid.delta == pytest.approx(free.delta - 4, abs=0.01)
    assert paid.p_move_beats_hold < free.p_move_beats_hold
    assert paid.hit_cost == 4


def test_the_confidence_interval_brackets_the_delta():
    c = cmp_for(7.0, 5.0)
    lo, hi = c.delta_ci95
    assert lo < c.delta < hi
    assert c.n_sims == 8000


def test_no_scenarios_produces_no_false_precision():
    c = decision.compare(Scen({}, n=0), move_xi=[1], move_captain=None,
                         hold_xi=[2], hold_captain=None)
    assert c.n_sims == 0 and c.delta == 0.0 and c.p_move_beats_hold == 0.0


def test_an_empty_side_is_not_scored_as_zero_points():
    c = decision.compare(Scen({1: 5.0}), move_xi=[1], move_captain=None,
                         hold_xi=[], hold_captain=None)
    assert c.delta == 0.0, "an unknown hold must not read as a 5-point win"


# --------------------------------------------------------------------------
# The minimum actionable threshold
# --------------------------------------------------------------------------

def test_a_negligible_edge_is_too_close_to_call():
    action, reason = decision.classify(cmp_for(5.3, 5.0))
    assert action == decision.ACTION_TOO_CLOSE
    assert "inside" in reason and "bar" in reason


def test_a_clear_edge_is_a_transfer():
    action, reason = decision.classify(cmp_for(8.0, 5.0))
    assert action == decision.ACTION_TRANSFER
    assert "+3" in reason or "+2" in reason


def test_a_clear_loss_is_a_roll():
    action, reason = decision.classify(cmp_for(3.0, 5.0))
    assert action == decision.ACTION_ROLL
    assert "roll" in reason.lower()


def test_a_good_mean_with_a_coin_flip_distribution_is_not_a_recommendation():
    """+2 points that only wins 51% of the time is a coin flip with a mean."""
    c = decision.Comparison(
        move_expected=52.0, hold_expected=50.0, delta=2.0,
        delta_ci95=(-6.0, 10.0), p_move_beats_hold=0.51, n_sims=4000,
        short_term_delta=2.0, horizon_delta=2.0, hit_cost=0)
    action, reason = decision.classify(c)
    assert action == decision.ACTION_TOO_CLOSE
    assert "51%" in reason


def test_a_decisive_edge_waives_the_probability_gate():
    """A huge projected gain should not be blocked by a wide distribution."""
    c = decision.Comparison(
        move_expected=60.0, hold_expected=50.0, delta=10.0,
        delta_ci95=(-2.0, 22.0), p_move_beats_hold=0.52, n_sims=4000,
        short_term_delta=10.0, horizon_delta=10.0, hit_cost=0)
    action, _ = decision.classify(c)
    assert action == decision.ACTION_TRANSFER


def test_the_threshold_boundary_is_exact():
    below = decision.Comparison(0, 0, 0.99, (0.5, 1.5), 0.9, 4000, 0.99, 0.99, 0)
    at = decision.Comparison(0, 0, 1.00, (0.5, 1.5), 0.9, 4000, 1.0, 1.0, 0)
    assert decision.classify(below)[0] == decision.ACTION_TOO_CLOSE
    assert decision.classify(at)[0] == decision.ACTION_TRANSFER


def test_a_horizon_driven_move_can_qualify_on_horizon_value_alone():
    c = decision.Comparison(0, 0, 0.2, (-0.3, 0.7), 0.9, 4000, 0.2, 4.0, 0)
    action, _ = decision.classify(c)
    assert action == decision.ACTION_TRANSFER, "a fixture-swing move is real"


def test_thresholds_are_configurable_and_tested_as_such():
    c = cmp_for(5.5, 5.0)
    assert decision.classify(c, min_points=0.1)[0] == decision.ACTION_TRANSFER
    assert decision.classify(c, min_points=9.0)[0] == decision.ACTION_TOO_CLOSE


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def test_a_wide_interval_is_low_confidence():
    c = decision.Comparison(0, 0, 1.0, (-3.0, 5.0), 0.6, 4000, 1.0, 1.0, 0)
    assert decision.confidence_band(c) == "low"


def test_a_large_delta_relative_to_its_error_is_high_confidence():
    c = decision.Comparison(0, 0, 5.0, (4.5, 5.5), 0.99, 4000, 5.0, 5.0, 0)
    assert decision.confidence_band(c) == "high"


def test_no_simulations_is_unknown_confidence_not_low():
    c = decision.Comparison(0, 0, 0.0, (0.0, 0.0), 0.0, 0, 0.0, 0.0, 0)
    assert decision.confidence_band(c) == "unknown"


# --------------------------------------------------------------------------
# Executability
# --------------------------------------------------------------------------

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    for pid in range(1, 21):
        c.execute("INSERT INTO players (id, web_name, team_id, position, price) "
                  "VALUES (?,?,?,?,?)", (pid, f"P{pid}", 1, "MID", 50 + pid))
    for pid in range(1, 16):
        c.execute(
            "INSERT INTO my_squad (gw, player_id, multiplier, purchase_price, "
            "selling_price, price_source, price_exact) VALUES (?,?,1,?,?,?,1)",
            (7, pid, 50 + pid, 50 + pid, "transfer_in"))
    c.commit()
    return c


def test_selling_price_not_market_price_funds_the_move(conn):
    """Player 1's market price rose to 51 but he sells for 51 here; player 16
    costs 66. The bank must be checked against the SELLING price."""
    conn.execute("UPDATE my_squad SET selling_price=45 WHERE player_id=1")
    conn.commit()
    e = decision.executability(conn, [16], [1], free_transfers=1, bank=100)
    assert e.recouped == 45, "must use the FPL selling price, not now_cost"
    assert e.bank_after == 100 + 45 - 66


def test_an_unknown_bank_is_never_treated_as_zero_or_generous(conn):
    e = decision.executability(conn, [16], [1], free_transfers=1, bank=None)
    assert e.affordable is False
    assert e.bank_before is None and e.bank_after is None
    assert "unknown" in e.reason


def test_an_unaffordable_move_says_how_short_it_is(conn):
    e = decision.executability(conn, [20], [1], free_transfers=1, bank=0)
    assert e.affordable is False
    assert "short by" in e.reason


def test_free_transfers_roll_and_cap(conn):
    e = decision.executability(conn, [], [], free_transfers=1, bank=10)
    assert e.free_transfers_after == 2, "an unused transfer rolls"
    e5 = decision.executability(conn, [], [], free_transfers=5, bank=10)
    assert e5.free_transfers_after == config.MAX_FREE_TRANSFERS, "capped at five"


def test_paid_transfers_are_counted_beyond_the_free_ones(conn):
    e = decision.executability(conn, [16, 17, 18], [1, 2, 3],
                               free_transfers=1, bank=500)
    assert e.paid_transfers == 2
    assert e.free_transfers_after == 1, "all FTs spent, then one accrues"


# --------------------------------------------------------------------------
# The biggest reason this could be wrong
# --------------------------------------------------------------------------

def test_a_flagged_incoming_player_is_named_as_the_biggest_risk(conn):
    conn.execute("UPDATE players SET status='d', news='Knock - 50% chance' "
                 "WHERE id=16")
    conn.commit()
    risk = decision.biggest_risk(conn, [16], captain=1, horizon_driven=False)
    assert "P16" in risk and "flagged" in risk
    assert "Knock" in risk


def test_low_start_probability_is_surfaced_as_the_risk(conn):
    conn.execute("INSERT INTO projections (player_id, gw, p_start, exp_points) "
                 "VALUES (16, 1, 0.25, 3.0)")
    conn.commit()
    risk = decision.biggest_risk(conn, [16], captain=None, horizon_driven=False)
    assert "25%" in risk and "minutes" in risk


def test_a_horizon_driven_move_names_the_weak_horizon(conn):
    risk = decision.biggest_risk(conn, [], captain=None, horizon_driven=True)
    assert "2-6" in risk


def test_there_is_always_exactly_one_named_risk(conn):
    risk = decision.biggest_risk(conn, [], captain=None, horizon_driven=False)
    assert risk and risk.count(".") <= 2, "one sentence, not a caveat list"


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------

def test_a_decision_serialises_with_its_evidence():
    d = decision.Decision(
        action=decision.ACTION_ROLL, headline="Roll", reason="nothing beats it",
        comparison=cmp_for(5.1, 5.0),
        executability=decision.Executability(True, 5, 5, 0, 0, 1, 2, 0))
    out = d.as_dict()
    assert out["action"] == "roll"
    assert out["comparison"]["simulations"] == 8000
    assert out["executability"]["free_transfers_after"] == 2


def test_every_action_is_in_the_declared_vocabulary():
    for a in (decision.ACTION_TRANSFER, decision.ACTION_ROLL,
              decision.ACTION_TOO_CLOSE, decision.ACTION_UNAVAILABLE):
        assert a in decision.ALL_ACTIONS


# --------------------------------------------------------------------------
# The GW2 2026-27 regression: a big mean from a rare tail bought a -20 hit
# --------------------------------------------------------------------------

def test_a_horizon_mean_cannot_buy_a_hit_that_loses_this_gameweek():
    """The exact numbers Gaffer published on 2026-08-21, which were indefensible.

    It said "Make this transfer (-20)" at high confidence for a move worth -12.4
    points in the only week it projects well, ahead in 13% of 2000 scenarios,
    justified entirely by a horizon mean the same artifact calls "materially
    weaker" than its one-week numbers.
    """
    c = decision.Comparison(
        move_expected=40.99, hold_expected=53.4, delta=-12.41,
        delta_ci95=(-12.94, -11.89), p_move_beats_hold=0.133, n_sims=2000,
        short_term_delta=-12.41, horizon_delta=15.52, hit_cost=20)
    action, reason = decision.classify(c)
    assert action != decision.ACTION_TRANSFER, (
        "a move losing 12.4 points now, winning 13% of the time, must never be "
        "published as an action"
    )
    assert "13%" in reason


def test_the_waiver_still_needs_the_edge_to_be_present_not_promised():
    """A decisive HORIZON mean with a poor this-week delta is not decisive."""
    c = decision.Comparison(
        move_expected=50.0, hold_expected=50.0, delta=0.1,
        delta_ci95=(-0.4, 0.6), p_move_beats_hold=0.20, n_sims=4000,
        short_term_delta=0.1, horizon_delta=12.0, hit_cost=0)
    assert decision.classify(c)[0] != decision.ACTION_TRANSFER


def test_the_waiver_survives_for_the_case_it_was_written_for():
    """Wide distribution, large PRESENT gain, no hit — still an action."""
    c = decision.Comparison(
        move_expected=60.0, hold_expected=50.0, delta=10.0,
        delta_ci95=(-2.0, 22.0), p_move_beats_hold=0.52, n_sims=4000,
        short_term_delta=10.0, horizon_delta=10.0, hit_cost=0)
    assert decision.classify(c)[0] == decision.ACTION_TRANSFER


def test_nothing_is_waived_below_a_coin_flip():
    """Same large present edge, but it loses more often than it wins."""
    c = decision.Comparison(
        move_expected=60.0, hold_expected=50.0, delta=10.0,
        delta_ci95=(-2.0, 22.0), p_move_beats_hold=0.49, n_sims=4000,
        short_term_delta=10.0, horizon_delta=10.0, hit_cost=0)
    assert decision.classify(c)[0] != decision.ACTION_TRANSFER
