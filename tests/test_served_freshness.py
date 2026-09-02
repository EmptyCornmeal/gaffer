"""The refresh gate judges what is SERVED, not what was generated.

`data/` is the pipeline's output directory. The site is built from
`web/public/data/`, and the refresh workflow copies one to the other. Any
process that writes `data/` without that copy makes them diverge — and then the
gate reads a fresh timestamp while the reader is looking at a stale page.

Found on 2026-09-02: local pipeline runs were committed to `data/` alone, and
the gate reported the data 15 minutes old while the site served an artifact 85
minutes old. Nothing was watching the gap, and the whole point of the gate is
that a reader is never shown advice older than the bar.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from gaffer import schedule

NOW = datetime(2026, 9, 2, 19, 0, tzinfo=UTC)


def _write(path, generated_at, deadline="2026-09-04T17:30:00Z"):
    path.parent.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({
        "generated_at": generated_at, "deadline": deadline,
    }), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "web" / "public" / "data").mkdir(parents=True)
    return tmp_path


def test_the_older_of_the_two_wins(repo):
    """The reader's freshness is the served copy's freshness."""
    _write(repo / "data", "2026-09-02T18:45:00Z")            # 15 min old
    _write(repo / "web" / "public" / "data", "2026-09-02T17:35:00Z")  # 85 min
    state = schedule.read_published_state(repo / "data")
    assert state["generated_at"] == datetime(2026, 9, 2, 17, 35, tzinfo=UTC)
    assert "web" in state["freshness_source"]


def test_a_fresher_served_copy_does_not_make_the_answer_younger(repo):
    """Only ever older. A served copy somehow ahead of the source is not a
    reason to believe the site is fresher than the pipeline that fed it."""
    _write(repo / "data", "2026-09-02T17:35:00Z")
    _write(repo / "web" / "public" / "data", "2026-09-02T18:45:00Z")
    state = schedule.read_published_state(repo / "data")
    assert state["generated_at"] == datetime(2026, 9, 2, 17, 35, tzinfo=UTC)


def test_no_served_copy_is_not_evidence_of_anything(repo):
    """A checkout that has never built the site is a normal state, not a
    reason to force a refresh on every tick."""
    _write(repo / "data", "2026-09-02T18:45:00Z")
    state = schedule.read_published_state(repo / "data")
    assert state["generated_at"] == datetime(2026, 9, 2, 18, 45, tzinfo=UTC)
    assert state["degraded"] is None


def test_a_corrupt_served_copy_is_degraded_not_ignored(repo):
    """Corruption fails OPEN elsewhere in this module, and it must reach that
    path rather than being swallowed here."""
    _write(repo / "data", "2026-09-02T18:45:00Z")
    (repo / "web" / "public" / "data" / "meta.json").write_text(
        "{not json", encoding="utf-8")
    state = schedule.read_published_state(repo / "data")
    assert state["degraded"]


def test_the_gate_acts_on_the_divergence(repo):
    """End to end: the reason the fix exists. Same instant, same `data/`, and
    the answer flips on what the site is actually serving."""
    _write(repo / "data", "2026-09-02T18:55:00Z")
    _write(repo / "web" / "public" / "data", "2026-09-02T12:00:00Z")
    state = schedule.read_published_state(repo / "data")
    d = schedule.should_refresh(
        NOW, deadline=state["deadline"],
        last_generated_at=state["generated_at"],
        degraded=state["degraded"])
    assert d.should_refresh, d.reason

    _write(repo / "web" / "public" / "data", "2026-09-02T18:55:00Z")
    state = schedule.read_published_state(repo / "data")
    d = schedule.should_refresh(
        NOW, deadline=state["deadline"],
        last_generated_at=state["generated_at"],
        degraded=state["degraded"])
    assert not d.should_refresh, d.reason


def test_a_served_copy_with_no_timestamp_is_a_fault(repo):
    _write(repo / "data", "2026-09-02T18:45:00Z")
    (repo / "web" / "public" / "data" / "meta.json").write_text(
        json.dumps({"deadline": "2026-09-04T17:30:00Z"}), encoding="utf-8")
    state = schedule.read_published_state(repo / "data")
    assert state["degraded"]
    assert "generated_at" in state["degraded"]


def test_the_bar_itself_is_unchanged(repo):
    """This changes WHICH timestamp is judged, never the bar it is judged
    against."""
    _write(repo / "data", "2026-09-02T18:55:00Z")
    _write(repo / "web" / "public" / "data", "2026-09-02T18:55:00Z")
    state = schedule.read_published_state(repo / "data")
    d = schedule.should_refresh(
        NOW - timedelta(minutes=0), deadline=state["deadline"],
        last_generated_at=state["generated_at"])
    assert d.max_age_minutes == schedule.MAX_AGE["idle"].total_seconds() / 60
