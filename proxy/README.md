# Gaffer proxy (Cloudflare Worker)

The FPL API blocks browser CORS, so per-user **live** data (your team, picks,
history, classic leagues, live points, player photos) is fetched in-browser
through this tiny Worker. The heavy analytics come from the static `data/*.json`
built by the Python pipeline — this proxy is only for live, per-user calls.

Ported from the v1 `fpl-assistant` worker. It resolves any origin (so it works
from GitHub Pages and localhost) and exposes friendly `/api/*` paths.

## Friendly paths
| Path | FPL endpoint |
|---|---|
| `/api/bs` | `bootstrap-static/` |
| `/api/es/:id` | `element-summary/:id/` |
| `/api/ev/:gw/live` | `event/:gw/live/` |
| `/api/ev/status` | `event-status/` |
| `/api/ep/:entry/:gw/picks` | `entry/:entry/event/:gw/picks/` |
| `/api/en/:entry` · `/api/en/:entry/history` | `entry/:entry/` · `.../history/` |
| `/api/fx` · `/api/fx/:gw` | `fixtures/` · `fixtures/?event=:gw` |
| `/api/lc/:league/:page` | `leagues-classic/:league/standings/?page_standings=:page` |
| `/api/player-photo/:code` | player photo (cached, SVG fallback) |
| `/api/up` | health check |

## Deploy — Val Town (recommended, ~2 min, no card)
`valtown.ts` is a ready-to-paste HTTP val.
1. https://val.town → **New → HTTP val**
2. Paste the contents of `valtown.ts`, **Save**.
3. Copy the val's URL and set it in Gaffer's **Settings → Proxy API base** as `<url>/api`.

Same file runs on **Deno Deploy** — add `Deno.serve(handler)` at the bottom and deploy.

## Deploy — Cloudflare Worker (alternative)
```bash
cd proxy
npx wrangler login        # once
npx wrangler deploy       # → https://gaffer-proxy.<subdomain>.workers.dev
```
Then set the Worker base in Gaffer settings (or `localStorage.gaffer.apiBase`,
the `?api=` query param, or `window.__GAFFER_API__`).

## What it unlocks
With a proxy set + your Entry ID: **My Team** (live squad), **Mini-League**
standings, and the Planner's **"Import my team"** button (pull your real squad,
then plan transfers for the next GW). Live data appears once the GW1 deadline
passes (the FPL picks endpoint is empty pre-season).
