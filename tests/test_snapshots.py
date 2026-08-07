"""T-21 — immutable pre-deadline decision snapshots.

The whole point of this table is that it is the one thing in Gaffer a later run
cannot improve. Without immutability, T-23's "was this a good decision?" question
degenerates into scoring the model against its own hindsight: a Sunday refresh
would quietly rewrite what Friday's advice had been, and the review would always
look clever.

These tests pin the boundary exactly — before, at, and after the deadline — plus
idempotency, season-awareness, and the schema migration onto an existing database.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from gaffer import config, snapshots
from gaffer.store import db

ENTRY = 1066421
DEADLINE = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)
DEADLINE_ISO = DEADLINE.isoformat().replace("+00:00", "Z")

BEFORE = DEADLINE - timedelta(hours=2)
JUST_BEFORE = DEADLINE - timedelta(seconds=1)
EXACTLY = DEADLINE
JUST_AFTER = DEADLINE + timedelta(seconds=1)
AFTER = DEADLINE + timedelta(hours=3)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.db")
    db.init_schema(c)
    yield c
    c.close()


def payload(action="roll", **kw):
    return {"decision": {"action": action, "captain": 1}, "gameweek": 1,
            "generated_at": "2026-08-20T10:00:00+00:00", **kw}


def write(conn, when, p=None, event=1, entry=ENTRY, deadline=DEADLINE_ISO):
    return snapshots.record(conn, entry_id=entry, target_event=event,
                            deadline=deadline, payload=p or payload(), now=when)


# --------------------------------------------------------------------------
# The deadline boundary
# --------------------------------------------------------------------------

def test_a_snapshot_before_the_deadline_is_written(conn):
    snap, outcome = write(conn, BEFORE)
    assert outcome == "written"
    assert snap.is_pre_deadline is True
    assert snap.target_event == 1 and snap.entry_id == ENTRY


def test_one_second_before_the_deadline_still_writes(conn):
    _, outcome = write(conn, JUST_BEFORE)
    assert outcome == "written"


def test_exactly_at_the_deadline_is_locked(conn):
    """The deadline is the cutoff, not a grace period."""
    _, outcome = write(conn, EXACTLY)
    assert outcome == "locked"


def test_one_second_after_the_deadline_is_locked(conn):
    _, outcome = write(conn, JUST_AFTER)
    assert outcome == "locked"


def test_a_post_deadline_run_cannot_overwrite_the_pre_deadline_record(conn):
    write(conn, BEFORE, payload("roll"))
    before = snapshots.final_pre_deadline(conn, ENTRY, 1)
    assert before.payload["decision"]["action"] == "roll"

    # A later refresh now thinks a transfer was right. It must change nothing.
    _, outcome = write(conn, AFTER, payload("transfer"))
    assert outcome == "locked"

    after = snapshots.final_pre_deadline(conn, ENTRY, 1)
    assert after.payload["decision"]["action"] == "roll"
    assert after.as_of == before.as_of
    assert after.content_hash == before.content_hash


def test_the_row_count_does_not_grow_after_the_deadline(conn):
    write(conn, BEFORE)
    n = conn.execute("SELECT COUNT(*) c FROM decision_snapshots").fetchone()["c"]
    for t in (EXACTLY, JUST_AFTER, AFTER):
        write(conn, t, payload("transfer"))
    assert conn.execute(
        "SELECT COUNT(*) c FROM decision_snapshots").fetchone()["c"] == n


def test_a_missing_deadline_is_refused_not_guessed(conn):
    _, outcome = write(conn, BEFORE, deadline="")
    assert outcome == "no_deadline"
    _, outcome = write(conn, BEFORE, deadline="not-a-time")
    assert outcome == "no_deadline"


def test_a_naive_deadline_is_rejected_rather_than_assumed_utc(conn):
    _, outcome = write(conn, BEFORE, deadline="2026-08-21T17:30:00")
    assert outcome == "no_deadline", "guessing the zone is how stale reads fresh"


def test_a_naive_now_is_treated_as_utc_not_crashed(conn):
    _, outcome = write(conn, BEFORE.replace(tzinfo=None))
    assert outcome == "written"


def test_is_locked_reflects_the_stored_deadline(conn):
    write(conn, BEFORE)
    assert snapshots.is_locked(conn, 1, BEFORE) is False
    assert snapshots.is_locked(conn, 1, AFTER) is True


def test_assert_immutable_raises_only_after_the_deadline(conn):
    write(conn, BEFORE)
    snapshots.assert_immutable(conn, ENTRY, 1, BEFORE)   # no raise
    with pytest.raises(snapshots.DeadlinePassedError):
        snapshots.assert_immutable(conn, ENTRY, 1, AFTER)


def test_an_unknown_event_is_not_locked(conn):
    assert snapshots.is_locked(conn, 99, AFTER) is False


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------

def test_an_unchanged_decision_does_not_create_a_second_row(conn):
    write(conn, BEFORE)
    _, outcome = write(conn, BEFORE + timedelta(hours=1))
    assert outcome == "unchanged"
    assert conn.execute(
        "SELECT COUNT(*) c FROM decision_snapshots").fetchone()["c"] == 1


def test_a_changed_decision_appends_a_new_row(conn):
    write(conn, BEFORE, payload("roll"))
    snap, outcome = write(conn, BEFORE + timedelta(hours=1), payload("transfer"))
    assert outcome == "written"
    assert conn.execute(
        "SELECT COUNT(*) c FROM decision_snapshots").fetchone()["c"] == 2
    assert snapshots.final_pre_deadline(
        conn, ENTRY, 1).payload["decision"]["action"] == "transfer"


def test_only_the_timestamp_changing_is_not_a_new_decision(conn):
    write(conn, BEFORE, payload("roll", generated_at="2026-08-20T10:00:00+00:00"))
    _, outcome = write(
        conn, BEFORE + timedelta(minutes=30),
        payload("roll", generated_at="2026-08-20T10:30:00+00:00"))
    assert outcome == "unchanged", "a refresh is not a new recommendation"


def test_the_content_hash_ignores_volatile_stamps_only():
    a = snapshots.content_hash({"decision": "roll", "as_of": "x",
                                "generated_at": "y", "data_age_seconds": 1})
    b = snapshots.content_hash({"decision": "roll", "as_of": "DIFFERENT",
                                "generated_at": "ALSO", "data_age_seconds": 999})
    c = snapshots.content_hash({"decision": "transfer", "as_of": "x",
                                "generated_at": "y", "data_age_seconds": 1})
    assert a == b
    assert a != c


def test_the_hash_is_order_independent():
    assert (snapshots.content_hash({"a": 1, "b": 2})
            == snapshots.content_hash({"b": 2, "a": 1}))


# --------------------------------------------------------------------------
# Identity: season, entry, event
# --------------------------------------------------------------------------

def test_two_entries_do_not_share_a_snapshot(conn):
    write(conn, BEFORE, payload("roll"), entry=1)
    write(conn, BEFORE, payload("transfer"), entry=2)
    assert snapshots.final_pre_deadline(
        conn, 1, 1).payload["decision"]["action"] == "roll"
    assert snapshots.final_pre_deadline(
        conn, 2, 1).payload["decision"]["action"] == "transfer"


def test_two_events_do_not_share_a_snapshot(conn):
    write(conn, BEFORE, payload("roll"), event=1)
    write(conn, BEFORE, payload("transfer"), event=2)
    assert snapshots.final_pre_deadline(
        conn, ENTRY, 1).payload["decision"]["action"] == "roll"
    assert snapshots.final_pre_deadline(
        conn, ENTRY, 2).payload["decision"]["action"] == "transfer"


def test_snapshots_are_season_aware(conn):
    """FPL reuses element ids every year; a season-blind key would merge them."""
    snapshots.record(conn, entry_id=ENTRY, target_event=1, deadline=DEADLINE_ISO,
                     payload=payload("roll"), now=BEFORE, season="2025-26")
    snapshots.record(conn, entry_id=ENTRY, target_event=1, deadline=DEADLINE_ISO,
                     payload=payload("transfer"), now=BEFORE, season="2026-27")
    old = snapshots.final_pre_deadline(conn, ENTRY, 1, season="2025-26")
    new = snapshots.final_pre_deadline(conn, ENTRY, 1, season="2026-27")
    assert old.payload["decision"]["action"] == "roll"
    assert new.payload["decision"]["action"] == "transfer"


def test_the_default_season_is_the_configured_one(conn):
    write(conn, BEFORE)
    assert snapshots.final_pre_deadline(conn, ENTRY, 1).season == config.SEASON


def test_no_snapshot_returns_none_not_an_empty_decision(conn):
    assert snapshots.final_pre_deadline(conn, ENTRY, 1) is None


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def test_history_returns_one_row_per_event_most_recent_first(conn):
    for ev in (1, 2, 3):
        dl = (DEADLINE + timedelta(days=7 * ev)).isoformat().replace("+00:00", "Z")
        write(conn, BEFORE, payload(f"a{ev}"), event=ev, deadline=dl)
        write(conn, BEFORE + timedelta(hours=1), payload(f"b{ev}"), event=ev,
              deadline=dl)
    h = snapshots.history(conn, ENTRY)
    assert [s.target_event for s in h] == [3, 2, 1]
    # the LAST pre-deadline snapshot for each event, not the first
    assert all(s.payload["decision"]["action"].startswith("b") for s in h)


def test_history_is_scoped_to_the_entry(conn):
    write(conn, BEFORE, entry=1)
    write(conn, BEFORE, entry=2)
    assert len(snapshots.history(conn, 1)) == 1


# --------------------------------------------------------------------------
# Schema and migration
# --------------------------------------------------------------------------

def test_the_schema_creates_the_table_on_a_fresh_database(tmp_path):
    c = db.connect(tmp_path / "fresh.db")
    db.init_schema(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(decision_snapshots)")}
    assert {"season", "entry_id", "target_event", "as_of", "deadline",
            "is_pre_deadline", "schema_version", "content_hash",
            "payload"} <= cols
    c.close()


def test_migration_onto_an_existing_database_preserves_its_data(tmp_path):
    """A user's live DB predates these tables; adding them must not lose rows."""
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES ('current_gw', '7')")
    c.commit()
    c.close()

    c2 = db.connect(path)
    db.init_schema(c2)
    assert c2.execute(
        "SELECT value FROM meta WHERE key='current_gw'").fetchone()[0] == "7"
    # and the new tables now exist
    names = {r["name"] for r in c2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"decision_snapshots", "gw_reviews", "notifications"} <= names
    c2.close()


