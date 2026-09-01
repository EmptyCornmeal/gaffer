"""The human half of the decision record.

1.12 -- Gaffer records what it advised and whether the advice was followed. It
does not record what was done INSTEAD, or why, and those are the informative
rows: this season already has two overrides where the human was right for a
stated reason (a half-time withdrawal read as a demotion, and a fixture-
concentrated xG read as form).

The obvious place to capture that is a form in the web app. The measured usage
says nobody is there: the decision happens in conversation, mid-week, and the
gameweek note in the vault is already written every gameweek by hand and
already contains the reasoning. So the journal lives THERE and Gaffer reads it.

Consequences, and they are deliberate:

* Gaffer stays read-only. It gains no write path and no new UI, and the manager
  gains no new habit.
* GitHub Actions cannot see the vault, so this joins at the MCP layer -- the
  published artifacts stay CI-only and the journal is a local read. A CI run
  with no vault simply finds nothing, which is not an error.

The block is deliberately not YAML: `pyyaml` is a dev dependency and the
shipped package must not grow one for four keys.

    ```gaffer-decision
    gameweek: 3
    followed: no
    i_did: Le Fee -> M.Sangare, captain Haaland
    because: Konsa has 0 starts and 11 minutes; the model is reading last season
    ```
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

#: The fenced block Gaffer looks for.
FENCE = "gaffer-decision"

#: Keys the block understands. Anything else is kept verbatim under `extra`, so
#: a note that says more than this is never truncated to what the parser knew
#: about on the day it was written.
KNOWN = ("gameweek", "followed", "i_did", "because")

_FENCE_RE = re.compile(
    r"^```" + FENCE + r"\s*$(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)

_TRUE = {"yes", "true", "y", "1"}
_FALSE = {"no", "false", "n", "0"}


def journal_dir() -> Path | None:
    """Where the gameweek notes live, or None when there is no vault here.

    Absence is the normal state in CI and must never be an error.
    """
    env = os.environ.get("GAFFER_JOURNAL_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    for candidate in (
        Path.home() / "Knowledge" / "Myles Home Lab" / "01 Projects" / "FPL2627",
        Path.home() / "Documents" / "Myles Home Lab" / "01 Projects" / "FPL2627",
    ):
        if candidate.is_dir():
            return candidate
    return None


def _coerce(key: str, value: str) -> Any:
    v = value.strip()
    if key == "gameweek":
        try:
            return int(v)
        except ValueError:
            return None
    if key == "followed":
        low = v.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        return None          # "partly" is a real answer; do not force a boolean
    return v or None


def parse_block(text: str) -> dict[str, Any] | None:
    """The first `gaffer-decision` block in one note, or None."""
    m = _FENCE_RE.search(text or "")
    if not m:
        return None
    entry: dict[str, Any] = {"extra": {}}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in KNOWN:
            entry[key] = _coerce(key, value)
        else:
            entry["extra"][key] = value.strip()
    if not entry["extra"]:
        entry.pop("extra")
    return entry or None


def read(gameweek: int | None = None) -> list[dict[str, Any]]:
    """Every journal entry found, newest gameweek first.

    A note without a block contributes nothing. A malformed block contributes
    what it could parse, because a half-written line must not hide the rest.
    """
    d = journal_dir()
    if d is None:
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        entry = parse_block(text)
        if not entry:
            continue
        entry["source"] = path.name
        if gameweek is not None and entry.get("gameweek") != gameweek:
            continue
        out.append(entry)
    out.sort(key=lambda e: (e.get("gameweek") or 0), reverse=True)
    return out


def status() -> dict[str, Any]:
    """Whether a journal is readable from here, and what it holds."""
    d = journal_dir()
    if d is None:
        return {
            "available": False,
            "reason": ("no vault on this machine — the journal is a LOCAL join; "
                       "GitHub Actions cannot see it and is not expected to"),
            "entries": 0,
        }
    entries = read()
    return {
        "available": True,
        "directory": str(d),
        "entries": len(entries),
        "gameweeks": sorted(
            {e["gameweek"] for e in entries if e.get("gameweek")}, reverse=True),
        "format": (f"a ```{FENCE} fenced block in the gameweek note, with "
                   f"{', '.join(KNOWN)}"),
    }
