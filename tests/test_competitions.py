"""Fixtures Gaffer's own data cannot see.

The bugs these hold down are the ones actually hit while building the module,
and they share a shape: **every one of them makes a club look like it has no
European football**, which is precisely the error the module exists to remove.
A silent miss here is worse than a crash.
"""
from __future__ import annotations

import urllib.error
from datetime import date, datetime

import pytest

from gaffer import competitions as C

SAMPLE = """= UEFA Champions League 2025/26

# Date       Tue Sep 16 2025 - Sat May 30 2026 (256d)
# Teams      36

▪ League, Matchday 1
  Tue Sep 16 2025
    18:45  Athletic Club (ESP)     v Arsenal FC (ENG)         0-2 (0-0)
           PSV (NED)               v Royale Union Saint-Gilloise (BEL)  1-3 (0-2)
  Wed Sep 17
    21:00  Liverpool FC (ENG)      v Club Atletico de Madrid (ESP)  3-2 (2-1)
           FC Bayern Munchen (GER) v Chelsea FC (ENG)         3-1 (2-1)

▪ League, Matchday 2
  Tue Sep 30 2025
    21:00  Manchester City FC (ENG) v SSC Napoli (ITA)        2-0
"""


@pytest.fixture()
def fixtures():
    return C.parse_openfootball(SAMPLE, "UEFA Champions League", "european")


# --------------------------------------------------------------------------
# The bug that would have silently ruined every measurement
# --------------------------------------------------------------------------

def test_the_year_carries_forward_to_days_that_omit_it(fixtures):
    """openfootball prints the year on the first day of a matchday and omits it
    on every later day: "Tue Sep 16 2025" then "Wed Sep 17". Without carrying
    it, every fixture after the first day of a matchday lands on the wrong date
    -- and a congestion feature built on wrong dates is confidently wrong.
    """
    days = sorted({f.day for f in fixtures})
    assert days == [date(2025, 9, 16), date(2025, 9, 17), date(2025, 9, 30)]


def test_kickoff_times_are_read_where_published(fixtures):
    liv = next(f for f in fixtures if f.home == "Liverpool FC")
    assert liv.kickoff == datetime(2025, 9, 17, 21, 0)


def test_a_fixture_under_a_time_header_inherits_that_time_slot(fixtures):
    """The second line of a time block has no time of its own; it belongs to the
    block above and must not be dropped."""
    psv = next(f for f in fixtures if f.home == "PSV")
    assert psv.day == date(2025, 9, 16)


def test_scores_are_not_mistaken_for_club_names(fixtures):
    """'Arsenal FC (ENG)         0-2 (0-0)' -- the trailing score is separated
    by run-on whitespace and must not end up inside the away club."""
    ars = next(f for f in fixtures if f.away == "Arsenal FC")
    assert ars.away_country == "ENG"
    assert "0-2" not in ars.away


def test_headers_and_stage_markers_are_not_fixtures(fixtures):
    assert all("Matchday" not in f.home for f in fixtures)
    assert len(fixtures) == 5


# --------------------------------------------------------------------------
# The club join, which is where the second silent failure lived
# --------------------------------------------------------------------------

def test_english_clubs_map_to_the_names_the_archive_uses(fixtures):
    rows = C.english_fixtures(fixtures)
    teams = {r["team"] for r in rows}
    assert teams == {"Arsenal", "Liverpool", "Chelsea", "Man City"}


def test_openfootballs_inconsistent_spellings_both_resolve():
    """It writes "Manchester United FC" in one season's file and "Manchester
    United" in another. Both are the same club and a season where one form
    fails is a season where that club looks like it plays no European
    football."""
    for name in ("Manchester United FC", "Manchester United",
                 "Tottenham Hotspur FC", "Tottenham Hotspur",
                 "Brighton & Hove Albion FC", "Brighton & Hove Albion"):
        assert C.ENGLISH_CLUBS.get(C._canon(name)), name


def test_an_unknown_english_club_is_reported_not_dropped():
    """The loudness requirement. A club the map does not know must arrive with
    `unmapped_name` set, because a dropped row is indistinguishable from a club
    with no European fixtures."""
    txt = SAMPLE.replace("Arsenal FC (ENG)", "Notreal Town FC (ENG)")
    rows = C.english_fixtures(C.parse_openfootball(txt, "x", "european"))
    unmapped = [r for r in rows if r["unmapped_name"]]
    assert len(unmapped) == 1
    assert unmapped[0]["unmapped_name"] == "Notreal Town FC"
    assert unmapped[0]["team"] is None


def test_non_english_clubs_are_not_returned(fixtures):
    rows = C.english_fixtures(fixtures)
    assert all(r["team"] or r["unmapped_name"] for r in rows)
    assert not any("Napoli" in (r["unmapped_name"] or "") for r in rows)


