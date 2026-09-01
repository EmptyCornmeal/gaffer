"""P0.3 -- the watchdog must grade SUCCESS, not activity.

On 2026-09-01 `refresh.yml` failed 22 consecutive times while
`com.myles.gaffer-watchdog` logged "scheduler healthy (last run 20m ago)" on
every pass. Seven of those failures were its own dispatches. The site served a
GW2-in-play snapshot as current analysis for 26 hours, three days before a
deadline, and nothing said a word.

`conclusion` was already being fetched from `gh` and thrown away.
"""
from __future__ import annotations

import importlib.util
import json

from gaffer import config

WD = config.REPO_ROOT / "deploy" / "macmini" / "refresh_watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("gaffer_watchdog", WD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Proc:
    def __init__(self, out): self.returncode, self.stdout, self.stderr = 0, out, ""


def _runs(rows):
    return _Proc(json.dumps(rows))


def test_the_watchdog_reads_success_not_merely_activity(monkeypatch):
    """The exact 2026-09-01 shape: runs firing every 20 minutes, all failing."""
    wd = _load()
    rows = [{"createdAt": "2026-09-01T18:00:00Z", "status": "completed",
             "conclusion": "failure"} for _ in range(22)]
    monkeypatch.setattr(wd, "run", lambda *a, **k: _runs(rows))
    assert wd.last_success_age_minutes() == float("inf"), (
        "22 failed runs must not read as a recent success")
    assert wd.consecutive_failures() == 22


def test_a_success_is_found_behind_failures(monkeypatch):
    wd = _load()
    rows = [
        {"createdAt": "2026-09-01T20:00:00Z", "status": "completed", "conclusion": "failure"},
        {"createdAt": "2026-09-01T19:00:00Z", "status": "completed", "conclusion": "failure"},
        {"createdAt": "2026-09-01T18:00:00Z", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(wd, "run", lambda *a, **k: _runs(rows))
    age = wd.last_success_age_minutes()
    assert age is not None and age != float("inf")
    assert wd.consecutive_failures() == 2


def test_an_in_flight_run_is_not_counted_as_a_failure(monkeypatch):
    """A queued or running job has no conclusion yet and must not inflate the
    failure streak, or the watchdog alerts every time it dispatches one."""
    wd = _load()
    rows = [
        {"createdAt": "2026-09-01T20:10:00Z", "status": "in_progress", "conclusion": None},
        {"createdAt": "2026-09-01T20:00:00Z", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(wd, "run", lambda *a, **k: _runs(rows))
    assert wd.consecutive_failures() == 0


def test_the_no_success_alert_clock_is_tighter_than_the_scheduler_clock():
    """A stalled scheduler is rescuable by dispatching; a failing pipeline is
    not, so it must reach a person sooner."""
    wd = _load()
    assert wd.ALERT_NO_SUCCESS_MINUTES < wd.ALERT_SCHEDULE_SILENT_MINUTES


def test_the_health_line_no_longer_calls_a_failing_pipeline_healthy():
    src = WD.read_text(encoding="utf-8")
    # Only EMITTED lines matter; the incident is quoted in a comment on purpose.
    emitted = [ln for ln in src.splitlines()
               if "log(" in ln and not ln.lstrip().startswith("#")]
    assert not any("scheduler healthy" in ln for ln in emitted), (
        "the word 'healthy' on a run-age predicate is what made 26 hours of "
        "failure look fine; say what was actually measured")
    assert "last_success_age_minutes" in src
    assert any("publish health" in ln for ln in emitted), (
        "publish success must be reported on every pass, not only on failure")
