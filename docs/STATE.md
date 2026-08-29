# Gaffer — master state, 2026-08-23

**Read this first if you are a fresh session.** What Gaffer is, what runs, what
today's crossover programme proved about its models, and what should and should
not happen next.

Verified against the repository and the committed evidence documents on
2026-08-23. Approximate values say so. Unresolved questions say UNKNOWN.

| | |
|---|---|
| commit | `7f69088` on `main`, tree clean |
| tests | **1,419 passed, 1 skipped** |
| ruff / deps | clean / OK — 51 locked packages, Python 3.13.15 |
| season | **live, mid-season** — leave the running product alone |
| this commit changed | documentation and `scripts/` only. `src/` untouched. |

---

## 1. What Gaffer optimises

Gaffer is **not a betting model** and shares no decision logic with Ledger.

It optimises **expected Fantasy Premier League outcomes under squad constraints
across a whole season** — with the secondary and explicit goal of keeping the
game interesting enough to stay engaged every gameweek.

It is a portfolio and planning problem, not "pick the eleven highest projections":

player projections · expected minutes · starting probability · squad
optimisation · transfers · captaincy and vice · bench quality · chips ·
league-scoped ownership and rivals · future flexibility.

Everything runs on free, unauthenticated endpoints. Nothing writes to FPL.

---

## 2. What is running

**Gaffer's pipeline runs in GitHub Actions.** One launchd agent on the Mac mini
watches it — `com.myles.gaffer-watchdog`, added 2026-08-28. It computes nothing
and publishes nothing; it dispatches `refresh.yml` when GitHub stops firing it,
and fast-forwards this checkout so the MCP server answers from the artifacts the
site is actually serving. Details in `deploy/macmini/README.md`.

Corrected 2026-08-29: this section used to say "not on the Mac mini. No launchd
agent is installed for it", which stopped being true the day the watchdog
shipped, and would send a fresh session looking in the wrong place for the
scheduler that is currently keeping the site current.

| workflow | schedule | notes |
|---|---|---|
| `refresh.yml` | `*/15` + 02:00 / 11:00 / 17:00 UTC | gated: deps → tests → ruff → pipeline → artifact contract → commit → dispatch deploy |
| `deploy.yml` | on push touching `web/**` or `data/**` | build → Pages |
| `keepalive.yml` | — | GitHub disables `schedule:` after 60 quiet days |
| `ci.yml` | on push | |

A scheduled run that produces no publishable diff **fails**, because a valid run
always advances `generated_at`.

Live product: `https://emptycornmeal.github.io/gaffer/`. The read-only MCP server
(`gaffer.mcp_server`) answers from artifacts the pipeline wrote and
`gaffer.contract` validated — it computes nothing the app computes.

---

## 3. What the crossover programme proved about Gaffer's models

Five experiments ran on 2026-08-23 using Ledger data passed as **immutable
files**. Four were refuted. **Nothing was promoted.** The shipped projection
model, expected minutes, `p_start`, the solver and the publishing pipeline are
all unchanged.

Full record: `docs/MODEL-EVALUATION.md` and the exchange's
`CROSSOVER-EVIDENCE.md`.

### 3a. `p_start` loses to "he started last week" — BOTH MODELS REJECTED

Retrospective shootout on **113,592 archive rows**, ESTABLISHED regime
(100,392 rows, 88% of the population):

| contender | Brier | XI hit rate |
|---|---|---|
| **started-last-match** | **0.0965** | **74.0%** |
| Ledger's XI model | 0.1279 | 64.1% |
| **Gaffer `p_start`** | 0.1403 | 60.2% |

**No canonical owner was appointed and no duplicate was deleted.** Both models
need conceptual improvement, not consolidation.

The diagnosis is the same for both: **they score a rate and neither scores
recency.** Premier League managers change roughly two or three of eleven per
fixture, and that is precisely the information the trivial baseline uses.
Gaffer's `p_start` is the best *calibrated* of the three (gap 0.160 against
0.409) and the least *sharp*.

Gaffer **wins COLD_START decisively** (Brier 0.163 against 0.273, XI hit 46.8%
against 24.2%), so the prior-season machinery is doing real work where it matters
most. *Caveat: Ledger's real cold-start branch uses `ep_next` and ownership,
neither of which exists in the archive, so its figure there is a floor rather
than a measurement.*

The `not_ruled_out` note in `MODEL_CANDIDATES` — "a minutes-only classifier
feeding the existing `p_start` gate is the version worth testing next" — is
**supported**, and the first thing such a classifier should be given is the
previous fixture's start.

