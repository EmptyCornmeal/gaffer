"""Projection-model behaviour tests."""

import pytest

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection

# --------------------------------------------------------------------------
# M2/M3 — a zero in the prior-season baseline is not always a measurement
#
# FPL back-fills `history_past` for seasons that predate a statistic with 0
# rather than omitting the key, so `base_starts == 0` and `base_xg90 == 0.0` each
# mean one of two opposite things. Verified against the live API on 2026-08-15:
# B.Fernandes' 2021/22 row reports 3110 minutes, 0 starts and 0.00 xG.
# --------------------------------------------------------------------------

def _ctx():
    """Two identical average teams, so every fixture effect is neutral and the
    only thing these tests can move is the baseline branch."""
    ids = (1, 2)
    return F.TeamContext.from_ratings(
        att_home={i: 1100.0 for i in ids}, att_away={i: 1050.0 for i in ids},
        def_home={i: 1100.0 for i in ids}, def_away={i: 1050.0 for i in ids},
        team_xgc={i: 1.3 for i in ids})


def _player(**over):
    base = {
        "position": "MID", "price": 90, "team_id": 1,
        "minutes": 0, "starts": None,
        "base_minutes": 0, "base_starts": 0, "base_xg90": 0.0, "base_xa90": 0.0,
        "base_season": "", "xg_per_90": 0.0, "xa_per_90": 0.0, "defcon_per_90": 0.0,
    }
    base.update(over)
    return base


def _rates(fixtures_played=0, **over):
    return projection.fixture_rates(
        _player(**over), F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3),
        _ctx(), avail=1.0, fixtures_played=fixtures_played)


@pytest.mark.parametrize("label,expected", [
    ("2021/22", False), ("2022/23", True), ("2025/26", True),
    ("", None), (None, None), ("garbage", None),
])
def test_which_seasons_could_report_starts_and_xg(label, expected):
    assert config.season_reports_advanced_stats(label) is expected


def test_a_zero_from_a_season_that_could_measure_it_is_believed():
    """326 minutes and no starts in a season that reported starts is a
    substitute, and the number to use is 0 — not a prior invented from price."""
    assert _rates(base_minutes=326, base_starts=0,
                  base_season="2025/26")["p_start"] == 0.0


def test_a_zero_from_a_season_that_could_not_measure_it_is_not():
    """The same numbers from 2021/22 say nothing at all: FPL had no `starts`
    column then. Falling back to the price prior is the honest answer."""
    p = _rates(base_minutes=326, base_starts=0, base_season="2021/22")["p_start"]
    assert p == pytest.approx(projection._start_prior("MID", 90), abs=1e-9)


def test_minutes_that_cannot_come_from_the_bench_override_the_zero():
    """The guard that works without provenance. 3110 minutes with no starts is
    not a career substitute, whatever the record says — a season is 38 games and
    a cameo is not 80 minutes long. This is what protects an existing database
    between the schema migration and the next enrichment run."""
    p = _rates(base_minutes=3110, base_starts=0, base_season="")["p_start"]
    assert p == pytest.approx(projection._start_prior("MID", 90), abs=1e-9)


def test_a_real_starter_is_unaffected():
    r = _rates(base_minutes=3017, base_starts=35, base_season="2024/25")
    assert r["p_start"] == pytest.approx(35 / 38.0, abs=1e-9)


def test_a_measured_zero_xg_outranks_the_positional_prior():
    """A midfielder with a full modern season and no goal threat has told us his
    rate. Substituting a positional average there discards the only evidence
    there is."""
    r = _rates(base_minutes=2000, base_starts=25, base_season="2024/25",
               base_xg90=0.0, base_xa90=0.0)
    assert r["exp_goals"] == 0.0
    assert r["exp_assists"] == 0.0


