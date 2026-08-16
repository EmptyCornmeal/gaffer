"""G1 — longitudinal state survives an ephemeral runner.

Every scheduled run gets a fresh machine and an empty database. What is being
tested here is a handover between two of them: run A writes a decision, the
machine disappears, run B must be able to read what A decided. If that fails on
21 August the GW1 record is gone permanently — there is no second attempt at a
pre-deadline snapshot once the deadline passes.

The other half is refusal to make things worse. A damaged archive must cost the
archive, never the gameweek: a corrupt line is skipped, a mangled file is
ignored, and the run publishes regardless.
"""

from __future__ import annotations

import json

import pytest

from gaffer.store import db, persist

SEASON = "2026/27"
ENTRY = 1066421


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "a.db")
    db.init_schema(c)
    yield c
    c.close()


def _decision(as_of, target_event=1, content_hash="h1"):
    return {
        "season": SEASON, "entry_id": ENTRY, "target_event": target_event,
        "as_of": as_of, "deadline": "2026-08-21T17:30:00Z",
        "is_pre_deadline": 1, "schema_version": 1, "content_hash": content_hash,
        "payload": json.dumps({"action": "roll", "captain": 1}),
    }


def _projection(player_id, as_of, target_gw=1, pre=1):
    return {
        "season": SEASON, "target_gw": target_gw, "player_id": player_id,
        "as_of": as_of, "model_version": "heuristic-0.4", "horizon": 0,
        "is_pre_deadline": pre, "deadline_time": "2026-08-21T17:30:00Z",
        "exp_points": 5.5,
    }


def _fresh(tmp_path, name):
    c = db.connect(tmp_path / name)
    db.init_schema(c)
    return c


def test_a_second_runner_reads_what_the_first_decided(conn, tmp_path):
    """The acceptance criterion, and the only one with a deadline attached."""
    db.upsert(conn, "decision_snapshots", [_decision("2026-08-21T15:00:00Z")],
              ["season", "entry_id", "target_event", "as_of"])
    conn.commit()
    persist.dump(conn, tmp_path)

    # The machine is gone. Nothing carries over but the repository.
    second = _fresh(tmp_path, "b.db")
    persist.restore(second, tmp_path)
    row = second.execute(
        "SELECT as_of, content_hash FROM decision_snapshots").fetchone()
    assert row["as_of"] == "2026-08-21T15:00:00Z"
    assert row["content_hash"] == "h1"
    second.close()


def test_restoring_twice_is_restoring_once(conn, tmp_path):
    """Runs overlap and workflows get re-dispatched; a replay must not duplicate
    history or a review would score the same decision twice."""
    db.upsert(conn, "decision_snapshots", [_decision("2026-08-21T15:00:00Z")],
              ["season", "entry_id", "target_event", "as_of"])
    conn.commit()
    persist.dump(conn, tmp_path)

    second = _fresh(tmp_path, "b.db")
    persist.restore(second, tmp_path)
    persist.restore(second, tmp_path)
    n = second.execute("SELECT COUNT(*) AS n FROM decision_snapshots").fetchone()["n"]
    assert n == 1
    second.close()


def test_a_corrupt_line_costs_the_line_and_not_the_run(conn, tmp_path):
    """A bad archive must never stop a gameweek publishing."""
    rows = [_decision("2026-08-21T15:00:00Z"),
            _decision("2026-08-21T16:00:00Z", content_hash="h2")]
    db.upsert(conn, "decision_snapshots", rows,
              ["season", "entry_id", "target_event", "as_of"])
    conn.commit()
    persist.dump(conn, tmp_path)

    p = persist.state_dir(tmp_path) / "decisions.ndjson"
    good = p.read_text(encoding="utf-8").splitlines()
    p.write_text(good[0] + "\n{not json at all\n" + good[1] + "\n", encoding="utf-8")

    second = _fresh(tmp_path, "b.db")
    out = persist.restore(second, tmp_path)
    assert out["decisions.ndjson"] == 2
    assert out["decisions.ndjson:skipped"] == 1
    second.close()


