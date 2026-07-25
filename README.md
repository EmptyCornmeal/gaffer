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
- **Optimise** (`gaffer.solver`) — a PuLP MILP. *Build mode* picks the optimal
  £100m squad; *transfer mode* trades expected-point gains against −4 hits.
  Respects budget, position quotas, 3-per-club, formation, captaincy.
- **Export** (`gaffer.export`) — denormalised `data/*.json` the front-end reads.

## Quick start

```bash
# backend
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # or .venv/bin/pip on macOS
.venv/Scripts/python -m gaffer.pipeline        # writes data/*.json  (--fast skips DEFCON enrichment)
python -m pytest                               # 12 tests

# front-end
cd web && npm install
cp ../data/*.json public/data/                 # stage artifacts for the dev server
npm run dev                                    # http://localhost:5173
```

To track your own team, drop a git-ignored `gaffer.local.toml` at the repo root:

```toml
[fpl]
entry_id = 1234567
league_ids = [111, 222]
```

## Deployment (free, hybrid)

- **Mac Mini** runs `scripts/refresh.sh` on a schedule
  (`scripts/com.gaffer.pipeline.plist` launchd job): pipeline → stage JSON →
  commit + push. Later hosts the subscription AI layer.
- **GitHub Pages** serves the static Svelte build; `.github/workflows/deploy.yml`
  rebuilds on every push that touches `web/**` or `data/**`.

## Roadmap

- **Phase 1 (now):** heuristic model + solver + phone-first UI. ✅
- **Phase 2:** swap the heuristic for trained LightGBM component models on the
  vaastav historical dataset; multi-GW transfer paths + chip timing.
- **Phase 3:** news layer (predicted XIs, injuries, pressers) + an AI briefing
  that ranks moves, explains them, and flags model-vs-news conflicts.

## Stack

Python 3.12+ (httpx, pandas, PuLP) · SQLite · Svelte 5 + Vite + Tailwind 4.
