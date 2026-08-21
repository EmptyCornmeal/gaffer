"""Client for the official Fantasy Premier League API.

All endpoints are public (no auth). We add a small on-disk cache so repeated
pipeline runs and tests don't hammer the API. ``bootstrap-static`` and
``fixtures`` change slowly within a gameweek, so a short TTL is plenty; live
endpoints use a very short TTL.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from gaffer import config, gameweek

BASE = "https://fantasy.premierleague.com/api"
USER_AGENT = "gaffer/0.1 (+https://github.com/EmptyCornmeal)"

#: Characters that must not reach a filename. `?` and `=` are illegal on Windows.
_UNSAFE_CACHE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


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
        """Cache filename for an endpoint path.

        Everything outside ``[A-Za-z0-9._-]`` is collapsed to an underscore. The
        previous version only replaced ``/``, which left the query string intact —
        and ``?``/``=`` are illegal in Windows filenames, so every query-bearing
        endpoint (``leagues-classic/{id}/standings/?page_standings=1``) raised
        ``OSError: [Errno 22]`` on write. Distinct queries still map to distinct
        names, so page 1 and page 2 cannot collide.
        """
        safe = _UNSAFE_CACHE_CHARS.sub("_", path.strip("/")) or "root"
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
    def events(self) -> list[dict[str, Any]]:
        return self.bootstrap()["events"]

    def projection_event(self, now: datetime | None = None) -> int:
        """The event to project and decide for (the next one still actionable)."""
        return gameweek.projection_event(self.events(), now)

    def readable_squad_event(self, now: datetime | None = None) -> int | None:
        """Latest event whose picks are publicly readable, or None pre-GW1.

        NOT the same as :meth:`projection_event` — requesting the projection
        event's picks 404s before every deadline.
        """
        return gameweek.readable_squad_event(self.events(), now)

    def live_event(self, now: datetime | None = None) -> int | None:
        """The event being played right now, or None before the first deadline.

        NOT :meth:`projection_event` - once a deadline passes those two diverge
        for the whole gameweek, and the live view must follow the football.
        """
        return gameweek.live_event(self.events(), now)

    def current_gw(self) -> int:
        """Deprecated alias for :meth:`projection_event`.

        Kept so external callers keep working; the pipeline uses the explicit
        names so the two concepts can never be confused again.
        """
        return self.projection_event()

    def last_finished_gw(self) -> int | None:
        return gameweek.last_finished_event(self.events())
