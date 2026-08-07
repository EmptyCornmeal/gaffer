"""T-10 — retained player history and projection snapshots.

`player_gw` existed with 14 columns and zero rows; nothing ever wrote it, and
`projections` was wiped every run, so there was no record to score the model
against once results landed.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from gaffer import config, ingest
from gaffer.model import projection
from gaffer.store import db

SEASON_A = "2026-27"
SEASON_B = "2025-26"


def hist(fixture, rnd, points, minutes=90, **kw):
    d = {
        "fixture": fixture, "round": rnd, "total_points": points, "minutes": minutes,
        "goals_scored": 0, "assists": 0, "clean_sheets": 0, "goals_conceded": 0,
        "own_goals": 0, "penalties_saved": 0, "penalties_missed": 0,
        "yellow_cards": 0, "red_cards": 0, "saves": 0, "bonus": 0, "bps": 10,
        "starts": 1, "defensive_contribution": 5,
        "expected_goals": "0.10", "expected_assists": "0.05",
        "expected_goal_involvements": "0.15", "expected_goals_conceded": "1.10",
        "value": 50, "selected": 1000, "was_home": True, "opponent_team": 2,
        "kickoff_time": "2026-08-21T19:00:00Z",
    }
    d.update(kw)
    return d


class HistClient:
    def __init__(self, by_player):
        self.by_player = by_player
        self.calls = 0

    def element_summary(self, pid):
        self.calls += 1
        if pid not in self.by_player:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://x.test"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://x.test")),
            )
        return {"history": self.by_player[pid], "history_past": []}


def rows(conn, season=None):
    sql = "SELECT * FROM player_gw"
    args = ()
    if season:
        sql += " WHERE season=?"
        args = (season,)
    return conn.execute(sql + " ORDER BY season, player_id, fixture", args).fetchall()


# --------------------------------------------------------------------------
# player_gw
# --------------------------------------------------------------------------

def test_fresh_database_has_the_new_shape(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(player_gw)")}
    assert {"season", "fixture", "ingested_at", "xgi", "xgc", "starts"} <= cols
    pk = [r["name"] for r in conn.execute("PRAGMA table_info(player_gw)") if r["pk"]]
    assert set(pk) == {"season", "player_id", "fixture"}


def test_history_is_persisted_not_discarded(conn):
    c = HistClient({1: [hist(101, 1, 6), hist(102, 2, 2)]})
    n = ingest.ingest_player_history(conn, c, season=SEASON_A, player_ids=[1])
    assert n == 2
    got = rows(conn)
    assert len(got) == 2
    assert got[0]["total_points"] == 6
    assert got[0]["gw"] == 1 and got[0]["fixture"] == 101
    assert got[0]["season"] == SEASON_A
    assert got[0]["ingested_at"]


def test_double_gameweek_stores_both_fixtures(conn):
    """Keyed by fixture, not gameweek — a DGW is two rows in one round."""
    c = HistClient({1: [hist(201, 7, 5), hist(202, 7, 9)]})
    ingest.ingest_player_history(conn, c, season=SEASON_A, player_ids=[1])
    got = rows(conn)
    assert len(got) == 2
    assert {r["gw"] for r in got} == {7}
    assert {r["fixture"] for r in got} == {201, 202}


def test_repeated_ingestion_is_idempotent(conn):
    c = HistClient({1: [hist(101, 1, 6)]})
    for _ in range(3):
        ingest.ingest_player_history(conn, c, season=SEASON_A, player_ids=[1])
    assert len(rows(conn)) == 1


def test_upstream_correction_overwrites_rather_than_duplicating(conn):
    """FPL revises bonus/xG after review; the corrected value must win."""
    ingest.ingest_player_history(
        conn, HistClient({1: [hist(101, 1, 6, bonus=0)]}), season=SEASON_A, player_ids=[1])
    ingest.ingest_player_history(
        conn, HistClient({1: [hist(101, 1, 9, bonus=3)]}), season=SEASON_A, player_ids=[1])
    got = rows(conn)
    assert len(got) == 1
    assert got[0]["total_points"] == 9
    assert got[0]["bonus"] == 3


def test_seasons_are_separated(conn):
    h = [hist(101, 1, 6)]
    ingest.ingest_player_history(conn, HistClient({1: h}), season=SEASON_A, player_ids=[1])
    ingest.ingest_player_history(conn, HistClient({1: h}), season=SEASON_B, player_ids=[1])
    assert len(rows(conn)) == 2
    assert len(rows(conn, SEASON_A)) == 1
    assert len(rows(conn, SEASON_B)) == 1


def test_no_cross_season_contamination(conn):
    """Element ids are reused across seasons; player 1 is two different people."""
    ingest.ingest_player_history(
        conn, HistClient({1: [hist(101, 1, 6)]}), season=SEASON_B, player_ids=[1])
    ingest.ingest_player_history(
        conn, HistClient({1: [hist(101, 1, 2)]}), season=SEASON_A, player_ids=[1])
    a = rows(conn, SEASON_A)[0]
    b = rows(conn, SEASON_B)[0]
    assert a["total_points"] == 2 and b["total_points"] == 6


def test_one_players_failure_does_not_abort_the_run(conn):
    c = HistClient({1: [hist(101, 1, 6)], 3: [hist(301, 1, 4)]})
    n = ingest.ingest_player_history(conn, c, season=SEASON_A, player_ids=[1, 2, 3])
    assert n == 2  # player 2 404s and is skipped
    assert {r["player_id"] for r in rows(conn)} == {1, 3}


def test_rows_without_a_fixture_key_are_skipped_not_invented(conn):
    bad = hist(101, 1, 6)
    del bad["fixture"]
    c = HistClient({1: [bad, hist(102, 2, 3)]})
    ingest.ingest_player_history(conn, c, season=SEASON_A, player_ids=[1])
    assert [r["fixture"] for r in rows(conn)] == [102]


def test_empty_history_is_not_an_error(conn):
    c = HistClient({1: []})
    assert ingest.ingest_player_history(conn, c, season=SEASON_A, player_ids=[1]) == 0


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def test_migration_rebuilds_the_legacy_player_gw(tmp_path):
    """An existing local DB has the season-less table; CREATE IF NOT EXISTS
    would silently leave it in place forever."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE player_gw (player_id INTEGER, gw INTEGER, minutes INTEGER,"
        " total_points INTEGER, PRIMARY KEY (player_id, gw))"
    )
    raw.commit()
    raw.close()

    c = db.connect(path)
    applied = db.migrate(c)
    assert any("player_gw" in a for a in applied)
    db.init_schema(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(player_gw)")}
    assert "season" in cols and "fixture" in cols
    c.close()


def test_migration_is_idempotent(conn):
    assert db.migrate(conn) == []  # already current
    assert db.migrate(conn) == []


def test_migration_preserves_unexpected_legacy_rows(tmp_path):
    """If a legacy table somehow has rows, rename rather than drop."""
    path = tmp_path / "legacy2.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE player_gw (player_id INTEGER, gw INTEGER)")
    raw.execute("INSERT INTO player_gw VALUES (1, 1)")
    raw.commit()
    raw.close()

    c = db.connect(path)
    applied = db.migrate(c)
    assert any("legacy" in a for a in applied)
    n = c.execute("SELECT COUNT(*) AS n FROM player_gw_legacy").fetchone()["n"]
    assert n == 1
    c.close()


