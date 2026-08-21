# T-01 → T-29 — evidence ledger

Every task from the 2026-08-06 audit, with what implements it, what proves it,
where a user sees it, and what is still unproven.

**Status is evidence, not existence.** `complete` means a test asserts the
behaviour and something in the product exposes it. `evidence-limited` means the
code is there and tested against recorded fixtures, but the claim cannot be
verified until real data exists. `rejected` means it was built, measured and
removed.

Counts as of Batch 7: **1059 backend tests**, **158 front-end tests**.

| | Count |
|---|---|
| complete | 26 |
| evidence-limited | 4 |
| rejected | 1 |
| deferred | 0 |

---

## Phase 1 — correctness and trust

| ID | Status | Implemented in | Proven by | Visible as | Limitation |
|---|---|---|---|---|---|
| **T-01** editable install + `DATA_DIR` under `REPO_ROOT` | complete | `config.resolve_repo_root`, `config.verify_publish_paths`, `refresh.yml` | `test_config_paths.py` (22), `test_pipeline_artifacts.py::test_pipeline_writes_every_artifact_inside_the_repo` | fresh `generated_at` on the site | The published proof needs one real Actions run |
| **T-02** entry/league config + example | complete | `config.Settings`, `gaffer.example.toml`, `refresh.yml` env | `test_config_paths.py` precedence tests | Meta page shows the resolved entry | — |
| **T-03** freshness surfaced + CI freshness gate | complete | `artifacts.build_meta`, `Topbar.svelte`, `contract.MAX_META_AGE`, `refresh.yml` | `test_contract.py`, `freshness.test.ts` | age chip, amber >12h / red >36h | — |
| **T-04** CI gate before publish | complete | `refresh.yml`, `deploy.yml` | `test_schedule.py` workflow-structure tests (25) | a failed gate blocks the deploy | Needs one real Actions run |
| **T-05** projection GW vs readable-squad GW | complete | `gaffer.gameweek`, `ingest.run` | `test_gameweek.py` (22), `test_squad_ingest.py` (21) | squad status + reason on Meta | — |
| **T-06** verdict grounded in the real squad | complete | `ai/verdict.py` | `test_verdict.py` (18) | AI Verdict names only owned players | — |
| **T-07** price double-divide + route fallback | complete | `Meta.svelte`, `App.svelte` | `nav.test.ts`, `format.test.ts` | prices agree across pages | — |
| **T-08** keepalive against the 60-day cron disable | evidence-limited | `gaffer.schedule`, `keepalive.yml` | `test_schedule.py` (25) | — | The 60-day behaviour cannot be observed without waiting 60 days; the assumption is stated in the README |

## Phase 2 — make the model measurable

| ID | Status | Implemented in | Proven by | Visible as | Limitation |
|---|---|---|---|---|---|
| **T-09** backtest the *shipped* model | complete | `gaffer.backtest`, `gaffer.histdata`, `gaffer.leakage` | `test_backtest_parity.py` (46) | Accuracy page | Team ratings are reconstructed, not the live construction — stated in `limitations` |
| **T-10** `player_gw` + projection snapshots | complete | `ingest.ingest_player_history`, `projection.snapshot_projections` | `test_history_store.py` (21) | Review page reads it | Empty until GW1 finishes |
| **T-11** real selling price, manual FT/bank | complete | `gaffer.teamstate`, `optimize`, Sidebar | `test_teamstate.py` (45), `test_solver_executable.py` (11) | "you can afford this" on the decision screen | Bank is unknown pre-season and stays unknown |
| **T-12** fit `STRENGTH_GAMMA` / clamp | complete | `gaffer.fitting`, `model.features` | `test_historical_ratings.py` (12), `test_features.py` | fixture difficulty across the whole league | Fitted on 2023-24; since G-N reported on 2025-26, which no sweep has ever seen. G19 closed the split drift 2026-08-21 — `gaffer.fitting` now derives train/select/test and its exclusions from `backtest.SEASON_SPLIT` rather than restating them, so the two cannot disagree again |
| **T-13** missing scoring elements | complete | `model.projection`, `config` scoring constants | `test_scoring.py` (36) | component breakdown per player | DEFCON is measured for the first time on the 2025-26 test season, and is worth +3.4 legal-XI pts/gw there. Every earlier backtest ran with no `defensive_contribution` column at all, so the term was inert — one season of evidence, not four |
| **T-14** refit risk weights, ownership out of the objective | complete | `solver.optimize` (`RISK_WEIGHTS` all zero) | `test_objective.py` (13) | captain = highest xP unless a league reason is shown | — |
| **T-15** blend `ep_next` | **complete, re-labelled** | `model.projection`, `config.EP_NEXT_BLEND_WEIGHT` | `test_projection.py`, `test_model_evidence.py` | Accuracy page and the MCP `get_model_evidence` tool both state it is unfitted | **The weight is not fitted.** The archive cannot certify its `xP` column as the pre-deadline forecast; it becomes fittable in-season from `projection_snapshots` |

