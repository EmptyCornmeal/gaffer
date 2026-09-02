"""Central configuration and paths for Gaffer.

Runtime values (your FPL entry id, league ids) can be overridden without editing
code via a local, git-ignored ``gaffer.local.toml`` at the repo root, or via
environment variables (``GAFFER_ENTRY_ID``, ``GAFFER_LEAGUE_IDS``).

Path resolution (see ``resolve_repo_root``) deliberately does NOT infer the
writable data directory from the package's own install location. Deriving it
from ``__file__`` silently sends artifacts into ``site-packages`` under a
non-editable install, which is how the scheduled refresh published nothing for
37 consecutive green runs.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# --- Repo layout -----------------------------------------------------------
PKG_DIR = Path(__file__).resolve().parent


class PathResolutionError(RuntimeError):
    """Raised when the resolved output directory would discard artifacts."""


def _looks_like_repo_root(p: Path) -> bool:
    """A Gaffer checkout: has the project file *and* the package source tree."""
    try:
        return (p / "pyproject.toml").is_file() and (p / "src" / "gaffer").is_dir()
    except OSError:  # pragma: no cover - unreadable path
        return False


def resolve_repo_root() -> tuple[Path, str]:
    """Resolve the repository root, returning (path, provenance).

    Order: explicit env var -> marker search upward from the package source ->
    marker search upward from the working directory (this is what rescues a
    non-editable install running inside a checkout) -> an unsafe package-relative
    guess that ``verify_publish_paths`` is expected to reject.
    """
    env = os.environ.get("GAFFER_REPO_ROOT")
    if env and env.strip():
        return Path(env).expanduser().resolve(), "env:GAFFER_REPO_ROOT"
    for cand in (PKG_DIR, *PKG_DIR.parents):
        if _looks_like_repo_root(cand):
            return cand, "source-checkout"
    try:
        cwd = Path.cwd().resolve()
    except OSError:  # pragma: no cover - deleted cwd
        cwd = None
    if cwd is not None:
        for cand in (cwd, *cwd.parents):
            if _looks_like_repo_root(cand):
                return cand, "working-directory"
    return PKG_DIR.parents[1], "unsafe:package-relative"


def resolve_data_dir(repo_root: Path) -> tuple[Path, str]:
    """Resolve the writable data directory, returning (path, provenance)."""
    env = os.environ.get("GAFFER_DATA_DIR")
    if env and env.strip():
        return Path(env).expanduser().resolve(), "env:GAFFER_DATA_DIR"
    return repo_root / "data", "repo-root"


REPO_ROOT, REPO_ROOT_SOURCE = resolve_repo_root()
DATA_DIR, DATA_DIR_SOURCE = resolve_data_dir(REPO_ROOT)
CACHE_DIR = DATA_DIR / ".cache"
HISTORY_DIR = DATA_DIR / "history"
DB_PATH = DATA_DIR / "gaffer.db"
SCHEMA_PATH = PKG_DIR / "store" / "schema.sql"


def reload_paths() -> None:
    """Recompute the path globals from the current environment.

    Exists so tests (and any caller that mutates ``GAFFER_*`` at runtime) get a
    deterministic re-resolution instead of reimporting the module.
    """
    global REPO_ROOT, REPO_ROOT_SOURCE, DATA_DIR, DATA_DIR_SOURCE
    global CACHE_DIR, HISTORY_DIR, DB_PATH
    REPO_ROOT, REPO_ROOT_SOURCE = resolve_repo_root()
    DATA_DIR, DATA_DIR_SOURCE = resolve_data_dir(REPO_ROOT)
    CACHE_DIR = DATA_DIR / ".cache"
    HISTORY_DIR = DATA_DIR / "history"
    DB_PATH = DATA_DIR / "gaffer.db"


def describe_paths() -> dict[str, str | None]:
    """Diagnostics for logs and error messages."""
    return {
        "repo_root": str(REPO_ROOT),
        "repo_root_source": REPO_ROOT_SOURCE,
        "data_dir": str(DATA_DIR),
        "data_dir_source": DATA_DIR_SOURCE,
        "package_dir": str(PKG_DIR),
        "cwd": os.getcwd(),
        "GAFFER_REPO_ROOT": os.environ.get("GAFFER_REPO_ROOT"),
        "GAFFER_DATA_DIR": os.environ.get("GAFFER_DATA_DIR"),
    }


def verify_publish_paths(
    repo_root: Path | None = None, data_dir: Path | None = None
) -> None:
    """Fail loudly when the output directory sits outside the repository.

    Uses resolved-``Path`` containment, not string prefixes: ``/repo-backup``
    starts with ``/repo`` but is a different tree.

    Containment alone is not enough, and this is the second time that has caught
    us out. When the repository root itself came from the package-relative
    fallback, ``data_dir`` defaults to a directory *inside* that guess — so the
    containment test passes trivially and the pipeline publishes into
    ``site-packages/../data``, which is the original Tier-1 failure verbatim.
    Reproduced by installing the wheel and running it outside any checkout.
    So an unsafe root is refused before containment is even considered.
    """
    root = Path(repo_root if repo_root is not None else REPO_ROOT).expanduser().resolve()
    out = Path(data_dir if data_dir is not None else DATA_DIR).expanduser().resolve()
    explicit_out = bool(os.environ.get("GAFFER_DATA_DIR", "").strip())
    if repo_root is None and REPO_ROOT_SOURCE.startswith("unsafe:") and not explicit_out:
        raise PathResolutionError(
            "Refusing to publish: no repository checkout could be found.\n"
            f"    repo_root = {root}   (guessed from the package's own location)\n"
            f"    data_dir  = {out}\n"
            "This is a non-editable install running outside a checkout. Artifacts "
            "would land beside site-packages, where nothing reads them — the run "
            "would report success and publish nothing, which is exactly the "
            "failure that cost 37 green-but-empty scheduled runs.\n"
            "Fix: install editable (pip install -e '.[ai]') and run from the "
            "checkout, or set GAFFER_REPO_ROOT and GAFFER_DATA_DIR explicitly.\n"
            "Resolution detail:\n"
            + "\n".join(f"    {k:18} = {v!r}" for k, v in describe_paths().items())
        )
    if out == root or out.is_relative_to(root):
        return
    diag = describe_paths()
    diag["checked_repo_root"] = str(root)
    diag["checked_data_dir"] = str(out)
    detail = "\n".join(f"    {k:18} = {v!r}" for k, v in diag.items())
    raise PathResolutionError(
        "Refusing to publish: the resolved data directory is not inside the "
        "resolved repository root.\n"
        f"    data_dir  = {out}\n"
        f"    repo_root = {root}\n"
        "Artifacts written there are NOT part of the checkout, so the refresh "
        "workflow would see no diff, commit nothing, and deploy nothing — the "
        "run would report success while publishing stale data.\n"
        "Fix: install the package editable (pip install -e '.[ai]'), run from "
        "the checkout, or set GAFFER_REPO_ROOT / GAFFER_DATA_DIR explicitly.\n"
        f"Resolution detail:\n{detail}"
    )


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
SQUAD_SIZE = 15
BUDGET_TENTHS = 1000  # £100.0m, stored in tenths of a million like the FPL API
CLUB_LIMIT = 3
FORMATION_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
SQUAD_QUOTA = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
POSITIONS = ["GKP", "DEF", "MID", "FWD"]
HIT_COST = 4  # points per extra transfer
MAX_FREE_TRANSFERS = 5  # 2026/27: roll up to five
# (No CHIP_HALF_SPLIT_GW: chip windows come from `bootstrap.chips` at runtime,
#  not a hard-coded halfway gameweek — see gaffer.chips.parse_windows.)
PROJECTION_HORIZON = 6  # gameweeks projected ahead

# Points per attacking return by position (FPL scoring).
# GKP is 10, not 6: a goalkeeper's goal has been worth ten points since 2026/27.
# This table sat at 6 for an entire pre-season because a hard-coded rule agrees
# with itself. `gaffer.rules` now re-reads `bootstrap.game_config.scoring` on
# every ingest and refuses to run when the two disagree, so it cannot drift
# silently again.
GOAL_POINTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
ASSIST_POINTS = 3
CS_POINTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
DEFCON_POINTS = 2  # awarded once per match if the defensive-action threshold is met
# DEFCON thresholds (CBIT / CBIRT) by position for 2025/26+.
DEFCON_THRESHOLD = {"GKP": 999, "DEF": 10, "MID": 12, "FWD": 12}

# --- remaining FPL scoring rules (T-13) ------------------------------------
# Goals conceded: -1 for every 2 conceded while on the pitch, GKP/DEF only.
CONCEDED_PENALTY = -1
CONCEDED_PER_PENALTY = 2
CONCEDED_POSITIONS = ("GKP", "DEF")
# Saves: +1 per 3 saves, goalkeepers only.
SAVE_POINTS = 1
SAVES_PER_POINT = 3
# Discipline and rare events.
YELLOW_POINTS = -1
RED_POINTS = -3
OWN_GOAL_POINTS = -2
PENALTY_SAVE_POINTS = 5
PENALTY_MISS_POINTS = -2
# Appearance: 1 point for any minutes, 2 once 60' is reached. Named rather than
# inlined so `gaffer.rules` can check them against the API's long_play/short_play.
APPEARANCE_SHORT = 1
APPEARANCE_LONG = 2

# --- prior-season baseline ---------------------------------------------------
# How many minutes a prior season must contain before its rates count as a
# sample. Below this, `base_*` is 0 because nothing was RECORDED; at or above
# it, a 0 means the player genuinely did that thing zero times — a measurement,
# and much stronger evidence than any positional prior.
#
# Named here because three places must agree or the distinction collapses:
# `ingest.enrich_history` refuses to store a shorter sample, `histdata` zeroes
# one out for the backtest, and `export.artifacts` hides it from the player card.
# `model.projection` then tests this one number to tell absence from zero.
BASE_SAMPLE_MINUTES = 300

# FPL back-fills `history_past` for seasons that predate a statistic with 0
# rather than omitting the key, so key-presence tells you nothing and a
# structural zero is indistinguishable from a measurement unless you know which
# season it came from. Verified against the live API on 2026-08-15 — one player,
# seven consecutive seasons:
#
#     season     mins  starts     xG      xA   defcon
#     2021/22    3110       0   0.00    0.00        0     <- fields did not exist
#     2022/23    3317      37   9.33   10.70        0
#     2024/25    3017      35   9.93    7.89      359
#
# 3110 minutes with 0 starts is not a substitute; it is a column that had not
# been invented. Treating it as a measurement projects an ever-present as a
# bench player.
BASE_STATS_FROM_SEASON = "2022/23"      # starts, expected_goals, expected_assists
BASE_DEFCON_FROM_SEASON = "2024/25"     # defensive_contribution


def season_reports_advanced_stats(label: str | None) -> bool | None:
    """Could a `history_past` season report starts/xG/xA at all?

    Returns None when the season is not recorded — which is emphatically not the
    same answer as False, and callers must not collapse the two.
    """
    if not label:
        return None
    try:
        year = int(str(label).split("/")[0])
    except (ValueError, IndexError):
        return None
    return year >= int(BASE_STATS_FROM_SEASON.split("/")[0])


def season_reports_defcon(label: str | None) -> bool | None:
    """Could a `history_past` season report `defensive_contribution` at all?

    The same contract as `season_reports_advanced_stats` against a different
    cutoff — DEFCON arrived two seasons after starts/xG/xA — and the same
    three-valued answer, because None ("not recorded") is emphatically not False
    ("recorded, and could not have measured this"). A prior season from before
    2024/25 reports 0 defensive contributions for every player alive, and
    reading that as a measurement would project the league's best ball-winners
    as men who never make a tackle.
    """
    if not label:
        return None
    try:
        year = int(str(label).split("/")[0])
    except (ValueError, IndexError):
        return None
    return year >= int(BASE_DEFCON_FROM_SEASON.split("/")[0])

# --- xA -> FPL assists calibration (G-P): MEASURED, RECORDED, NOT APPLIED ----
# Opta's expected assists and FPL's assists are not the same quantity. FPL's
# assist rules are more generous — a shot rebounding to a scorer, a won free
# kick or penalty, and a deflection all pay an FPL assist that xA does not model.
# The mismatch is specific to assists, which is the evidence that it is a
# DEFINITIONAL gap and not general model error: over the same rows goals track
# xG to within 2% (MID 1.014 fit / 0.981 held out, FWD 0.991 / 0.981) while
# assists run 22% to 111% ahead of xA depending on position.
#
# Fitted as sum(assists)/sum(expected_assists) on 2023-24 + 2024-25, held out on
# 2025-26:
#
#     pos   factor   held-out error   at 1.0 (today)   at a blanket 1.400
#     DEF    1.221        -5.5%           -22.6%              +8.3%
#     MID    1.358        +2.5%           -24.5%              +5.6%
#     FWD    2.107        +0.1%           -52.5%             -33.5%
#
# Per-position rather than one number precisely because of that last column: a
# blanket 1.400 under-corrects forwards by a third, because a forward's pass to
# a teammate in the box is a low-xA ball to a high-quality finisher.
#
# GKP is 1.0 and is NOT fitted. The naive fit is 5.179, off 1.4 xA and 5 assists
# across two entire seasons; the same fit one season earlier gives 2.922 and
# misses the next season by -62.5%. There is no sample, so no correction.
#
# ---------------------------------------------------------------------------
# THE FACTORS ARE NOT APPLIED. `model.projection.fixture_rates` still computes
# `exp_assists = xa90 * mins_frac * att_mult` with no calibration, and that is
# deliberate. Two independent measurements, both held out, said not to:
#
#   DECISIONS. Driving the shipped projection over the leak-free historical
#   frame and comparing PAIRED per gameweek (same gameweeks, same players, so
#   the difference is what is being tested rather than the level):
#
#     2025-26   rank corr -0.00050 +/- 0.00036 (t=-1.37, 24 of 38 gws worse)
#               XI points -0.868  +/- 0.760    (t=-1.14, 14 of 38 gws worse)
#     2024-25   rank corr -0.00050 +/- 0.00025 (t=-1.98)   XI -0.763 +/- 1.034
#     2023-24   rank corr -0.00164 +/- 0.00046 (t=-3.58)   XI -0.368 +/- 0.881
#
#   No single season's XI delta clears its own noise, but all six numbers are
#   negative and pooling the three rank correlations gives t = -4.2. It degrades
#   the ORDERING, which is the only thing the solver consumes.
#
#   THE QUANTITY IS WRONG, which is why. The factors above were fitted against
#   Opta's raw per-match xA. The model does not consume that: it consumes a
#   season-to-date xA/90 that has already been empirical-Bayes shrunk toward a
#   prior-season baseline and then multiplied by a fixture strength term. Those
#   are different numbers, and the model's own assist total is already 20.8%
#   UNDER the realised figure on 2025-26 rows where the player played 60+
#   minutes (626.1 projected against 791 actual) rather than the 30% under that
#   a 1.4x gap would imply. Applying these factors overshoots to +10.5%, and
#   forwards swing from -27.7% to +52.4%.
#
#   Refitting on the model's own output instead of on Opta's (DEF 1.220,
#   MID 1.291, FWD 1.693) does not rescue it: 2025-26 XI points -1.342 +/- 0.625
#   (t = -2.15), and forwards are still +22.4% out of sample.
#
# So the honest state is: the gap is real and measured, the correction as fitted
# makes the decision worse, and the constant is kept here so the next attempt
# starts from the measurement rather than repeating it. Wiring it in needs a
# factor fitted against what `fixture_rates` actually produces, and needs to
# beat the paired numbers above, not the aggregate ratio.
#
# STABILITY CAVEAT. Refitting on 2022-23 + 2023-24 and holding out on 2024-25
# gives DEF 1.441, MID 1.606, FWD 2.642 with held-out errors of +30.1%, +17.1%
# and +27.2%. 2022-23's league-wide A/xA is 2.111 against 1.42, 1.37 and 1.38
# for the three seasons after it, so that season's xA column is not measuring
# the same thing. Anything fitted here must exclude it.
XA_TO_ASSIST = {"GKP": 1.0, "DEF": 1.221, "MID": 1.358, "FWD": 2.107}
#: The seasons XA_TO_ASSIST was fitted on and the season it was held out on,
#: named rather than described so a refit can state them instead of guessing.
XA_TO_ASSIST_FIT_SEASONS = ("2023-24", "2024-25")
XA_TO_ASSIST_HELDOUT_SEASON = "2025-26"
#: False while the factors above are recorded but not multiplied into any
#: projection. The projection and its tests read this rather than restating it.
XA_TO_ASSIST_APPLIED = False

# --- ep_next ensemble (T-15, re-labelled by T-26) ---------------------------
# FPL publishes its own expected points for the NEXT gameweek only, and the
# shipped h=1 projection is a blend of it with Gaffer's component model.
#
# This weight is a POLICY CHOICE, not a fitted parameter, and saying so is the
# whole point of this comment. It was originally set to 0.7 because the backtest
# measured FPL's number at rank corr 0.760 vs Gaffer's 0.440 — but that
# measurement used the historical archive's `xP` column, and the archive cannot
# certify that value as the pre-deadline forecast managers saw. The upstream
# dataset explicitly warns it may contain post-match information (it is FPL's
# `ep_this`, scraped after the gameweek, with an undocumented update cadence), so
# it is inadmissible as a benchmark. See backtest.WITHDRAWN_BASELINES and
# `python -m gaffer.backtest --xp-diagnostic`. The archive holds no faithful copy
# of the live `ep_next`, so the weight cannot be fitted offline at all.
#
# It is left at 0.7 deliberately. The evidence for the value was withdrawn, not
# reversed: the standalone component model is genuinely weak on the honest
# numbers (rank corr 0.440, 50.9 legal-XI points per gameweek, barely ahead of a
# rolling average), and deferring most of the one-week estimate to a model with
# access to team news is defensible a priori. Moving the weight on withdrawn
# evidence would be exactly as unfounded as setting it was.
#
# It becomes fittable in-season: `projection_snapshots` stores `ep_next` beside
# the model's own number before each deadline, and `player_gw` stores what
# happened. After ~10 gameweeks there is a real sample to fit against.
EP_NEXT_BLEND_WEIGHT = 0.7
#: True while the weight above rests on judgement rather than measurement. The
#: Accuracy page and the decision explanations read this, so the claim on screen
#: changes with the code rather than drifting away from it.
EP_NEXT_BLEND_IS_FITTED = False

# The weight above is the NOMINAL ceiling. What actually reaches a player is
# scaled twice: by Gaffer's own availability read, and — since 2026-08-31 — by
# the model's own start probability.
#
# The rotation scaler exists because `ep_next` carries no start information
# whatsoever. Measured after GW1 of 2026/27 it was FPL's own backward-looking
# `form` for 596 of 626 players, and `form` is an average over matches the
# player actually PLAYED. Handed to somebody the model says is a bench option
# it is not merely noisy, it is biased high by roughly 1/p_start. Availability
# cannot catch this: a fit backup scores 1.0 there. On that date a backup
# goalkeeper with p_start 0.30, whose ep_next was 10.0 because he happened to
# score 10 in GW1, was published at 7.27 expected points against his own
# simulated 90th-percentile ceiling of 2.0.
#
# Linear ramp between the two points below: none of the external weight at or
# under ZERO, all of it at or over FULL. The midpoint falls at p_start 0.55, so
# a coin-flip starter receives half of it — which is the claim being made, that
# deferring to FPL is worth exactly as much as the chance the player FPL's
# number describes is the player who takes the field.
#
# POLICY, NOT FITTED — the same status as EP_NEXT_BLEND_WEIGHT itself, and for
# the same reason: the archive holds no faithful copy of the live `ep_next`, so
# there is nothing offline to fit the attenuation against either.
EP_NEXT_ROTATION_ZERO_P_START = 0.35
EP_NEXT_ROTATION_FULL_P_START = 0.75


class ConfigError(ValueError):
    """Raised for malformed personal configuration (bad entry id, bad league list)."""


def _int_or_fail(raw: object, field_name: str, source: str) -> int:
    """Parse an int, naming the offending field and where it came from."""
    if isinstance(raw, bool):  # bool is an int subclass; never a valid id
        raise ConfigError(f"{field_name} from {source} must be an integer, got {raw!r}")
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{field_name} from {source} must be an integer, got {raw!r}"
        ) from exc


def _league_weights_from(raw: object, source: str) -> dict[int, float]:
    """Parse ``league_id:weight`` pairs into the objective's weighting.

    3.8. Accepts ``"271619:1,733241:0.3"`` or a mapping. A malformed value
    RAISES rather than defaulting to an even split: a weighting nobody chose
    is exactly the invented winner the resolution layer already refuses to
    produce, and it would be harder to notice here.
    """
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, str):
        items = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                raise ConfigError(
                    f"league_weights from {source} must be 'id:weight' pairs, "
                    f"got {part!r}")
            k, _, v = part.partition(":")
            items.append((k.strip(), v.strip()))
    else:
        raise ConfigError(
            f"league_weights from {source} must be a mapping or string, "
            f"got {raw!r}")
    out: dict[int, float] = {}
    for k, v in items:
        lid = _int_or_fail(k, "league_weights key", source)
        try:
            w = float(v)
        except (TypeError, ValueError):
            raise ConfigError(
                f"league_weights[{lid}] from {source} must be a number, "
                f"got {v!r}") from None
        if w < 0:
            raise ConfigError(
                f"league_weights[{lid}] from {source} must not be negative")
        out[lid] = w
    return out


def _league_ids_from(raw: object, source: str) -> list[int]:
    """Accept a TOML list or a comma/space-separated string. Always a list."""
    if raw is None:
        return []
    items: list[object]
    if isinstance(raw, str):
        items = [chunk for chunk in raw.replace(";", ",").split(",") if chunk.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ConfigError(f"league_ids from {source} must be a list or string, got {raw!r}")
    out: list[int] = []
    for item in items:
        val = _int_or_fail(item, "league_ids entry", source)
        if val not in out:  # preserve order, drop duplicates
            out.append(val)
    return out


def _bank_tenths(raw: object, source: str) -> int:
    """Parse a bank value into TENTHS of a million.

    Accepts the API's own unit (an int like ``5`` = £0.5m) and the human unit
    (a float like ``0.5`` = £0.5m). Ambiguity is resolved by type, not by
    guessing at magnitude, so ``2`` is £0.2m and ``2.0`` is £2.0m.
    """
    if isinstance(raw, bool):
        raise ConfigError(f"bank from {source} must be a number, got {raw!r}")
    if isinstance(raw, float):
        return int(round(raw * 10))
    if isinstance(raw, int):
        return raw
    text = str(raw).strip().lstrip("£")
    if not text:
        raise ConfigError(f"bank from {source} is empty")
    try:
        return int(round(float(text) * 10)) if "." in text else int(text)
    except ValueError as exc:
        raise ConfigError(
            f"bank from {source} must be a number (tenths as int, or millions "
            f"as a decimal), got {raw!r}"
        ) from exc


def _purchase_prices_from(raw: object, source: str) -> dict[int, int]:
    """Parse purchase-price overrides: a TOML table or ``pid:tenths`` pairs."""
    if raw is None:
        return {}
    items: list[tuple[object, object]]
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, str):
        items = []
        for chunk in raw.replace(";", ",").split(","):
            if not chunk.strip():
                continue
            if ":" not in chunk:
                raise ConfigError(
                    f"purchase_prices from {source} must be 'player_id:price' "
                    f"pairs, got {chunk!r}"
                )
            k, _, v = chunk.partition(":")
            items.append((k, v))
    else:
        raise ConfigError(
            f"purchase_prices from {source} must be a table or string, got {raw!r}"
        )
    out: dict[int, int] = {}
    for k, v in items:
        pid = _int_or_fail(k, "purchase_prices player id", source)
        out[pid] = _bank_tenths(v, source)
    return out


@dataclass
class Settings:
    entry_id: int | None = None
    league_ids: list[int] = field(default_factory=list)
    # Free transfers available this GW. The public FPL API doesn't expose this
    # (it needs the authenticated my-team endpoint), so it's a user-set value the
    # solver trusts; clamped to [1, MAX_FREE_TRANSFERS]. Planner can override live.
    free_transfers: int = 1
    # In-the-bank money, TENTHS of a million (the API's unit). None = unknown,
    # which is NOT the same as 0.0m and must never be silently treated as such.
    bank: int | None = None
    # Manual purchase-price overrides, player_id -> tenths. Only needed when the
    # public transfer history cannot recover a price.
    purchase_prices: dict[int, int] = field(default_factory=dict)
    # 3.8 -- WHICH COMPETITION this is for, as runtime state rather
    # than a project-level assumption. league_id -> relative
    # importance; an absent or empty map means no weighting is
    # configured, and the resolution then publishes a shortlist and
    # the conflicts rather than inventing a winner.
    #
    # Deliberately a SETTING and not yet a UI control. The Planner's
    # risk stance was hidden in 1.13 precisely because it offered
    # three settings that solved to identical squads; a selector
    # ships when the objective it selects already changes an answer,
    # never before. This is the objective; the control follows it.
    league_weights: dict[int, float] = field(default_factory=dict)
    # Where each value came from, for the pipeline log and the artifact contract.
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def personalised(self) -> bool:
        """True when an entry id resolved, i.e. output is about a real squad.

        The pipeline stamps this into meta.json as ``build_mode`` so a generic
        build can never be mistaken for a personalised one.
        """
        return self.entry_id is not None

    @property
    def build_mode(self) -> str:
        return "personalised" if self.personalised else "generic"

    @classmethod
    def load(cls) -> Settings:
        """Resolve personal config. Precedence: env var > TOML > default."""
        entry_id: int | None = None
        league_ids: list[int] = []
        free_transfers = 1
        league_weights: dict[int, float] = {}
        bank: int | None = None
        purchase_prices: dict[int, int] = {}
        sources: dict[str, str] = {
            "entry_id": "unset", "league_ids": "default", "free_transfers": "default",
            "bank": "unset", "purchase_prices": "unset",
        }

        local = REPO_ROOT / "gaffer.local.toml"
        if local.exists():
            try:
                cfg = tomllib.loads(local.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{local} is not valid TOML: {exc}") from exc
            fpl = cfg.get("fpl", {})
            if fpl.get("entry_id") is not None:
                entry_id = _int_or_fail(fpl["entry_id"], "entry_id", str(local))
                sources["entry_id"] = "gaffer.local.toml"
            if fpl.get("league_ids") is not None:
                league_ids = _league_ids_from(fpl["league_ids"], str(local))
                sources["league_ids"] = "gaffer.local.toml"
            if fpl.get("free_transfers") is not None:
                free_transfers = _int_or_fail(
                    fpl["free_transfers"], "free_transfers", str(local)
                )
                sources["free_transfers"] = "gaffer.local.toml"
            if fpl.get("bank") is not None:
                bank = _bank_tenths(fpl["bank"], str(local))
                sources["bank"] = "gaffer.local.toml"
            if fpl.get("purchase_prices") is not None:
                purchase_prices = _purchase_prices_from(
                    fpl["purchase_prices"], str(local))
                sources["purchase_prices"] = "gaffer.local.toml"

        # Environment wins over the file.
        if os.environ.get("GAFFER_ENTRY_ID", "").strip():
            entry_id = _int_or_fail(
                os.environ["GAFFER_ENTRY_ID"], "entry_id", "env:GAFFER_ENTRY_ID"
            )
            sources["entry_id"] = "env:GAFFER_ENTRY_ID"
        if os.environ.get("GAFFER_LEAGUE_IDS", "").strip():
            league_ids = _league_ids_from(
                os.environ["GAFFER_LEAGUE_IDS"], "env:GAFFER_LEAGUE_IDS"
            )
            sources["league_ids"] = "env:GAFFER_LEAGUE_IDS"
        raw_w = os.environ.get("GAFFER_LEAGUE_WEIGHTS")
        if raw_w:
            league_weights = _league_weights_from(
                raw_w, "env:GAFFER_LEAGUE_WEIGHTS")
            sources["league_weights"] = "env:GAFFER_LEAGUE_WEIGHTS"
        if os.environ.get("GAFFER_FREE_TRANSFERS", "").strip():
            free_transfers = _int_or_fail(
                os.environ["GAFFER_FREE_TRANSFERS"], "free_transfers",
                "env:GAFFER_FREE_TRANSFERS",
            )
            sources["free_transfers"] = "env:GAFFER_FREE_TRANSFERS"

        if os.environ.get("GAFFER_BANK", "").strip():
            bank = _bank_tenths(os.environ["GAFFER_BANK"], "env:GAFFER_BANK")
            sources["bank"] = "env:GAFFER_BANK"
        if os.environ.get("GAFFER_PURCHASE_PRICES", "").strip():
            purchase_prices = _purchase_prices_from(
                os.environ["GAFFER_PURCHASE_PRICES"], "env:GAFFER_PURCHASE_PRICES")
            sources["purchase_prices"] = "env:GAFFER_PURCHASE_PRICES"

        if entry_id is not None and entry_id <= 0:
            raise ConfigError(
                f"entry_id must be a positive FPL entry id, got {entry_id} "
                f"(from {sources['entry_id']})"
            )

        free_transfers = max(1, min(free_transfers, MAX_FREE_TRANSFERS))
        return cls(
            entry_id=entry_id, league_ids=league_ids,
            free_transfers=free_transfers, bank=bank,
            purchase_prices=purchase_prices,
            league_weights=league_weights, sources=sources,
        )


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