def test_a_missing_store_is_a_first_run_not_a_failure(tmp_path):
    c = _fresh(tmp_path, "b.db")
    assert persist.restore(c, tmp_path) == {
        "decisions.ndjson": 0, "reviews.ndjson": 0, "projections.ndjson": 0}
    c.close()


def test_projection_snapshots_are_compacted_to_what_is_actually_read(conn, tmp_path):
    """Full fidelity would be ~3,500 rows per run, several times a day, forever.

    `projection.latest_pre_deadline_snapshot` takes the newest `as_of` per player
    for a target event, so the intermediate re-runs inside one gameweek are
    already invisible to every reader. Persisting them would cost hundreds of
    megabytes a season to store what nothing looks at.
    """
    rows = [_projection(1, "2026-08-20T09:00:00Z"),
            _projection(1, "2026-08-21T09:00:00Z"),   # newer, same key
            _projection(1, "2026-08-22T09:00:00Z", pre=0),  # other side
            _projection(2, "2026-08-20T09:00:00Z")]
    db.upsert(conn, "projection_snapshots", rows,
              ["season", "target_gw", "player_id", "as_of"])
    conn.commit()
    written = persist.dump(conn, tmp_path)
    assert written["projections.ndjson"] == 3

    second = _fresh(tmp_path, "b.db")
    persist.restore(second, tmp_path)
    kept = [r["as_of"] for r in second.execute(
        "SELECT as_of FROM projection_snapshots WHERE player_id=1 AND "
        "is_pre_deadline=1")]
    assert kept == ["2026-08-21T09:00:00Z"]     # the latest, not the first
    second.close()


def test_both_sides_of_the_deadline_survive_compaction(conn, tmp_path):
    """`is_pre_deadline` is a fact about the row, and only the pre-deadline side
    may inform a decision. Collapsing them would let a post-deadline number pass
    as the advice that was actually given."""
    db.upsert(conn, "projection_snapshots",
              [_projection(1, "2026-08-20T09:00:00Z", pre=1),
               _projection(1, "2026-08-22T09:00:00Z", pre=0)],
              ["season", "target_gw", "player_id", "as_of"])
    conn.commit()
    persist.dump(conn, tmp_path)
    second = _fresh(tmp_path, "b.db")
    persist.restore(second, tmp_path)
    flags = sorted(r["is_pre_deadline"] for r in second.execute(
        "SELECT is_pre_deadline FROM projection_snapshots WHERE player_id=1"))
    assert flags == [0, 1]
    second.close()


def test_output_is_byte_stable_so_git_records_only_real_changes(conn, tmp_path):
    """Unsorted output would rewrite the whole file on every run, turning a
    quiet week into a large diff and hiding what actually moved."""
    db.upsert(conn, "decision_snapshots",
              [_decision("2026-08-21T16:00:00Z", content_hash="h2"),
               _decision("2026-08-21T15:00:00Z")],
              ["season", "entry_id", "target_event", "as_of"])
    conn.commit()
    persist.dump(conn, tmp_path)
    first = (persist.state_dir(tmp_path) / "decisions.ndjson").read_bytes()
    persist.dump(conn, tmp_path)
    assert (persist.state_dir(tmp_path) / "decisions.ndjson").read_bytes() == first
    # and sorted by key, so an inserted older row lands in place rather than
    # displacing every line after it
    assert first.decode().splitlines()[0].find("15:00:00") > 0


def test_reviews_round_trip(conn, tmp_path):
    db.upsert(conn, "gw_reviews", [{
        "season": SEASON, "entry_id": ENTRY, "event": 1,
        "generated_at": "2026-08-25T10:00:00Z",
        "snapshot_as_of": "2026-08-21T15:00:00Z",
        "schema_version": 1, "payload": json.dumps({"lesson": "captain held"}),
    }], ["season", "entry_id", "event"])
    conn.commit()
    persist.dump(conn, tmp_path)
    second = _fresh(tmp_path, "b.db")
    persist.restore(second, tmp_path)
    assert second.execute(
        "SELECT payload FROM gw_reviews").fetchone()["payload"].find("captain") > 0
    second.close()
