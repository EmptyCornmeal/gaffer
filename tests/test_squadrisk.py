"""Structural risk: a squad made of good picks that is badly built.

The distinctions these hold are the ones that make the feature worth having.
A bench-slot count that treats a backup keeper like a rotation risk, or a
concentration warning that fires on "you own two players in one match", would
be noise wearing a warning's clothes.
"""
from __future__ import annotations

import sqlite3

import pytest

from gaffer import squadrisk as SR


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE teams (id INTEGER PRIMARY KEY, short TEXT);"
        "CREATE TABLE players (id INTEGER PRIMARY KEY, web_name TEXT,"
        " position TEXT, team_id INTEGER, status TEXT, minutes INTEGER,"
        " price INTEGER);"
        "CREATE TABLE projections (player_id INTEGER, gw INTEGER, p_start REAL);"
        "CREATE TABLE player_gw (player_id INTEGER, gw INTEGER, minutes INTEGER);"
        "CREATE TABLE fixtures (id INTEGER PRIMARY KEY, gw INTEGER,"
        " team_h INTEGER, team_a INTEGER);"
        "INSERT INTO teams VALUES (1,'ARS'),(2,'CHE'),(3,'BHA'),(4,'LEE'),"
        "(5,'AVL'),(6,'FUL'),(7,'WOL'),(8,'NEW');"
        "INSERT INTO player_gw VALUES (1,1,90),(1,2,90);")
    players = [
        (1, "Raya", "GKP", 1, "a", 180, 60),          # first-choice keeper
        (2, "Backup", "GKP", 1, "a", 0, 40),          # behind an ever-present
        (3, "Calafiori", "DEF", 1, "a", 180, 56),
        (4, "Ghost", "MID", 2, "a", 0, 45),           # never plays
        (5, "Cheap", "DEF", 3, "a", 120, 40),         # cheap but plays
        (6, "Striker", "FWD", 4, "a", 180, 70),
        (7, "Crocked", "DEF", 2, "i", 400, 50),       # injured
        (8, "Fringe", "MID", 3, "a", 60, 45),         # marginal
    ]
    c.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?)", players)
    c.executemany("INSERT INTO projections VALUES (?,?,?)", [
        (1, 3, 0.95), (2, 3, 0.05), (3, 3, 0.90), (4, 3, 0.03),
        (5, 3, 0.70), (6, 3, 0.88), (7, 3, 0.00), (8, 3, 0.25)])
    c.executemany("INSERT INTO fixtures VALUES (?,?,?,?)",
                  [(1, 3, 1, 2), (2, 3, 3, 4), (3, 3, 5, 6), (4, 3, 7, 8)])
    c.commit()
    return c


# --------------------------------------------------------------------------
# Bench: the distinctions that make it useful
# --------------------------------------------------------------------------

def test_a_backup_keeper_is_dead_for_the_right_reason(conn):
    """Not a rotation risk. He is waiting for an injury, and until it happens
    his expected contribution is zero. Calling him 'thin' would flatter every
    squad in the game."""
    b = SR.bench_robustness(conn, [2], 3)
    p = b["players"][0]
    assert p["state"] == SR.BENCH_DEAD
    assert "backup goalkeeper" in p["why"]


def test_a_cheap_player_who_plays_is_not_dead(conn):
    """The distinction the whole feature turns on: cheap and playing is a fine
    bench slot; cheap and not playing is a hole."""
    b = SR.bench_robustness(conn, [5], 3)
    assert b["players"][0]["state"] == SR.BENCH_LIVE


def test_no_minutes_across_played_matches_is_dead(conn):
    b = SR.bench_robustness(conn, [4], 3)
    assert b["players"][0]["state"] == SR.BENCH_DEAD
    assert "no minutes" in b["players"][0]["why"]


def test_an_injury_is_dead_and_says_so(conn):
    b = SR.bench_robustness(conn, [7], 3)
    assert b["players"][0]["state"] == SR.BENCH_DEAD
    assert b["players"][0]["why"] == "injured"


def test_a_marginal_player_is_thin_not_dead(conn):
    """Three states, not two. 'Some route to minutes but not one you would
    choose to need' is a real category."""
    b = SR.bench_robustness(conn, [8], 3)
    assert b["players"][0]["state"] == SR.BENCH_THIN


def test_autosub_cover_counts_outfield_players_who_could_actually_come_on(conn):
    b = SR.bench_robustness(conn, [2, 4, 5, 8], 3)
    assert b["dead"] == 2                      # backup keeper + ghost
    assert b["autosub_cover"]["usable_outfield_substitutes"] == 2   # cheap, fringe
    assert b["bench_boost_ready"] is False


def test_bench_boost_readiness_is_all_or_nothing(conn):
    assert SR.bench_robustness(conn, [5, 8], 3)["bench_boost_ready"] is True
    assert SR.bench_robustness(conn, [5, 4], 3)["bench_boost_ready"] is False


def test_the_threshold_admits_it_is_a_choice(conn):
    b = SR.bench_robustness(conn, [5], 3)
    assert "POLICY CHOICE" in b["threshold_is_policy"]


def test_an_empty_bench_is_a_stated_absence(conn):
    assert SR.bench_robustness(conn, [], 3)["available"] is False


# --------------------------------------------------------------------------
# Concentration: describing the week, not banning a shape
# --------------------------------------------------------------------------

