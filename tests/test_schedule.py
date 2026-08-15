"""T-08 — schedule-continuity decision logic and workflow structure.

Also covers the refresh gate: GitHub's scheduler drifts by up to an hour, so the
workflow fires every 15 minutes and asks `should_refresh` whether there is any
point. Every boundary below is a fixed clock rather than a wait.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gaffer import config, schedule

NOW = datetime(2026, 10, 1, 3, 17, tzinfo=UTC)

# The GW1 deadline this whole gate exists to protect.
DEADLINE = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)


def before(hours: float) -> datetime:
    return DEADLINE - timedelta(hours=hours)


def decide(hours_before: float, age_min: float | None = 0.0, states=None):
    now = before(hours_before)
    return schedule.should_refresh(
        now, deadline=DEADLINE, fixture_states=states,
        last_generated_at=None if age_min is None
        else now - timedelta(minutes=age_min))


# --------------------------------------------------------------------------
# Refresh gate — windows
# --------------------------------------------------------------------------

def test_far_from_a_deadline_is_idle():
    assert decide(7).window == "idle"
    assert decide(48).window == "idle"


def test_the_pre_deadline_window_opens_six_hours_out():
    assert decide(6.01).window == "idle"
    assert decide(5.99).window == "pre_deadline"


def test_the_final_approach_starts_two_hours_out():
    assert decide(2.01).window == "pre_deadline"
    assert decide(1.99).window == "final_approach"


def test_the_window_shuts_before_the_deadline():
    """A run started inside this would finish after the deadline and would be
    projecting the NEXT gameweek — not the question being asked."""
    assert decide(0.4).window == "final_approach"       # 24 min out
    assert decide(0.25).window == "idle"                # 15 min out
    assert decide(-1).window == "idle"                  # deadline gone


def test_live_football_is_its_own_window():
    d = decide(48, 0, states=["scheduled", "live"])
    assert d.window == "live"
    for state in ("live", "half_time", "awaiting_bonus"):
        assert decide(48, 0, states=[state]).window == "live"
    for state in ("scheduled", "finished", "postponed"):
        assert decide(48, 0, states=[state]).window == "idle"


def test_a_deadline_outranks_live_football():
    """Both can be true during a double gameweek. If you are picking a team,
    that is the more urgent number."""
    assert decide(1, 0, states=["live"]).window == "final_approach"


# --------------------------------------------------------------------------
# Refresh gate — the age bar
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hours,age,expected", [
    (7, 200, False), (7, 400, True),        # idle: 6h bar
    (5, 30, False), (5, 100, True),         # pre-deadline: 90 min bar
    (1, 10, False), (1, 30, True),          # final approach: 20 min bar
])
def test_each_window_has_its_own_staleness_bar(hours, age, expected):
    assert decide(hours, age).should_refresh is expected


def test_the_final_approach_guarantees_a_recent_publish():
    """The GW1 failure mode: at 17:29 the last publish must not be from 11:45."""
    d = decide(0.5, age_min=345)            # published ~11:45 for a 17:30 deadline
    assert d.should_refresh is True
    assert d.window == "final_approach"


def test_nothing_published_fails_open():
    d = decide(48, age_min=None)
    assert d.should_refresh is True
    assert "nothing is published" in d.reason


def test_ordinary_clock_skew_does_not_trigger_a_refresh_loop():
    """Runner clocks drift by seconds. A publish timestamped slightly ahead of us
    is current, not stale, and must not make every tick refresh."""
    now = before(48)
    d = schedule.should_refresh(
        now, deadline=DEADLINE,
        last_generated_at=now + schedule.CLOCK_SKEW_GRACE / 2)
    assert d.should_refresh is False
    assert d.age_minutes == 0


def test_a_far_future_timestamp_cannot_disable_the_schedule():
    """C3. Skew has a bound. Without one, a single corrupt timestamp makes `age`
    negative forever, every later tick skips, and the schedule is off with
    nothing to say so — the failure is silent and unbounded in time."""
    now = before(48)
    d = schedule.should_refresh(
        now, deadline=DEADLINE, last_generated_at=now + timedelta(days=2))
    assert d.should_refresh is True
    assert "corrupt" in d.reason


def test_the_decision_reports_what_it_measured():
    d = decide(1, 30)
    assert d.age_minutes == pytest.approx(30, abs=0.1)
    assert d.max_age_minutes == 20
    assert d.window in d.reason


# --------------------------------------------------------------------------
# Refresh gate — reading the committed artifacts
# --------------------------------------------------------------------------

def test_published_state_is_read_from_the_artifacts(tmp_path):
    import json

    (tmp_path / "meta.json").write_text(json.dumps({
        "deadline": "2026-08-21T17:30:00Z",
        "generated_at": "2026-08-21T11:45:00Z",
    }), encoding="utf-8")
    (tmp_path / "live.json").write_text(json.dumps({
        "fixtures": [{"state": "scheduled"}, {"state": "live"}],
    }), encoding="utf-8")
    state = schedule.read_published_state(tmp_path)
    assert state["deadline"] == DEADLINE
    assert state["generated_at"] == datetime(2026, 8, 21, 11, 45, tzinfo=UTC)
    assert state["fixture_states"] == ["scheduled", "live"]


@pytest.mark.parametrize("setup", ["missing", "malformed", "empty"])
def test_unreadable_artifacts_never_block_a_refresh(tmp_path, setup):
    """The gate runs before anything is installed. It must never be the reason a
    refresh does not happen."""
    if setup == "malformed":
        (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
    elif setup == "empty":
        (tmp_path / "meta.json").write_text("{}", encoding="utf-8")
    state = schedule.read_published_state(tmp_path)
    assert state["generated_at"] is None
    assert schedule.should_refresh(
        NOW, deadline=state["deadline"],
        last_generated_at=state["generated_at"],
        fixture_states=state["fixture_states"],
        degraded=state["degraded"]).should_refresh is True


@pytest.mark.parametrize("payload", ["[]", "null", '"a string"', "7"])
def test_valid_json_of_the_wrong_shape_is_reported_not_raised(tmp_path, payload):
    """C2. `[].get(...)` raises AttributeError, which the old except tuple did not
    catch. The gate job then died, `needs: gate` skipped, and every refresh
    stopped — a crash in the cheap pre-check taking the pipeline with it."""
    (tmp_path / "meta.json").write_text(payload, encoding="utf-8")
    (tmp_path / "live.json").write_text(payload, encoding="utf-8")
    state = schedule.read_published_state(tmp_path)          # must not raise
    assert state["degraded"]
    assert "meta.json" in state["degraded"] and "live.json" in state["degraded"]


def test_an_unreadable_deadline_fails_open_rather_than_dropping_to_idle(tmp_path):
    """C4. The window is computed from the artifacts. When the deadline is the
    field that failed, `_window` sees None, answers "idle", and applies the 6 h
    bar — so on deadline day a five-hour-old projection reads as fine. Partial
    corruption must fail open, not quietly relax the standard."""
    import json

    (tmp_path / "meta.json").write_text(json.dumps({
        "generated_at": "2026-08-21T12:30:00Z",
        "deadline": "not a timestamp",
    }), encoding="utf-8")
    state = schedule.read_published_state(tmp_path)
    assert state["generated_at"] is not None, "the readable half still parses"
    assert state["deadline"] is None

    d = schedule.should_refresh(
        datetime(2026, 8, 21, 17, 25, tzinfo=UTC),
        deadline=state["deadline"], last_generated_at=state["generated_at"],
        fixture_states=state["fixture_states"], degraded=state["degraded"])
    assert d.should_refresh is True
    assert "unreliable" in d.reason


def test_an_absent_file_is_not_corruption(tmp_path):
    """The other half of C4, and the reason it is not simply "any failure fails
    open": `live.json` does not exist before the first run, and a deadline is
    legitimately absent at the end of a season. Treating absence as corruption
    would refresh every 15 minutes forever."""
    import json

    (tmp_path / "meta.json").write_text(json.dumps({
        "generated_at": "2026-08-21T12:30:00Z",
    }), encoding="utf-8")                       # no deadline key, no live.json
    state = schedule.read_published_state(tmp_path)
    assert state["degraded"] is None
    assert state["fixture_states"] == []


# --------------------------------------------------------------------------
# Refresh gate — the CLI the workflow calls
# --------------------------------------------------------------------------

def test_cli_emits_github_output_lines(tmp_path, capsys):
    import json

    (tmp_path / "meta.json").write_text(json.dumps({
        "deadline": "2026-08-21T17:30:00Z",
        "generated_at": "2026-08-21T16:50:00Z",
    }), encoding="utf-8")
    code = schedule.main(["--should-refresh", "--data-dir", str(tmp_path),
                          "--now", "2026-08-21T17:00:00Z"])
    out = capsys.readouterr().out
    assert code == 0, "the gate must never fail the build"
    assert "refresh=false" in out and "window=final_approach" in out


def test_cli_inside_the_cutoff_stops_asking(tmp_path, capsys):
    """Ten minutes out, a run would publish after the deadline for a gameweek you
    can no longer change. The window is deliberately shut."""
    import json

    (tmp_path / "meta.json").write_text(json.dumps({
        "deadline": "2026-08-21T17:30:00Z",
        "generated_at": "2026-08-21T11:45:00Z",
    }), encoding="utf-8")
    schedule.main(["--should-refresh", "--data-dir", str(tmp_path),
                   "--now", "2026-08-21T17:20:00Z"])
    assert "window=idle" in capsys.readouterr().out


def test_cli_force_always_refreshes(tmp_path, capsys):
    code = schedule.main(["--should-refresh", "--force",
                          "--data-dir", str(tmp_path)])
    assert code == 0
    assert "refresh=true" in capsys.readouterr().out


def test_cli_exits_zero_even_with_no_artifacts(tmp_path, capsys):
    code = schedule.main(["--should-refresh", "--data-dir", str(tmp_path / "nope")])
    assert code == 0
    assert "refresh=true" in capsys.readouterr().out


def test_cli_survives_wrong_shaped_artifacts(tmp_path, capsys):
    """C2, end to end. The workflow reads this line and skips the pipeline on
    anything but `refresh=true`, so the CLI answering at all is the guarantee."""
    (tmp_path / "meta.json").write_text("[]", encoding="utf-8")
    (tmp_path / "live.json").write_text("[]", encoding="utf-8")
    code = schedule.main(["--should-refresh", "--data-dir", str(tmp_path)])
    assert code == 0
    assert "refresh=true" in capsys.readouterr().out


def test_cli_still_answers_when_the_gate_itself_explodes(tmp_path, capsys,
                                                          monkeypatch):
    """The catch-all is load-bearing, not defensive habit: an unhandled exception
    anywhere in here prints nothing, and a missing `refresh=true` reads to the
    workflow exactly like a considered no."""
    def boom(*a, **k):
        raise RuntimeError("unanticipated")

    monkeypatch.setattr(schedule, "read_published_state", boom)
    code = schedule.main(["--should-refresh", "--data-dir", str(tmp_path)])
    out = capsys.readouterr()
    assert code == 0
    assert "refresh=true" in out.out
    assert "unanticipated" in out.err, "and it says so, loudly"


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Decision logic
# --------------------------------------------------------------------------

@pytest.mark.parametrize("days", [0, 1, 10, 30, 44])
def test_healthy_repository_needs_no_keepalive(days):
    d = schedule.evaluate(ago(days), NOW)
    assert d.should_act is False
    assert d.days_since_push == days
    assert d.days_until_disable == schedule.GITHUB_DISABLE_DAYS - days


@pytest.mark.parametrize("days", [45, 50, 59, 61, 200])
def test_quiet_repository_triggers_the_keepalive(days):
    d = schedule.evaluate(ago(days), NOW)
    assert d.should_act is True
    assert "investigate" in d.reason


def test_threshold_boundary_is_exact():
    assert schedule.evaluate(ago(44.9), NOW).should_act is False
    assert schedule.evaluate(ago(45), NOW).should_act is True


def test_monthly_cadence_always_beats_the_disable_window():
    """A monthly run is at most 31 days apart, so the 45-day threshold is
    always crossed before the 60-day limit — even if one run is missed."""
    assert schedule.KEEPALIVE_THRESHOLD_DAYS + 31 > schedule.GITHUB_DISABLE_DAYS
    assert schedule.KEEPALIVE_THRESHOLD_DAYS < schedule.GITHUB_DISABLE_DAYS


def test_unreadable_timestamp_fails_closed():
    """A broken check must not let the schedule lapse silently."""
    for raw in (None, "", "nonsense", 12345):
        d = schedule.evaluate(raw, NOW)
        assert d.should_act is True
        assert "could not parse" in d.reason


def test_future_timestamp_is_treated_as_fresh():
    d = schedule.evaluate(ago(-5), NOW)
    assert d.should_act is False
    assert "future" in d.reason


def test_real_repository_state_is_within_the_window():
    """The audited value: pushed 2026-07-26, disable due ~2026-09-24."""
    d = schedule.evaluate("2026-07-26T16:32:44Z", datetime(2026, 8, 6, tzinfo=UTC))
    assert d.should_act is False
    assert d.days_since_push == 10
    assert d.days_until_disable == 50


def test_cli_exit_codes():
    assert schedule.main(["--pushed-at", ago(10), "--now", NOW.isoformat()]) == 0
    assert schedule.main(["--pushed-at", ago(50), "--now", NOW.isoformat()]) == 10


def test_cli_json_shape(capsys):
    schedule.main(["--pushed-at", ago(50), "--now", NOW.isoformat(), "--json"])
    import json
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "should_act", "days_since_push", "days_until_disable", "reason"}
    assert payload["should_act"] is True


def test_render_is_human_readable():
    assert "KEEPALIVE REQUIRED" in schedule.render(schedule.evaluate(ago(50), NOW))
    assert "no action needed" in schedule.render(schedule.evaluate(ago(5), NOW))


# --------------------------------------------------------------------------
# Workflow structure (static checks — no remote run required)
# --------------------------------------------------------------------------

WORKFLOWS = config.REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    # A hard import, not importorskip: pyyaml is a declared [dev] dependency, and
    # skipping here would silently stop validating the workflows that publish.
    import yaml

    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_keepalive_workflow_exists_and_parses():
    wf = _load("keepalive.yml")
    assert "keepalive" in wf["jobs"]


def test_keepalive_is_monthly_not_daily():
    """Bounded: at most ~12 runs a year, and it only commits when needed."""
    wf = _load("keepalive.yml")
    crons = [c["cron"] for c in wf[True]["schedule"]]
    assert len(crons) == 1
    # day-of-month field must be a specific day, not '*'
    assert crons[0].split()[2] != "*", "a daily keepalive would be churn"


def test_workflow_dispatch_is_preserved_everywhere():
    for name in ("refresh.yml", "deploy.yml", "keepalive.yml"):
        wf = _load(name)
        assert "workflow_dispatch" in wf[True], f"{name} lost manual dispatch"


def test_keepalive_requests_minimum_permissions():
    wf = _load("keepalive.yml")
    assert wf["permissions"] == {"contents": "write"}


def test_keepalive_cannot_trigger_a_deploy_loop():
    """The committed path must sit outside deploy.yml's trigger filter."""
    deploy = _load("deploy.yml")
    paths = deploy[True]["push"]["paths"]
    body = (WORKFLOWS / "keepalive.yml").read_text(encoding="utf-8")
    assert ".github/last-activity.json" in body
    assert not any(p.startswith(".github") for p in paths)


