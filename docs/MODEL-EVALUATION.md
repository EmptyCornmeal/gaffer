# T-26 — the ML decision

**Outcome: no trained points model ships.** `src/gaffer/ml.py`,
`data/model/gaffer_gbm.joblib` and the `ml` packaging extra are removed. Gaffer's
projection is a heuristic component model, and the product says so.

Three candidates, three different findings — collapsing them into one verdict is
a mistake this document made in its first version and has since corrected:

| Candidate | Decision | Why |
|---|---|---|
| **GBM** (gradient-boosted trees) | **rejected** | Worse than the heuristic on legal-XI points at all six horizons; four of six intervals exclude zero against it; captain accuracy 13.5% against 29.7% |
| **Ridge** (regularised linear) | **inconclusive — not selected** | Beat the heuristic at h=1 (+2.70 points/GW, 22 gameweeks to 14, captain accuracy 32.4% against 29.7%) but the interval spans zero and the edge is gone by h=2 |
| **xP-based models** | **invalid experiment** | The only apparent decisive win (+10.73/GW). Its key feature is inadmissible, so the result cannot be scored — which is not the same as losing |

Reproduce anything here with `python -m gaffer.backtest` and
`python -m gaffer.backtest --xp-diagnostic`. The exploratory scripts are not
committed; the evidence is in `data/backtest.json` under `model_candidates` and
`withdrawn_baselines`.

---

## 1. What was already there

`ml.py` trained a `HistGradientBoostingRegressor` on the vaastav archive and
saved a 408 KB joblib. After Batch 3 rewrote the backtest, **nothing imported
it** — not the pipeline, not the backtest, not a script, not a workflow. It had
three problems beyond being unused:

- Its team-strength features came from `teams_<season>.csv`'s season-end
  `strength_attack_*` / `strength_defence_*`. Those fields are **all zero in the
  live API before the season starts** — the exact regime the model would have to
  run in for the highest-stakes decision of the year.
- It read its features from the CSVs directly, bypassing `histdata` and therefore
  the leakage contract.
- Loading it meant `joblib.load` on a committed binary — arbitrary pickle
  execution as a startup step.

So the question was not "is the existing model good" but "is *a* small model
worth building properly".

## 2. Dataset

Built through `gaffer.histdata`, the same leakage-enforcing adapter the corrected
backtest uses. One row per `(decision_gw, target_gw, element)`; features frozen
at the decision deadline and carried onto each target gameweek, so horizon 6 sees
exactly what horizon 1 saw.

| Season | Rows | Decision GWs | Zero-minute rows retained | Role **in this experiment** |
|---|---|---|---|---|
| 2022-23 | 128,424 | 36 | 57.2% | train |
| 2023-24 | 151,475 | 37 | 61.6% | selection |
| 2024-25 | 146,073 | 37 | 58.2% | **test — untouched until selection closed** |

Those row counts matched `gaffer.backtest.build_evaluation` when they were taken.
They no longer do, and neither do the roles — twice over:

- **Schema 6** started evaluating GW1, which the first five schema versions
  skipped while their own comment claimed otherwise. 2024-25 is now 149,769 rows
  over 38 decision gameweeks, not 146,073 over 37.
- **G-N** moved the project's split forward a season and **G-Q** took 2022-23 out
  of it. What ships is `gaffer.backtest.SEASON_SPLIT`: train **2023-24**, select
  **2024-25**, test **2025-26** — 161,700 rows, 38 decision gameweeks, 61.4%
  zero-minute.

