"""Phase 7 -- the post-gameweek loop.

Gaffer already stores what it advised before each deadline and what happened
afterwards. What it did not do was CLOSE the loop: say whether the advice was
good, separately from whether the week was, and say what it would take before
the policy bars could be fitted rather than declared.

Three things this module refuses to do, each of which is the easy version:

**It never scores hindsight.** The decision axis is judged on the pre-deadline
snapshot alone -- what was expected when the call was made. The realised points
are the OUTCOME axis and are never allowed to leak into the judgement of the
decision. A good decision with a bad outcome is the most common shape of a
correct call, and a loop that cannot say so teaches the reader to chase
variance.

**It never fits a threshold it lacks the power to fit.** `min_actionable_points`
(1.0) and `min_actionable_probability` (0.55) are declared policy. The obvious
move after a few gameweeks is to "tune" them against results. The sample
requirement below is computed, pre-registered and honest about what it implies,
and until it is met the answer is `insufficient_data` -- however many gameweeks
have gone by.

**It never counts a gameweek as a decision it can learn from.** Only DISCORDANT
decisions inform a threshold: weeks where the candidate bar would have chosen
differently. A season of "roll" under both the old and the new bar teaches
nothing about where the bar belongs.
"""
from __future__ import annotations

import math
from typing import Any

LOOP_VERSION = "loop-1"

# ---------------------------------------------------------------------------
# 7.2 -- the four cells
# ---------------------------------------------------------------------------

#: decision (was it the right call on what was known?) x outcome (did it work?)
RIGHT_CALL_GOOD_RESULT = "right call, good result"
RIGHT_CALL_BAD_RESULT = "right call, bad result"
WRONG_CALL_GOOD_RESULT = "wrong call, good result"
WRONG_CALL_BAD_RESULT = "wrong call, bad result"
UNRESOLVED = "unresolved"

CELLS = (RIGHT_CALL_GOOD_RESULT, RIGHT_CALL_BAD_RESULT,
         WRONG_CALL_GOOD_RESULT, WRONG_CALL_BAD_RESULT)

CELL_MEANING: dict[str, str] = {
    RIGHT_CALL_GOOD_RESULT: "the evidence supported it and it worked",
    RIGHT_CALL_BAD_RESULT: (
        "the evidence supported it and it did not work. The most common shape "
        "of a correct call, and the one a results-only review punishes"),
    WRONG_CALL_GOOD_RESULT: (
        "the evidence did not support it and it worked anyway. Got away with "
        "it -- the most dangerous cell, because it rewards the wrong habit"),
    WRONG_CALL_BAD_RESULT: "the evidence did not support it and it did not work",
    UNRESOLVED: "not enough of the record survives to place this decision",
}