def test_keepalive_has_a_timeout():
    wf = _load("keepalive.yml")
    assert wf["jobs"]["keepalive"]["timeout-minutes"] <= 30


def test_a_failed_gate_job_cannot_stop_the_refresh():
    """C2, at the workflow level. The Python gate now answers whatever happens,
    but a gate *job* that never got to run Python — bad checkout, setup-python
    outage, the 5-minute timeout — emits no outputs, and `== 'true'` reads that
    exactly like a considered no. Only an explicit `false` may stop the run."""
    cond = _load("refresh.yml")["jobs"]["refresh"]["if"]
    assert "!= 'false'" in cond, (
        "the refresh job must fail OPEN when the gate did not decide; "
        f"found: {cond}")
    assert "== 'true'" not in cond
    assert "cancelled()" in cond, "an explicit cancel should still stop it"


def test_refresh_still_gates_publishing():
    """Batch 1 behaviour must survive Batch 2."""
    wf = _load("refresh.yml")
    steps = wf["jobs"]["refresh"]["steps"]
    names = [s.get("name", "") for s in steps]
    runs = " ".join(s.get("run", "") for s in steps)
    assert "Backend tests" in names and "Lint" in names
    assert "gaffer.contract" in runs
    assert "pip install -e" in runs
    # The no-diff failure must remain scheduled-only.
    fail = next(s for s in steps if "published nothing" in s.get("name", ""))
    assert "schedule" in fail["if"]