### 3b. Expected minutes is worse than a running average

| | MAE |
|---|---|
| `exp_minutes` (with the real fitted cameo curve) | **25.08** |
| naive: the player's own minutes per fixture to date | **16.21** |

113,592 rows. Well powered.

**The conceptual flaw:** deriving minutes too directly from a start probability
forces a **bimodal** estimate — about 78 minutes or about 3 — and represents a
reliable ~60-minute player badly, because he is in neither mode.

**Recorded as future model research. The live model was NOT changed.** Two
cautions before anyone acts on it:

- MAE on minutes is **not** Gaffer's objective. Minutes reach points through
  appearance points and per-90 scaling.
- E2 below demonstrated **in this same system** that a better input need not
  become a better decision. The follow-up is a points-level test, not a rewrite.

### 3c. Market-derived team strength — REJECTED

| season | control | market | diff | t |
|---|---|---|---|---|
| 2023-24 **train** | 46.60 | 50.30 | +3.74 | **+2.34** |
| 2024-25 select | 51.80 | 52.20 | +0.39 | +0.21 |
| 2025-26 **test** | 49.70 | 48.20 | **−1.50** | −0.82 |
| pooled, 114 gw | | | +0.88 | 0.84 |

**The only significant season is the training season and it does not replicate.**
The test-season direction reverses. Resolving the pooled effect would need ~16
seasons.

**No pre-season / GW1 advantage was demonstrated** — the prediction that this
would pay most at GW1 is refuted, in no consistent direction.

Two secondary results matter more than the headline. **MAE improved in 3/3
seasons** and rank correlation likewise, over ~155,000 rows per season, and none
of it reached the fifteen players a squad is built from — **a better projection
did not become a better decision.** And **the forward-looking mechanism is
dead**: including the upcoming round's own prices is indistinguishable from
excluding them.

**Do not import Ledger market strength into Gaffer's live model.**
`scripts/market_expectations.py` is research code with no pipeline caller, and it
lives outside `src/` deliberately.

### 3d. The availability denominator — REJECTED, and M6 is closed

`backtest.py` said `starts / 38` "cannot separate rotation from injury absence"
and that "a real fix needs per-fixture history". Ledger supplied exactly that:
1,167 player-seasons, per fixture.

On the 5,271 rows where the denominators differ:

| | Brier |
|---|---|
| `base_starts / 38` | **0.17870** |
| `base_starts / fixtures_available` | 0.22381 |

Paired **+0.04511**, **t = +23.90 — the correction is WORSE**, replicated
independently in all three seasons including the held-out 2025-26. Because this
corrects a *prior-season* rate, the test season was never compromised.

**This confirms the conjecture already written in `backtest.py`: absence predicts
absence.** A player who missed three months is likelier to miss more, so
`starts / 38` is not a defect awaiting data — it carries two signals, and
separating them throws one away.

**Roadmap item M6 is closed as measured and rejected. Do not reopen without
genuinely new evidence.**

---

## 4. Prior model verdicts that still stand

From `docs/MODEL-EVALUATION.md` — three candidates, three different findings, and
collapsing them into one verdict is a mistake this document has already made once
and corrected:

| candidate | decision |
|---|---|
| **GBM** | **rejected** — worse than the heuristic at all six horizons |
| **Ridge** | **inconclusive, not selected** — beat the heuristic at h=1 but the interval spans zero and the edge is gone by h=2 |
| **`xP`-based models** | **invalid experiment** — the key feature is inadmissible on provenance |

**No trained points model ships.** `tests/test_ml_removed.py` keeps the deleted
architecture out; `tests/test_model_evidence.py` keeps the three verdicts from
collapsing into one.

`EP_NEXT_BLEND_WEIGHT = 0.7` and the minimum-actionable thresholds are **labelled
policy choices, not fitted parameters** (`config.EP_NEXT_BLEND_IS_FITTED =
False`). The evidence for the blend weight was **withdrawn, not reversed**.

---

## 5. Identity and leakage rules

**FPL identity is `(season, element_id)`.** An `element_id` alone is not globally
stable — FPL reuses them every summer. Prior-season joins go by normalised name.

**The two projects have different cutoffs, and this is the rule most likely to
cause a silent error:**

| | boundary |
|---|---|
| **Gaffer** | the **FPL gameweek deadline** — 90 minutes before the round's *first* fixture |
| **Ledger** | the individual **fixture kickoff** |

