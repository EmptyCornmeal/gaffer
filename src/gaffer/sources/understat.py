"""Fetch per-player season stats from Understat.

Understat embeds its data as hex-escaped JSON inside ``<script>`` tags, e.g.
``var playersData = JSON.parse('...')``. We pull the EPL league page for a season
and decode ``playersData``.

Season is labelled by starting year: "2025" == the 2025/26 season.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; gaffer/0.1)"
_PATTERN = re.compile(r"var\s+playersData\s*=\s*JSON\.parse\('(?P<payload>.*?)'\)", re.DOTALL)


def _decode_payload(escaped: str) -> Any:
    """Understat double-escapes: \\xNN byte escapes wrapping a JSON string."""
    decoded = escaped.encode("utf-8").decode("unicode_escape")
    return json.loads(decoded)


def fetch_players(season: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    url = f"https://understat.com/league/EPL/{season}"
    owns = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0)
    try:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text
    finally:
        if owns:
            client.close()

    m = _PATTERN.search(html)
    if not m:
        return []
    raw = _decode_payload(m.group("payload"))

    out: list[dict[str, Any]] = []
    for p in raw:
        try:
            out.append(
                {
                    "us_id": int(p["id"]),
                    "name": p["player_name"],
                    "team": p["team_title"],
                    "season": season,
                    "games": int(p.get("games") or 0),
                    "minutes": int(p.get("time") or 0),
                    "goals": int(p.get("goals") or 0),
                    "assists": int(p.get("assists") or 0),
                    "xg": float(p.get("xG") or 0.0),
                    "xa": float(p.get("xA") or 0.0),
                    "npxg": float(p.get("npxG") or 0.0),
                    "shots": int(p.get("shots") or 0),
                    "key_passes": int(p.get("key_passes") or 0),
                }
            )
        except (KeyError, ValueError):
            continue
    return out
