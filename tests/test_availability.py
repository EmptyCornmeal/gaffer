"""Availability, and how old the claim is.

The age is the whole contribution. Gaffer has always been able to read "groin
injury - unknown return date"; it has never been able to say that the line was
written six weeks and two matches ago, because `news_added` was published by
FPL and never stored.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from gaffer import availability as A

NOW = datetime.now(UTC)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE players (id INTEGER PRIMARY KEY, web_name TEXT,"
        " team_id INTEGER, status TEXT, chance_playing INTEGER, news TEXT,"
        " news_added TEXT);"
        "CREATE TABLE fixtures (id INTEGER PRIMARY KEY, gw INTEGER,"
        " team_h INTEGER, team_a INTEGER, kickoff TEXT, finished INTEGER);")
    old = (NOW - timedelta(days=40)).isoformat()
    recent = (NOW - timedelta(hours=6)).isoformat()
    c.executemany("INSERT INTO players VALUES (?,?,?,?,?,?,?)", [
        (1, "Stale", 1, "i", 0, "Groin injury - Unknown return date", old),
        (2, "Fresh", 1, "d", 75, "Knock - 75% chance", recent),
        (3, "Fit", 1, "a", None, "", None),
        (4, "Gone", 2, "u", 0, "Has joined FC Barcelona permanently", recent),
    ])
    # one finished fixture, played AFTER the stale news and BEFORE the fresh
    c.executemany("INSERT INTO fixtures VALUES (?,?,?,?,?,?)", [
        (1, 1, 1, 2, (NOW - timedelta(days=20)).isoformat(), 1),
        (2, 2, 2, 1, (NOW - timedelta(days=13)).isoformat(), 1),
    ])
    c.commit()
    return c


def test_a_claim_that_survived_a_match_is_called_stale(conn):
    """The strongest available signal that a line is out of date: the news is
    older evidence than the match is."""
    r = A.squad_availability(conn, [1])
    p = r["flagged"][0]
    assert p["outdated_by_a_match"] is True
    assert p["matches_since_news"] == 2
    assert p["news_age_days"] > 39


def test_this_mornings_news_is_not_stale(conn):
    r = A.squad_availability(conn, [2])
    p = r["flagged"][0]
    assert p["outdated_by_a_match"] is False
    assert p["matches_since_news"] == 0


def test_an_unflagged_player_is_not_reported(conn):
    r = A.squad_availability(conn, [1, 3])
    assert [p["name"] for p in r["flagged"]] == ["Stale"]
    assert r["clear"] == 1
    assert r["of"] == 2


def test_the_status_codes_are_translated(conn):
    r = A.squad_availability(conn, [1, 2, 4])
    labels = {p["name"]: p["status_label"] for p in r["flagged"]}
    assert labels == {"Stale": "injured", "Fresh": "doubtful",
                      "Gone": "unavailable"}


def test_missing_news_added_is_absent_not_zero(conn):
    """A claim with no timestamp is undated, not brand new."""
    conn.execute("UPDATE players SET news='Something', news_added=NULL WHERE id=3")
    conn.commit()
    r = A.squad_availability(conn, [3])
    p = r["flagged"][0]
    assert p["news_added"] is None
    assert p["news_age_days"] is None
    assert p["outdated_by_a_match"] is False


def test_the_reading_names_the_stale_ones(conn):
    r = A.squad_availability(conn, [1, 2, 3])
    assert "Stale" in r["reading"]
    assert "older evidence than the match" in r["reading"]


def test_an_empty_squad_is_a_stated_absence(conn):
    assert A.squad_availability(conn, [])["available"] is False


# --------------------------------------------------------------------------
# The interface for a source Gaffer does not have
# --------------------------------------------------------------------------

def test_the_absent_provider_reports_an_absence_not_an_empty_lineup(conn):
    """An empty dict from a real provider would mean 'nobody is expected to
    start', which is never true. A caller that cannot tell the two apart will
    render one as the other."""
    s = A.NoLineupProvider().status()
    assert s["available"] is False
    assert s["reason"]
    assert A.NoLineupProvider().expected_starters("Arsenal", 3) == {}
    assert A.NoLineupProvider().as_of() is None


def test_the_constraint_is_documented_rather_than_hacked_around(conn):
    """Every route was checked and each is recorded with why it was rejected,
    so the next person does not repeat the search."""
    inv = A.NoLineupProvider().status()["investigated"]
    assert "159" in inv["sportmonks"]
    assert "scraping" in inv["fantasy_football_scout"]
    assert "deprecated" in inv["apify"]


def test_the_plug_in_instructions_are_concrete(conn):
    s = A.NoLineupProvider().status()
    assert "expected_starters" in s["how_to_plug_one_in"]
    assert "as_of" in s["how_to_plug_one_in"]


def test_it_says_plainly_that_it_cannot_see_rotation(conn):
    """The most important limitation. A fit, unflagged player who will be
    rested looks identical here to one who will start, and a reader who does
    not know that will trust this feed for something it cannot do."""
    r = A.squad_availability(conn, [1])
    assert "does NOT report rotation" in r["limitation"]
