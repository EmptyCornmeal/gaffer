"""Season scoring rules — read from the FPL API, verified against our constants.

The 2026/27 ``bootstrap-static`` payload carries a top-level ``game_config``
object whose ``scoring`` block is the authoritative, machine-readable points
table. Before it existed Gaffer had no choice but to hard-code the table — and
that is exactly how ``GOAL_POINTS["GKP"] = 6`` survived into a season where a
goalkeeper's goal is worth **10**. The constant was wrong for a whole pre-season
and nothing could see it, because a hard-coded rule agrees with itself.

So: read the table, compare it against what the model encodes, and **fail
visibly** on any disagreement rather than quietly projecting last season's rules.
A refusal costs one red run; a silent divergence costs a season of decisions made
on the wrong arithmetic.

The divisors (goals conceded per 2, saves per 3) are *not* in the payload and
remain encoded here. They are listed in ``UNVERIFIABLE`` so the gap stays visible
rather than being mistaken for something this check covers.
"""

from __future__ import annotations

import os
from typing import Any

from gaffer import config

#: Where the scoring table came from on this run. Stamped into ``meta``.
SOURCE_API = "game_config"
SOURCE_ABSENT = "absent"

#: Status stamped into ``meta`` beside the source.
STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"
STATUS_DRIFT_ALLOWED = "drift_allowed"

#: Rules Gaffer encodes that the API does **not** publish, so they cannot be
#: verified here and must be re-read by a human when FPL announces a change.
UNVERIFIABLE = (
    "goals conceded: one penalty per 2 shipped while on the pitch",
    "saves: one point per 3 saves",
    "DEFCON thresholds: DEF 10 CBIT, MID/FWD 12 CBIRT",
    "bonus: 3/2/1 to the top three BPS in each match",
)

#: Set to bypass the hard failure when FPL changes a rule mid-season and the
#: refresh must keep running while the model catches up. The drift is still
#: recorded in ``meta`` and reported on every run — it is never silent.
DRIFT_OVERRIDE_ENV = "GAFFER_ALLOW_RULE_DRIFT"


class ScoringRuleDrift(RuntimeError):
    """Raised when the API's scoring table disagrees with what Gaffer models.

    Deliberately fatal by default. The alternative is a green run that projects
    a whole season under the previous season's rules.
    """

    def __init__(self, drift: list[str]) -> None:
        self.drift = list(drift)
        bullets = "\n".join(f"    - {d}" for d in self.drift)
        super().__init__(
            "Refusing to run: the FPL API's scoring table disagrees with the "
            "rules Gaffer models.\n"
            f"{bullets}\n"
            "Nothing was ingested. Update `gaffer.config` to match the live "
            "rules (and re-check anything downstream that assumed the old "
            f"value), or set {DRIFT_OVERRIDE_ENV}=1 to run anyway with the "
            "drift recorded in meta.json.\n"
            "Rules the API does not publish, which this check cannot cover:\n"
            + "\n".join(f"    - {u}" for u in UNVERIFIABLE)
        )


def parse_scoring(bootstrap: dict[str, Any] | None) -> dict[str, Any] | None:
    """The live scoring table, or None when this API build does not ship one."""
    gc = (bootstrap or {}).get("game_config")
    if not isinstance(gc, dict):
        return None
    scoring = gc.get("scoring")
    return scoring if isinstance(scoring, dict) else None


def parse_rules(bootstrap: dict[str, Any] | None) -> dict[str, Any]:
    """Squad/transfer rules, preferring ``game_config.rules``.

    Falls back to the older top-level ``game_settings``, which carries the same
    keys, so callers do not care which build of the API they are talking to.
    """
    out: dict[str, Any] = {}
    settings = (bootstrap or {}).get("game_settings")
    if isinstance(settings, dict):
        out.update(settings)
    gc = (bootstrap or {}).get("game_config")
    if isinstance(gc, dict) and isinstance(gc.get("rules"), dict):
        out.update(gc["rules"])
    return out


