# Gaffer — full audit (2026-08-06)

Audited against two goals:
1. **Win the mini-league** (Crouch Potatoes, ID 271619)
2. **Stay engaged enough to actually play every gameweek**

Scope: repo `EmptyCornmeal/gaffer` @ `af1c006`, the live site, GitHub Actions history,
the live FPL API, and your FPL entry (1066421).

---

## Verdict in one paragraph

The engineering is genuinely good — clean separation, honest explanations, a real MILP
solver, a published backtest, graceful degradation everywhere. But three things are true
at once and each is fatal on its own: **the site has been serving 11-day-old data because
the pipeline writes its output into the Python install directory**; **the projection model
is beaten by a number FPL already gives you for free, by 20 points a gameweek**; and
**every "rank defence" mechanism optimises for global rank when your actual objective is
beating three specific people**. None of these is a design flaw — they're all narrow,
locatable, and fixable.

---

# Tier 1 — Broken

## 1.1 The pipeline has been discarding 100% of its output for 11 days

The refresh workflow runs 3×/day and reports success every time. The site has not been
redeployed since **2026-07-26T16:32**. Deployed `meta.json` still says
`generated_at: 2026-07-26T15:53:50`.

**Root cause** — `src/gaffer/config.py:19-22`:

```python
PKG_DIR   = Path(__file__).resolve().parent   # .../site-packages/gaffer
REPO_ROOT = PKG_DIR.parents[1]                # .../lib/python3.12   ← wrong
DATA_DIR  = REPO_ROOT / "data"
```

`REPO_ROOT = PKG_DIR.parents[1]` only resolves correctly for an **editable** install
(`src/gaffer` → `src` → repo root). The README installs with `-e`; `.github/workflows/refresh.yml`
installs with `pip install ".[ai]"` — a regular install. So in CI the package lands in
`site-packages/gaffer/` and `parents[1]` walks up to `lib/python3.12/`.

Confirmed in run `31103338063`:

```
artifacts: ['/opt/hostedtoolcache/Python/3.12.13/x64/lib/python3.12/data/meta.json',
            '.../data/players.json', '.../data/recommendation.json', ...]
...
No data changes.
```

Then `cp data/*.json web/public/data/` copies the stale committed files onto themselves,
`git status --porcelain` comes back clean, the commit is skipped, `changed=false`, and
`gh workflow run deploy.yml` never fires.

**Cost so far:** ~30 wasted runs. Each one makes ~350 sequential `element-summary` calls
(~200s), plus an **Opus** verdict call and an **Opus** news digest call — 3× a day, every
day, written to a directory nobody reads. That's real spend on the metered pool.

**Fix:** `pip install -e ".[ai]"` in the workflow, or better, add a `GAFFER_DATA_DIR`
env override so the package stops inferring its location from `__file__`.

**Secondary:** the workflow should fail loudly, not silently no-op. "No data changes"
after a run that regenerates a timestamp is a contradiction and should be an error.

---

## 1.2 The pre-season fixture model collapses the league into three buckets

Verified live today (15 days from GW1): FPL still ships `strength_attack_home/away` and
`strength_defence_home/away` as **all zeros** for all 20 clubs. Only the coarse 1–5
`strength_overall_home/away` is populated.

`ingest.ingest_teams` correctly detects this and falls back to the coarse rating. Then
`features.TeamContext` applies `STRENGTH_GAMMA = 1.7` and `STRENGTH_CLAMP = (0.5, 1.85)`
to de-compress it. The de-compression overshoots into the clamp:

| Opponent's coarse rating | Teams | Attack multiplier |
|---|---|---|
| 2 | 8 (BHA, COV, FUL, HUL, IPS, LEE, NEW, SUN) | **1.850** — clamped |
| 3 | 8 (AVL, BOU, BRE, CRY, EVE, NFO, TOT + …) | 0.967 |
| 4 | 4 (ARS, CHE, LIV, MCI, MUN) | 0.593 |

Three distinct values for the entire league. Facing newly-promoted Coventry is treated as
**identical** to facing Newcastle. And one integer of coarse strength is a **1.91× step
change** in projected attacking output — a cliff, not a slope.