# --------------------------------------------------------------------------
# The windows the calendar asks about
# --------------------------------------------------------------------------

def test_the_congestion_index_is_per_club_and_sorted(fixtures):
    idx = C.congestion_index(C.english_fixtures(fixtures))
    assert idx["Man City"] == [date(2025, 9, 30)]
    assert idx["Arsenal"] == [date(2025, 9, 16)]


def test_window_helpers_are_half_open_at_the_target():
    days = [date(2025, 9, 16), date(2025, 9, 30), date(2025, 10, 21)]
    # a match ON the target day is not "in the prior fortnight"
    assert C.extra_matches_between(days, date(2025, 9, 16), date(2025, 9, 30)) == 1
    assert C.rest_days_before(days, date(2025, 9, 30)) == 14
    assert C.next_match_after(days, date(2025, 9, 30)) == 21


def test_absent_history_is_none_rather_than_zero():
    """Nought days' rest and no known previous match are different claims."""
    assert C.rest_days_before([], date(2025, 9, 30)) is None
    assert C.next_match_after([date(2025, 1, 1)], date(2025, 9, 30)) is None


# --------------------------------------------------------------------------
# Honesty about the source
# --------------------------------------------------------------------------

def test_availability_states_the_lag_rather_than_implying_completeness():
    a = C.availability()
    assert "LAGS" in a["known_lag"]
    assert any("FA Cup" in x for x in a["does_not_cover"])
    assert any("result" in x or "projection" in x for x in a["does_not_cover"])


def test_a_missing_competition_is_reported_not_swallowed(monkeypatch):
    """`load_season` returns coverage ALWAYS. A caller that cannot see which
    competitions were missing cannot tell an uncongested week from an
    unobserved one.

    Offline by construction. An earlier version of this test hit a real 404 and
    passed for the wrong reason: `fetch` catches every transport failure, so it
    was swallowing the suite's own network guard and reporting that as the
    behaviour under test.
    """
    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(C.urllib.request, "urlopen", boom)

    with pytest.raises(C.SourceUnavailable) as e:
        C.fetch(C.COMPETITIONS[0], "1800-01")
    assert "404" in str(e.value)

    fixtures, cov = C.load_season("1800-01")
    assert fixtures == []
    assert cov["complete"] is False
    assert set(cov["missing"]) == {c.key for c in C.COMPETITIONS}
    assert cov["found"] == {}


def test_the_module_has_no_opinion_about_who_wins():
    """It is a calendar, not a second football model. Nothing here carries a
    score, a projection or a strength rating."""
    f = C.parse_openfootball(SAMPLE, "x", "european")[0]
    assert not hasattr(f, "score")
    assert not any("score" in k for k in f.__dataclass_fields__)


# --------------------------------------------------------------------------
# Caching, because the pipeline runs every fifteen minutes
# --------------------------------------------------------------------------

def test_a_fetched_file_is_reused_rather_than_refetched(tmp_path, monkeypatch):
    """A fixture list is one of the slowest-moving things in football and the
    pipeline runs on a fifteen-minute schedule. Re-fetching on every tick would
    put ~300 pointless requests a day on a volunteer-run public repository."""
    monkeypatch.setattr(C.config, "CACHE_DIR", tmp_path)
    calls = []

    class R:
        def read(self):
            return SAMPLE.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def once(*a, **k):
        calls.append(1)
        return R()

    monkeypatch.setattr(C.urllib.request, "urlopen", once)
    comp = C.COMPETITIONS[0]
    first = C.fetch(comp, "2025-26")
    second = C.fetch(comp, "2025-26")
    assert len(calls) == 1
    assert len(first) == len(second) == 5


def test_a_404_is_remembered_so_an_absent_season_is_not_re_asked(tmp_path, monkeypatch):
    """openfootball has no file for a season until someone writes one. Asking
    three times a minute for the next twelve weeks would be rude as well as
    pointless."""
    monkeypatch.setattr(C.config, "CACHE_DIR", tmp_path)
    calls = []

    def gone(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(C.urllib.request, "urlopen", gone)
    comp = C.COMPETITIONS[0]
    for _ in range(3):
        with pytest.raises(C.SourceUnavailable):
            C.fetch(comp, "2099-00")
    assert len(calls) == 1


def test_a_transport_failure_is_NOT_cached(tmp_path, monkeypatch):
    """A timeout is a fact about this minute. Caching it would turn one bad
    minute into a bad day."""
    monkeypatch.setattr(C.config, "CACHE_DIR", tmp_path)
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        raise TimeoutError("slow")

    monkeypatch.setattr(C.urllib.request, "urlopen", flaky)
    for _ in range(3):
        with pytest.raises(C.SourceUnavailable):
            C.fetch(C.COMPETITIONS[0], "2025-26")
    assert len(calls) == 3