def test_an_unmeasurable_zero_xg_falls_back_to_the_prior():
    """The same zeros from 2021/22 are a missing column. Believing them would
    project a player who never threatens."""
    r = _rates(base_minutes=3110, base_starts=0, base_season="2021/22",
               base_xg90=0.0, base_xa90=0.0)
    assert r["exp_goals"] > 0.0
    assert r["exp_assists"] > 0.0


def test_no_prior_sample_at_all_still_uses_the_prior():
    """Absence and zero must not converge: a player with no recorded season is
    not a player measured at zero."""
    r = _rates(base_minutes=0, base_season="")
    assert r["exp_goals"] > 0.0
    assert r["p_start"] == pytest.approx(projection._start_prior("MID", 90), abs=1e-9)


# --------------------------------------------------------------------------
# M3b — `starts` is a fixture count, so its denominator must be one too
#
# The old code divided by `last_finished_gw`, an EVENT count. The two agree only
# while every team plays exactly once per gameweek. Across 2024-25 they differed
# for at least one team in 17 of 37 decision gameweeks, covering 3.9% of
# evaluated rows — small in aggregate, wrong in kind.
# --------------------------------------------------------------------------

def test_the_start_rate_divides_fixtures_by_fixtures():
    """A double gameweek breaks an event-count denominator. Four starts from five
    fixtures is 0.80; against the four gameweeks those fixtures fell in it reads
    1.00, and the model calls a rotated player nailed."""
    assert _rates(minutes=400, starts=4, fixtures_played=5)["p_start"] == \
        pytest.approx(4 / 5, abs=1e-9)


def test_a_blank_does_not_punish_a_player_for_a_match_never_played():
    """The mirror case. The team played three of a possible four and the player
    started all three, so he is an ever-present — not a 75% starter. (0.98 is the
    model's ceiling on any start probability, applied here as everywhere.)"""
    assert _rates(minutes=270, starts=3, fixtures_played=3)["p_start"] == \
        pytest.approx(0.98, abs=1e-9)


def test_the_current_season_branch_still_waits_for_three_fixtures():
    """Below three the sample is too thin and last season keeps doing the work.
    The threshold is now correctly three FIXTURES — a team with an early double
    reaches it sooner than the gameweek number suggests, which is the point."""
    prior = _rates(minutes=180, starts=2, fixtures_played=2,
                   base_minutes=2000, base_starts=19, base_season="2024/25")
    assert prior["p_start"] == pytest.approx(19 / 38.0, abs=1e-9)


def test_played_fixtures_counts_both_sides_of_every_finished_match(conn):
    from gaffer.model import features as FF

    conn.execute("UPDATE fixtures SET finished=1 WHERE id IN (1,2)")
    conn.commit()
    played = FF.played_fixtures_by_team(conn)
    # Fixtures 1 and 2 are (1 v 2) and (3 v 4); fixture 3 (5 v 6) is unfinished.
    assert played == {1: 1, 2: 1, 3: 1, 4: 1}, played
    assert 5 not in played and 6 not in played, "an unfinished match counts for no one"


# --------------------------------------------------------------------------
# M2 — the baseline says WHICH season it came from
# --------------------------------------------------------------------------