def test_init_schema_is_idempotent(tmp_path):
    c = db.connect(tmp_path / "i.db")
    db.init_schema(c)
    snapshots.record(conn := c, entry_id=ENTRY, target_event=1,
                     deadline=DEADLINE_ISO, payload=payload(), now=BEFORE)
    db.init_schema(c)   # again
    assert conn.execute(
        "SELECT COUNT(*) c FROM decision_snapshots").fetchone()["c"] == 1
    c.close()


def test_the_stored_payload_is_valid_json_round_trippable(conn):
    write(conn, BEFORE, payload("roll", nested={"a": [1, 2, {"b": None}]}))
    raw = conn.execute("SELECT payload FROM decision_snapshots").fetchone()[0]
    assert json.loads(raw)["nested"]["a"][2]["b"] is None


def test_the_snapshot_serialises_with_its_identity_and_version(conn):
    snap, _ = write(conn, BEFORE)
    d = snap.as_dict()
    assert d["schema_version"] == snapshots.SNAPSHOT_SCHEMA_VERSION
    assert d["entry_id"] == ENTRY and d["target_event"] == 1
    assert d["is_pre_deadline"] is True
    assert d["deadline"].endswith("+00:00")


def test_a_nested_timestamp_does_not_defeat_idempotency(conn):
    """The freshness block carries its own `generated_at`.

    Stripping only the top-level one made every refresh look like a new
    recommendation, which would fill the table with identical rows and make
    "what did Gaffer advise?" ambiguous.
    """
    a = payload("roll")
    a["freshness"] = {"generated_at": "2026-08-20T10:00:00+00:00",
                      "squad_retrieved_at": "2026-08-20T09:55:00+00:00"}
    write(conn, BEFORE, a)

    b = payload("roll")
    b["freshness"] = {"generated_at": "2026-08-20T11:00:00+00:00",
                      "squad_retrieved_at": "2026-08-20T10:55:00+00:00"}
    _, outcome = write(conn, BEFORE + timedelta(hours=1), b)
    assert outcome == "unchanged"
    assert conn.execute(
        "SELECT COUNT(*) c FROM decision_snapshots").fetchone()["c"] == 1