What that produces for GW1 (Gaffer's own top-20, deployed):

| Gaffer xP | Player | Fixture | FPL `ep_next` |
|---|---|---|---|
| **7.44** | B.Fernandes | MUN away at HUL (rated 2) | 4.0 |
| 6.49 | Enzo | CHE away at FUL (2) | 2.5 |
| 6.33 | Virgil | LIV away at NEW (2) | 3.1 |
| 6.16 | Mbeumo | MUN away at HUL (2) | 2.8 |
| 5.83 | Gabriel | ARS home to COV (2) | 4.0 |
| **5.33** | **Haaland** | MCI home to BOU (3) | 4.0 |
| 4.94 | Mukiele | SUN away at IPS (2) | 2.5 |
| 4.86 | Hume | SUN away at IPS (2) | 1.7 |
| 4.83 | Alderete | SUN away at IPS (2) | 2.2 |

Bruno is projected **40% above Haaland** and three Sunderland defenders make the top 20 —
entirely because of which bucket their GW1 opponent falls into. Six different teams
(ARS, AVL, CHE, LIV, MUN, **SUN**) all receive the maximum-easy `difficulty=1, att=1, def=1`.

This is not a pre-season curiosity. It is the regime that governs **the initial 15** —
the single highest-stakes decision of the season, and the one you're about to make.

---

## 1.3 The shipped model is the worst of the four in Gaffer's own backtest

> [!WARNING] **Superseded — corrected 2026-08-21. Kept as a record of the 2026-08-14 audit.**
> Two of the four columns below are no longer admissible or comparable, so the
> heading's claim does not describe any current measurement.
>
> - **`FPL ep_next` has been withdrawn as a baseline** — `withdrawn_baselines.fpl_xp`
>   in `data/backtest.json`, withdrawn in schema 4. It was computed from the archive's
>   `xP` column, which the upstream data dictionary states is FPL's `ep_this` **scraped
>   after each gameweek has ended**. It is inadmissible as a pre-deadline forecast, so
>   the column it "wins" here is not a win.
> - **`naive` is cumulative season-to-date points per game**, not "a rolling average of
>   recent points" as the reading below calls it.
>
> **Current measurement** — 2025-26, h=1, `heuristic-0.5`, 29,338 predictions:
> gaffer's legal XI **49.3 pts/gw vs naive 44.6** (gaffer *ahead*, the first season it
> has been), while rank correlation is **0.447 vs 0.692** and MAE **1.592 vs 1.075**
> (gaffer behind). The direction of the original finding — the model is weak at
> *ordering* — still holds. The table itself does not.


From `data/backtest.json` — 10,011 predictions, 2024-25, out-of-sample:

| Metric | **gaffer (shipped)** | ml (trained, unused) | **FPL `ep_next`** | naive |
|---|---|---|---|---|
| Rank correlation ↑ | **0.300** | 0.379 | **0.572** | 0.308 |
| MAE ↓ | 1.889 | 1.858 | **1.804** | 2.064 |
| Top-20% lift ↑ | 3.91 | 4.16 | **5.06** | — |
| **Top-XI actual pts/GW ↑** | **55.3** | 55.7 | **76.0** | 47.9 |

Two readings that matter:

- On ordering players — the only thing a picker needs — the shipped model scores **0.300
  vs naive's 0.308**. It is *worse than a rolling average of recent points.*
- On the decision-level test (field each model's best legal XI, count what it actually
  scored): **55.3 vs 76.0 pts/GW**. Over 38 gameweeks that's a ~790-point gap against a
  number that is already sitting in the `bootstrap-static` payload Gaffer downloads
  every run.

Live cross-check today: Spearman between Gaffer's `next_gw_xp` and FPL's `ep_next` across
514 players is **0.521** — they substantially disagree, and the backtest says FPL is right.

Related: `src/gaffer/ml.py` trains a gradient-boosted model that beats the heuristic on
every metric. It is imported **only** by `backtest.py`. It is not in the pipeline.
`data/model/gaffer_gbm.joblib` (408 KB) is committed dead weight.

