"""T-29 — season identity and rollover.

Every test uses recorded fixtures and an injected clock. The failure this guards
against only happens once a year, which is exactly why it cannot be tested by
waiting for it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from gaffer import config, ingest, season
from gaffer.store import db

# --- fixtures ---------------------------------------------------------------

def _events(start_year: int, n: int = 38) -> list[dict]:
    """A season's events: GW1 in August, GW38 the following May."""
    out = [{"id": 1, "name": "Gameweek 1",
            "deadline_time": f"{start_year}-08-15T17:30:00Z"}]
    for i in range(2, n):
        month = 8 + (i // 5)
        year, month = (start_year, month) if month <= 12 else (start_year + 1, month - 12)
        out.append({"id": i, "name": f"Gameweek {i}",
                    "deadline_time": f"{year}-{month:02d}-{(i % 27) + 1:02d}T13:30:00Z"})
    out.append({"id": n, "name": f"Gameweek {n}",
                "deadline_time": f"{start_year + 1}-05-24T13:30:00Z"})
    return out


def boot(start_year: int = 2026) -> dict:
    return {"events": _events(start_year), "teams": [], "elements": []}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    yield c
    c.close()


def _seed(c: sqlite3.Connection, label: str) -> None:
    """A database that looks like a season was actually played."""
    db.set_meta(c, "season", label)
    db.set_meta(c, "current_gw", "38")
    db.set_meta(c, "deadline", "2027-05-24T13:30:00Z")
    db.set_meta(c, "entry_name", "The Odeyssey")
    db.set_meta(c, "rule_squad_size", "15")
    c.execute("INSERT INTO teams(id, name, short) VALUES(1,'Arsenal','ARS')")
    c.execute("INSERT INTO players(id, web_name, team_id, position, price) "
              "VALUES(328,'Saka',1,'MID',100)")
    c.execute("INSERT INTO projections(player_id, gw, exp_points) VALUES(328,38,6.1)")
    c.execute("INSERT INTO my_squad(gw, player_id, selling_price) VALUES(38,328,101)")
    c.execute("INSERT INTO player_gw(season, player_id, gw, fixture, minutes, "
              "total_points) VALUES(?,328,38,900,90,12)", (label,))
    c.execute("INSERT INTO gw_reviews(season, entry_id, event, generated_at, "
              "payload) VALUES(?,1066421,38,'2027-05-25T00:00:00Z','{}')", (label,))
    c.execute("INSERT INTO notifications(season, dedupe_key, kind, severity, "
              "event, title, body, created_at, state) VALUES(?,'deadline|38',"
              "'deadline','critical',38,'t','b','x','dry_run')", (label,))
    c.commit()


# --- labels ------------------------------------------------------------------

@pytest.mark.parametrize("label,year", [("2026-27", 2026), ("1999-00", 1999),
                                        ("2099-00", 2099), (" 2024-25 ", 2024)])
def test_valid_labels_parse(label, year):
    assert season.parse(label) == year


@pytest.mark.parametrize("bad", [
    "2026-28",      # self-contradictory
    "2026/27", "2026", "26-27", "", "  ", None, 2026, "abcd-ef",
    "2026-27-28",
])
def test_invalid_labels_are_refused(bad):
    assert season.is_valid(bad) is False
    with pytest.raises(season.SeasonError):
        season.parse(bad)


def test_label_arithmetic_wraps_the_century():
    assert season.next_label("2026-27") == "2027-28"
    assert season.next_label("1999-00") == "2000-01"
    assert season.slug("2026-27") == "2026_27"


# --- deriving the season from the API ----------------------------------------

def test_the_season_comes_from_event_deadlines_not_the_clock():
    label, why = season.derive_from_bootstrap(boot(2026))
    assert label == "2026-27"
    assert "2026-08" in why and "2027-05" in why


def test_next_seasons_payload_derives_the_next_season():
    assert season.derive_from_bootstrap(boot(2027))[0] == "2027-28"


@pytest.mark.parametrize("payload,fragment", [
    ({}, "no events"),
    ({"events": []}, "no events"),
    ({"events": [{"id": 1}]}, "parseable deadline_time"),
    ({"events": [{"id": 1, "deadline_time": "not-a-date"}]}, "parseable"),
    ("nonsense", "not an object"),
])
def test_an_unidentifiable_payload_returns_no_season(payload, fragment):
    label, why = season.derive_from_bootstrap(payload)
    assert label is None
    assert fragment in why


def test_a_payload_holding_two_seasons_is_ambiguous_not_guessed():
    """The real hazard: a mid-rollover payload with events from both sides."""
    mixed = {"events": [
        {"id": 1, "deadline_time": "2026-08-14T17:30:00Z"},
        {"id": 2, "deadline_time": "2027-08-21T17:30:00Z"}]}
    label, why = season.derive_from_bootstrap(mixed)
    assert label is None
    assert "more than one season" in why


def test_a_single_event_still_identifies_the_season():
    """Pre-season, and every test stub, has one event. That is enough."""
    label, why = season.derive_from_bootstrap(
        {"events": [{"id": 1, "deadline_time": "2026-08-21T17:30:00Z"}]})
    assert label == "2026-27"
    assert "1 event" in why


def test_a_late_calendar_year_deadline_belongs_to_the_season_that_started_it():
    """A January gameweek is still 2026-27, not 2027-28."""
    label, _ = season.derive_from_bootstrap(
        {"events": [{"id": 20, "deadline_time": "2027-01-17T13:30:00Z"}]})
    assert label == "2026-27"


# --- classification ----------------------------------------------------------

def test_first_ever_run():
    i = season.identify(api="2026-27", database=None, empty_database=True)
    assert i.state == season.STATE_FIRST_RUN
    assert i.safe_to_run


def test_same_season_is_the_normal_case():
    i = season.identify(api="2026-27", database="2026-27", artifacts="2026-27")
    assert i.state == season.STATE_SAME
    assert i.safe_to_run


def test_a_legitimate_new_season_is_detected_and_not_auto_applied():
    i = season.identify(api="2027-28", database="2026-27")
    assert i.state == season.STATE_NEW
    assert not i.safe_to_run, "a rollover must never happen as a side effect"
    assert "--rollover" in i.detail


def test_a_populated_database_with_no_season_stamp_refuses():
    i = season.identify(api="2026-27", database=None, empty_database=False)
    assert i.state == season.STATE_MISSING
    assert not i.safe_to_run
    assert "--adopt" in i.detail


def test_an_invalid_stored_season_refuses():
    i = season.identify(api="2026-27", database="garbage")
    assert i.state == season.STATE_MISSING


def test_an_older_api_season_is_refused_as_a_downgrade():
    i = season.identify(api="2025-26", database="2026-27")
    assert i.state == season.STATE_DOWNGRADE
    assert not i.safe_to_run
    assert "Nothing is modified" in i.detail


def test_a_multi_season_jump_is_ambiguous_not_a_rollover():
    i = season.identify(api="2029-30", database="2026-27")
    assert i.state == season.STATE_AMBIGUOUS
    assert not i.safe_to_run


def test_an_unidentifiable_api_refuses_rather_than_assuming():
    i = season.identify(api=None, database="2026-27", api_detail="no events")
    assert i.state == season.STATE_AMBIGUOUS
    assert "no events" in i.detail


def test_artifacts_from_another_season_are_flagged_even_when_db_agrees():
    i = season.identify(api="2026-27", database="2026-27", artifacts="2025-26")
    assert i.state == season.STATE_MISSING
    assert "artifacts say 2025-26" in i.detail


# --- the rollover ------------------------------------------------------------

def test_preview_writes_nothing(conn, tmp_path):
    _seed(conn, "2026-27")
    before = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    res = season.rollover(conn, "2027-28", db_path=tmp_path / "t.db",
                          data_dir=tmp_path)
    assert not res.applied
    assert res.error is None
    assert dict(conn.execute("SELECT key, value FROM meta").fetchall()) == before
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1
    assert "PREVIEW ONLY" in res.render()


def test_rollover_archives_rather_than_deletes(conn, tmp_path):
    _seed(conn, "2026-27")
    res = season.rollover(conn, "2027-28", confirm=True,
                          db_path=tmp_path / "t.db", backup_dir=tmp_path / "bk",
                          data_dir=tmp_path, stamp="test")
    assert res.applied, res.render()
    # The outgoing season is still there, under its own name.
    assert conn.execute("SELECT COUNT(*) FROM players_2026_27").fetchone()[0] == 1
    assert conn.execute(
        "SELECT web_name FROM players_2026_27 WHERE id=328").fetchone()[0] == "Saka"
    # ...and the working tables are empty and ready.
    for t in season.CURRENT_SEASON_TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0


def test_reused_element_ids_cannot_collide_across_seasons(conn, tmp_path):
    """The actual failure: element 328 is a different player next season."""
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    conn.execute("INSERT INTO teams(id, name, short) VALUES(1,'Leeds','LEE')")
    conn.execute("INSERT INTO players(id, web_name, team_id, position, price) "
                 "VALUES(328,'Somebody Else',1,'DEF',45)")
    conn.commit()
    assert conn.execute("SELECT web_name FROM players WHERE id=328").fetchone()[0] \
        == "Somebody Else"
    assert conn.execute(
        "SELECT web_name FROM players_2026_27 WHERE id=328").fetchone()[0] == "Saka"


def test_history_survives_the_boundary(conn, tmp_path):
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    for table in ("player_gw", "gw_reviews"):
        rows = [r[0] for r in conn.execute(f"SELECT season FROM {table}")]
        assert rows == ["2026-27"], f"{table} lost its history"


def test_a_review_stays_attached_to_the_season_it_judged(conn, tmp_path):
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    conn.execute("INSERT INTO gw_reviews(season, entry_id, event, generated_at,"
                 " payload) VALUES('2027-28',1066421,1,'2027-08-16T00:00:00Z','{}')")
    conn.commit()
    got = {r[0]: r[1] for r in conn.execute(
        "SELECT season, event FROM gw_reviews ORDER BY season")}
    assert got == {"2026-27": 38, "2027-28": 1}


def test_notification_dedupe_resets_without_resending_old_alerts(conn, tmp_path):
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    # The old row is kept (so it is never re-sent) and does not block the new
    # season's identically-keyed alert, because the key is (season, dedupe_key).
    kept = [r[0] for r in conn.execute("SELECT season FROM notifications")]
    assert kept == ["2026-27"]
    conn.execute("INSERT INTO notifications(season, dedupe_key, kind, severity,"
                 " event, title, body, created_at, state) VALUES('2027-28',"
                 "'deadline|38','deadline','critical',38,'t','b','y','dry_run')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2


def test_season_specific_meta_is_reset_and_manager_meta_is_kept(conn, tmp_path):
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    assert db.get_meta(conn, "season") == "2027-28"
    assert db.get_meta(conn, "season_rolled_from") == "2026-27"
    for gone in ("current_gw", "deadline", "free_transfers", "bank"):
        assert db.get_meta(conn, gone) is None, f"{gone} survived the rollover"
    assert db.get_meta(conn, "entry_name") == "The Odeyssey"
    assert db.get_meta(conn, "rule_squad_size") == "15"


def test_rerunning_a_completed_rollover_is_a_no_op(conn, tmp_path):
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    conn.execute("INSERT INTO teams(id, name, short) VALUES(1,'Sunderland','SUN')")
    conn.execute("INSERT INTO players(id, web_name, team_id, position, price) "
                 "VALUES(7,'New Guy',1,'FWD',60)")
    conn.commit()
    again = season.rollover(conn, "2027-28", confirm=True,
                            db_path=tmp_path / "t.db",
                            backup_dir=tmp_path / "bk", data_dir=tmp_path,
                            stamp="t2")
    assert not again.applied
    assert again.plan.already_done
    assert "already" in again.render()
    # Crucially, the second call did not archive the new season's rows.
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 1


def test_a_failure_midway_leaves_the_prior_database_usable(conn, tmp_path,
                                                           monkeypatch):
    _seed(conn, "2026-27")
    bad = tmp_path / "broken.sql"
    bad.write_text("CREATE TABLE players (this is not sql;", encoding="utf-8")
    res = season.rollover(conn, "2027-28", confirm=True,
                          db_path=tmp_path / "t.db", backup_dir=tmp_path / "bk",
                          data_dir=tmp_path, stamp="t", schema_path=bad)
    assert not res.applied
    assert res.error
    assert "rolled back" in res.render()
    # Everything is exactly where it was.
    assert conn.execute("SELECT web_name FROM players WHERE id=328").fetchone()[0] \
        == "Saka"
    assert db.get_meta(conn, "season") == "2026-27"
    assert db.get_meta(conn, "current_gw") == "38"
    assert not season.archived_seasons(conn)


def test_the_backup_is_taken_and_verified_before_anything_changes(conn, tmp_path):
    _seed(conn, "2026-27")
    res = season.rollover(conn, "2027-28", confirm=True,
                          db_path=tmp_path / "t.db", backup_dir=tmp_path / "bk",
                          data_dir=tmp_path, stamp="t")
    assert res.backup and res.backup["ok"]
    assert res.backup["integrity_check"] == "ok"
    assert res.backup["mismatched"] == []
    copy = sqlite3.connect(res.backup["path"])
    try:
        assert copy.execute(
            "SELECT web_name FROM players WHERE id=328").fetchone()[0] == "Saka"
    finally:
        copy.close()


def test_the_cache_is_cleared_because_every_entry_describes_the_old_season(
        conn, tmp_path):
    _seed(conn, "2026-27")
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "bootstrap-static.json").write_text("{}", encoding="utf-8")
    p = season.plan(conn, "2027-28", data_dir=tmp_path)
    assert p.caches and "1 file" in p.caches[0]
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    assert not cache.exists()


def test_the_plan_names_what_must_be_revalidated_by_hand(conn, tmp_path):
    _seed(conn, "2026-27")
    p = season.plan(conn, "2027-28", data_dir=tmp_path)
    joined = " ".join(p.revalidate)
    assert "entry_id" in joined
    assert "league_ids" in joined
    assert "chips" in joined
    assert "NOTHING IS DELETED" in p.render()


def test_a_partially_started_new_season_is_warned_about_not_overwritten(
        conn, tmp_path):
    _seed(conn, "2026-27")
    conn.execute("INSERT INTO player_gw(season, player_id, gw, fixture, minutes, "
                 "total_points) VALUES('2027-28',5,1,1,90,2)")
    conn.commit()
    p = season.plan(conn, "2027-28", data_dir=tmp_path)
    assert any("2027-28" in w and "player_gw" in w for w in p.warnings)
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    assert conn.execute(
        "SELECT COUNT(*) FROM player_gw WHERE season='2027-28'").fetchone()[0] == 1


def test_changed_team_ids_do_not_leak_across_the_boundary(conn, tmp_path):
    """Promotion and relegation renumber teams; last season's must not linger."""
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    assert conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0
    conn.execute("INSERT INTO teams(id, name, short) VALUES(1,'Burnley','BUR')")
    conn.commit()
    assert conn.execute("SELECT name FROM teams WHERE id=1").fetchone()[0] == "Burnley"
    assert conn.execute(
        "SELECT name FROM teams_2026_27 WHERE id=1").fetchone()[0] == "Arsenal"


def test_foreign_keys_still_point_at_the_live_tables_after_a_rollover(conn,
                                                                     tmp_path):
    """Renaming `players` must not repoint player_gw at the archive."""
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='player_gw'"
    ).fetchone()[0]
    assert "players_2026_27" not in sql, "the FK was rewritten to the archive"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_archived_seasons_are_discoverable(conn, tmp_path):
    _seed(conn, "2026-27")
    assert season.archived_seasons(conn) == []
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    assert season.archived_seasons(conn) == ["2026-27"]