## Phase 3 — decisions worth trusting

| ID | Status | Implemented in | Proven by | Visible as | Limitation |
|---|---|---|---|---|---|
| **T-16** correlated scenario simulation | complete | `model.scenarios` | `test_scenarios.py` (16) | every probability in the app | — |
| **T-17** league-scoped EO and placing probability | evidence-limited | `gaffer.league`, `gaffer.strategy` | `test_league.py` (41), `test_strategy.py` (34) | Strategy page | Rivals have no picks pre-season; `placing.available` is false and the UI renders `—` |
| **T-18** multi-league posture and conflicts | complete | `gaffer.multileague` | `test_strategy.py` | Strategy page conflict panel | CI runs one league |
| **T-19** one shared objective | complete | `solver.objective` | `test_objective_shared.py` (34) | planner and optimiser agree | — |
| **T-20** real chip optimisation from API windows | complete | `gaffer.chips` | `test_chips.py` (27) | Chips page | No chip is recommended without a known squad |

## Phase 4 — engagement

| ID | Status | Implemented in | Proven by | Visible as | Limitation |
|---|---|---|---|---|---|
| **T-21** weekly decision screen | complete | `gaffer.decision`, `gaffer.weekly`, `gaffer.snapshots`, `Home.svelte` | `test_decision.py` (28), `test_snapshots.py` (37) | the home page | Pre-season the action is `unavailable`; the threshold is a labelled policy choice |
| **T-22** live gameweek | evidence-limited | `gaffer.live`, `Live.svelte`, `lib/refresh.ts` | `test_live.py` (62), `refresh.test.ts` (20) | Live page | Verified against recorded fixtures only. The live endpoint returns `{"elements": []}` until GW1 |
| **T-23** post-gameweek review | evidence-limited | `gaffer.review`, `Review.svelte` | `test_review.py` (41) | Review page | No gameweek has finished, so `review.json` does not exist |
| **T-24** notifications | complete, **inactive** | `gaffer.notify`, `deploy/macmini/` | `test_notify.py` (59) | `notifications.json`, dry-run only | Nothing has ever been delivered; the webhook path is tested for refusal, not for success |
| **T-25** accessibility, mobile, code splitting | complete | `App.svelte`, `app.css`, 5 component fixes | `perf.test.ts` (28), `npm run check` 0 warnings | 48 kB gzip initial JS, 11 lazy chunks | — |

## Phase 5 — model decision and release readiness

| ID | Status | Implemented in | Proven by | Visible as | Limitation |
|---|---|---|---|---|---|
| **T-26** ship or remove ML | **complete — nothing shipped** | removal of `ml.py` + `gaffer_gbm.joblib` + the `ml` extra; `backtest.MODEL_CANDIDATES` (schema 5), `WITHDRAWN_BASELINES`, `xp_leakage_diagnostic` | `test_ml_removed.py`, `test_model_evidence.py`, `test_backtest_parity.py` | Accuracy page renders **every** candidate with its own verdict | **GBM rejected**, **ridge inconclusive** (it beat the heuristic at h=1; the interval spans zero and the edge is gone by h=2), **xP models invalid**. The appearance-prediction advantage is real and untested as a minutes-only model |
| **T-27** remove dead code | complete | see the deletion ledger in the batch report | `test_ml_removed.py`, `test_contract.py::test_an_unrecognised_artifact_is_rejected` | — | `proxy/` was **retained**: it serves live per-user data (audit finding corrected) |
| **T-28** reproducible environments | complete | `requirements.lock.txt`, `gaffer.deps`, `.python-version`, `web/.nvmrc`, `scripts/verify.py` | `test_deps.py` (17), clean-venv install | `python -m gaffer.deps` | The lock resolved on CPython 3.14; CI installs it on the same pin, but that path runs for the first time on the next Actions run |
| **T-29** safe season rollover | complete | `gaffer.season`, `db.migrate`, ingest gate, artifact season stamps, `contract` season agreement, `data.ts` | `test_season.py` (58), `data.test.ts` | `python -m gaffer.season` | Rollover is proven against recorded fixtures and synthetic databases; the real 2027-28 boundary is a year away |