def test_a_nested_decision_change_is_still_detected(conn):
    a = payload("roll")
    a["decision"] = {"action": "roll", "comparison": {"delta": 0.2}}
    write(conn, BEFORE, a)
    b = payload("roll")
    b["decision"] = {"action": "roll", "comparison": {"delta": 4.8}}
    _, outcome = write(conn, BEFORE + timedelta(hours=1), b)
    assert outcome == "written", "the numbers moved materially; that is new advice"


def test_volatile_stripping_reaches_inside_lists(conn):
    a = payload("roll")
    a["leagues"] = [{"id": 1, "generated_at": "x"}]
    b = payload("roll")
    b["leagues"] = [{"id": 1, "generated_at": "y"}]
    assert snapshots.content_hash(a) == snapshots.content_hash(b)


# --------------------------------------------------------------------------
# Additive column migration
# --------------------------------------------------------------------------

def test_a_pre_batch3_database_gains_the_columns_it_lacks(tmp_path):
    """The real failure: `table players has no column named cost_change_start`.

    CREATE TABLE IF NOT EXISTS is a no-op against an existing table, so every
    column added after a user first ran Gaffer is simply missing. CI never sees
    it (fresh checkout); the person who has been running it since July does, on
    the very next ingest.
    """
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE players (id INTEGER PRIMARY KEY, web_name TEXT, "
              "team_id INTEGER, position TEXT, price INTEGER)")
    c.execute("INSERT INTO players VALUES (1, 'Haaland', 1, 'FWD', 150)")
    c.execute("CREATE TABLE projections (player_id INTEGER, gw INTEGER, "
              "exp_points REAL)")
    c.execute("INSERT INTO projections VALUES (1, 1, 6.5)")
    c.commit()
    c.close()

    c2 = db.connect(path)
    db.init_schema(c2)

    cols = {r["name"] for r in c2.execute("PRAGMA table_info(players)")}
    assert "cost_change_start" in cols
    assert {"saves_per_90", "yellow_per_90", "bonus_per_90"} <= cols
    proj = {r["name"] for r in c2.execute("PRAGMA table_info(projections)")}
    assert {"exp_conceded_pts", "exp_saves_pts", "exp_points_model"} <= proj
    c2.close()


