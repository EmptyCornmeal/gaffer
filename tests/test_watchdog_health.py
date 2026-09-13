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


# --- W16: say what it is failing on, and stop hammering it ------------------

#: The shape of `gh run view --log-failed`, from run 34765222080.
_P = "refresh\tUNKNOWN STEP\t2026-09-13T15:20:42.4546131Z "
_FAILED_LOG = "\n".join([
    _P,
    _P + "=========================== short test summary info ===",
    _P + "FAILED tests/test_mcp_evals.py::test_eval_case[gameweek-brief] - "
         "AssertionError: gameweek-brief: you.position is absent",
    _P + "1 failed, 2066 passed, 33 skipped, 2 deselected in 71.25s (0:01:11)",
    _P + "##[error]Process completed with exit code 1.",
])


def test_the_failing_test_is_named_from_the_run_log():
    wd = _load()
    assert wd.failure_reason_from_log(_FAILED_LOG) == (
        "test tests/test_mcp_evals.py::test_eval_case[gameweek-brief]")


def test_several_failing_tests_are_counted_not_listed():
    wd = _load()
    log = _FAILED_LOG + "\n" + _P + "FAILED tests/test_a.py::test_b - boom"
    assert wd.failure_reason_from_log(log).endswith("(+1 more)")


def test_a_non_test_failure_names_its_error_rather_than_the_exit_code():
    wd = _load()
    log = "\n".join([
        _P + "##[error]artifact contract FAILED - 1 violation(s) in data",
        _P + "##[error]Process completed with exit code 1.",
    ])
    assert wd.failure_reason_from_log(log).startswith("artifact contract FAILED")
    assert wd.failure_reason_from_log("") is None


def test_a_failing_pipeline_is_dispatched_into_on_a_slower_clock_not_never():
    """Dispatching cannot fix a failing gate, but since W16 a failure tied to
    one gameweek state can clear when the state moves on -- so slower, not off."""
    wd = _load()
    assert wd.dispatch_cooldown_minutes(0) == wd.DISPATCH_COOLDOWN_MINUTES
    assert wd.dispatch_cooldown_minutes(None) == wd.DISPATCH_COOLDOWN_MINUTES
    slowed = wd.dispatch_cooldown_minutes(wd.FAILING_AFTER_CONSECUTIVE)
    assert wd.DISPATCH_COOLDOWN_MINUTES < slowed < float("inf")


def test_the_publish_alert_names_the_fault_and_speaks_again_when_it_changes():
    wd = _load()
    state = {}
    reason = "test tests/test_mcp_evals.py::test_eval_case[gameweek-brief]"

    first = wd.publish_health(state, float("inf"), 21, reason)
    assert len(first) == 1 and reason in first[0], (
        "the alert must name the failing test, not only that it is failing")

    assert wd.publish_health(state, float("inf"), 22, reason) == [], (
        "the same fault is not news twice")

    other = wd.publish_health(state, float("inf"), 23, "test tests/test_x.py::test_y")
    assert len(other) == 1 and "test_x.py" in other[0], (
        "a DIFFERENT fault is news, even while already alerted")

    cleared = wd.publish_health(state, 5.0, 0, None)
    assert len(cleared) == 1 and "publishing again" in cleared[0]
    assert state["publish_alerted"] is False and state["publish_alert_reason"] is None


def test_a_healthy_pipeline_announces_nothing():
    wd = _load()
    assert wd.publish_health({}, 5.0, 0, None) == []
    assert wd.publish_health({}, None, None, None) == []