def test_an_empty_database_rolls_over_without_incident(conn, tmp_path):
    db.set_meta(conn, "season", "2026-27")
    res = season.rollover(conn, "2027-28", confirm=True,
                          db_path=tmp_path / "t.db", backup_dir=tmp_path / "bk",
                          data_dir=tmp_path, stamp="t")
    assert res.applied, res.render()
    assert db.get_meta(conn, "season") == "2027-28"


def test_rollover_refuses_an_invalid_target(conn, tmp_path):
    _seed(conn, "2026-27")
    with pytest.raises(season.SeasonError):
        season.plan(conn, "not-a-season", data_dir=tmp_path)


def test_adopt_is_explicit(conn):
    assert season.stored(conn) in (None, config.SEASON)
    season.adopt(conn, "2024-25")
    assert season.stored(conn) == "2024-25"
    with pytest.raises(season.SeasonError):
        season.adopt(conn, "2024-26")


# --- the gate in front of ingest ---------------------------------------------

def test_migrating_an_unstamped_database_adopts_the_configured_season(tmp_path):
    """A database written before season identity existed must stay runnable."""
    path = tmp_path / "old.db"
    c = db.connect(path)
    db.init_schema(c)
    c.execute("DELETE FROM meta WHERE key='season'")
    c.commit()
    c.close()
    c = db.connect(path)
    applied = db.migrate(c)
    assert any("season adopted" in a for a in applied)
    assert db.get_meta(c, "season") == config.SEASON
    assert db.get_meta(c, "season_adopted_by_migration") == config.SEASON
    c.close()


