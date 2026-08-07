# Release checklist

**Batch 7 executed this list.** It is kept as the runbook for the next release
and as the record of what was done: branch `release/gaffer-v1`, two commits, a
pull request gated by `.github/workflows/ci.yml`, then a merge and a manual
refresh. Re-read it before shipping anything else.

Deadline for GW1: **2026-08-21 17:30 BST**.

---

## 1. Review the change

```bash
git status --short                    # ~130 paths
git diff --stat                       # tracked changes
git ls-files --others --exclude-standard   # new files
python scripts/verify.py --all        # deps, ruff, pytest, contract, web
```

Read `docs/MODEL-EVALUATION.md` before anything else. It is the one change that
alters what the product *claims*: no ML, and the `ep_next` blend weight
relabelled as a policy choice rather than a measurement.

## 2. Confirm the generated artifacts are the ones you want

```bash
python -m gaffer.contract --max-age-hours 8760
python -m gaffer.contract --data-dir web/public/data --max-age-hours 8760
python -m gaffer.season                            # identity: same_season
```

- 13 artifacts in each directory, byte-identical between them.
- `data/backtest.json` is **schema 5**, reports only `gaffer` and `naive` as
  measured, keeps `fpl_xp` and `ensemble` under `withdrawn_baselines`, and
  carries one record per model candidate with its own verdict.
- `data/news.json` is `news-2.0`: every claim cites an item present in the same
  file, and no claim text contains a URL.
- `verdict.json` and `news.json` both have `source` ∈ {ai, template} with the
  reason in `fallback_reason`, never inside `source`.
- `data/review.json` should **not** exist. No gameweek has finished.
- `data/live.json` should say `available: false, unavailable_reason: not_started`.
- `data/decision.json` should say `action: unavailable`.

Anything else means something fabricated pre-season data.

## 3. Confirm there are no secrets

```bash
git ls-files --others --exclude-standard | xargs grep -nEI "sk-|ANTHROPIC_API_KEY=|Bearer |password|secret" 2>/dev/null
grep -rn "ANTHROPIC" data/ web/public/data/ 2>/dev/null
ls -la .env gaffer.local.toml 2>/dev/null        # both must be git-ignored
```

`.env` and `gaffer.local.toml` are in `.gitignore`. `gaffer.example.toml` is
committed and holds no key.

## 4. Commit

Six batches of overlapping edits cannot be separated into an honest per-batch
history: the same files were rewritten several times, so a reconstructed
sequence would claim states that never compiled. **Two commits**, both true:

1. `feat: complete Gaffer decision engine and grounded AI interface` — code,
   tests, workflows, manifests, docs, deletions, the MCP server. No generated
   artifacts, no local config.
2. `data: publish validated 2026-27 artifacts` — `data/*.json` and
   `web/public/data/*.json` only.

The first commit's body must state that the `ep_next` blend weight is
unvalidated and that GBM was rejected while ridge was inconclusive, because
those are the facts a future reader will need and the ones easiest to lose.

```bash
git checkout -b release/gaffer-v1
git add <code paths>   && git commit    # inspect `git diff --cached --stat` first
git add data web/public/data && git commit
```

Do **not** commit `data/history/` (git-ignored, 20 MB) or `data/gaffer.db`.

## 5. Push and open the pull request

```bash
git push -u origin release/gaffer-v1
gh pr create --title "..." --body-file <notes>
```

A branch rather than `main`, so `ci.yml` runs and nothing deploys before you have
looked at it. Wait for every required check; fix genuine failures on the branch.
Never weaken a test or a workflow to turn a check green.

## 6. First GitHub Actions refresh

```bash
gh workflow run refresh.yml
gh run watch
```

Check, in order:

- [ ] `Install Gaffer` used `pip install -r requirements.lock.txt`
- [ ] `Dependency check` passed — this is the first time the lock has been
      installed on Linux
- [ ] `setup-python` resolved **3.14** from `.python-version`
- [ ] backend tests and `ruff` passed in CI
- [ ] the pipeline logged `[artifacts] target directory:` **inside the checkout**,
      not `site-packages`
- [ ] the artifact contract passed
- [ ] the run **committed** a data change (a run with no diff fails by design)
- [ ] `deploy.yml` was dispatched