# --------------------------------------------------------------------------
# Projection snapshots
# --------------------------------------------------------------------------

def _snap_rows(conn):
    return conn.execute(
        "SELECT * FROM projection_snapshots ORDER BY as_of, target_gw, player_id"
    ).fetchall()


def test_projection_writes_a_snapshot_before_wiping(conn):
    n = projection.project(conn, 1, 2)
    assert n > 0
    snaps = _snap_rows(conn)
    assert snaps, "projections were wiped without a snapshot"
    assert {s["target_gw"] for s in snaps} == {1, 2}
    assert all(s["model_version"] == projection.MODEL_VERSION for s in snaps)
    assert all(s["season"] == config.SEASON for s in snaps)


def test_snapshot_records_horizon_and_availability(conn):
    projection.project(conn, 1, 3)
    by_gw = {s["target_gw"]: s for s in _snap_rows(conn)}
    assert by_gw[1]["horizon"] == 0
    assert by_gw[3]["horizon"] == 2
    assert by_gw[1]["availability"] is not None


def test_multiple_snapshots_accumulate(conn):
    projection.project(conn, 1, 1)
    first = {s["as_of"] for s in _snap_rows(conn)}
    # A later run with a different timestamp must not overwrite the earlier one.
    rows_ = [{"player_id": 1, "gw": 1, "exp_points": 9.9, "p_start": 0.9,
              "confidence": 0.5, "exp_minutes": 80}]
    projection.snapshot_projections(
        conn, rows_, from_gw=1, generated_at="2026-08-20T10:00:00+00:00")
    stamps = {s["as_of"] for s in _snap_rows(conn)}
    assert len(stamps) == len(first) + 1


