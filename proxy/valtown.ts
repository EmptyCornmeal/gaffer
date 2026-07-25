// Gaffer proxy — Val Town / Deno Deploy edition.
//
// The FPL API blocks browser CORS, so per-user live data (team, picks, history,
// leagues, live points) is fetched through this tiny proxy. It also proxies
// player photos. No Cloudflare account needed.
//
// DEPLOY on Val Town (fastest, no card):
//   1. https://val.town → New → HTTP val
//   2. Paste this file's contents, Save.
//   3. Copy the val's URL and set it in Gaffer settings as:  <url>/api
//
// DEPLOY on Deno Deploy:
//   Add at the bottom:  Deno.serve(handler)   then deploy the file.
//
// The default export IS the handler (Val Town runs it directly).

const FPL = 'https://fantasy.premierleague.com/api'
const PHOTO = 'https://resources.premierleague.com/premierleague/photos/players/250x250'

const UA = {
  'User-Agent': 'Mozilla/5.0 (compatible; Gaffer/1.0)',
  Accept: 'application/json, text/javascript, */*; q=0.01',
  Referer: 'https://fantasy.premierleague.com/',
}

function cors(extra: Record<string, string> = {}): Record<string, string> {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    Vary: 'Origin',
    ...extra,
  }
}

// friendly /api/* → FPL path
function mapPath(sub: string): string | null {
  const rules: [RegExp, (m: RegExpMatchArray) => string][] = [
    [/^bs\/?$/, () => 'bootstrap-static/'],
    [/^es\/(\d+)\/?$/, (m) => `element-summary/${m[1]}/`],
    [/^ev\/(\d+)\/live\/?$/, (m) => `event/${m[1]}/live/`],
    [/^ev\/status\/?$/, () => 'event-status/'],
    [/^ep\/(\d+)\/(\d+)\/picks\/?$/, (m) => `entry/${m[1]}/event/${m[2]}/picks/`],
    [/^en\/(\d+)\/history\/?$/, (m) => `entry/${m[1]}/history/`],
    [/^en\/(\d+)\/?$/, (m) => `entry/${m[1]}/`],
    [/^fx\/(\d+)\/?$/, (m) => `fixtures/?event=${m[1]}`],
    [/^fx\/?$/, () => 'fixtures/'],
    [/^lc\/(\d+)\/(\d+)\/?$/, (m) => `leagues-classic/${m[1]}/standings/?page_standings=${m[2]}`],
  ]
  for (const [re, to] of rules) {
    const m = sub.match(re)
    if (m) return to(m)
  }
  return null
}

export async function handler(req: Request): Promise<Response> {
  const url = new URL(req.url)
  const path = url.pathname

  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors() })

  if (['/api/up', '/api/health', '/up', '/health'].includes(path)) {
    return Response.json({ ok: true, ts: Date.now() }, { headers: cors() })
  }

  // player photo proxy
  const photo = path.match(/^\/api\/player-photo\/(\d+)(?:\.png)?$/i)
  if (photo) {
    try {
      const r = await fetch(`${PHOTO}/p${photo[1]}.png`, { headers: UA })
      return new Response(r.body, {
        status: r.status,
        headers: cors({ 'Content-Type': 'image/png', 'Cache-Control': 'public, max-age=86400' }),
      })
    } catch {
      return new Response(null, { status: 502, headers: cors() })
    }
  }

  if (!path.startsWith('/api/')) return new Response('OK', { headers: cors() })

  const mapped = mapPath(path.slice(5))
  if (!mapped) return Response.json({ error: 'unknown path' }, { status: 404, headers: cors() })

  try {
    const upstream = new URL(`${FPL}/${mapped}`)
    if (!upstream.search && url.search) upstream.search = url.search
    const r = await fetch(upstream.toString(), { headers: UA, redirect: 'follow' })
    const body = await r.text()
    return new Response(body, {
      status: r.status,
      headers: cors({ 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }),
    })
  } catch (err) {
    return Response.json({ error: 'upstream failed', detail: String(err) }, { status: 502, headers: cors() })
  }
}

export default handler
