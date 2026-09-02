"""5.1 / 5.3 -- the information calendar, and what it refuses to claim.

The scope IS the feature. A calendar that quietly implied it knew about team
news would be worse than no calendar: a reader seeing "nothing pending" would
take it to mean "nothing is coming", which this cannot know.

These tests hold the refusals as hard as the content, because the refusals are
the part that erodes first.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from gaffer import calendar

NOW = datetime(2026, 9, 2, 18, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(hours=48)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE teams (id INTEGER PRIMARY KEY, short TEXT);"
        "CREATE TABLE players (id INTEGER PRIMARY KEY, web_name TEXT,"
        " team_id INTEGER, price INTEGER, price_change_percent REAL,"
        " price_change_locked_until TEXT, price_change_projections TEXT);"
        "INSERT INTO teams VALUES (1,'ARS'),(2,'BOU');")
    rows = [
        # due to rise, in the move
        (10, "Semenyo", 2, 75, 104.0, None,
         json.dumps([{"offset": 2, "projected_percent": "160", "likelihood": 5}])),
        # due to fall, owned
        (11, "Osula", 2, 50, -109.6, None, None),
        # near a rise, owned
        (12, "Calafiori", 1, 60, 85.4, None, None),
        # near a fall, owned
        (13, "Mbeumo", 2, 80, -79.3, None, None),
        # locked
        (14, "Raya", 1, 55, 96.0, "2026-09-03T09:00:00Z", None),
        # quiet
        (15, "Gabriel", 1, 60, 4.0, None, None),
    ]
    c.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?)", rows)
    return c


def _cal(conn, squad, move):
    return calendar.build(conn, now=NOW, deadline=DEADLINE, window="pre_deadline",
                          squad_ids=squad, move_ids=move)


def _kinds(cal):
    return [(e["kind"], (e.get("player") or {}).get("name")) for e in cal["events"]]


# --------------------------------------------------------------------------
# What it knows
# --------------------------------------------------------------------------

def test_the_deadline_is_the_first_thing_and_carries_the_time_left(conn):
    cal = _cal(conn, [15], [])
    first = cal["events"][0]
    assert first["kind"] == "deadline"
    assert first["in_hours"] == 48.0
    assert first["passed"] is False


def test_a_due_change_on_a_player_being_bought_is_an_event(conn):
    cal = _cal(conn, [15], [10])
    assert ("price_change_due", "Semenyo") in _kinds(cal)


def test_a_near_change_is_reported_and_marked_as_NOT_due(conn):
    """The distinction that keeps it honest. Calafiori at 85.4% is worth
    knowing about and is not a prediction that he will rise."""
    cal = _cal(conn, [12], [])
    ev = next(e for e in cal["events"] if e["kind"] == "price_change_near")
    assert ev["player"]["name"] == "Calafiori"
    assert "NOT due" in ev["certainty"]
    assert "presentation choice" in ev["threshold_is_policy"]


def test_a_locked_player_is_an_event_because_waiting_is_free(conn):
    cal = _cal(conn, [14], [14])
    ev = next(e for e in cal["events"] if e["kind"] == "price_locked")
    assert ev["player"]["name"] == "Raya"
    assert "cannot move" in ev["changes"]


def test_a_locked_player_is_not_also_reported_as_nearly_moving(conn):
    """Raya is at 96% and locked. Both facts are true; only one of them is
    actionable, and showing "nearly rising" beside "cannot rise" would read as
    a contradiction."""
    cal = _cal(conn, [14], [])
    assert not [e for e in cal["events"] if e["kind"] == "price_change_near"]


def test_a_quiet_player_generates_nothing(conn):
    cal = _cal(conn, [15], [])
    assert _kinds(cal) == [("deadline", None)]


def test_it_is_scoped_to_the_reader_not_to_the_database(conn):
    """A calendar of all 651 players' price movements is a database dump. What
    makes an event an event is that it could change THIS answer."""
    cal = _cal(conn, [15], [])
    assert "not the whole player list" in cal["scope"]
    assert len(cal["events"]) == 1


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------

def test_no_due_change_carries_a_time(conn):
    """The most useful-looking lie available. FPL says a change is due; it does
    not say when, the price system is rolling rather than nightly, and the
    hourly rate does not reconcile with the projections in any units."""
    cal = _cal(conn, [11], [])
    ev = next(e for e in cal["events"] if e["kind"] == "price_change_due")
    assert ev["at"] is None
    assert "does not say when" in ev["certainty"]


def test_the_things_it_cannot_see_are_published_with_it(conn):
    cal = _cal(conn, [15], [])
    subjects = " ".join(x["what"] for x in cal["does_not_cover"])
    for missing in ("team news", "European", "predicted lineups", "WHEN"):
        assert missing in subjects
    assert "not football awareness" in cal["honesty"]


def test_an_empty_calendar_does_not_claim_the_coast_is_clear(conn):
    cal = _cal(conn, [15], [])
    assert "does not mean no team news is coming" in cal["honesty"]


# --------------------------------------------------------------------------
# 5.3 -- wait versus act
# --------------------------------------------------------------------------

def _decision(action="transfer", delta=1.45):
    return {"action": action,
            "comparison": {"delta": delta,
                           "delta_ci95_interval_type": "monte_carlo"}}


def test_it_says_it_is_not_an_evpi(conn):
    """Named explicitly. It does not price the information and does not compute
    an optimal time to act; calling it EVPI would borrow an authority it has
    not earned."""
    w = calendar.wait_vs_act(_decision(), _cal(conn, [15], []))
    assert w["kind"] == "comparison"
    assert "does not price the information" in w["not_an_evpi"]


def test_a_buy_that_is_due_to_rise_says_act_and_says_what_it_costs(conn):
    w = calendar.wait_vs_act(_decision(), _cal(conn, [15], [10]))
    assert w["verdict"] == "act"
    assert w["money_cost_of_waiting_m"] == 0.1
    assert any("due to rise" in d for d in w["detail"])


def test_a_buy_that_is_due_to_fall_says_waiting_is_cheaper(conn):
    w = calendar.wait_vs_act(_decision(), _cal(conn, [15], [11]))
    assert w["verdict"] == "waiting is cheaper"
    assert w["money_cost_of_waiting_m"] == -0.1


def test_a_near_change_is_a_risk_of_waiting_not_a_cost_of_it(conn):
    """Deliberately weaker language than the `act` branch. Nothing here is due;
    something here is close, and saying "act" on a maybe would borrow the
    certainty of the branch that measured one."""
    # Mbeumo: owned and nearly falling, so his selling price is what is at
    # risk. Calafiori is owned and nearly RISING, which is good news -- the
    # rule has to tell those two apart, and this asserts that it does.
    w = calendar.wait_vs_act(_decision(), _cal(conn, [12, 13], []))
    assert w["verdict"] == "money may be at stake"
    assert "risk of waiting, not a cost" in w["reason"]
    assert w["near_at_risk_if_you_wait"] == ["Mbeumo"]


def test_owning_a_player_who_is_about_to_rise_is_not_a_risk(conn):
    """Direction and ownership both matter. A player you already own gaining
    value costs you nothing by waiting, and flagging it would turn good news
    into a warning."""
    w = calendar.wait_vs_act(_decision(), _cal(conn, [12], []))
    assert w["near_at_risk_if_you_wait"] == []
    assert w["verdict"] == "no money either way"


def test_money_and_points_are_never_combined(conn):
    """0.1m of team value has no fixed worth in expected points. Inventing a
    rate to produce one number would be a made-up precision."""
    w = calendar.wait_vs_act(_decision(), _cal(conn, [15], [10]))
    assert w["edge_points"] == 1.45
    assert "money_cost_of_waiting_m" in w
    assert "no fixed worth" in w["no_exchange_rate"]


def test_the_missing_football_term_is_named_every_time(conn):
    for squad, move in ([15], []), ([15], [10]), ([12], []):
        w = calendar.wait_vs_act(_decision(), _cal(conn, squad, move))
        assert "NOT in this comparison" in w["missing_term"]
        assert "larger term" in w["missing_term"]


def test_a_roll_has_nothing_to_wait_on(conn):
    w = calendar.wait_vs_act(_decision(action="roll"), _cal(conn, [15], [10]))
    assert w["verdict"] == "nothing to wait on"