The Accuracy page does print the full table honestly, but the narrative copy frames it as
"ML beats the transparent heuristic," which soft-pedals the actual headline — that both
lose to the free number.

*Fair caveat:* `ep_next` comes from the same payload, so using it isn't cheating, and
these aren't mutually exclusive. The obvious move is a blend, or `ep_next` as a prior the
component model adjusts, with the backtest arbitrating.

---

# Tier 2 — Optimising for the wrong thing

## 2.1 Global effective ownership is the wrong reference class for a 4-person league

League 271619 "Crouch Potatoes" has **four entries**:

| Entry | Manager | Team |
|---|---|---|
| 1066421 | **Myles Colling** (you, admin) | The Ødeyssey |
| 1094262 | Kevin Colling | Dynamo Kev |
| 408467 | Nat Uttley | nuttley |
| 1711764 | Hakan Duzel | Mikel Farteta |

The whole rank-defence apparatus keys off `selected_by_percent` — ownership across ~11M
managers:

```python
# solver/optimize.py
RISK_WEIGHTS = {"differential": 0.0, "balanced": 8.0, "template": 11.0}
obj += template_weight * lpSum(start[i] * (players[i].ownership / 100.0) * next_gw_points)
```

In a four-person league the only ownership values that exist are **0%, 33%, 67%, 100%**,
over three specific squads you can read from the API. Owning Haaland at 74.5% global EO is
rank defence against the field. Against Kevin, Nat and Hakan it is worth **nothing** if all
three own him — and it is actively a *liability* if none of them do and you're behind.

The objectives are mathematically different, not just differently tuned:

- **Global rank** is a smooth distribution over millions → maximise EV, hedge EO.
- **A 4-man league** is a discrete race → maximise **P(finish 1st of 4)**, a function of
  the *point-differential distribution against three known squads*. Leading late, you
  minimise variance vs the field. Trailing, you maximise it. Gaffer models neither.

Gaffer already computes the Monte-Carlo distributions (`model/simulate.py`, 3000 sims per
player) that would make this tractable — they're used only for a ceiling term in the
objective, not for win probability.

## 2.2 The League page is a scoreboard, not a weapon

`web/src/pages/MiniLeague.svelte` fetches standings and each member's `entryHistory`, then
draws points-race / per-GW / rank charts, GW wins, hits, bench points.

It **never fetches a single rival's picks** — despite `fpl.picks(id, gw)` already existing
and working in `web/src/lib/fpl.ts`, and `entry_picks` / `entry_transfers` /
`league_classic` all sitting unused in the Python client.

Everything that would actually win the league is absent:

- Who owns what that you don't (and vice versa)
- League-EO per player (0/1/2/3 of your rivals)
- Captain divergence — the biggest single weekly swing
- "You and Kevin differ by exactly these 4 players; that's ±X projected"
- Rival chip usage (wildcard/BB/TC/FH burned or held)
- Rival bank and team value
- "You're 32 behind with 6 GWs left — here's the variance you need"

## 2.3 Your record says the constraint is consistency, not sharpness

From `entry/1066421/history/`:

| Season | Points | Rank | Percentile |
|---|---|---|---|
| 2020/21 | 891 | 7,818,884 | 95% |
| 2021/22 | — | **season skipped entirely** | — |
| 2022/23 | 2,204 | 2,644,294 | 23% |
| 2023/24 | 2,000 | 4,785,011 | 44% |
| 2024/25 | 2,141 | 3,704,825 | 32% |
| 2025/26 | 2,069 | 2,800,709 | 22% |

Best finish is the 22nd percentile. One season missing. One season at 891 points — about
what a squad scores when it's left on autopilot from partway through.

The second half of your brief isn't a nice-to-have bolted onto the first. **It is the
first.** A 2,069-point manager who plays all 38 gameweeks attentively beats a 2,200-point
model that gets abandoned in November.

---

# Tier 3 — Model weaknesses, by leverage

## 3.1 Minutes — the crudest component, and the one that matters most

Every projection is gated by `p_start`. Pre-season that comes from:

```python
elif base_min > 90 and player["base_starts"]:
    base_start = clamp(player["base_starts"] / 38.0, 0.0, 0.98)
else:
    base_start = _start_prior(pos, player["price"])   # price-based guess
```

`starts_last_season / 38` punishes anyone who missed time. Live example:

**Lewis Cook** (BOU, status `a`, fully fit) — 8 starts last season after injury →
`p_start = 0.21` → badge **"CAMEO?"** → 23 expected minutes → his **14.8 CBIRT/90**
becomes a **0.7%** DEFCON hit probability. He is a first-choice Bournemouth midfielder.

The same error hits every January signing, every promoted player, every player returning
from a long injury, and everyone new to the league. Eight players priced ≥£6.0m have no
baseline at all and fall back to the price heuristic: N.Jackson, Rashford, Muñoz,
Manzambi, Maddison, Rodríguez, Touré, Kulusevski.

Nothing ingests pre-season minutes, predicted XIs, or lineup/press-conference data.

## 3.2 DEFCON dispersion is an unfitted guess, and it's too fat

```python
DEFCON_NB_DISPERSION = 6.0  # "~6 is a mild, defensible over-dispersion"
```

NegBin(μ=14.8, r=6) implies variance 51.3, SD 7.2 — a coefficient of variation of **0.49**.
Consequence, computed directly:

| Expected minutes | μ | P(hit 12) |
|---|---|---|
| 90 | 14.80 | **0.638** |
| 82 | 13.48 | 0.568 |
| 70 | 11.51 | 0.445 |
| 60 | 9.87 | 0.328 |

A player averaging **14.8** actions against a threshold of **12**, playing a full 90, is
given only a **64%** hit rate. That is far too flat. It drags high-volume specialists
toward the middle and inflates low-volume ones — precisely inverting the edge that cheap
DEFCON defenders and defensive mids are supposed to provide.

The parameter is fittable. `data/history/` already holds 15 MB of vaastav data, and the
live API exposes `tackles`, `clearances_blocks_interceptions`, `recoveries`,
`defensive_contribution` and `defensive_contribution_per_90` per player.

**Also:** DEFCON receives **no fixture adjustment at all**. Defensive actions are strongly
opponent-dependent — you make more tackles and clearances against better teams, the
*opposite* direction to clean sheets. Gaffer applies a fixture multiplier to goals and to
clean sheets, and nothing to the third scoring category.

