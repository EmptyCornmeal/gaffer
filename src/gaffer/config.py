"""Central configuration and paths for Gaffer.

Runtime values (your FPL entry id, league ids) can be overridden without editing
code via a local, git-ignored ``gaffer.local.toml`` at the repo root, or via
environment variables (``GAFFER_ENTRY_ID``, ``GAFFER_LEAGUE_IDS``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# --- Repo layout -----------------------------------------------------------
PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parents[1]
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = DATA_DIR / ".cache"
HISTORY_DIR = DATA_DIR / "history"
DB_PATH = DATA_DIR / "gaffer.db"
SCHEMA_PATH = PKG_DIR / "store" / "schema.sql"


def _load_dotenv() -> None:
    """Populate os.environ from a git-ignored ``.env`` at the repo root.

    Lets the pipeline pick up ANTHROPIC_API_KEY (and similar) without exporting
    shell vars. Existing environment values win.
    """
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# --- Season constants (2026/27) --------------------------------------------
SEASON = "2026-27"
UNDERSTAT_SEASON = "2026"  # Understat labels seasons by starting year
SQUAD_SIZE = 15
BUDGET_TENTHS = 1000  # £100.0m, stored in tenths of a million like the FPL API
CLUB_LIMIT = 3
FORMATION_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
SQUAD_QUOTA = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
POSITIONS = ["GKP", "DEF", "MID", "FWD"]
HIT_COST = 4  # points per extra transfer
MAX_FREE_TRANSFERS = 5  # 2026/27: roll up to five
CHIP_HALF_SPLIT_GW = 19  # first chip set must be used by the GW19 deadline
PROJECTION_HORIZON = 6  # gameweeks projected ahead

# Points per attacking return by position (FPL scoring).
GOAL_POINTS = {"GKP": 6, "DEF": 6, "MID": 5, "FWD": 4}
ASSIST_POINTS = 3
CS_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
DEFCON_POINTS = 2  # awarded once per match if the defensive-action threshold is met
# DEFCON thresholds (CBIT / CBIRT) by position for 2025/26+.
DEFCON_THRESHOLD = {"GKP": 999, "DEF": 10, "MID": 12, "FWD": 12}


@dataclass
class Settings:
    entry_id: int | None = None
    league_ids: list[int] = field(default_factory=list)
    # Free transfers available this GW. The public FPL API doesn't expose this
    # (it needs the authenticated my-team endpoint), so it's a user-set value the
    # solver trusts; clamped to [1, MAX_FREE_TRANSFERS]. Planner can override live.
    free_transfers: int = 1

    @classmethod
    def load(cls) -> Settings:
        entry_id: int | None = None
        league_ids: list[int] = []
        free_transfers = 1

        local = REPO_ROOT / "gaffer.local.toml"
        if local.exists():
            cfg = tomllib.loads(local.read_text(encoding="utf-8"))
            fpl = cfg.get("fpl", {})
            if fpl.get("entry_id") is not None:
                entry_id = int(fpl["entry_id"])
            league_ids = [int(x) for x in fpl.get("league_ids", [])]
            if fpl.get("free_transfers") is not None:
                free_transfers = int(fpl["free_transfers"])

        if os.environ.get("GAFFER_ENTRY_ID"):
            entry_id = int(os.environ["GAFFER_ENTRY_ID"])
        if os.environ.get("GAFFER_LEAGUE_IDS"):
            league_ids = [int(x) for x in os.environ["GAFFER_LEAGUE_IDS"].split(",") if x.strip()]
        if os.environ.get("GAFFER_FREE_TRANSFERS"):
            free_transfers = int(os.environ["GAFFER_FREE_TRANSFERS"])

        free_transfers = max(1, min(free_transfers, MAX_FREE_TRANSFERS))
        return cls(entry_id=entry_id, league_ids=league_ids, free_transfers=free_transfers)


def fpl_selling_price(purchase: int, now: int) -> int:
    """FPL's selling-price rule (all in tenths of a million).

    You get your purchase price back plus half of any *rise*, rounded DOWN to the
    nearest 0.1m; a price fall is taken in full. So a £6.0m buy now worth £6.5m
    sells for £6.2m (0.5 rise -> +0.2); worth £5.7m sells for £5.7m.
    """
    if now <= purchase:
        return now
    return purchase + (now - purchase) // 2


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)
