const ALLOWED_ORIGINS = [
  "https://emptycornmeal.github.io",
  "https://emptycornmeal.github.io/fpl-assistant",
  "http://localhost:4173",
  "http://localhost:5173",
  "http://127.0.0.1:4173",
];

const IMAGE_ROUTES = [
  {
    type: "player",
    re: /^\/img\/player\/(\d+x\d+)\/p?(\d+)\.png$/i,
    to: (m) => `https://resources.premierleague.com/premierleague/photos/players/${m[1]}/p${m[2]}.png`,
    cacheSeconds: 60 * 60 * 24, // 1 day
    contentType: "image/png",
    fallback: true,
  },
  {
    type: "badge",
    re: /^\/img\/badge\/(\d+)\/t(\d+)\.png$/i,
    to: (m) => `https://resources.premierleague.com/premierleague/badges/${m[1]}/t${m[2]}.png`,
    cacheSeconds: 60 * 60 * 24,
    contentType: "image/png",
  },
];

function resolveOrigin(request) {
  const origin = request.headers.get("Origin");
  if (!origin) return "*";
  if (ALLOWED_ORIGINS.includes(origin)) return origin;
  if (origin.endsWith(".github.io")) return origin;
  if (/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin)) return origin;
  return origin;
}

function buildCorsHeaders(request, extra = {}) {
  return {
    "Access-Control-Allow-Origin": resolveOrigin(request),
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": request.headers.get("Access-Control-Request-Headers") || "Content-Type, Authorization, Accept",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
    ...extra,
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // ----- CORS preflight -----
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: buildCorsHeaders(request),
      });
    }

    // Dedicated player photo proxy (avoids CORS and enables caching)
    const playerPhotoMatch = url.pathname.match(/^\/api\/player-photo\/(\d+)(?:\.png)?$/i);
    if (playerPhotoMatch) {
      return handlePlayerPhoto(request, ctx, playerPhotoMatch[1]);
    }

    // Health ping (support multiple aliases to avoid noisy 404s)
    if (["/api/up", "/api/health", "/api/status", "/up", "/health", "/status"].includes(url.pathname)) {
      return json(request, 200, { ok: true, ts: Date.now() });
    }

    const imgRoute = matchImageRoute(url.pathname);
    if (imgRoute) {
      return proxyImage(request, imgRoute);
    }

    // Non-API paths → simple OK
    if (!url.pathname.startsWith("/api/")) {
      return new Response("OK", {
        status: 200,
        headers: buildCorsHeaders(request, {
          "Cache-Control": "no-store",
          "CDN-Cache-Control": "no-store",
        })
      });
    }

    // Map friendly path to FPL API path
    const sub = url.pathname.slice(5); // strip "/api/"
    const mapped = mapPath(sub);
    if (!mapped.ok) {
      return json(request, 404, { error: "Unknown API path", sub, hint: mapped.hint });
    }

    // Build upstream URL and preserve querystring only if rule didn't add one
    const upstream = new URL(`https://fantasy.premierleague.com/api/${mapped.path}`);
    if (!upstream.search && url.search) upstream.search = url.search;

    try {
      const r = await fetch(upstream.toString(), {
        method: "GET",
        redirect: "follow",
        headers: {
          "User-Agent": "Mozilla/5.0 (compatible; FPL-Dashboard/1.0)",
          "Accept": "application/json, text/javascript, */*; q=0.01",
          "Origin": "https://fantasy.premierleague.com",
          "Referer": "https://fantasy.premierleague.com/"
        },
        cf: { cacheTtl: 0, cacheEverything: false }
      });

      const h = new Headers(r.headers);
      const corsHeaders = buildCorsHeaders(request, {
        "Cache-Control": "no-store",
        "CDN-Cache-Control": "no-store",
      });
      Object.entries(corsHeaders).forEach(([k, v]) => h.set(k, v));
      if (!h.has("Content-Type")) h.set("Content-Type", "application/json; charset=utf-8");

      return new Response(r.body, { status: r.status, statusText: r.statusText, headers: h });
    } catch (err) {
      return json(request, 502, {
        error: "Upstream fetch failed",
        details: String(err?.message || err),
        upstream: upstream.toString()
      });
    }
  }
};

/* ---------------- helpers ---------------- */
function json(request, status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: buildCorsHeaders(request, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "CDN-Cache-Control": "no-store",
    })
  });
}

function matchImageRoute(pathname) {
  for (const route of IMAGE_ROUTES) {
    const match = pathname.match(route.re);
    if (match) return { ...route, match };
  }
  return null;
}

