"""T-08 — schedule-continuity decision logic and workflow structure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gaffer import config, schedule

NOW = datetime(2026, 10, 1, 3, 17, tzinfo=UTC)


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