def test_the_migration_preserves_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE players (id INTEGER PRIMARY KEY, web_name TEXT, "
              "team_id INTEGER, position TEXT, price INTEGER)")
    c.execute("INSERT INTO players VALUES (1, 'Haaland', 1, 'FWD', 150)")
    c.commit()
    c.close()

    c2 = db.connect(path)
    db.init_schema(c2)
    row = c2.execute("SELECT web_name, price FROM players WHERE id=1").fetchone()
    assert row["web_name"] == "Haaland" and row["price"] == 150
    assert c2.execute(
        "SELECT cost_change_start FROM players WHERE id=1").fetchone()[0] in (0, None)
    c2.close()


def test_the_migration_is_idempotent(tmp_path):
    path = tmp_path / "m.db"
    c = db.connect(path)
    db.init_schema(c)
    first = db.migrate(c)
    second = db.migrate(c)
    assert first == [] and second == [], "a current database needs no migration"
    c.close()


def test_the_migration_reports_what_it_changed(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE players (id INTEGER PRIMARY KEY, web_name TEXT)")
    c.commit()
    c.close()
    c2 = db.connect(path)
    applied = db.migrate(c2)
    assert any("players +=" in a for a in applied)
    c2.close()


def test_schema_columns_ignores_comments_and_constraints():
    sql = ("CREATE TABLE IF NOT EXISTS t (\n"
           "    a INTEGER PRIMARY KEY,   -- the id\n"
           "    b TEXT NOT NULL,\n"
           "    -- a standalone comment\n"
           "    PRIMARY KEY (a, b)\n"
           ");\n")
    cols = dict(db._schema_columns(sql, "t"))
    assert set(cols) == {"a", "b"}
    assert "--" not in cols["a"]