If the run fails on the dependency check, the lock resolved to something with no
Linux wheel. `python -m gaffer.deps --regenerate-hint` and re-lock.

## 7. First Pages deployment

- [ ] the PR's own `ci.yml` run was green before merging — backend, front-end,
      MCP self-test, artifact contract on both trees, wheel content
- [ ] `deploy.yml` used **Node 22** from `web/.nvmrc`
- [ ] `npm ci` succeeded from the regenerated lockfile
- [ ] `npm run check` reported **0 errors, 0 warnings**
- [ ] the build published; initial JS ≈ 48 kB gzip

## 8. Validate the live site

Open <https://emptycornmeal.github.io/gaffer/>:

- [ ] the freshness chip is neutral (< 12 h), not amber or red
- [ ] `meta.json` `generated_at` is within a few hours of the run
- [ ] **Home** leads with the decision, and says "we do not know your squad yet"
- [ ] **Accuracy** renders schema 4, shows the withdrawn baselines, and says the
      blend weight is a policy choice
- [ ] **Live** says the gameweek has not started
- [ ] **Review** is absent or says there is nothing to review yet
- [ ] **Strategy** shows no manufactured league probabilities
- [ ] Accuracy shows **all three** model candidates with their own verdicts —
      GBM rejected, ridge inconclusive, xP models invalid — not one collapsed
      "trained models lost" line
- [ ] Accuracy says the `ep_next` blend weight is a policy choice
- [ ] Home says the action threshold is not fitted
- [ ] every News claim carries at least one source link
- [ ] no page shows an ML claim

## 9. Confirm the schedule and the notification engine

- [ ] three scheduled runs land in the next 24 h
- [ ] each one commits, so the 60-day inactivity clock keeps resetting
- [ ] `data/notifications.json` says `dry_run: true`
- [ ] nothing has been delivered anywhere

The Mac Mini job is **not installed**. If you want it, read
`deploy/macmini/README.md` and run `install.sh` yourself; `launchctl load` is
always your call, and the job carries no `--send` flag even once loaded.

## 10. GW1 checks (2026-08-21 17:30 BST)

The things that cannot be validated before a real gameweek:

- [ ] before the deadline: `decision.json` has a real action, and
      `decision_snapshots` holds a pre-deadline row
- [ ] **at** the deadline: the snapshot stops changing (`locked`)
- [ ] during the matches: `live.json` populates; confirmed points, provisional
      bonus and predicted-remaining are three separate numbers
- [ ] check one provisional bonus by hand against the FPL app, including a tie
- [ ] check one autosub by hand
- [ ] rival scores appear and the league swing names a differential, not a
      shared player
- [ ] after the gameweek: `review.json` appears, luck is measured against the
      stored distribution, and no lesson is claimed from one gameweek
- [ ] `player_gw` and `projection_snapshots` both have GW1 rows

Then, and only then, the `ep_next` blend weight has real data behind it. Revisit
`config.EP_NEXT_BLEND_WEIGHT` after ~10 gameweeks.

## 11. Rollback

**The site.** Pages serves whatever `data/` holds on the deployed commit.

```bash
git revert <commit>                  # or: git checkout <good-sha> -- data web/public/data
git push                             # deploy.yml redeploys the old artifacts
```

**A bad artifact only.** Restore the previous JSON and push; the pipeline will
overwrite it on the next run, so fix the cause first or disable the schedule:

```bash
gh workflow disable refresh.yml
```

**The database.** `data/gaffer.db` is git-ignored, so a revert does not touch it.
Delete it and re-run the pipeline to rebuild from the API — only
`projection_snapshots`, `player_gw`, `decision_snapshots` and `gw_reviews` are
irreplaceable, and those are exactly what a rollover preserves.

**A season rollover.** `python -m gaffer.season --rollover --confirm` writes a
verified backup to `data/backups/` first and prints its path. To undo:

```bash
cp data/backups/gaffer-<season>-<stamp>.db data/gaffer.db
```

Nothing is ever deleted: the outgoing season stays in `<table>_<season>` tables
even after a successful rollover.

**Notifications.** Nothing to roll back. Nothing was ever sent.

**The MCP server.** Nothing to roll back: it is read-only, local, and holds no
state. To remove it from a client, `claude mcp remove gaffer` (or delete the
entry from `claude_desktop_config.json`) and restart.