def test_pre_deadline_selection_ignores_post_deadline_snapshots(conn):
    deadline = "2026-08-21T17:30:00+00:00"
    base = {"player_id": 1, "p_start": 0.9, "confidence": 0.5, "exp_minutes": 80}
    # Two legitimate pre-deadline snapshots, then one taken after kickoff.
    projection.snapshot_projections(
        conn, [{**base, "gw": 1, "exp_points": 4.0}], from_gw=1,
        generated_at="2026-08-20T10:00:00+00:00", deadlines={1: deadline})
    projection.snapshot_projections(
        conn, [{**base, "gw": 1, "exp_points": 5.0}], from_gw=1,
        generated_at="2026-08-21T09:00:00+00:00", deadlines={1: deadline})
    projection.snapshot_projections(
        conn, [{**base, "gw": 1, "exp_points": 99.0}], from_gw=1,
        generated_at="2026-08-21T20:00:00+00:00", deadlines={1: deadline})

    chosen = projection.latest_pre_deadline_snapshot(conn, 1)
    # Deterministic rule: the LATEST snapshot that could still have informed the
    # decision — never the one that has seen the team sheet.
    assert chosen[1]["exp_points"] == 5.0
    assert chosen[1]["is_pre_deadline"] == 1

    flags = {s["as_of"]: s["is_pre_deadline"] for s in _snap_rows(conn)}
    assert flags["2026-08-21T20:00:00+00:00"] == 0


def test_post_deadline_snapshot_never_overwrites_a_pre_deadline_one(conn):
    deadline = "2026-08-21T17:30:00+00:00"
    base = {"player_id": 1, "p_start": 0.9, "confidence": 0.5, "exp_minutes": 80}
    projection.snapshot_projections(
        conn, [{**base, "gw": 1, "exp_points": 4.0}], from_gw=1,
        generated_at="2026-08-20T10:00:00+00:00", deadlines={1: deadline})
    projection.snapshot_projections(
        conn, [{**base, "gw": 1, "exp_points": 99.0}], from_gw=1,
        generated_at="2026-08-22T10:00:00+00:00", deadlines={1: deadline})
    pre = projection.latest_pre_deadline_snapshot(conn, 1)
    assert pre[1]["exp_points"] == 4.0


def test_snapshots_are_season_scoped(conn):
    base = {"player_id": 1, "gw": 1, "exp_points": 4.0, "p_start": 0.9,
            "confidence": 0.5, "exp_minutes": 80}
    projection.snapshot_projections(
        conn, [base], from_gw=1, generated_at="2026-08-20T10:00:00+00:00")
    projection.snapshot_projections(
        conn, [{**base, "exp_points": 7.0}], from_gw=1,
        generated_at="2026-08-20T10:00:00+00:00", season=SEASON_B)
    assert len(_snap_rows(conn)) == 2
    assert projection.latest_pre_deadline_snapshot(conn, 1, SEASON_B)[1]["exp_points"] == 7.0


def test_snapshot_of_nothing_is_a_noop(conn):
    assert projection.snapshot_projections(
        conn, [], from_gw=1, generated_at="2026-08-20T10:00:00+00:00") == 0


def test_snapshot_rolls_back_on_partial_failure(conn):
    """A bad row must not leave half a snapshot behind."""
    good = {"player_id": 1, "gw": 1, "exp_points": 4.0, "p_start": 0.9,
            "confidence": 0.5, "exp_minutes": 80}
    bad = {**good, "player_id": None}  # NOT NULL on the primary key
    before = len(_snap_rows(conn))
    with pytest.raises(sqlite3.IntegrityError):
        projection.snapshot_projections(
            conn, [good, bad], from_gw=1, generated_at="2026-08-20T10:00:00+00:00")
    assert len(_snap_rows(conn)) == before
