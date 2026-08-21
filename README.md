# Gaffer ⚽

An **FPL decision engine** — successor to [`fpl-assistant`](https://github.com/EmptyCornmeal/fpl-assistant).

Last season's tool showed you data and left you to decide. Gaffer *decides*: it
projects every player's points with a transparent, minutes-shrunk model, runs a
solver to pick the optimal squad / captain / transfers, and serves it in a
phone-first UI. Everything runs on **free** data (the official FPL API — which
already carries Opta xG/xA/xGC and defensive-contribution) — cost only enters at
the optional AI layer.

## How it works

```
FPL API ──► Python pipeline ──► SQLite ──► JSON artifacts ──► Svelte front-end
            (ingest → project → optimise → export)            (GitHub Pages)
```

- **Ingest** (`gaffer.ingest`) — teams, players (Opta per-90 rates + DEFCON,
  the latter enriched from last season's `history_past`), fixtures, your picks.
- **Project** (`gaffer.model`) — component model: minutes gate → goals/assists
  (fixture-adjusted xGI) → clean sheet (Poisson on expected goals conceded) →
  DEFCON (Poisson survival vs the threshold) → bonus. Small samples are
  empirical-Bayes shrunk toward priors, and every projection carries a
  confidence read. Projects the next 6 GWs (handles blanks/doubles).
- **Optimise** (`gaffer.solver`) — a PuLP MILP over one shared objective
  (`solver.objective`), so the single-window optimiser and the multi-gameweek
  planner cannot disagree. *Build mode* picks the optimal £100m squad;
  *transfer mode* trades expected-point gains against −4 hits. Respects budget,
  position quotas, 3-per-club, formation, captaincy.
- **Simulate** (`gaffer.model.scenarios`) — a match is drawn **once** and every
  player in it is conditioned on that draw, so teammates are positively
  correlated, an opponent's attack is negatively correlated with your clean
  sheet, and goals/clean sheets/goals-conceded can never contradict each other.
  Every probability downstream reads this one `ScenarioSet`.
- **Strategy** (`gaffer.strategy`) — league-scoped effective ownership from
  rival squads (never global `selected_by_percent`), placing probabilities,
  situational posture, multi-league conflicts, and chip values measured in the
  same simulated football. Contained: a league API outage costs you the league
  analysis and nothing else.
- **Decide** (`gaffer.decision`, `gaffer.weekly`) — the week's *one* action:
  transfer, roll, or an honest "we cannot tell you", compared against a legal
  hold baseline in the same scenarios, with an uncertainty-aware threshold so a
  0.3-point edge is reported as too close to call rather than as advice. Every
  pre-deadline recommendation is written to an **immutable snapshot**
  (`gaffer.snapshots`) that no later run can rewrite.
- **Live** (`gaffer.live`) — during a gameweek: confirmed points, provisional
  bonus computed from live BPS with FPL's real tie rules, and predicted points
  for players yet to play, kept separate all the way to the UI. Autosubs and
  captain-to-vice fallback follow the actual substitution rules.
- **Review** (`gaffer.review`) — after it: what was advised, what you did, what
  holding would have scored, and where the result landed in the distribution
  published *before* the deadline. Decision quality and outcome luck are judged
  independently, so a good call that lost is not recorded as a mistake.
- **Notify** (`gaffer.notify`) — deadline, injury, changed-recommendation and
  chip-window alerts, deduplicated on the fact rather than the clock, with
  Europe/London quiet hours. **Dry-run by default and shipped inactive**; see
  `deploy/macmini/`.
- **Export** (`gaffer.export`) — denormalised `data/*.json` the front-end reads,
  gated by `gaffer.contract` in `refresh.yml` and `ci.yml` before anything is
  published. Every write announces its targets first, and `--dry-run` skips the
  artifacts, the verdict and the news (it still writes SQLite rows — snapshot,
  review and notification dedupe — so it is not yet fully dry).

## Quick start

**Python 3.14** (`.python-version`; `pyproject.toml` supports ≥3.12) and
**Node ≥22.12** (`web/.nvmrc`). CI, this machine and the Mac Mini all install
from the same two lockfiles.

```bash
# backend — install from the lock, then the package itself without re-resolving
python -m venv .venv
.venv/Scripts/pip install -r requirements.lock.txt   # .venv/bin/pip on macOS
.venv/Scripts/pip install -e . --no-deps             # -e is required: see "Paths"

.venv/Scripts/python -m gaffer.deps            # lock ↔ pyproject ↔ environment
.venv/Scripts/python -m gaffer.pipeline --dry-run   # print the target files, write nothing
.venv/Scripts/python -m gaffer.pipeline        # writes data/*.json  (--fast skips DEFCON enrichment)
.venv/Scripts/python -m gaffer.contract        # validate the artifacts it just wrote
.venv/Scripts/python -m gaffer.notify          # DRY RUN: what would be alerted, sent nowhere
.venv/Scripts/python -m gaffer.season          # season identity + rollover preview

# front-end
cd web && npm ci                               # exact versions from package-lock.json
cp ../data/*.json public/data/                 # stage artifacts for the dev server
npm run dev                                    # http://localhost:5173
```

The gitignored `web/shot*.mjs` screenshot helpers need `npm i -D puppeteer-core`
— it was removed from `package.json` because no tracked file imports it.

**One command for all of it:**

```bash
.venv/Scripts/python scripts/verify.py --all
```

Runs the dependency check, `ruff`, the backend suite, the artifact contract, and
the front-end typecheck/tests/build — the same gates, in the same order, as CI.
Writes nothing, sends nothing, touches no git state.

### Dependencies

`pyproject.toml` holds the ranges; `requirements.lock.txt` holds what those
ranges resolved to, for production, `[ai]` and `[dev]` in one file. `pulp<4.0`
is a deliberate cap — 4.0 removes `LpVariable(...)` construction and
`PULP_CBC_CMD`, both of which the solver calls on every run.

`python -m gaffer.deps` fails on any of the three ways this drifts: an import
nothing declares (which is how `numpy` went undeclared for months), a lock that
no longer satisfies `pyproject.toml`, or an environment that no longer matches
the lock. `python -m gaffer.deps --regenerate-hint` prints how to rebuild it.
The refresh workflow runs the same check before it publishes.

## Configuration

Copy the documented example and edit it:

```bash
cp gaffer.example.toml gaffer.local.toml       # git-ignored
```

```toml
[fpl]
entry_id = 1066421
league_ids = [271619]      # always a list — multiple leagues are supported
free_transfers = 1
```

Every value is also settable by environment variable, which is what CI uses.
**Precedence, highest first:**

1. environment variable — `GAFFER_ENTRY_ID`, `GAFFER_LEAGUE_IDS` (comma-separated),
   `GAFFER_FREE_TRANSFERS`
2. `gaffer.local.toml`
3. built-in default

Verify what actually resolved:

```bash
python -c "from gaffer import config; print(config.describe_paths()); print(config.Settings.load())"
```

With no entry id the pipeline still runs, but stamps `meta.build_mode = "generic"`
so a non-personalised squad can never be mistaken for one built around your team.

**Paths.** The repo root is discovered from the checkout and `data/` hangs off it;
`GAFFER_REPO_ROOT` / `GAFFER_DATA_DIR` override that. The pipeline refuses to
publish when the data directory is not inside the repository root — deriving it
from the *package* location is what silently wrote artifacts into `site-packages`
under a non-editable install, so scheduled runs published nothing while reporting
success.

**Secrets.** `ANTHROPIC_API_KEY` (optional) goes in a git-ignored `.env` or a
GitHub Actions secret — never in `gaffer.local.toml`, and never in the committed
example.

**Paid AI narration is opt-in and off by default.** A configured key is not
consent to spend: set `GAFFER_AI_NARRATION=1` as well, or the Verdict and News
ship their deterministic templates (same shape, same numbers, same links,
`source: "template"`, `fallback_reason: "narration_disabled"`). The AI layer is a
narrator — it writes prose about numbers the pipeline has already computed and
never calculates, ranks or alters one — so this switch changes the words on the
page and nothing else.

## Deployment

- **GitHub Actions** `.github/workflows/refresh.yml` runs the pipeline on a
  schedule (02:00 / 11:00 / 17:00 UTC). Publishing is gated: backend tests →
  `ruff` → pipeline → artifact contract → commit → dispatch the deploy. A
  scheduled run that produces no publishable diff **fails**, because a valid run
  always advances `generated_at`.
- **GitHub Pages** serves the static Svelte build; `.github/workflows/deploy.yml`
  runs `npm ci` → `npm run check` → `npm run test` → `npm run build` on every push
  touching `web/**` or `data/**`.
- `proxy/` is **live**, not legacy. The static artifacts cannot hold per-user
  data, so your picks, entry history, league standings and live points are
  fetched client-side through a small read-only proxy — `proxy/valtown.ts` is
  the one actually deployed (`DEFAULT_API_BASE` in `web/src/lib/config.ts`), and
  `proxy/worker.js` + `wrangler.toml` are the Cloudflare equivalent, kept as the
  escape hatch if the Val Town quota ever bites. It carries no secrets.
- `deploy/macmini/` holds the **notification** launchd template and a validating
  installer. Nothing there is installed or loaded by Gaffer: the installer
  renders and checks paths, and `launchctl load` is always your call. The
  scheduled job has no `--send` flag, so even once loaded it is a dry run until
  you add one deliberately.

### Keeping the schedule alive

GitHub disables `schedule:` triggers on a public repository after **60 days with
no repository activity**. Workflow *runs* do not count; a push does.

The primary defence is the refresh itself. Every valid run stamps a new
`generated_at` into the artifacts, so there is always a diff to commit, and that
commit is a real push that resets the 60-day clock. In a healthy season no
artificial churn is needed.

`.github/workflows/keepalive.yml` covers the case the refresh cannot: the
pipeline failing (or pushing nothing) for weeks, after which the schedule would
be silently disabled *on top of* the outage. It runs monthly, asks the API for
`pushed_at`, and commits a single `.github/last-activity.json` record **only**
when the repository has been quiet for 45+ days — at most a handful of commits a
year, and normally none. The decision logic lives in `gaffer.schedule` and is
unit-tested; the workflow fails loudly if it cannot read the API.

Assumptions, stated because they cannot be proven without waiting 60 days:
`pushed_at` advances on a `GITHUB_TOKEN` push (it is a repository-level field
updated by any push), and that is the signal GitHub's inactivity timer uses. The
45-day threshold with a monthly cadence leaves 15 days of slack even if one run
is missed. A push to `.github/**` cannot trigger `deploy.yml` (its filter is
`web/**` / `data/**`), and pushes made with `GITHUB_TOKEN` do not start new
workflow runs, so no loop is possible.

## Seasons

FPL reuses element ids every summer: element 328 was one player last season and
is somebody else this one. Most working tables are keyed on that id alone, so
ingesting a new season over an old one does not fail — every row still parses —
it silently rewrites last season's squad as this season's.

`gaffer.season` derives the current season from the API's event deadlines (FPL
publishes no season string) and compares it with the database and the published
artifacts. The gate sits in front of ingest, so a mismatch costs nothing:

```bash
python -m gaffer.season                    # identity + what a rollover would do
python -m gaffer.season --rollover         # preview; still writes nothing
python -m gaffer.season --rollover --confirm
```

A rollover archives `teams`/`players`/`fixtures`/`projections`/`my_squad` into
`<table>_<season>` and recreates them empty. Everything season-keyed —
`player_gw`, `projection_snapshots`, `decision_snapshots`, `gw_reviews`,
`notifications` — is left exactly where it is. It is transactional, backed up and
integrity-checked before it writes, idempotent, refuses a downgrade, and
**deletes nothing**.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the whole thing fits together
- [`docs/MODEL-EVALUATION.md`](docs/MODEL-EVALUATION.md) — why there is no ML, and the `xP` leak
- [`docs/TRACEABILITY.md`](docs/TRACEABILITY.md) — every task, with its evidence and its limits
- [`docs/RELEASE.md`](docs/RELEASE.md) — the ship checklist and how to roll back

## History and evaluation

- **`player_gw`** retains every per-player per-fixture result, keyed by
  `(season, player_id, fixture)` — season-aware because FPL reuses element ids,
  fixture-keyed so double gameweeks store both matches. Populated from the
  `element_summary` history the enrichment step already fetches. Re-ingestion is
  idempotent and upstream corrections overwrite in place.
- **`projection_snapshots`** records what the model projected and when, before
  `projections` is wiped each run. `is_pre_deadline` marks snapshots taken before
  the target event's deadline; only those are a fair basis for scoring.
- **`python -m gaffer.backtest`** evaluates the *shipped* projection — the same
  `TeamContext` and `_project_one_fixture` the live pipeline calls — over
  horizons 1-6, keeping zero-minute outcomes in the population and selecting a
  legal 15 under budget, quota and the club limit. Features come through
  `gaffer.histdata`, which enforces the `gaffer.leakage` contract: post-match
  columns may be evaluation targets, never pre-deadline inputs.

## Is the model any good?

**Honestly: it is the weakest part of the system, and the app says so.** On the
held-out 2025-26 season the component model fields a legal XI worth **49.3 points
a gameweek against the naive baseline's 44.6** — the first season it has won that
comparison. It loses every other one: rank correlation **0.447 vs 0.692**, MAE
**1.592 vs 1.075**, and captaincy **5.87 vs 5.97** points a gameweek. The
Accuracy page publishes all of that, not a flattering subset.

Two things about the baseline, because the earlier wording here got both wrong.
It is **cumulative season-to-date points per game**, not a rolling average of
recent form — a stronger and lower-variance comparator than "rolling average"
suggests. And Gaffer is **behind** it on ordering, not "barely ahead": ranking
players is the one thing a picker needs, and that is exactly where the naive
baseline wins. Figures are h=1 from `data/backtest.json` (schema 7,
`heuristic-0.5`, 29,338 predictions).

Two things are deliberately *not* claimed:

- **No trained points model ships.** Three candidates were built properly —
  same leakage-checked adapter, production-available features only,
  chronological splits, test season untouched until selection closed — and each
  got its own verdict, because they did not produce the same result:
  - **GBM** (gradient-boosted trees) — **rejected**: worse than the heuristic on
    legal-XI points at all six horizons, and much worse on captaincy.
  - **Ridge** (regularised linear) — **inconclusive, not selected**: it *beat*
    the heuristic at h=1 (+2.70 legal-XI points per gameweek, better captaincy),
    but the interval spans zero and the edge is gone by h=2. Not material, not
    durable, not worth a training pipeline.
  - **Models using the archive's `xP`** — **invalid experiment**: the only
    apparent decisive win, built on an inadmissible feature.

  Full study, including the ridge numbers, in
  [`docs/MODEL-EVALUATION.md`](docs/MODEL-EVALUATION.md).
- **The `ep_next` blend weight is a policy choice, not a fitted one.** The
  archive cannot certify its `xP` column as the pre-deadline forecast managers
  saw, and the upstream dataset explicitly warns it may contain post-match
  information — so it is inadmissible as a benchmark, and the weight cannot be
  fitted offline. It becomes fittable in-season from `projection_snapshots`.
  Corroborating diagnostic: `python -m gaffer.backtest --xp-diagnostic`.
- **The decision threshold is a policy choice too.** A move must clear 1.0
  expected points *and* a 55% chance of beating the hold before Gaffer calls it
  an action. Neither bar is fitted; both exist to stop sub-point noise being
  published as advice, and both should be reassessed after ~6 gameweeks of real
  decision snapshots.

## Roadmap

- **Phase 1 (now):** heuristic model + solver + phone-first UI. ✅
- **Phase 2:** the weekly loop — decision screen, live gameweek, post-gameweek
  review, notifications. ✅
- **Phase 3:** fit the `ep_next` blend on real in-season data; a minutes-only
  classifier for the `p_start` gate (the one place the trained models were
  clearly better).
- **Phase 4 — structured availability evidence.** Today's news layer is
  headlines with an AI summary, and every generated claim is source-linked
  precisely because a headline is not evidence. The next step is *structured*
  availability from official press conferences and attributable reporting:
  `player → status → source → confidence → observed_at`, with per-source
  accuracy tracked over time so a source that is often wrong is weighted as
  such.

  Two hard constraints, stated now so they are not quietly dropped later:
  **no paywalled or private predicted-lineup providers will be scraped**, and
  **news sentiment will never be fed directly into projected points**. Any
  future use must first produce an explicit availability or minutes claim with
  a citation, which the existing `p_start` gate can consume and the UI can show
  you. A number that moved because of a rumour, with no way to see the rumour,
  is worse than no number.

## Stack

Python 3.12 (httpx, pandas, numpy, PuLP) · SQLite · Svelte 5 + Vite + Tailwind 4.

## Talking to Gaffer — the local MCP interface

A read-only [MCP](https://modelcontextprotocol.io) server, so the same decisions
can be asked about in conversation rather than read off a page.

```bash
.venv/Scripts/python -m gaffer.mcp_server --self-test    # exercise every tool
.venv/Scripts/python -m gaffer.mcp_server --list-tools
```

**Claude Code** — one command, using an absolute path to *this* checkout's
interpreter (the server reads `gaffer.config`, not your working directory):

```bash
claude mcp add gaffer -s user -- C:/Users/you/Gaffer/.venv/Scripts/python.exe -m gaffer.mcp_server
```

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gaffer": {
      "command": "C:/Users/you/Gaffer/.venv/Scripts/python.exe",
      "args": ["-m", "gaffer.mcp_server"]
    }
  }
}
```

Restart the client afterwards; MCP servers are launched at startup.

Eleven tools: `gaffer_status`, `get_weekly_decision`, `find_players`,
`get_player_outlook`, `compare_players`, `get_transfer_plan`,
`get_league_strategy`, `get_live_gameweek`, `get_decision_review`,
`get_model_evidence`, `what_changed`.

**What it cannot do**, by construction and by test: no writes (SQLite is opened
`mode=ro`), no FPL authentication, no transfer execution, no notification
sending, no arbitrary SQL, no filesystem path, no URL, no shell, no LLM call
inside the server, and no remote transport — it is stdio on your machine only.
Every result carries its season, its source artifact, its freshness and its
limitations; every failure is a named state rather than an empty success, because
a tool that returns nothing when the season has not started teaches a model to
invent the answer.

There is deliberately **no** price-prediction tool and **no** `make_transfers`.