Thresholds themselves are correct for 2026/27: 10 CBIT for defenders, 12 CBIRT for
midfielders and forwards, capped at +2. ([Premier League](https://www.premierleague.com/en/news/4361991/whats-happening-with-defensive-contribution-points-in-202627-fantasy), [FPL Oracle](https://fploracle.team/blog/defensive-contributions-fpl-explained))

## 3.3 The bonus proxy bakes in rules that changed this season

```python
exp_bonus_pts = 0.55 * (exp_goals + exp_assists) + 0.25 * exp_defcon_pts
if pos in ("GKP", "DEF"):
    exp_bonus_pts += 0.35 * exp_cs_pts / max(cs_pts_per, 1)
```

Hand-tuned coefficients, no BPS modelling. For 2026/27 the Premier League explicitly
changed BPS to reduce overlap with defensive contributions — clearances, blocks and
interceptions now count for **less** toward bonus, raising bonus potential for keepers,
full-backs, attacking mids and forwards relative to pure-CBI centre-backs.
([Fantasy Football Scout](https://www.fantasyfootballscout.co.uk/2026/07/20/fpl-2026-27-5-rule-changes-new-features-announced), [RotoWire](https://www.rotowire.com/soccer/article/fpl-2026-27-rule-changes-player-prices-and-position-changes-explained-124721))
Gaffer's proxy encodes the old relationship.

## 3.4 No autosub modelling

Bench is ordered by `value` and nothing values the probability a bench player is
auto-subbed on. `multiperiod.BENCH_WEIGHT = 0.15` is a flat weight, not an autosub
probability. Grep for `autosub` across the repo: zero hits. Bench value is a real fraction
of a season's points, especially with rotation-risk assets.

## 3.5 Price changes aren't in the optimiser

`export/artifacts._price_pred` estimates rises/falls for display, but
`solver/multiperiod.py` states plainly: *"Prices are treated as static across the short
planning horizon."* Team-value growth compounds across 38 gameweeks and is a meaningful
part of the gap between a mid-table manager and a good one.

## 3.6 Two sources of truth for "my team"

`data/my_team.json` (FPL API → backend) and a **localStorage** plan (`lib/squad.ts`
`loadCurrent()`, written by the Planner). `Overview.svelte` prefers the localStorage one
whenever it's valid. They can silently disagree — and the localStorage copy doesn't survive
a browser change or a phone.

## 3.7 Stale UI copy contradicting the engine

Overview's "Template check" card:

> "The model optimises points-per-£ and doesn't weigh ownership — so it punts these
> heavily-owned picks."

The shipped solver runs `template_weight = 8.0` and is explicitly EO-aware, including for
captaincy. The card describes a previous version of the engine.

---

# Tier 4 — What's missing for engagement

This is the half of the brief the product doesn't address at all.

- **No notification of any kind.** The deadline countdown exists in the Topbar — visible
  only if you've already opened the site. No PWA manifest, no service worker, no push, no
  email, no Telegram/Discord. The tool cannot reach you.
- **Nothing exists between deadlines.** No live-gameweek view, though the proxy already
  exposes `/ev/{gw}/live` and nothing calls it. No "your rank moved." No post-GW retro:
  what you scored, what your rivals scored, what each decision cost or won.
- **No habit mechanic.** Nothing records whether you actually made your move. No streak,
  no "you've set your team 12 weeks running."
- **The stakes aren't surfaced.** *Your dad is in this league.* That's the strongest
  engagement hook available and the tool doesn't know it exists.
- **No season narrative.** Nothing accumulates. Nothing you'd want to come back and read.
- **The Verdict is generic and one-shot.** "Build from scratch, and the whole thing hinges
  on captaining Haaland" — written once per pipeline run, referencing no rival, no league
  position, and nothing that happened last week.

---

# Tier 5 — Hygiene

- `enrich_history` makes ~350 **sequential** HTTP calls per run (~200s). Trivially
  parallelisable. In CI it re-runs in full every time (fresh checkout, no cache).
- The site is a client-rendered SPA with no prerender — crawlers and link previews get an
  empty shell (`web/index.html` is a bare `<div id="app">`).
- `web/shot-v1.mjs`, `shot4.mjs`, `shot5.mjs` — dev screenshot scripts, gitignored but
  sitting in the working tree.
- CI: Node 20 deprecation warnings on `actions/checkout@v4` and `actions/setup-python@v5`.
- PuLP: 6,795 deprecation warnings across the test run (`LpVariable` construction,
  `PULP_CBC_CMD`) — both removed in PuLP 4.0, and `pyproject.toml` pins `<4.0`.
- `scripts/refresh.sh` + `com.gaffer.pipeline.plist` are dead now that Actions runs the
  refresh; the plist still contains the literal `REPO_PATH` placeholder.
- Tests: **29 pass**, ~3s. Nothing covers the export layer, the artifact write path, or the
  pre-season regime — i.e. none of the three Tier-1 failures would have been caught.
- Unused API fields worth having: `scout_news_link` (27 players carry a direct injury-news
  URL today), `chance_of_playing_this_round`, `starts_per_90`, `can_select` / `can_transact`,
  `birth_date`, `squad_number`, `team_join_date`.

---

# What I'd do, in order

**Before the GW1 deadline — 21 August 2026 at 18:30 BST (17:30 UTC) — you have 15 days.**

1. **Fix the data path** (Tier 1.1). One line. Nothing else matters until the site is
   showing today's numbers. Add a CI assertion that `generated_at` actually advanced.
2. **Blend `ep_next` into the projection** (Tier 1.3), with the backtest deciding the
   weight. This is the largest single accuracy win available and it costs nothing — the
   data is already downloaded. Re-run `backtest.py` to set the blend rather than guessing.
3. **Fix the pre-season fixture regime** (Tier 1.2). Options: raise the clamp ceiling and
   lower gamma so the mapping is monotone rather than saturating; or build team ratings
   from last season's actual xG/xGA instead of the coarse 1–5 (you already have the
   history data); or blend both. Anything that stops Coventry and Newcastle being the same
   fixture.
4. **Sanity-gate the initial squad.** After 1–3, the GW1 fifteen is the deliverable.

**Then, for the league (weeks 1–4 of the season):**

5. **Rival intelligence layer.** Ingest all four entries' picks each GW into SQLite
   (`entry_picks` already exists in the client). Compute league-EO, differentials, captain
   divergence, and the exact player-level delta against each rival. This is the highest-
   value feature in the entire backlog and it's mostly plumbing you've already written.
6. **Swap the objective from global EO to P(win the league).** You already have per-player
   Monte-Carlo distributions. Simulate your XI vs the three rival XIs and optimise the win
   probability, not expected points. This is the thing that makes Gaffer a league-winning
   tool rather than a good generic one.
7. **Chip timing against rivals, not the calendar.** A Triple Captain is worth far more
   when your rivals *don't* own the captain.

**For engagement (do at least one before GW1):**

8. **Get the tool to reach you.** PWA + push is the cheap version; a Telegram bot from the
   Mac Mini is the reliable one. Two messages a week: a deadline nudge with the one
   decision that matters, and a post-GW result vs your rivals.
9. **Post-gameweek retro.** "You 61, Kevin 58, Nat 44, Hakan 67. Captaining Bruno over
   Haaland cost you 8. You're 2nd, 9 behind." Concrete, personal, rivalrous — this is what
   makes you open it again.
10. **Make the Verdict about your league.** It already has an LLM in the loop; give it the
    rival context from (5) and it writes something worth reading instead of generic
    touchline prose.

**Deliberately later:** wire in the trained GBM (3.1–3.3 will move accuracy more than the
model class does), price-change optimisation, autosub modelling, BPS modelling.

---

# Corrections (added 2026-08-07, after Batch 6)

Three findings above were wrong, and one number was measured against a leaky
column. Recorded here rather than edited away, because an audit that quietly
rewrites itself is not an audit.

**1.3 — "FPL `ep_next` beats the shipped model by 20 points a gameweek."**
Withdrawn. Both the `ep_next` and blend numbers were computed from the vaastav
archive's `xP` column, which is **not** FPL's pre-deadline `ep_next`. Within a
player, across single-fixture gameweeks he completed 60+ minutes of, `xP` moves
with the *result* — sd 1.75 points, correlation +0.40 to +0.46 with the deviation
in what he scored, against +0.09 and −0.13 for two quantities that are
pre-deadline by construction. See [`docs/MODEL-EVALUATION.md`](docs/MODEL-EVALUATION.md)
and `python -m gaffer.backtest --xp-diagnostic`.

The live `ep_next` may well be excellent. It is simply **unmeasured**: the
archive holds no faithful copy of it. `EP_NEXT_BLEND_WEIGHT = 0.7` is unchanged
and now labelled a policy choice rather than a fitted parameter.

**1.3 — "`ml.py` beats the heuristic on every metric."** Also from the same
harness. Rebuilt properly — leakage-checked adapter, production-available
features only, chronological splits, test season untouched until selection closed
— a trained model **loses to the heuristic on every decision metric at every
horizon** (−4.2 to −8.2 legal-XI points per gameweek) while winning every
statistical one. `ml.py` and the committed model artifact are removed.

**T-27 — "delete `proxy/worker.js` and `wrangler.toml`."** Wrong. The proxy is
**live**: `DEFAULT_API_BASE` in `web/src/lib/config.ts` points at the deployed
Val Town instance, and `MiniLeague`, `MyTeam` and `Planner` all fetch through it
for per-user data the static artifacts cannot hold. The whole `proxy/` directory
is retained, Cloudflare variant included, as the escape hatch for the one piece
of infrastructure the live site depends on.

**T-27 — "duplicate `player_gw` definitions."** There is one definition. What
existed was a *pre-migration* table shape, handled by `db.migrate`, which is
migration support for old databases and must not be deleted.
