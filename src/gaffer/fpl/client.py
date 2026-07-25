"""Client for the official Fantasy Premier League API.

All endpoints are public (no auth). We add a small on-disk cache so repeated
pipeline runs and tests don't hammer the API. ``bootstrap-static`` and
``fixtures`` change slowly within a gameweek, so a short TTL is plenty; live
endpoints use a very short TTL.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from gaffer import config

BASE = "https://fantasy.premierleague.com/api"
USER_AGENT = "gaffer/0.1 (+https://github.com/EmptyCornmeal)"


class FplClient:
    def __init__(self, cache_dir: Path | None = None, default_ttl: float = 3600.0) -> None:
        self.cache_dir = cache_dir or config.CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        )

    # -- low level ----------------------------------------------------------
    def _cache_file(self, path: str) -> Path:
        safe = path.strip("/").replace("/", "_") or "root"
        return self.cache_dir / f"{safe}.json"

    def _get(self, path: str, ttl: float | None = None) -> Any:
        ttl = self.default_ttl if ttl is None else ttl
        cf = self._cache_file(path)
        if cf.exists() and (time.time() - cf.stat().st_mtime) < ttl:
            return json.loads(cf.read_text(encoding="utf-8"))
        resp = self._client.get(f"{BASE}/{path.lstrip('/')}")
        resp.raise_for_status()
        data = resp.json()
        cf.write_text(json.dumps(data), encoding="utf-8")
        return data

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FplClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- endpoints ----------------------------------------------------------
    def bootstrap(self) -> dict[str, Any]:
        """Players (elements), teams, positions (element_types), and events."""
        return self._get("bootstrap-static/")

    def fixtures(self) -> list[dict[str, Any]]:
        return self._get("fixtures/")

    def element_summary(self, element_id: int) -> dict[str, Any]:
        """Per-player: history (this season), history_past, and fixtures."""
        return self._get(f"element-summary/{element_id}/")

    def event_live(self, gw: int) -> dict[str, Any]:
        return self._get(f"event/{gw}/live/", ttl=60.0)

    def entry(self, entry_id: int) -> dict[str, Any]:
        return self._get(f"entry/{entry_id}/", ttl=300.0)

    def entry_picks(self, entry_id: int, gw: int) -> dict[str, Any]:
        return self._get(f"entry/{entry_id}/event/{gw}/picks/", ttl=300.0)

    def entry_history(self, entry_id: int) -> dict[str, Any]:
        return self._get(f"entry/{entry_id}/history/", ttl=300.0)

    def entry_transfers(self, entry_id: int) -> list[dict[str, Any]]:
        return self._get(f"entry/{entry_id}/transfers/", ttl=300.0)

    def league_classic(self, league_id: int, page: int = 1) -> dict[str, Any]:
        return self._get(
            f"leagues-classic/{league_id}/standings/?page_standings={page}",
            ttl=300.0,
        )

    # -- helpers ------------------------------------------------------------
    def current_gw(self) -> int:
        """The gameweek to act on: the first not-yet-finished event.

        Falls back to ``is_next``/``is_current`` flags; before the season starts
        this returns GW1.
        """
        events = self.bootstrap()["events"]
        for ev in events:
            if ev.get("is_next"):
                return int(ev["id"])
        for ev in events:
            if not ev.get("finished"):
                return int(ev["id"])
        return int(events[-1]["id"]) if events else 1

    def last_finished_gw(self) -> int | None:
        events = self.bootstrap()["events"]
        finished = [int(ev["id"]) for ev in events if ev.get("finished")]
        return max(finished) if finished else None