async function proxyImage(request, route) {
  const cacheSeconds = route.cacheSeconds || 3600;
  const upstream = route.to(route.match);
  const placeholderSvg = `
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="120" height="120">
      <rect width="120" height="120" rx="12" ry="12" fill="#0f172a" />
      <circle cx="60" cy="50" r="22" fill="#1e293b" stroke="#334155" stroke-width="4" />
      <rect x="32" y="74" width="56" height="30" rx="8" fill="#1e293b" stroke="#334155" stroke-width="4" />
      <text x="60" y="68" text-anchor="middle" fill="#94a3b8" font-family="Inter, sans-serif" font-size="12" font-weight="700">FPL</text>
    </svg>`;

  try {
    const r = await fetch(upstream, {
      method: "GET",
      redirect: "follow",
      headers: {
        "User-Agent": "Mozilla/5.0 (compatible; FPL-Dashboard/1.0)",
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Origin": "https://fantasy.premierleague.com",
        "Referer": "https://fantasy.premierleague.com/"
      },
      cf: { cacheTtl: cacheSeconds, cacheEverything: true }
    });

    if (!r.ok && route.fallback) {
      return new Response(placeholderSvg.trim(), {
        status: 200,
        headers: buildCorsHeaders(request, {
          "Content-Type": "image/svg+xml",
          "Cache-Control": "public, max-age=600",
          "CDN-Cache-Control": "max-age=600",
        })
      });
    }

    const headers = buildCorsHeaders(request, {
      "Content-Type": r.headers.get("Content-Type") || route.contentType || "image/png",
      "Cache-Control": `public, max-age=${cacheSeconds}`,
      "CDN-Cache-Control": `max-age=${cacheSeconds}`,
    });

    return new Response(r.body, { status: r.status, statusText: r.statusText, headers });
  } catch (err) {
    return new Response(placeholderSvg.trim(), {
      status: 200,
      headers: buildCorsHeaders(request, {
        "Content-Type": "image/svg+xml",
        "Cache-Control": "public, max-age=300",
        "CDN-Cache-Control": "max-age=300",
      })
    });
  }
}

async function handlePlayerPhoto(request, ctx, photoId) {
  const cache = caches.default;
  const cacheKey = new Request(request.url, request);

  const cached = await cache.match(cacheKey);
  if (cached) {
    return new Response(cached.body, {
      status: cached.status,
      statusText: cached.statusText,
      headers: buildCorsHeaders(request, {
        "Content-Type": cached.headers.get("Content-Type") || "image/png",
        "Cache-Control": cached.headers.get("Cache-Control") || "public, max-age=86400",
        "CDN-Cache-Control": cached.headers.get("CDN-Cache-Control") || "max-age=86400",
      })
    });
  }

  const upstream = `https://resources.premierleague.com/premierleague/photos/players/250x250/p${photoId}.png`;
  try {
    const response = await fetch(upstream, {
      method: request.method === "HEAD" ? "HEAD" : "GET",
      headers: {
        "User-Agent": "Mozilla/5.0 (compatible; FPL-Dashboard/1.0)",
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Origin": "https://fantasy.premierleague.com",
        "Referer": "https://fantasy.premierleague.com/"
      },
      cf: { cacheEverything: true, cacheTtl: 86400 }
    });

    if (response.status === 404) {
      return new Response(null, {
        status: 404,
        headers: buildCorsHeaders(request, {
          "Cache-Control": "public, max-age=60",
          "Content-Type": "image/png",
          "CDN-Cache-Control": "max-age=60",
        })
      });
    }

    const proxied = new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: buildCorsHeaders(request, {
        "Content-Type": "image/png",
        "Cache-Control": "public, max-age=86400",
        "CDN-Cache-Control": "max-age=86400",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
      })
    });

    ctx.waitUntil(cache.put(cacheKey, proxied.clone()));
    return proxied;
  } catch (err) {
    return new Response(JSON.stringify({ error: "Failed to fetch player photo", details: String(err?.message || err) }), {
      status: 502,
      headers: buildCorsHeaders(request, {
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
      })
    });
  }
}

/**
 * Friendly → FPL API path mapper.
 * Ensures trailing slashes where FPL expects them (avoids 301s/CORS).
 */
function mapPath(sub) {
  const rules = [
    // Bootstrap & element summaries
    [/^bs\/?$/,                     () => "bootstrap-static/"],
    [/^es\/(\d+)\/?$/,              m => `element-summary/${m[1]}/`],

    // Live scoring + status
    [/^ev\/(\d+)\/live\/?$/,        m => `event/${m[1]}/live/`],
    [/^ev\/status\/?$/,             () => "event-status/"],

    // Entry & picks
    [/^ep\/(\d+)\/(\d+)\/picks\/?$/, m => `entry/${m[1]}/event/${m[2]}/picks/`],
    [/^en\/(\d+)\/?$/,               m => `entry/${m[1]}/`],
    [/^en\/(\d+)\/history\/?$/,      m => `entry/${m[1]}/history/`],

    // Fixtures (optionally by event)
    [/^fx\/?$/,                     () => "fixtures/"],
    [/^fx\/(\d+)\/?$/,              m => `fixtures/?event=${m[1]}`],

    // Classic leagues (paginated)
    [/^lc\/(\d+)\/(\d+)\/?$/,       m => `leagues-classic/${m[1]}/standings/?page_standings=${m[2]}`],
  ];

  for (const [re, to] of rules) {
    const m = sub.match(re);
    if (m) return { ok: true, path: to(m) };
  }
  return { ok: false, hint: "Unknown pattern. Example: ev/2/live, ev/status, bs, es/1, en/12345" };
}