def test_the_schema_carries_the_baseline_season(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(players)")}
    assert "base_season" in cols, (
        "without provenance the export stamps the current prior season on every "
        "baseline, and a four-year-old cameo renders as last season")


def test_enrich_history_records_the_season_it_read(conn, monkeypatch):
    """`history_past[-1]` is the most recent season FPL HAS for a player, not
    last season. For anyone who has been abroad it can be years old, and the
    number is only interpretable alongside the year it came from."""
    from gaffer import ingest

    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE players SET price=100, base_minutes=0 WHERE id=?", (pid,))
    conn.execute("UPDATE players SET price=30, selected_by_pct=0 WHERE id<>?", (pid,))
    conn.commit()

    class Client:
        def element_summary(self, _pid):
            return {"history_past": [
                {"season_name": "2019/20", "minutes": 500, "starts": 4,
                 "expected_goals": "0.00", "expected_assists": "0.00"},
                {"season_name": "2021/22", "minutes": 326, "starts": 0,
                 "expected_goals": "0.00", "expected_assists": "0.00"},
            ]}

    assert ingest.enrich_history(conn, Client()) == 1
    row = conn.execute(
        "SELECT base_season, base_minutes, base_starts FROM players WHERE id=?",
        (pid,)).fetchone()
    assert row["base_season"] == "2021/22"
    assert row["base_minutes"] == 326


@pytest.mark.parametrize("recorded,season,is_prior", [
    ("2025/26", "2025/26", True),      # the season just gone
    ("2021/22", "2021/22", False),     # real numbers, four years stale
    ("", None, None),                  # never recorded — no claim either way
])
def test_the_player_card_reports_the_season_it_actually_has(
        conn, recorded, season, is_prior, monkeypatch):
    """The defect this replaces: `season` was computed from the calendar and
    stamped on every player alike, so an old cameo rendered as "Last season"."""
    from gaffer.export import artifacts

    monkeypatch.setattr(artifacts, "_PRIOR_SEASON", "2025/26")
    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute(
        "UPDATE players SET base_minutes=326, base_starts=0, base_xg90=0, "
        "base_xa90=0, base_season=? WHERE id=?", (recorded, pid))
    conn.commit()
    row = conn.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()

    card = artifacts._last_season(row)
    assert card["season"] == season
    assert card["is_prior_season"] is is_prior
    assert card["minutes"] == 326


def test_too_short_a_sample_is_still_no_card_at_all(conn):
    from gaffer.export import artifacts

    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE players SET base_minutes=? WHERE id=?",
                 (config.BASE_SAMPLE_MINUTES - 1, pid))
    conn.commit()
    row = conn.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()
    assert artifacts._last_season(row) is None


def test_a_projection_survives_a_row_without_the_provenance_column():
    """`base_season` was added after existing databases were created, and
    sqlite3.Row raises IndexError for a column it does not have. A migration that
    has not run yet must not take the projection down."""
    player = _player(base_minutes=2000, base_starts=25)
    del player["base_season"]
    out = projection.fixture_rates(
        player, F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3),
        _ctx(), avail=1.0, fixtures_played=0)
    assert out["p_start"] > 0


def test_project_writes_rows(conn):
    n = projection.project(conn, from_gw=1, horizon=1)
    assert n == conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]


def test_unavailable_player_scores_near_zero(conn):
    # mark one player injured
    conn.execute("UPDATE players SET status='i' WHERE id=7")
    conn.commit()
    projection.project(conn, from_gw=1, horizon=1)
    row = conn.execute(
        "SELECT exp_points, p_start FROM projections WHERE player_id=7 AND gw=1"
    ).fetchone()
    assert row["p_start"] == 0.0
    assert row["exp_points"] < 0.2


def test_defcon_contributes_for_ballwinner(conn):
    projection.project(conn, from_gw=1, horizon=1)
    # DEF players in the fixture have defcon_per_90=11 (>10 threshold) -> a DEFCON term
    row = conn.execute(
        "SELECT exp_defcon_pts FROM projections pr JOIN players pl ON pl.id=pr.player_id "
        "WHERE pl.position='DEF' AND pl.defcon_per_90>10 AND pr.gw=1 LIMIT 1"
    ).fetchone()
    assert row["exp_defcon_pts"] > 0.5


def test_clean_sheet_only_for_defensive_positions(conn):
    projection.project(conn, from_gw=1, horizon=1)
    fwd = conn.execute(
        "SELECT exp_cs_pts FROM projections pr JOIN players pl ON pl.id=pr.player_id "
        "WHERE pl.position='FWD' AND pr.gw=1 LIMIT 1"
    ).fetchone()
    assert fwd["exp_cs_pts"] == 0.0