def test_a_season_mismatch_carries_the_identity_and_says_nothing_was_written():
    i = season.identify(api="2027-28", database="2026-27")
    exc = ingest.SeasonMismatch(i)
    assert exc.identity is i
    assert "Nothing was written" in str(exc)
    assert "gaffer.season" in str(exc)


def test_artifacts_all_declare_the_same_season():
    """The published set, as it stands on disk."""
    meta_path = config.DATA_DIR / "meta.json"
    if not meta_path.exists():  # pragma: no cover - artifact-free checkout
        pytest.skip("no published artifacts")
    declared = json.loads(meta_path.read_text(encoding="utf-8")).get("season")
    assert season.is_valid(declared)
    for path in config.DATA_DIR.glob("*.json"):
        if path.name in ("meta.json", "backtest.json"):
            continue
        blob = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(blob, dict) and "season" in blob:
            assert blob["season"] == declared, f"{path.name} is from another season"


# --- the pipeline refuses rather than mixing ---------------------------------

def test_the_pipeline_refuses_a_new_season_before_writing_anything(
        tmp_path, monkeypatch):
    """The gate sits in front of ingest, so nothing is written on refusal."""
    import gaffer.ingest as ing

    calls: list[str] = []

    class Stub:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bootstrap(self):
            calls.append("bootstrap")
            return boot(2027)          # the API has moved on

        def fixtures(self):            # pragma: no cover - never reached
            calls.append("fixtures")
            return []

    monkeypatch.setattr(ing, "FplClient", Stub)
    monkeypatch.setenv("GAFFER_DATA_DIR", str(tmp_path))
    config.reload_paths()
    path = tmp_path / "gaffer.db"
    c = db.connect(path)
    db.init_schema(c)
    _seed(c, "2026-27")
    c.close()

    with pytest.raises(ing.SeasonMismatch) as exc:
        ing.run(db_path=path, skip_enrich=True)
    assert exc.value.identity.state == season.STATE_NEW
    assert calls == ["bootstrap"], "it must stop before fetching anything else"

    c = db.connect(path)
    try:
        assert db.get_meta(c, "season") == "2026-27"
        assert c.execute(
            "SELECT web_name FROM players WHERE id=328").fetchone()[0] == "Saka"
    finally:
        c.close()
    config.reload_paths()


def test_new_rows_are_stamped_with_the_databases_season_not_the_constant(
        conn, tmp_path):
    """After a rollover the constant is a season stale. The database is not."""
    _seed(conn, "2026-27")
    season.rollover(conn, "2027-28", confirm=True, db_path=tmp_path / "t.db",
                    backup_dir=tmp_path / "bk", data_dir=tmp_path, stamp="t")
    assert season.current(conn) == "2027-28"
    assert config.SEASON == "2026-27", "the fixture assumes the constant is stale"

    from datetime import UTC, datetime

    from gaffer import review, snapshots
    deadline = "2027-08-21T17:30:00Z"
    snap, status = snapshots.record(
        conn, entry_id=1, target_event=1, deadline=deadline,
        payload={"action": "roll"}, now=datetime(2027, 8, 20, tzinfo=UTC))
    assert status == "written"
    assert conn.execute(
        "SELECT season FROM decision_snapshots").fetchone()[0] == "2027-28"

    # `Review.season` is filled by `review.build`, which defaults to the same
    # helper — so the two sides of the loop cannot end up in different seasons.
    assert review.load(conn, entry_id=1, event=1) is None
    assert snap is not None