# --- the pull-request gate ---------------------------------------------------
#
# Before ci.yml existed, `deploy.yml` ran only on pushes to main and
# `refresh.yml` only on a schedule. A pull request therefore had NO required
# check: "all green" meant "nothing ran". These tests are about the trigger as
# much as the steps.

def _ci() -> dict:
    import yaml

    from gaffer import config
    return yaml.safe_load(
        (config.REPO_ROOT / ".github" / "workflows" / "ci.yml")
        .read_text(encoding="utf-8"))


def _steps(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) + str(s.get("uses", ""))
                     for s in job["steps"])


def test_ci_runs_on_pull_request():
    on = _ci().get(True) or _ci().get("on")
    assert "pull_request" in on, "a PR would have no required check"
    assert on["pull_request"]["branches"] == ["main"]


def test_ci_installs_from_the_lock_and_checks_it():
    backend = _steps(_ci()["jobs"]["backend"])
    assert "pip install -r requirements.lock.txt" in backend
    assert "pip install -e . --no-deps" in backend
    assert "python -m gaffer.deps" in backend


def test_ci_runs_every_local_gate():
    backend = _steps(_ci()["jobs"]["backend"])
    for gate in ("ruff check", "pytest", "gaffer.contract",
                 "gaffer.mcp_server --self-test"):
        assert gate in backend, f"CI does not run {gate}"


