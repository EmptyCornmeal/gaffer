#!/usr/bin/env python3
"""Backstop GitHub's scheduler, and keep the mini's checkout current.

Gaffer's pipeline runs in GitHub Actions, not here (docs/STATE.md). Two things
about that arrangement broke silently on 2026-08-28 and this job fixes both.

1. **The scheduler stopped.** `refresh.yml` asks for `*/15 * * * *` plus three
   fixed daily crons. Between 13:44Z and 19:35Z not one of them fired -- the
   `0 17 * * *` belt-and-braces tick included -- straight through the GW2
   deadline at 17:30Z. GitHub's scheduler is best-effort and drops ticks under
   load; nothing in the repo notices, because a cron that never runs produces
   no failed run to look at. Silence read as health.

   The detector is therefore **the age of the last refresh RUN, not the age of
   the last data commit.** Those are different questions. The refresh gate
   deliberately no-ops when nothing needs refreshing -- 10-second runs, hours
   apart, with `generated_at` legitimately unmoved -- so stale artifacts are
   normal and prove nothing. A workflow that has not *started* in 90 minutes,
   when it is asked to start every 15, is the actual fault.

2. **Nothing pulled.** The MCP server reads `GAFFER_REPO_ROOT` -- this
   checkout -- while every refresh commit lands on origin. The tree sat 30
   commits behind, so Claude answered from a build that still thought GW1 was
   unfinished.

Read-only towards GitHub apart from `gh workflow run`. It never pushes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(os.environ.get("GAFFER_REPO_ROOT", Path.home() / "Projects" / "Gaffer"))
STATE_PATH = Path.home() / ".local" / "state" / "gaffer-watchdog.json"
LOG_PATH = Path.home() / "Library" / "Logs" / "gaffer-watchdog.log"
DISCORDCTL = Path.home() / "bin" / "discordctl"
DISCORD_CHANNEL = "gaffer"

# The schedule asks for a tick every 15 minutes. Six missed ticks is not drift.
STALE_RUN_MINUTES = 90
# A forced dispatch runs the whole pipeline. Do not do that on every wake-up
# while Actions is down -- once an hour is enough to keep the site current.
DISPATCH_COOLDOWN_MINUTES = 60
# Announcing is a separate decision from rescuing, on a separate clock.
#
# Rescuing is cheap and should be eager. Announcing is not: a channel that
# reports every rescue posts an alert AND a stand-down per cycle, which at the
# throttle observed on 2026-08-29 (GitHub firing roughly every 100 minutes
# instead of every 15) is about fourteen messages a day, all of them saying
# the same thing. A channel nobody reads is worse than no channel, and that is
# the exact failure Job Radar's health alerts were built to avoid.
#
# It cannot be a bigger number on the SAME clock either: the age below counts
# the last run of any kind, our own dispatches included, so once the watchdog is
# rescuing hourly that age can never grow past ~110 minutes however dark GitHub
# goes. Raising the threshold would not damp the alert, it would delete it.
#
# So alerting reads the age of the last SCHEDULE-triggered run, which our
# dispatches do not reset. Silence here means GitHub itself has stopped, which
# is the only thing worth waking someone for.
ALERT_SCHEDULE_SILENT_MINUTES = 240


def log(msg):
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} {msg}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)


def run(*args, cwd=None):
    return subprocess.run(
        args, cwd=cwd or REPO, capture_output=True, text=True, timeout=180
    )


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def announce(msg):
    """Post to #gaffer. Best effort: a dead webhook must not stop the sync."""
    if not DISCORDCTL.exists():
        log(f"no discordctl, not announcing: {msg}")
        return
    proc = subprocess.run(
        [str(DISCORDCTL), "post", DISCORD_CHANNEL, msg],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        log(f"discord post failed rc={proc.returncode}: {proc.stderr.strip()[:200]}")


def last_run_age_minutes(event=None):
    """Minutes since the most recent refresh.yml run STARTED, or None if unknown.

    ``event`` filters by trigger. Filtering happens here rather than through
    ``gh --event`` so that an older gh without the flag degrades to a wrong
    answer nobody notices -- it does not, because we never pass the flag.
    """
    proc = run(
        "gh", "run", "list",
        "--workflow=refresh.yml", "--limit", "30",
        "--json", "createdAt,status,conclusion,event",
    )
    if proc.returncode != 0:
        log(f"gh run list failed rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        return None
    try:
        runs = json.loads(proc.stdout)
    except ValueError:
        log("gh run list returned unparseable JSON")
        return None
    if event is not None:
        runs = [r for r in runs if r.get("event") == event]
    if not runs:
        # For a filtered query this is a real answer, not a missing one: within
        # the window we can see, GitHub has not fired once. Report it as the
        # width of that window so the caller alerts rather than shrugging.
        if event is not None:
            return float("inf")
        log("no refresh runs exist at all")
        return None
    started = datetime.fromisoformat(runs[0]["createdAt"].replace("Z", "+00:00"))
    return (datetime.now(UTC) - started).total_seconds() / 60.0


def sync_checkout():
    """Fast-forward this tree to origin/main so the MCP reads what the site does.

    Refuses to touch a dirty or diverged tree -- unpushed work here has been
    stranded before, and quietly rebasing it under a cron is how you lose it.
    """
    dirty = run("git", "status", "--porcelain").stdout.strip()
    if dirty:
        return f"dirty: {len(dirty.splitlines())} uncommitted path(s), not syncing"

    ahead = run("git", "rev-list", "--count", "origin/main..HEAD").stdout.strip()
    if ahead not in ("", "0"):
        return f"diverged: {ahead} local commit(s) not on origin/main, not syncing"

    behind = run("git", "rev-list", "--count", "HEAD..origin/main").stdout.strip()
    if behind in ("", "0"):
        return "already current"

    proc = run("git", "merge", "--ff-only", "origin/main")
    if proc.returncode != 0:
        return f"ff-only merge failed: {proc.stderr.strip()[:200]}"
    return f"pulled {behind} commit(s)"


BAD_SYNC = ("dirty", "diverged", "ff-only")


def main():
    if not (REPO / "src" / "gaffer").is_dir():
        log(f"not a Gaffer checkout: {REPO}")
        return 2

    state = load_state()
    now = datetime.now(UTC)

    fetch = run("git", "fetch", "--quiet", "origin", "main")
    if fetch.returncode != 0:
        log(f"git fetch failed rc={fetch.returncode}: {fetch.stderr.strip()[:200]}")

    age = last_run_age_minutes()
    stalled = age is not None and age > STALE_RUN_MINUTES

    if stalled:
        last = state.get("last_dispatch")
        cooling = False
        if last:
            since = (now - datetime.fromisoformat(last)).total_seconds() / 60.0
            cooling = since < DISPATCH_COOLDOWN_MINUTES
        if cooling:
            log(f"scheduler stalled ({age:.0f}m) but within dispatch cooldown")
        else:
            proc = run("gh", "workflow", "run", "refresh.yml")
            if proc.returncode == 0:
                state["last_dispatch"] = now.isoformat(timespec="seconds")
                log(f"scheduler stalled ({age:.0f}m) -- dispatched refresh.yml")
                # Rescued either way; whether to SAY so is a separate question,
                # asked of GitHub's own clock rather than of ours.
                sched_age = last_run_age_minutes(event="schedule")
                if (sched_age is not None
                        and sched_age > ALERT_SCHEDULE_SILENT_MINUTES
                        and not state.get("alerted")):
                    hours = sched_age / 60.0
                    since = ("in the last 30 runs" if sched_age == float("inf")
                             else f"for {hours:.1f} hours")
                    announce(
                        f"Gaffer: GitHub's scheduler has not fired refresh.yml "
                        f"{since} (it is scheduled every 15 minutes). The Mac "
                        f"mini is dispatching runs by hand to keep the site "
                        f"current, and will keep doing so silently."
                    )
                    state["alerted"] = True
            else:
                log(f"dispatch FAILED rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    else:
        if state.get("alerted"):
            sched_age = last_run_age_minutes(event="schedule")
            if sched_age is not None and sched_age <= ALERT_SCHEDULE_SILENT_MINUTES:
                announce("Gaffer: GitHub's scheduler is firing again. "
                         "Watchdog standing down.")
                state["alerted"] = False
        if age is not None:
            log(f"scheduler healthy (last run {age:.0f}m ago)")

    # Sync after the dispatch decision: a run dispatched just now is still in
    # flight, so this pass collects the PREVIOUS one and the next wake-up
    # collects today's.
    result = sync_checkout()
    log(f"checkout: {result}")
    if result.startswith(BAD_SYNC):
        if not state.get("sync_alerted"):
            announce(
                f"Gaffer: the Mac mini checkout is not tracking origin/main "
                f"({result}). The MCP will serve stale artifacts until this is "
                f"resolved."
            )
            state["sync_alerted"] = True
    else:
        state["sync_alerted"] = False

    state["last_check"] = now.isoformat(timespec="seconds")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