def test_it_groups_the_squad_by_fixture(conn):
    r = SR.fixture_concentration(conn, [1, 3, 4, 5, 6], 3)
    biggest = r["largest"]
    assert biggest["n"] == 3            # Raya, Calafiori (ARS) + Ghost (CHE)
    assert biggest["both_sides"] is True


def test_opposed_pairs_are_named_by_what_actually_cancels(conn):
    """A defender on one side and an attacker on the other cannot both have a
    good afternoon. That is the conflict worth naming -- not 'you own two
    players in one match'."""
    r = SR.fixture_concentration(conn, [5, 6], 3)   # BHA def v LEE forward
    pairs = r["groups"][0]["opposed_pairs"]
    assert len(pairs) == 1
    assert pairs[0]["clean_sheet_side"] == "Cheap"
    assert pairs[0]["attacking_side"] == "Striker"
    assert "scoring is a goal against" in pairs[0]["means"]


def test_two_attackers_in_one_fixture_are_not_opposed(conn):
    """They compound rather than cancel. Flagging them would be the dumb rule
    this metric was written to avoid."""
    conn.execute("UPDATE players SET position='FWD' WHERE id=5")
    r = SR.fixture_concentration(conn, [5, 6], 3)
    assert r["groups"][0]["opposed_pairs"] == []


def test_it_says_plainly_that_it_is_not_a_prohibition(conn):
    r = SR.fixture_concentration(conn, [1, 3], 3)
    assert "not 'do not own opposing players'" in r["not_a_rule"]


def test_the_top_two_rule_catches_what_one_fixture_misses(conn):
    """The live squad on 2026-09-02 is why this exists: no single fixture held
    more than 27%, so a single-fixture rule reported a clean week while eight
    of fifteen players sat in two matches."""
    squad = [1, 3, 4, 5, 6, 8]          # 3 in ARS v CHE, 3 in BHA v LEE
    r = SR.fixture_concentration(conn, squad, 3)
    assert r["largest"]["share_of_squad"] < SR.CONCENTRATED_SHARE + 0.21
    assert r["top_two_share"] == 1.0
    assert r["concentrated"] is True


def test_a_gameweek_with_no_fixtures_is_a_stated_absence(conn):
    r = SR.fixture_concentration(conn, [1, 3], 99)
    assert r["available"] is False
    assert "no fixtures" in r["reason"]


# --------------------------------------------------------------------------
# The horizon
# --------------------------------------------------------------------------

def test_warnings_carry_how_far_away_and_how_to_fix_them(conn):
    r = SR.horizon_warnings(conn, [1, 3, 4, 5, 6, 8], [2, 4], 3, horizon=1)
    assert r["available"] is True
    kinds = {w["kind"] for w in r["warnings"]}
    assert "dead_bench" in kinds
    for w in r["warnings"]:
        assert "fixable_by" in w
        assert isinstance(w["gameweeks_away"], int)


def test_the_horizon_is_the_reach_of_a_free_transfer(conn):
    """Three gameweeks, and it says why. A warning on deadline day is a
    complaint; the same warning three weeks out is a plan."""
    assert SR.HORIZON_GWS == 3
    r = SR.horizon_warnings(conn, [1, 3], [5], 3)
    assert "free transfer can still reach" in r["why_early"]


def test_nothing_here_claims_to_be_a_forecast(conn):
    r = SR.horizon_warnings(conn, [1, 3], [5], 3)
    assert "Nothing is forecast" in r["not_a_projection"]


def test_a_clean_squad_reports_clear():
    """A realistic squad spread across a full round, with a bench that plays.

    Built on its own database because the thresholds are calibrated for a
    fifteen-man squad: with seven players and four fixtures, two fixtures hold
    more than 45% of the squad by arithmetic rather than by concentration.

    A warning system that always warns is a warning system nobody reads, so
    this asserts the quiet case as hard as the loud ones.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE teams (id INTEGER PRIMARY KEY, short TEXT);"
        "CREATE TABLE players (id INTEGER PRIMARY KEY, web_name TEXT,"
        " position TEXT, team_id INTEGER, status TEXT, minutes INTEGER,"
        " price INTEGER);"
        "CREATE TABLE projections (player_id INTEGER, gw INTEGER, p_start REAL);"
        "CREATE TABLE player_gw (player_id INTEGER, gw INTEGER, minutes INTEGER);"
        "CREATE TABLE fixtures (id INTEGER PRIMARY KEY, gw INTEGER,"
        " team_h INTEGER, team_a INTEGER);"
        "INSERT INTO player_gw VALUES (1,1,90),(1,2,90);")
    c.executemany("INSERT INTO teams VALUES (?,?)",
                  [(t, f"T{t}") for t in range(1, 21)])
    c.executemany("INSERT INTO fixtures VALUES (?,?,?,?)",
                  [(f, 3, 2 * f - 1, 2 * f) for f in range(1, 11)])
    pos = ["GKP", "GKP", "DEF", "DEF", "DEF", "DEF", "DEF",
           "MID", "MID", "MID", "MID", "MID", "FWD", "FWD", "FWD"]
    c.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?)",
                  [(n, f"P{n}", pos[n - 1], n, "a", 180, 50)
                   for n in range(1, 16)])
    c.executemany("INSERT INTO projections VALUES (?,?,?)",
                  [(n, 3, 0.9) for n in range(1, 16)])
    c.commit()

    r = SR.horizon_warnings(c, list(range(1, 16)), [12, 13, 14, 15], 3, horizon=1)
    assert r["warnings"] == []
    assert r["clear"] is True