def test_ci_validates_both_artifact_trees_and_their_agreement():
    backend = _steps(_ci()["jobs"]["backend"])
    assert "--data-dir data" in backend
    assert "--data-dir web/public/data" in backend
    assert "cmp -s" in backend, "the two trees are never compared"


def test_ci_makes_no_live_fpl_call():
    """A PR must not be judged on whether the FPL API is up."""
    text = str(_ci())
    for banned in ("gaffer.pipeline", "fantasy.premierleague.com",
                   "bootstrap-static"):
        assert banned not in text, f"PR CI reaches {banned}"


def test_ci_builds_the_frontend_before_it_tests_it():
    """perf.test.ts measures dist/ and skips itself when it is absent."""
    names = [s.get("name", "") + str(s.get("run", ""))
             for s in _ci()["jobs"]["frontend"]["steps"]]
    build = next(i for i, n in enumerate(names) if "npm run build" in n)
    test = next(i for i, n in enumerate(names) if "npm run test" in n)
    assert build < test, "the performance budgets would not run"


def test_ci_pins_the_same_versions_as_everything_else():
    """Read the parsed YAML, not its repr: `python-version-file` is a key."""
    jobs = _ci()["jobs"]
    setup = [s for s in jobs["backend"]["steps"]
             if "setup-python" in str(s.get("uses", ""))]
    assert setup and setup[0]["with"]["python-version-file"] == ".python-version"
    node = [s for s in jobs["frontend"]["steps"]
            if "setup-node" in str(s.get("uses", ""))]
    assert node and node[0]["with"]["node-version-file"] == "web/.nvmrc"


def test_ci_refuses_a_committed_secret_or_binary():
    backend = _steps(_ci()["jobs"]["backend"])
    for guard in ("gaffer.local.toml", ".env", "data/history/", "joblib"):
        assert guard in backend, f"CI does not guard against {guard}"


def test_ci_has_read_only_permissions():
    assert _ci()["permissions"] == {"contents": "read"}
