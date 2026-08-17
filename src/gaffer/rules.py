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

``verified`` means *checked and agreed*, never merely *un-contradicted*. The
difference is the whole of C5: a comparison that skips absent keys reports no
drift for a payload that carried no rules at all, and the run was then stamped
with maximum confidence at the exact moment it had zero evidence. Absent data
and agreeing data have to be different answers, so every rule this module claims
to cover is enumerated once, in :func:`_checklist`, and what the payload did not
carry comes back in the record as ``unchecked``.
"""

from __future__ import annotations

import os
from typing import Any

from gaffer import config

#: Where the scoring table came from on this run. Stamped into ``meta``.
SOURCE_API = "game_config"
SOURCE_ABSENT = "absent"
#: The block was there and did not cover every rule Gaffer models. A separate
#: value from ``SOURCE_ABSENT`` because the two need different responses: absent
#: is an older API build, partial is a payload that arrived damaged, and at
#: 16:30 on a Friday that is the first thing an operator needs to know.
SOURCE_PARTIAL = "game_config_partial"

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

#: What counts as consent to run with known drift, and what counts as a refusal.
#: Anything in neither set is neither — see :func:`_override_consent`.
DRIFT_OVERRIDE_YES = frozenset({"1", "true", "yes", "y", "on"})
DRIFT_OVERRIDE_NO = frozenset({"", "0", "false", "no", "n", "off"})


class ScoringRuleDrift(RuntimeError):
    """Raised when the API's scoring table disagrees with what Gaffer models.

    Deliberately fatal by default. The alternative is a green run that projects
    a whole season under the previous season's rules.
    """

    def __init__(self, drift: list[str], override_note: str | None = None) -> None:
        self.drift = list(drift)
        self.override_note = override_note
        bullets = "\n".join(f"    - {d}" for d in self.drift)
        parts = [
            "Refusing to run: the FPL API's scoring table disagrees with the "
            "rules Gaffer models.",
            bullets,
            "Nothing was ingested. Update `gaffer.config` to match the live "
            "rules (and re-check anything downstream that assumed the old "
            f"value), or set {DRIFT_OVERRIDE_ENV}=1 to run anyway with the "
            "drift recorded in meta.json.",
        ]
        if override_note:
            # The operator already answered this question and we did not accept
            # the answer. Saying so here is the difference between a second red
            # run and a second red run they can act on.
            parts.append(override_note)
        parts.append(
            "Rules the API does not publish, which this check cannot cover:\n"
            + "\n".join(f"    - {u}" for u in UNVERIFIABLE))
        super().__init__("\n".join(parts))


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


def _checklist() -> list[tuple[str, str, Any]]:
    """``(API key, label, what Gaffer models)`` for every rule this check covers.

    One list, read by both :func:`compare` and :func:`unchecked_rules`, because
    the two questions have to be asked of exactly the same set of rules. C5 was
    that set drifting apart one level up: the comparison quietly skipped any key
    the payload did not carry, and nothing downstream could tell "we compared
    thirteen rules and they all agreed" from "we compared none".

    A dict value is compared per position; a scalar is compared flat.
    """
    return [
        ("goals_scored", "goal points", config.GOAL_POINTS),
        ("clean_sheets", "clean-sheet points", config.CS_POINTS),
        ("goals_conceded", "goals-conceded points",
         {p: (config.CONCEDED_PENALTY if p in config.CONCEDED_POSITIONS else 0)
          for p in config.POSITIONS}),
        # DEFCON: a position Gaffer never awards is encoded as an unreachable
        # threshold, so translate to points before comparing.
        ("defensive_contribution", "defensive-contribution points",
         {p: (config.DEFCON_POINTS if config.DEFCON_THRESHOLD[p] < 99 else 0)
          for p in config.POSITIONS}),
        ("assists", "assist points", config.ASSIST_POINTS),
        ("saves", "points per save block", config.SAVE_POINTS),
        ("penalties_saved", "penalty-save points", config.PENALTY_SAVE_POINTS),
        ("penalties_missed", "penalty-miss points", config.PENALTY_MISS_POINTS),
        ("yellow_cards", "yellow-card points", config.YELLOW_POINTS),
        ("red_cards", "red-card points", config.RED_POINTS),
        ("own_goals", "own-goal points", config.OWN_GOAL_POINTS),
        ("long_play", "appearance points (60'+)", config.APPEARANCE_LONG),
        ("short_play", "appearance points (under 60')", config.APPEARANCE_SHORT),
    ]


def unchecked_rules(scoring: dict[str, Any]) -> list[str]:
    """Every rule Gaffer models that this payload did not let us check.

    Reported per position rather than per key: a ``goals_scored`` block carrying
    only GKP and DEF has verified half a rule, and half a rule is not a rule.

    This is the other half of :func:`compare`. Drift is the API telling us a
    value changed; a gap here is the API telling us nothing at all, and the two
    must never arrive at the reader wearing the same badge.
    """
    gaps: list[str] = []
    for key, label, ours in _checklist():
        raw = scoring.get(key)
        if isinstance(ours, dict):
            theirs = _pos_map(raw) or {}
            absent = [p for p in config.POSITIONS if p not in theirs]
            if absent:
                gaps.append(f"{label} ({key}): {', '.join(absent)}")
        elif _flat(raw) is None:
            gaps.append(f"{label} ({key})")
    return gaps


def _override_consent() -> tuple[bool, str | None]:
    """Does ``GAFFER_ALLOW_RULE_DRIFT`` hold something that means yes?

    C16: the old test was ``value not in ("", "0", "false", "no")``, so the
    literal string ``False`` — what ``str(bool)`` writes, and therefore what any
    templating layer hands you — read as consent to silence a safety check. So
    did ``off``, ``FALSE``, and every typo. An override that silences a check
    has to be satisfied by an explicit yes and nothing else.

    An unrecognised value refuses consent rather than raising. Raising would
    replace the drift report with a complaint about a variable, and it would
    also fire on the overwhelming majority of runs, where there is no drift and
    the value changes nothing. But refusing *silently* would leave an operator
    staring at a red run they believe they already answered, so the refusal
    comes back as a note and is printed inside the drift report itself.
    """
    raw = os.environ.get(DRIFT_OVERRIDE_ENV)
    if raw is None:
        return False, None
    value = raw.strip().lower()
    if value in DRIFT_OVERRIDE_YES:
        return True, None
    if value in DRIFT_OVERRIDE_NO:
        return False, None
    return False, (
        f"Note: {DRIFT_OVERRIDE_ENV}={raw!r} is not one of "
        f"{sorted(DRIFT_OVERRIDE_YES)}, so it was NOT read as consent to run "
        "with drift. An override that silences a safety check only accepts an "
        "explicit yes. Set it to 1 if that is what you meant.")


def compare(scoring: dict[str, Any]) -> list[str]:
    """Every disagreement between the live table and Gaffer's constants.

    Only rules Gaffer actually models are compared — an API key the model does
    not consume is not drift, it is an unused field. A key the model *does*
    consume and the payload does not carry is not drift either: it is a gap, and
    :func:`unchecked_rules` is what reports it.
    """
    drift: list[str] = []

    for key, label, ours in _checklist():
        if isinstance(ours, dict):
            theirs = _pos_map(scoring.get(key))
            if theirs is None:
                continue
            for pos, value in theirs.items():
                mine = float(ours.get(pos, 0.0))
                if value != mine:
                    drift.append(
                        f"{label} ({pos}): API says {value:g}, "
                        f"gaffer.config has {mine:g}")
            continue
        theirs_flat = _flat(scoring.get(key))
        if theirs_flat is None:
            continue
        if theirs_flat != float(ours):
            drift.append(
                f"{label}: API says {theirs_flat:g}, "
                f"gaffer.config has {float(ours):g}")

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

    Returns a small record for ``meta`` — source, status, any drift, and any
    rule the payload left unchecked — and raises :class:`ScoringRuleDrift` when
    the live rules and the model disagree, unless the override is set.

    ``status == STATUS_VERIFIED`` means every rule in :func:`_checklist` was
    found in the payload and agreed with it. Nothing weaker earns that word.
    """
    scoring = parse_scoring(bootstrap)
    if scoring is None:
        return {
            "source": SOURCE_ABSENT,
            "status": STATUS_UNVERIFIED,
            "drift": [],
            "unchecked": unchecked_rules({}),
            "reason": "this API build ships no game_config.scoring block, so "
                      "gaffer.config constants are unverified this run",
        }
    gaps = unchecked_rules(scoring)
    drift = compare(scoring)
    if drift:
        # Drift outranks incompleteness. A payload that is both wrong and short
        # is wrong: evidence that a rule changed beats absence of evidence about
        # the rest, and the absence is still carried in the record.
        allowed, note = _override_consent()
        if allowed:
            return {"source": SOURCE_API, "status": STATUS_DRIFT_ALLOWED,
                    "drift": drift, "unchecked": gaps,
                    "reason": f"{len(drift)} rule(s) differ from gaffer.config "
                              f"and {DRIFT_OVERRIDE_ENV} is set"}
        raise ScoringRuleDrift(drift, note)
    if gaps:
        # C5. An empty or truncated table is UNVERIFIED — never verified, and
        # deliberately not fatal either.
        #
        # The distinction is evidence of wrongness versus absence of evidence.
        # Drift is FPL telling us a rule changed, and refusing to run is the only
        # honest answer to that. A missing key tells us nothing about the rule,
        # only that this payload did not carry it, and refusing there would hand
        # FPL's flakiest hour a switch that takes the product down: a truncated
        # bootstrap at 16:30 on a Friday would mean no publish at all before a
        # 17:30 deadline, and the reader would be left holding the previous run's
        # artifact — stale, and with nothing on screen to say so. Publishing on
        # the constants with `status: unverified` and the uncovered rules named
        # is strictly more informative than publishing nothing, and it is the
        # same bargain already struck when `game_config` is absent entirely.
        #
        # What is not acceptable is the behaviour this replaces, where the same
        # payload produced `status: verified`: maximum confidence, zero evidence.
        return {"source": SOURCE_PARTIAL, "status": STATUS_UNVERIFIED,
                "drift": [], "unchecked": gaps,
                "reason": f"game_config.scoring did not cover {len(gaps)} of the "
                          f"{len(_checklist())} rules Gaffer models, so those "
                          "constants are unverified this run"}
    return {"source": SOURCE_API, "status": STATUS_VERIFIED, "drift": [],
            "unchecked": [], "reason": None}
