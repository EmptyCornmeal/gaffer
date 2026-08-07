# Running Gaffer's notification check on the Mac Mini

**Nothing here is installed or activated by Gaffer.** These are the commands and
the template; running them is a deliberate act by you.

The notification engine is dry-run by default. It will not deliver anything
until *both* of the following are true:

1. you pass `--send`, **and**
2. a provider is configured and validated.

There is no config file setting and no environment variable that turns sending on
by itself.

---

## 0. Set the environment up the same way CI does

Same Python, same pinned versions, on all three machines. `.python-version` says
which interpreter; `requirements.lock.txt` says which packages.

```sh
cd /path/to/gaffer
python3.14 -m venv .venv                              # matches .python-version
.venv/bin/pip install -r requirements.lock.txt
.venv/bin/pip install -e . --no-deps                  # -e: artifacts must land in the checkout
.venv/bin/python -m gaffer.deps                       # confirm: lock == pyproject == environment
```

`--no-deps` on the second step matters: without it pip may re-resolve past the
lock and this box quietly stops matching the one that produced the artifacts.

Verify the whole thing at once with `.venv/bin/python scripts/verify.py`.

## 1. Check it works, sending nothing

```sh
cd /path/to/gaffer
.venv/bin/python -m gaffer.notify
```

You will see `DRY RUN — nothing was sent` and the alerts that *would* have gone
out. Run this as often as you like; it is read-only apart from its own dedupe
table.

To see the JSON the pipeline publishes:

```sh
.venv/bin/python -m gaffer.notify --json
```

## 2. Configure a provider

The `webhook` sink posts JSON to any HTTPS endpoint — Discord, Slack, ntfy,
Telegram (via a bot webhook), Home Assistant. One adapter covers all of them.

```sh
export GAFFER_NOTIFY_SINK=webhook
export GAFFER_NOTIFY_WEBHOOK='https://…your endpoint…'
```

Verify the configuration is complete *before* you ever pass `--send`:

```sh
.venv/bin/python -m gaffer.notify --json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["config"])'
```

`configured: true` means the required variables are present. It reports presence
only — Gaffer never logs, prints or publishes the value of a credential.

Put the exports in a file the launchd job can read (see step 4). **Do not commit
it.** `.env` is already git-ignored.

## 3. Send for real, once, by hand

```sh
.venv/bin/python -m gaffer.notify --send
```

If the sink is not configured this exits `2` and sends nothing. Do this manually
at least once before scheduling anything.

## 4. Schedule it (optional, and only when you are ready)

`com.gaffer.notify.plist.template` in this directory is a launchd job. It is a
**template**: it will not load until you fill in the two paths, and it validates
them at install time rather than failing silently at 07:00 on a Saturday.

```sh
# 1. Render the template with real paths (the script refuses if they don't exist)
./install.sh /Users/you/gaffer

# 2. Inspect what it produced — do not skip this
cat ~/Library/LaunchAgents/com.gaffer.notify.plist

# 3. Load it, when YOU decide to
launchctl load ~/Library/LaunchAgents/com.gaffer.notify.plist
```

To stop it:

```sh
launchctl unload ~/Library/LaunchAgents/com.gaffer.notify.plist
```

`install.sh` renders and validates. **It does not call `launchctl`** — loading
the job is the one step that is always yours.

## What it will alert on

| Alert | Severity | Fires when |
|---|---|---|
| Deadline reminder | important → critical | 24h, 3h and 1h before the deadline (once each) |
| Owned player flagged | critical / important | an owned player's status or chance-of-playing changes |
| Recommendation changed | important | the *action* changed, not the decimals |
| Captain changed | important | the recommended armband moved |
| Squad-state failure | important | Gaffer could not read your team |
| Stale data | important | the last successful publish is over 36h old |
| League swing | info | a differential moved your mini-league by 8+ points |
| Chip window | important | an unused chip expires within 3 gameweeks |

**Not implemented: price-change alerts.** Gaffer's price estimate is a documented
heuristic over net transfers against a guessed threshold; FPL's real thresholds
are secret. Alerting on it would present a guess as a forecast. A validated price
source is a prerequisite, not a to-do.

## Quiet hours

22:30–07:30 **Europe/London**, computed from the timezone database so the BST/GMT
change is handled without a seasonal edit. `critical` alerts ignore it — a
deadline you are about to miss is worth a buzz. Override with
`--no-quiet-hours`.

## If a provider breaks

A delivery failure is recorded against the alert (`state=failed`, with an attempt
count) and retried up to three times across runs. It never propagates: the data
pipeline publishes whether or not your phone buzzed. Check state with:

```sh
.venv/bin/python -m gaffer.notify --json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["summary"]["by_state"])'
```