---

## Batch 7 — grounded AI interface and release

| ID | Status | Implemented in | Proven by | Visible as | Limitation |
|---|---|---|---|---|---|
| **B7-1** evidence reconciliation | complete | `leakage.py`, `backtest.py`, `config.py`, `decision.py`, `contract.py`, `docs/` | `test_model_evidence.py` (22) | Accuracy page, `decision.json.threshold_status` | The `xP` exclusion rests on provenance; the correlation is corroborating, and Gaffer cannot observe when FPL wrote the field |
| **B7-2** AI envelope + claim grounding | complete | `ai/grounding.py`, `ai/news.py`, `ai/verdict.py`, `contract.py`, `News.svelte` | `test_ai_grounding.py` (52), `test_contract.py` | News page renders each claim beside its source links | Quarantine is pattern-based and deliberately blunt: a false positive costs one dropped headline |
| **B7-3** read-only MCP | complete | `gaffer/mcp_server.py` (11 tools) | `test_mcp_server.py` (57), `test_mcp_evals.py` (17) | `python -m gaffer.mcp_server` | Local stdio only. Never verified against a live gameweek's data, because none has been played |
| **B7-4** pull-request CI | complete | `.github/workflows/ci.yml` | `test_schedule.py` (34) | GitHub checks on every PR | Runs for the first time on this release PR |

---

## Batch 7.2 — production website acceptance fixes

Found by reviewing all fifteen deployed routes in a browser rather than by
reading code. Every fix below is verified in Chrome against the production
build, not only by unit test.

| ID | Status | Implemented in | Proven by | Visible as | Limitation |
|---|---|---|---|---|---|
| **B72-1** Fixtures route crash | complete | `web/src/lib/fixtures.ts` (new normalisation boundary), `Fixtures.svelte`, `types.ts` (removed the false `Fixtures` alias) | `fixtures.test.ts` (34), including the **published** `web/public/data/fixtures.json` | Fixture ticker renders 20 teams; a broken artifact shows a named error card instead of an empty grid | The guard requires `team` plus a `fixtures` array; a future artifact that renames either field degrades to "unavailable" rather than adapting |
| **B72-2** desktop navigation overflow | complete | `nav.ts` (`PRIMARY_TABS` / `MORE_TABS`), `Topbar.svelte` (More disclosure menu), `Icon.svelte` (`chevron-down`) | `nav.test.ts` (25) for the partition; Chrome measurement at 390/768/1024/1280/1366/1440/1920 for the widths and the menu behaviour | Six primary tabs plus a keyboard-accessible More menu; search stays 224 px on desktop, 160 px at 390 px | The primary set is a fixed key list, not a measured fit: a much longer label would need the list revisited |
| **B72-3** false Strategy coverage wording | complete | `strategy.ts` (`CLASS_LABELS` de-claimed, `describeCoverage`), `Strategy.svelte` | `strategy.test.ts` (40), incl. a structural assertion that no classification label claims readability | "Tiny private league" above "0 of 3 rival squads known — all 3 were modelled as a distribution, not as teams" | Coverage is reported, not improved; rival picks stay unreadable until the first deadline passes |
| **B72-4** Overview preseason honesty | complete | `web/src/lib/squadStatus.ts` (new), `Overview.svelte`, `types.ts` (`squad_status_reason`, `squad_source_event`), `Icon.svelte` (`alert`) | `squadStatus.test.ts` (21) | A caveat above the briefing: "This is a model-built reference squad, not your team" | The AI briefing text itself is unchanged — the artifact is not rewritten, so its imperative wording still reads confidently once past the caveat |