def classify(snapshot: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """Place one decision in the matrix.

    `snapshot` is the immutable pre-deadline record; `review` is what happened.
    The two axes are computed from different halves on purpose, and the
    function takes them as separate arguments so that separation is visible in
    the signature rather than trusted to a convention.
    """
    dec = (snapshot or {}).get("decision") or {}
    cmp_ = dec.get("comparison") or {}
    action = dec.get("action")

    # --- the DECISION axis: only what was known before the deadline --------
    delta = cmp_.get("delta")
    p_beat = cmp_.get("p_move_beats_hold")
    thresholds = dec.get("thresholds") or dec.get("threshold_status") or {}
    min_pts = thresholds.get("min_actionable_points")
    min_p = thresholds.get("min_actionable_probability")

    if action is None or delta is None:
        return {"cell": UNRESOLVED, "meaning": CELL_MEANING[UNRESOLVED],
                "why": "the snapshot carries no scored comparison"}

    if action == "transfer":
        # A transfer was the right call if the edge cleared BOTH declared bars.
        right = (isinstance(min_pts, (int, float)) and delta >= min_pts
                 and isinstance(p_beat, (int, float))
                 and isinstance(min_p, (int, float)) and p_beat >= min_p)
        basis = (f"the move projected {delta:+.2f} against a "
                 f"{min_pts}-point bar and beat holding in "
                 f"{p_beat:.0%} of scenarios against a {min_p:.0%} bar")
    else:
        # Rolling was the right call if no move cleared the bars -- which is
        # what the published action already encodes. Judging a roll by what
        # some unmade transfer would have returned is hindsight wearing a
        # decision's clothes.
        right = not (isinstance(min_pts, (int, float)) and delta >= min_pts
                     and isinstance(p_beat, (int, float))
                     and isinstance(min_p, (int, float)) and p_beat >= min_p)
        basis = (f"no move cleared the bars: best edge {delta:+.2f} against "
                 f"{min_pts}, winning {p_beat:.0%} against {min_p:.0%}"
                 if isinstance(p_beat, (int, float)) else
                 f"no move cleared the {min_pts}-point bar (best {delta:+.2f})")

    # --- the OUTCOME axis: what actually happened -------------------------
    quality = (review or {}).get("quality") or {}
    pct = quality.get("outcome_percentile")
    realised = quality.get("realised")
    expected = quality.get("expected_at_decision")

    if isinstance(pct, (int, float)):
        good = pct >= 0.5
        outcome_basis = (f"the week landed at the {pct:.0%} percentile of the "
                         f"distribution stored before the deadline")
    elif isinstance(realised, (int, float)) and isinstance(expected, (int, float)):
        good = realised >= expected
        outcome_basis = f"{realised:.0f} points against {expected:.1f} expected"
    else:
        return {"cell": UNRESOLVED, "meaning": CELL_MEANING[UNRESOLVED],
                "why": "the gameweek has no scored outcome yet",
                "decision_was_right": right, "decision_basis": basis}

    cell = (RIGHT_CALL_GOOD_RESULT if right and good
            else RIGHT_CALL_BAD_RESULT if right
            else WRONG_CALL_GOOD_RESULT if good
            else WRONG_CALL_BAD_RESULT)
    return {
        "cell": cell,
        "meaning": CELL_MEANING[cell],
        "decision_was_right": right,
        "decision_basis": basis,
        "outcome_was_good": good,
        "outcome_basis": outcome_basis,
        "hindsight_note": (
            "the decision axis is computed from the pre-deadline snapshot "
            "only; the outcome never informs it"),
    }


def matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The four cells, counted, with the reading that matters called out."""
    counts = dict.fromkeys([*CELLS, UNRESOLVED], 0)
    for r in rows:
        cell = (r or {}).get("cell")
        if cell in counts:
            counts[cell] += 1
    resolved = sum(counts[c] for c in CELLS)
    out: dict[str, Any] = {
        "counts": counts,
        "resolved": resolved,
        "meanings": dict(CELL_MEANING),
    }
    if not resolved:
        out["reading"] = "no gameweek has both a stored decision and a result yet"
        return out
    right = counts[RIGHT_CALL_GOOD_RESULT] + counts[RIGHT_CALL_BAD_RESULT]
    lucky = counts[WRONG_CALL_GOOD_RESULT]
    out["decision_quality_rate"] = round(right / resolved, 3)
    out["reading"] = (
        f"{right} of {resolved} decisions were supported by the evidence "
        f"available before the deadline. "
        + (f"{lucky} worked despite not being -- that cell is the one to watch, "
           f"because it rewards the wrong habit."
           if lucky else
           "None worked despite not being supported."))
    # An honest floor, not a hidden one.
    out["reportable"] = resolved >= MIN_RESOLVED_TO_READ
    out["reportable_floor"] = MIN_RESOLVED_TO_READ
    if not out["reportable"]:
        out["caveat"] = (
            f"{resolved} resolved decision(s). Below {MIN_RESOLVED_TO_READ} "
            f"the counts are shown because hiding the record is worse, but "
            f"the RATE above is not a measurement of anything and must not be "
            f"read as one.")
    return out


#: 7.5 -- below this the counts are shown and the rate is not.
#:
#: A DECLARED POLICY FLOOR. It is set where it is for one reason that can be
#: stated: at fewer than five resolved decisions a single week moves the rate
#: by 20 points or more, so the number carries no information a reader could
#: act on. It is not a power calculation, and it is not pretending to be --
#: the power calculation is below, for the thing that actually needs one.
MIN_RESOLVED_TO_READ = 5


# ---------------------------------------------------------------------------
# 7.3 -- what it would take to FIT the bars
# ---------------------------------------------------------------------------

#: Pre-registered, before any data is looked at.
#:
#: The MCP has always said the thresholds should be reassessed after "~6
#: completed gameweeks". That number was inherited without scrutiny. Six
#: gameweeks is six decisions, and fitting two thresholds on six observations
#: is overfitting one month of a season and calling it measurement.
#:
#: These are the values the requirement is computed at, fixed here so the
#: answer cannot be tuned after seeing it.
FIT_ALPHA = 0.05          #: two-sided
FIT_POWER = 0.80
#: The smallest improvement worth changing a policy bar for, in expected points
#: per decision. Below this the change is not worth the churn even if real.
FIT_EFFECT_POINTS = 0.5

_Z_ALPHA_2 = 1.959964
_Z_POWER = 0.841621


def required_decisions(sigma_points: float) -> int:
    """How many DISCORDANT decisions a threshold change would need.

    Paired, because each decision is scored against the alternative it was
    chosen over in the same simulated scenarios -- which is the one place
    Gaffer's design helps here.

    `sigma_points` is the standard deviation of the per-decision difference
    between acting and holding. Gaffer publishes it: the paired p10-p90 range
    of the delta, which for an approximately normal difference is 2.563 sigma.
    """
    if not sigma_points or sigma_points <= 0:
        return 0
    n = ((_Z_ALPHA_2 + _Z_POWER) ** 2 * sigma_points ** 2) / (FIT_EFFECT_POINTS ** 2)
    return int(math.ceil(n))


def sigma_from_range(p10: float | None, p90: float | None) -> float | None:
    """Recover the per-decision SD from the published prediction interval."""
    if p10 is None or p90 is None:
        return None
    spread = float(p90) - float(p10)
    if spread <= 0:
        return None
    return spread / 2.563103


def fitting_readiness(
    discordant: int, sigma_points: float | None,
) -> dict[str, Any]:
    """Can the action bars be fitted yet? Almost certainly not, and by how far.

    Returns `insufficient_data` until the pre-registered requirement is met.
    Gameweek count is deliberately not an input: it is the wrong unit, and
    using it is how "~6 gameweeks" became a plan.
    """
    base = {
        "status": "insufficient_data",
        "unit": "discordant decisions, not gameweeks",
        "why_not_gameweeks": (
            "only a decision the candidate bar would have CHANGED carries "
            "information about where the bar belongs. A season of rolls under "
            "both the old and the new bar teaches nothing"),
        "pre_registered": {
            "alpha": FIT_ALPHA, "power": FIT_POWER,
            "smallest_effect_worth_acting_on_points": FIT_EFFECT_POINTS,
            "fixed_before_looking": True,
        },
        "discordant_decisions_so_far": discordant,
    }
    if sigma_points is None or sigma_points <= 0:
        base["reason"] = (
            "no published per-decision spread to compute a requirement from")
        return base
    need = required_decisions(sigma_points)
    base["per_decision_sd_points"] = round(sigma_points, 2)
    base["required_discordant_decisions"] = need
    base["shortfall"] = max(0, need - discordant)
    # One decision a gameweek, 38 gameweeks a season, and only a fraction of
    # them discordant -- so this is a floor on the answer, not an estimate.
    base["at_one_decision_per_gameweek_seasons"] = round(need / 38.0, 1)
    base["reason"] = (
        f"detecting a {FIT_EFFECT_POINTS:.1f}-point improvement at "
        f"{FIT_POWER:.0%} power needs {need:,} discordant decisions when the "
        f"per-decision spread is {sigma_points:.1f} points. That is at least "
        f"{need / 38.0:.0f} seasons of one decision a week, and only the "
        f"discordant ones count, so the real figure is larger. The bars stay "
        f"declared policy.")
    if discordant >= need:
        base["status"] = "ready"
        base["reason"] = (
            f"{discordant:,} discordant decisions meets the pre-registered "
            f"requirement of {need:,}")
    return base


# ---------------------------------------------------------------------------
# 7.4 -- do the human overrides beat the model?
# ---------------------------------------------------------------------------

def override_analysis(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether Myles's overrides beat Gaffer, and in which classes.

    `entries` are journal rows joined to their reviews: each carries what
    Gaffer advised, what was actually done, and how the week went.

    The reporting floor is the same discipline as everywhere else. An override
    record of three weeks says nothing about whether human judgement wins, and
    a module that answered anyway would be the most flattering possible lie --
    it is being asked by the human in question.
    """
    overrides = [e for e in entries if e.get("followed") is False]
    followed = [e for e in entries if e.get("followed") is True]
    out: dict[str, Any] = {
        "overrides": len(overrides),
        "followed": len(followed),
        "total": len(entries),
        "reportable": False,
        "reportable_floor": MIN_OVERRIDES_TO_READ,
    }
    if len(overrides) < MIN_OVERRIDES_TO_READ:
        out["status"] = "insufficient_data"
        out["reason"] = (
            f"{len(overrides)} override(s) recorded, floor is "
            f"{MIN_OVERRIDES_TO_READ}. Whether human judgement beats the model "
            f"is exactly the question a small sample answers most flatteringly, "
            f"and it is being asked by the human in question.")
        return out

    def _pct(rows: list[dict[str, Any]]) -> float | None:
        vals = [r["outcome_percentile"] for r in rows
                if isinstance(r.get("outcome_percentile"), (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    out["status"] = "reportable"
    out["reportable"] = True
    out["mean_percentile_when_overridden"] = _pct(overrides)
    out["mean_percentile_when_followed"] = _pct(followed)
    by_class: dict[str, int] = {}
    for e in overrides:
        by_class[e.get("override_kind") or "unclassified"] = (
            by_class.get(e.get("override_kind") or "unclassified", 0) + 1)
    out["override_kinds"] = by_class
    return out


#: Same discipline as MIN_RESOLVED_TO_READ, and a higher bar on purpose: this
#: question flatters small samples more than any other in the product.
MIN_OVERRIDES_TO_READ = 8