The table above is left standing because it is what *this experiment* ran on. It
is not the current configuration and must not be read as one — see
[§11](#11-what-g-n-and-g-q-changed).

**Zero-minute outcomes are kept.** They are 58% of the population and they are
the majority of what a picker gets wrong.

**Splits are chronological.** No row is shuffled across gameweeks or seasons.

### The 2022-23 regime, diagnosed

2022-23 was previously excluded. The reason is not the season — it is the
*prior*: `data/history/` held no 2021-22, so `histdata._prior_season_baseline`
returned empty and every 2022-23 row carried `base_* = 0` while
`_prior_rates` classified all 20 clubs as promoted.

Fixed by fetching 2021-22 through the repository's established source
(`scripts/fetch_history.py`'s vaastav mirror) into the git-ignored
`data/history/`. Nothing bulky is committed. What that recovers, and what it
does not:

| Prior input | 2022-23 after the fix | 2023-24 | 2024-25 |
|---|---|---|---|
| `base_minutes > 0` | **44.0%** | 43.0% | 46.9% |
| `base_xg90 > 0` | **0.0%** | 39.2% | 42.5% |
| `base_starts` | absent | present | present |
| Per-team goals-for/against prior | **recovered** | present | present |

2021-22 predates FPL's `expected_goals` / `expected_assists` / `starts` columns,
so 2022-23 has a legitimate **minutes and team** prior but **no attacking
prior**. That is a documented regime difference, not a silent zero — and it runs
in the harder direction: the model is trained where the attacking prior is
missing and evaluated where it exists.

Name and team keys are consistent across the join (2021-22 → 2022-23 overlap
53.5%, versus 59.1% and 60.9% for the later pairs — normal squad turnover).

#### Superseded by G-Q: the prior was not the whole problem

The paragraph above is right about the prior and wrong about the conclusion it
drew from it. It claims the missing attacking prior "runs in the harder
direction". Measured end-to-end, it does not run in a harder direction — it runs
off the map.

2022-23's **own** `expected_goals` and `expected_assists` are identically **zero
for GW1-15**. The first gameweek carrying any xG at all is 16, and the covered
window holds 64.2% of the season's minutes. The whole-season goals/xG ratio of
**1.419** — which reads like a finishing-quality signal and is not one — is
entirely that gap: over GW16-38 alone it is **0.913**, i.e. ordinary. Assists
behave identically (A/xA 2.111 season-wide, 1.357 over the covered window,
against ~1.37 everywhere else).

| Season | goals/xG | assists/xA | first GW with xG |
|---|---|---|---|
| 2021-22 | — (no `expected_goals` column) | — | — |
| **2022-23** | **1.419** (0.913 over GW16-38) | **2.111** (1.357 over GW16-38) | **16** |
| 2023-24 | 0.998 | 1.424 | 1 |
| 2024-25 | 0.982 | 1.374 | 1 |
| 2025-26 | 0.943 | 1.379 | 1 |

So the column is not mis-scaled, it is 40% absent, and no multiplier repairs it:
zero times anything is zero. Stacked on a 2021-22 prior that cannot report xG
either, the model's h=1 numbers on 2022-23 are rank correlation **−0.050**, a
legal XI of **26.8** points per gameweek against the naive baseline's **48.2**,
and **8.1%** captain accuracy. That is not a weak season. It is the model reading
columns that are not there, and training on it teaches the shape of the gap.

2022-23 is therefore out of the split. It is still downloaded, because 2023-24's
`base_*` priors are read from its file — a residual documented in
`backtest.SEASON_SPLIT["residual"]`, and measured: correcting it costs 0.8
legal-XI points on 2023-24, so it was left alone.

## 3. Feature contract

Every candidate input was recorded with: source, when it becomes knowable,
missing-value behaviour, unit, whether it is safe before the deadline, whether it
exists in live production, and whether the historical reconstruction is
equivalent to the production construction. Training was rejected outright for any
feature failing the first two.

**Admitted** — `ppg_td`, `mpg_td`, `start_rate`, `games_td`, `min_td`,
`xg90_td`, `xa90_td`, `defcon90_td`, `base_mpg`, `base_start_rate`, `base_xg90`,
`base_xa90`, `att_mult`, `exp_conc`, `home_share`, `n_fix`, `value`, `pos_code`,
`horizon`.

**Admitted but flagged non-equivalent** — `att_mult` and `exp_conc` come from
`TeamContext`, which live reads FPL's published strength fields and historically
rebuilds ratings from played matches (T-12). Same consumer, different source.
This is the largest fidelity gap in the study and it applies equally to the
heuristic, so the comparison stays fair even though the absolute level does not
transfer.

**Rejected — not available historically:** `status`,
`chance_of_playing_next_round`. These exist live and would probably help; the
archive has no column for them, so a model trained without them cannot be
credited with them.

**Rejected — leaks:** `xP`. See below.

## 4. `xP` is inadmissible

This is the finding that decided the batch — and the grounds are **provenance**,
not a correlation.

The archive's `xP` column had been treated as a copy of FPL's pre-deadline
`ep_next` — as a feature here, and as the `fpl_xp` baseline in the published
backtest.

The upstream [data dictionary](https://github.com/vaastav/Fantasy-Premier-League/blob/master/DATA_DICTIONARY.md)
states that `xP` is FPL's `ep_this` field, **scraped after each gameweek has
ended**, that FPL's update cadence for that field is undocumented, and that
"empirical evidence suggests scraped values may reflect post-match information
rather than the pre-match prediction managers actually saw before the deadline".
Its own guidance is to `shift(1)` the column within each element, or drop it.

So the conclusion, stated exactly:

> **The archive cannot certify this value as the pre-deadline forecast managers
> saw, and the upstream dataset explicitly warns that it may contain post-match
> information. It is therefore inadmissible for this backtest.**

That is a statement about what the data can support, not a claim to have
established the timing. Gaffer has no way to observe when FPL wrote that field.

### Corroborating evidence — not proof

Restrict to rows where a player completed **60+ minutes** and his team played
**exactly one** fixture that gameweek, keep players with 15+ such gameweeks, and
measure how far each quantity moves from that player's own season average, and
how well the move predicts the move in his points:

| Quantity | sd of within-player deviation | corr. with points deviation |
|---|---|---|
| **`xP`** | **1.71 – 1.86** | **+0.15 to +0.46** |
| `ppg_td` — pre-deadline by construction | 0.94 – 1.03 | −0.10 to −0.11 |
| `expected_goals` — post-match by definition | 0.17 – 0.22 | +0.22 to +0.41 |

A forecast for a fixed player moves mainly with the fixture and the team news;
on 2022-23, 2023-24 and 2024-25 this moves with the *result*, about as strongly
as the xG he actually generated in the match.

**It does not reproduce on 2025-26**, and that season was added to the
diagnostic's default the moment it became the test season, because excluding a
season for disagreeing is how a diagnostic becomes an argument:

| Season | `xP` sd within player | `xP` corr. with points deviation |
|---|---|---|
| 2022-23 | 1.80 | +0.45 |
| 2023-24 | 1.71 | +0.46 |
| 2024-25 | 1.75 | +0.40 |
| **2025-26** | **1.86** | **+0.15** |

The withdrawal does not move, because it never rested on this measurement — the
grounds are the provenance statement above, and they are unchanged. What 2025-26
adds is that the correlation is not a stable property of the column, which is one
more reason it was right not to lean on it.

This is **consistent with** the upstream warning and it is why the warning was
taken seriously. It is not by itself proof of when the value was written — a
sufficiently good forecaster with team news could in principle move this much,
and correlation cannot separate that hypothesis from a post-match write. The
exclusion does not rest on it.

Two consequences, both applied:

1. `xP` is in `leakage.POST_MATCH_FIELDS`. It can be a target, never an input.
2. `fpl_xp` and `ensemble` are **withdrawn** from `data/backtest.json`
   (schema 4). Their previous values are kept in `withdrawn_baselines` so the
   retraction is visible rather than a gap.

`EP_NEXT_BLEND_WEIGHT = 0.7` was chosen against those withdrawn numbers. It is
**unchanged and relabelled**: the evidence was withdrawn, not reversed, and
moving the weight on withdrawn evidence would be as unfounded as setting it was.
The live `ep_next` is a genuine pre-deadline forecast — it is simply
*unmeasured*, because the archive contains no faithful copy of it.
`projection_snapshots` already stores it beside the model's own number before
each deadline, so from GW1 this becomes fittable on real data.

## 5. Candidates

Proportionate to a personal project: no deep learning, no large search, no
external service. sklearn only, which was already an optional dependency.

- the shipped standalone heuristic
- points-per-game to date (recent-form baseline)
- ridge, standardised, with position one-hots
- `HistGradientBoostingRegressor` — 300 iterations, depth 6, leaf 60, l2 1.0,
  fixed seed
- per-horizon GBM (one estimator per h, no `horizon` feature)
- GBM stacked on the heuristic's own output
- GBM plus ownership
- and, on the selection season only, the same models **with** `xP`, to measure
  what that column was worth before it was excluded

Both regimes were run: without the external estimate, to measure Gaffer's
independent value, and with it. `ep_next` was never forwarded past h=1 —
it does not exist for later gameweeks and copying it would manufacture a number.

## 6. Results

### Selection season (2023-24), where it looked like a win

| Model | XI pts/GW vs shipped blend | paired 95% CI | W/L |
|---|---|---|---|
| GBM **+ `xP`** | **+10.73** | [+4.57, +16.76] | 28/8 |
| ridge + `xP` | +7.14 | [+2.54, +11.38] | 21/9 |
| `xP` recalibrated (isotonic) | −2.43 | [−6.57, +1.19] | 16/19 |
| `xP` + price/position only | +0.30 | [−3.19, +3.81] | 19/16 |
| heuristic alone | −39.78 | [−46.43, −33.32] | 1/35 |

The gain was real *and* entirely attributable to `xP`: recalibrating it or using
it alone gave nothing, and permutation importance put it at +1.01 MAE against
+0.25 for the next feature. Once `xP` was excluded as a leak, the whole result
went with it.

### Test season (2024-25), reported once, after selection closed

*This experiment's test season. The project's is now 2025-26 — see
[§11](#11-what-g-n-and-g-q-changed). Nothing in this section has been restated on
2025-26, because the candidates cannot be re-run: `src/gaffer/ml.py` was deleted
by the same batch that recorded them.*

Read this table by candidate. GBM and ridge did **not** produce the same result,
and the earlier version of this document was wrong to summarise them together.

Trained on 2022-23 + 2023-24. Legal-15 → legal-XI under budget, quota and the
3-per-club limit, scored on what actually happened. Paired by gameweek against
the shipped heuristic, 4000-sample bootstrap.

| h | heuristic | ppg-to-date | ridge | GBM | GBM − heuristic | 95% CI |
|---|---|---|---|---|---|---|
| 1 | 50.86 | 52.08 | 53.57 | 46.22 | **−4.65** | [−10.03, +0.84] |
| 2 | 52.58 | 50.03 | 52.00 | 46.36 | **−6.22** | [−11.92, −0.05] |
| 3 | 51.03 | 47.94 | 48.69 | 44.97 | **−6.06** | [−11.71, −0.31] |
| 4 | 51.15 | 47.35 | 50.56 | 42.97 | **−8.18** | [−13.88, −2.41] |
| 5 | 48.91 | 46.39 | 47.61 | 44.76 | **−4.15** | [−9.33, +1.12] |
| 6 | 49.47 | 47.97 | 48.53 | 44.78 | **−4.69** | [−12.00, +2.69] |

Captain accuracy at h=1: heuristic 29.7%, ridge 32.4%, **GBM 13.5%**.
Captain regret: heuristic 5.59, ridge 4.43, GBM 6.32.

And the statistical metrics, on the same rows:

| h | rank corr. heuristic → GBM | MAE heuristic → GBM |
|---|---|---|
| 1 | 0.440 → **0.638** | 1.57 → **1.14** |
| 3 | 0.413 → **0.610** | 1.58 → **1.16** |
| 6 | 0.397 → **0.589** | 1.60 → **1.19** |

**The trained model wins every statistical metric and loses every decision
metric.** 58% of rows are zero-minute, so MAE and rank correlation mostly reward
predicting who will not play. Picking a squad needs the top tail, and there the
GBM regresses hard to the mean — exactly what a squared-error objective on a
zero-inflated target does.

**Ridge is the one that needs stating carefully.** At h=1 it beat the shipped
heuristic: 53.57 against 50.86 legal-XI points per gameweek, a paired mean
difference of **+2.70** [-1.38, +6.89], winning 22 gameweeks to 14, with better
captain accuracy (32.4% against 29.7%) and lower captain regret (4.43 against
5.59). That is a real result and the summary must not say otherwise.

It is still not a reason to ship:

- the interval spans zero (P(better) = 0.90, not 0.99);
- the edge does not survive the horizon — h=2 through h=6 are all negative
  (-0.58, -2.34, -0.59, -1.30, -0.94), every interval also spanning zero;
- h=1 was not pre-registered as the primary endpoint, and picking the horizon
  after seeing the numbers is how a coin flip becomes a finding;
- 37 gameweeks is a small paired sample.

So: **inconclusive, not selected**. An edge that is neither material nor durable
does not justify a training pipeline, a model artifact, a serialisation format, an
integrity check and a fallback path. That is a different verdict from GBM's, and
`data/backtest.json` now records them separately.

### Ablations (h≥2, selection season)

| Removed | rank corr. | MAE |
|---|---|---|
| — (full) | 0.576 | 1.145 |
| form | **0.509** | 1.157 |
| rates | 0.585 | 1.131 |
| prior | 0.578 | **1.112** |
| fixture | 0.572 | **1.211** |

Only `form` and `fixture` earn their place. `rates` and `prior` are neutral to
mildly harmful — the same conclusion the heuristic's own component structure
reaches.

### Cost

Fit 7.0 s on 279,899 rows, 177 MB peak; inference 0.49 s for 146,073 rows
(≈300k rows/s). Cost was never the reason to say no.

## 7. Shipping gate

| Requirement | Result |
|---|---|
| Materially improves untouched out-of-sample **decision** metrics | ❌ GBM worse at all six horizons; ridge +2.70 at h=1 with an interval spanning zero, negative at h=2-6 |
| No leakage | ✅ once `xP` was excluded — and that removed the entire gain |
| Works with production-available inputs | ✅ |
| Deterministic training | ✅ fixed seed |
| Safe fallback | ✅ would have been |
| Runtime limits | ✅ comfortably |
| Retrainable reproducibly | ✅ |
| Improves h=2–6, or states that it does not | ❌ **neither does** — GBM −4.2 to −8.2, ridge −0.58 to −2.34, all inconclusive |
| Adds value beyond `ep_next` | ❌ not demonstrable; `ep_next` itself is unmeasurable here |

GBM fails outright. Ridge fails the *material and durable* test rather than the
*better* test, which is why it is recorded as inconclusive rather than rejected.

Thresholds were fixed before the test season was loaded and were not moved.

## 8. What is not ruled out

The trained model is **much better at predicting appearances**: fringe-player
(pre-deadline start rate < 0.2) MAE 0.285 against the shipped model's 0.504,
rank correlation 0.462 against 0.325. Minutes is the crudest component in the
heuristic and the one that gates everything else.

A **minutes-only** classifier feeding the existing `p_start` gate is the version
of this worth testing next. A points regressor is not. That is a hypothesis, not
a plan, and it is not implemented.

## 9. OpenFPL — a methodological benchmark, not a dependency

[OpenFPL](https://arxiv.org/abs/2508.09992)
([code](https://github.com/daniegr/OpenFPL)) is an open reproducible FPL
prediction pipeline. It is worth recording here because several of its choices
are direct answers to weaknesses this study measured in Gaffer.

**Ideas worth taking:**

- **Position-specific models.** Gaffer's component model is already
  position-aware in its scoring, but a single regressor over all positions is
  exactly the shape that regressed to the mean here.
- **Player / team / opponent rolling features.** Gaffer has the player and
  fixture halves; opponent-side rolling form is thinner.
- **Return-category or sample weighting** so the 58% of zero-minute rows stop
  dominating the objective. This is the single most likely explanation for why
  the trained models won every statistical metric and lost the decision ones.
- **Explicit high-return evaluation.** Picking a squad is a top-tail problem;
  MAE over the whole population is close to irrelevant to it.
- **Prospective pre-deadline prediction**, rather than reconstructing a decision
  point from an archive.

**Its limitations, which are why this is not a drop-in:**

- the prospective test covers only **GW32–38** — seven gameweeks;
- development cross-validation is grouped by *team*, not chronologically by
  time, so it does not test the thing that matters here (does the model hold up
  on a season it has never seen?);
- it carries historical imputation assumptions for missing inputs;
- it is a research pipeline, not a production dependency, and adopting it would
  reintroduce exactly the training/serialisation/fallback surface this batch
  removed.

**The future evidence question**, recorded rather than acted on:

> Would a position-specific, top-tail-aware or minutes-only model improve
> Gaffer's *immutable decision metrics* — the ones stored in
> `decision_snapshots` and scored in `gw_reviews` — once enough real
> pre-deadline snapshots exist to test it on?

That question cannot be answered from the archive. It can be answered from
Gaffer's own records after a season of play, which is the first time the
evaluation and the production regime are the same thing.

## 10. Guard

`tests/test_ml_removed.py` guards five specific things:

1. `gaffer/ml.py` cannot return;
2. a serialised model binary cannot re-enter the repository;
3. `pickle.load` / `joblib.load` / `torch.load` cannot appear in `src/gaffer/` —
   a **security** property (those execute the file), not a statistical one;
4. a trained points column cannot reach a published recommendation unless a
   candidate in the evidence block is marked `shipped`;
5. withdrawn `xP` / `fpl_xp` / `ensemble` metrics cannot reappear as measured.

The first version of this guard also banned importing `sklearn`, `lightgbm` and
`xgboost` anywhere in the package. That was **wider than the evidence** and it
forbade the one experiment section 8 explicitly leaves open. A guard that blocks
the documented next step is freezing a finding, not protecting it. It was
narrowed.

`tests/test_model_evidence.py` guards the prose: it fails if a candidate claiming
it lost everywhere records a win, if a document says trained models lost every
decision metric without naming GBM, if the Accuracy page stops rendering every
candidate, if `rejected` and `inconclusive` collapse into one value, or if the
`xP` exclusion is argued from the correlation instead of from provenance.

Removing a rejected architecture is easy. Keeping the *reasoning* honest is the
part that needs a test.

## 11. What G-N and G-Q changed

Everything above is the T-26 record and is deliberately left at the numbers it
was measured on. This section says what is no longer true of the project.

**The split moved forward one season** (G-N), and 2022-23 left it (G-Q):

| | before | now |
|---|---|---|
| train | 2022-23 + 2023-24 | **2023-24** |
| select | 2023-24 *(also in the training set)* | **2024-25** |
| test | 2024-25 | **2025-26** |
| excluded | — | 2021-22, 2022-23 *(prior sources only)* |

Source of truth: `gaffer.backtest.SEASON_SPLIT`. `gaffer.fitting` still carries
its own pre-G-N triple and has **not** been reconciled.

**Why 2025-26 is worth reporting on.** It is the only season in the archive
carrying a `defensive_contribution` column, so it is the only one on which the
projection's DEFCON term can be measured rather than assumed. Ablated there it is
worth **+3.4 legal-XI points per gameweek** (49.3 with, 45.9 without), +0.90
captain points and +2.7pp captain accuracy — while making MAE 0.014 *worse*. The
same ablation on 2024-25 moves the XI by 0.1 in the *wrong* direction (50.6 with,
50.7 without): that season has no `defensive_contribution` column, so whatever
the term contributes there is fabricated from a positional prior.

**The headline, both halves.** At h=1, legal XI, model against the naive
baseline:

| | 2024-25 | 2025-26 |
|---|---|---|
| legal XI, model | 50.6 | 49.3 |
| legal XI, naive | 51.7 | 44.6 |
| **verdict** | **loses by 1.1** | **wins by 4.7**, and leads at all six horizons |
| captain pts, model | 8.76 | 5.87 |
| captain pts, naive | 8.00 | 5.97 |
| **verdict** | **wins** | **loses** |
| rank corr, model / naive | 0.440 / 0.666 | 0.447 / 0.692 |
| MAE, model / naive | 1.565 / 1.115 | 1.592 / 1.075 |

This is the first season in which the component model beats a rolling points
average at picking a legal XI. It is also a season in which it lost the captaincy
comparison it had won the year before, in which its absolute XI score *fell* from
50.6 to 49.3, and in which the naive baseline still beats it on both statistical
metrics — as it does in every season measured. Nothing about the model changed
between the two artifacts. Only the season did.

Every 2025-26 figure here was measured at `projection.MODEL_VERSION ==
"heuristic-0.5"`, while G-L/G-M/G-P was landing DEFCON shrinkage and xA
calibration. Under `heuristic-0.4` the same run gave 48.9 / 5.47 / 0.450 / 1.600:
every conclusion above survived the bump, the third decimal did not.

`data/backtest.json` is schema **7**. A 6 and a 7 describe different seasons with
different available columns and must not be differenced.