> **A fact that is legal for Ledger can be future information for Gaffer.** A
> price or team-news item safely pre-kickoff for a Sunday match is hours or days
> past the deadline that chose the squad.

Consequences, non-negotiable:

- **Closing odds are banned as Gaffer pre-deadline features.** They are
  post-deadline for every fixture of a gameweek except the first.
- **Same-fixture FPL outcome data is banned as a Ledger feature.** It may be a
  target, or an input to a *later* fixture.
- **`xP` and post-match fields are denied at the exchange boundary** by
  `gaffer.leakage.POST_MATCH_FIELDS`. `xP` is inadmissible on provenance, not on
  a correlation.
- **A structural zero is not a measured zero.** `starts` and the `expected_*`
  columns do not exist before 2022-23; `defensive_contribution` before 2025-26.
  Those cells are exported EMPTY and read back as `None`.
- **Historical exports stay immutable and pinned.**

---

## 6. The crossover boundary — permanent recommendation

| | |
|---|---|
| **NO** | shared Football Intelligence service · monorepo · shared ORM · shared runtime package · shared mutable database · shared AI implementation |
| **YES** | immutable, versioned file exchange, **only where a registered experiment requires it** |

```
/Users/mylescolling/Projects/Football Exchange     version 2026-08-23
```

**Not a git repository.** It is an immutable project artifact; both repositories
record its path and version. Neither project imports it, neither depends on it at
runtime, and either will run correctly with it deleted.

`pyproject.toml` packages `src/` only, so **nothing shipped in Gaffer can reach a
Ledger artifact even by accident.** Experiment code lives in `scripts/`.

**The strongest argument for this boundary is E4:** an imported dataset *refuted
the fix it was imported to make*. A shared live service would have made that
integration permanent before anyone measured it.

---

## 7. DO NOT REOPEN WITHOUT NEW EVIDENCE

New evidence means new data or a structurally different hypothesis — not a more
sophisticated version of the same idea.

- **Gaffer market-derived team strength** — REJECTED, train-only significance,
  test reversed
- **The availability-denominator correction (M6)** — REJECTED at t≈+23.90,
  replicated on held-out data
- **Choosing either current expected-XI model as canonical** — both lost to a
  one-line baseline
- **GBM / ridge / `xP`-based points models** — rejected, inconclusive and invalid
  respectively; the deleted architecture is kept out by test
- **A shared Football Intelligence service, monorepo, shared ORM, shared runtime
  package, shared mutable database, shared AI implementation**
- **A new paid football API**
- **Silently changing the live projection, expected minutes or `p_start`** on the
  strength of the MAE findings in §3 — they are inputs to a future test, not a
  mandate

---

## 8. What genuinely warrants future work

| trigger | then |
|---|---|
| Enough evidence to **redesign `p_start` around recency and rotation** | Build the minutes classifier the backtest already names, starting from the previous fixture's start. Pre-register it. |
| A **genuinely different formulation** for expected minutes | Test at the **points** level, not on minutes MAE. |
| Live performance reveals a **concrete projection or solver defect** | Fix it. This outranks research. |
| New season data supports an **already pre-registered** test | Run that test, not a new one invented to fit the data. |

**Crossover work re-opens only if one project holds a dataset that directly
resolves a registered blocker in the other.** No architecture work for its own
sake.

---

## 9. Handover — 24 August

Gaffer needs nothing tomorrow. It is mid-season and running.

**Default posture: check health, then leave it alone.**

- Refresh runs 02:00 / 11:00 / 17:00 UTC in Actions and publishes if the artifact
  contract passes.
- **An alert** is: refresh failing repeatedly, a contract violation, a stale
  `generated_at`, or the deployed site not advancing.
- **Not an alert:** a gameweek with no recommended transfer. "We cannot tell you"
  is a designed output, gated by the minimum-actionable thresholds.

**Do not start coding automatically.** The crossover programme is closed and the
model findings in §3 are recorded as research, not as a work queue.

---

## 10. Where the other records live

| record | location |
|---|---|
| Architecture | `docs/ARCHITECTURE.md` |
| Model evidence, verdicts, the crossover results | `docs/MODEL-EVALUATION.md` |
| Traceability | `docs/TRACEABILITY.md` |
| Release | `docs/RELEASE.md` |
| The 2026-08-06 audit | `AUDIT.md` — historical; its headline Tier-1 defect is fixed |
| **Crossover programme (E1–E5)** | `/Users/mylescolling/Projects/Football Exchange/CROSSOVER-EVIDENCE.md` |