def _pos_map(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        return {p: float(raw[p]) for p in config.POSITIONS if p in raw}
    except (TypeError, ValueError):
        return None


def _flat(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def compare(scoring: dict[str, Any]) -> list[str]:
    """Every disagreement between the live table and Gaffer's constants.

    Only rules Gaffer actually models are compared — an API key the model does
    not consume is not drift, it is an unused field.
    """
    drift: list[str] = []

    def check_positional(key: str, ours: dict[str, float], label: str) -> None:
        theirs = _pos_map(scoring.get(key))
        if theirs is None:
            return
        for pos, value in theirs.items():
            mine = float(ours.get(pos, 0.0))
            if value != mine:
                drift.append(
                    f"{label} ({pos}): API says {value:g}, "
                    f"gaffer.config has {mine:g}")

    def check_flat(key: str, mine: float, label: str) -> None:
        theirs = _flat(scoring.get(key))
        if theirs is None:
            return
        if theirs != float(mine):
            drift.append(
                f"{label}: API says {theirs:g}, gaffer.config has {float(mine):g}")

    check_positional("goals_scored", config.GOAL_POINTS, "goal points")
    check_positional("clean_sheets", config.CS_POINTS, "clean-sheet points")
    check_positional(
        "goals_conceded",
        {p: (config.CONCEDED_PENALTY if p in config.CONCEDED_POSITIONS else 0)
         for p in config.POSITIONS},
        "goals-conceded points",
    )
    # DEFCON: a position Gaffer never awards is encoded as an unreachable
    # threshold, so translate to points before comparing.
    check_positional(
        "defensive_contribution",
        {p: (config.DEFCON_POINTS if config.DEFCON_THRESHOLD[p] < 99 else 0)
         for p in config.POSITIONS},
        "defensive-contribution points",
    )

    check_flat("assists", config.ASSIST_POINTS, "assist points")
    check_flat("saves", config.SAVE_POINTS, "points per save block")
    check_flat("penalties_saved", config.PENALTY_SAVE_POINTS, "penalty-save points")
    check_flat("penalties_missed", config.PENALTY_MISS_POINTS, "penalty-miss points")
    check_flat("yellow_cards", config.YELLOW_POINTS, "yellow-card points")
    check_flat("red_cards", config.RED_POINTS, "red-card points")
    check_flat("own_goals", config.OWN_GOAL_POINTS, "own-goal points")
    check_flat("long_play", config.APPEARANCE_LONG, "appearance points (60'+)")
    check_flat("short_play", config.APPEARANCE_SHORT, "appearance points (under 60')")

    # The Assistant Manager chip is not in play for 2026/27 and Gaffer models no
    # part of it. A non-zero mng_* key means it is back and every squad decision
    # would be made blind to a whole scoring category.
    for key, value in scoring.items():
        if not key.startswith("mng_"):
            continue
        vals = list(value.values()) if isinstance(value, dict) else [value]
        for v in vals:
            num = _flat(v)
            if num:
                drift.append(
                    f"manager scoring is active again ({key} = {num:g}); Gaffer "
                    "models no Assistant Manager rules")
                break

    return drift


def verify(bootstrap: dict[str, Any] | None) -> dict[str, Any]:
    """Read and check the season's scoring table.

    Returns a small record for ``meta`` — source, status and any drift — and
    raises :class:`ScoringRuleDrift` when the live rules and the model disagree,
    unless the override is set.
    """
    scoring = parse_scoring(bootstrap)
    if scoring is None:
        return {
            "source": SOURCE_ABSENT,
            "status": STATUS_UNVERIFIED,
            "drift": [],
            "reason": "this API build ships no game_config.scoring block, so "
                      "gaffer.config constants are unverified this run",
        }
    drift = compare(scoring)
    if not drift:
        return {"source": SOURCE_API, "status": STATUS_VERIFIED, "drift": [],
                "reason": None}
    if os.environ.get(DRIFT_OVERRIDE_ENV, "").strip() not in ("", "0", "false", "no"):
        return {"source": SOURCE_API, "status": STATUS_DRIFT_ALLOWED,
                "drift": drift,
                "reason": f"{len(drift)} rule(s) differ from gaffer.config and "
                          f"{DRIFT_OVERRIDE_ENV} is set"}
    raise ScoringRuleDrift(drift)
