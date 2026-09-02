"""4.1 / 4.5 / 4.6 -- the canonical decision card.

One object answers "what should I do and why", and every surface renders that
same object: the site, the MCP tool, and the immutable pre-deadline snapshot.

**Why this exists.** The same recommendation was assembled three times from the
same artifact, by three renderers with three different ideas of what mattered.
The site showed the headline, the delta and the risk; the MCP tool showed a
different subset; the snapshot stored the raw decision and left the review to
reconstruct what had been on screen. So "did Gaffer's advice work?" was scored
against a reconstruction, and any of the three could drift without a test
noticing. Cardinality (§0.3) says exactly one canonical object per question.
This is that object for the weekly decision.

**What it is not.** It computes nothing. Every value is lifted from what the
decision already published, so the card cannot disagree with the artifact it
describes -- there is no second code path in which to disagree. The one thing
it adds is the `content_hash`, which is what lets the equality invariant in
`contract.py` prove that the three surfaces really are showing one object.

**Absence is a value.** A field that could not be filled says so and says why.
An empty list and "we could not measure this" are different answers, and the
review scores what was shown -- including a shown absence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: Bumped when the SHAPE changes in a way a stored card could not satisfy.
#: Old snapshots keep their own version: a card is a record of what was shown,
#: and re-versioning history would be a lie about the past.
CARD_VERSION = "decision-card-1"

#: The key the hash lives under, excluded from its own input.
HASH_FIELD = "content_hash"

#: Every field of the schema, in the order §4.1 names them. Exported so a test
#: can assert the builder emits exactly this set -- a card missing a field is a
#: renderer quietly dropping part of the answer, which is the failure this
#: whole task exists to stop.
CARD_FIELDS: tuple[str, ...] = (
    "card_version",
    "gameweek",
    "recommendation",
    "strength",
    "alternatives",
    "margin",
    "horizon",
    "cost",
    "upside",
    "downside",
    "sensitivity",
    "what_would_change_it",
    "league_effect",
    "evidence_quality",
)


def _absent(reason: str) -> dict[str, Any]:
    """A field that could not be filled, and why. Never an empty stand-in."""
    return {"available": False, "reason": reason}


def content_hash(card: dict[str, Any]) -> str:
    """A stable hash of everything in the card except the hash itself.

    Canonical JSON: sorted keys, no insignificant whitespace. Two surfaces
    showing the same card must agree on the digest even if one of them
    round-tripped the object through a different JSON writer.
    """
    body = {k: v for k, v in card.items() if k != HASH_FIELD}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def verify(card: Any) -> tuple[bool, str]:
    """Is this card internally consistent? Returns (ok, reason)."""
    if not isinstance(card, dict):
        return False, f"card is {type(card).__name__}, not an object"
    stored = card.get(HASH_FIELD)
    if not stored:
        return False, f"card has no {HASH_FIELD}"
    missing = [f for f in CARD_FIELDS if f not in card]
    if missing:
        return False, f"card is missing {', '.join(missing)}"
    actual = content_hash(card)
    if actual != stored:
        return False, f"content hash is {stored}, recomputes to {actual}"
    return True, ""


def _player(p: Any, resolve: Any) -> dict[str, Any] | None:
    """The identity fields a card needs, and nothing that changes hourly.

    Price and projection are deliberately excluded: the card is hashed, and a
    card whose digest moved because a projection ticked by 0.01 would fail the
    equality invariant for no reason a reader would recognise as a change.

    Accepts either an id (the decision layer speaks in ids) or an already
    resolved player object, so the card is built once and is self-contained on
    every surface. That is the point of 4.5: the snapshot and the site must be
    able to hash to the same digest, which they cannot do if one of them still
    needs a join to be readable.
    """
    if isinstance(p, dict):
        return {"id": p.get("id"), "name": p.get("name"),
                "team": p.get("team"), "pos": p.get("pos")}
    if p is None:
        return None
    got = resolve(p) if resolve else None
    if isinstance(got, dict):
        return got
    return {"id": p, "name": None, "team": None, "pos": None}


def _league_effect(effects: Any) -> Any:
    """The league effect, SUMMARISED. A card is not a rival table.

    The per-rival detail is 11 kB of a 20 kB MCP budget, and it answers a
    different question -- "how does this move affect each named rival?" --
    which `get_league_strategy` and the League page already own. Carrying it
    here would be the Cardinality violation this module exists to close,
    wearing the costume of completeness.

    What a decision card needs is whether the move helps in each competition,
    and against which rival it does best and worst, because that is what
    changes whether the move is worth making.
    """
    if not isinstance(effects, list) or not effects:
        return _absent("no league effect was computed for this decision")
    out = []
    for lg in effects:
        if not isinstance(lg, dict):
            continue
        rivals = [r for r in (lg.get("rivals") or []) if isinstance(r, dict)]
        scored = [r for r in rivals
                  if isinstance(r.get("d_p_above"), (int, float))]
        worst = min(scored, key=lambda r: r["d_p_above"]) if scored else None
        best = max(scored, key=lambda r: r["d_p_above"]) if scored else None
        out.append({
            "league_id": lg.get("league_id"),
            "name": lg.get("name"),
            "is_the_recommendation": lg.get("move_is_the_recommendation"),
            "rivals_measured": len(scored),
            "rivals_unresolved": sum(1 for r in rivals if not r.get("resolved")),
            "best_case": ({"rival": best.get("name"),
                           "d_p_above": best.get("d_p_above")}
                          if best else None),
            "worst_case": ({"rival": worst.get("name"),
                            "d_p_above": worst.get("d_p_above")}
                           if worst else None),
            "detail": "per-rival detail: get_league_strategy, or the League page",
        })
    return out or _absent("no league effect was computed for this decision")


def build(
    dec: dict[str, Any],
    *,
    gameweek: int | None,
    horizon: int | None,
    resolve: Any = None,
) -> dict[str, Any]:
    """Assemble the canonical card from a published decision.

    `dec` is `Decision.as_dict()` -- the artifact's own `decision` block, not
    the dataclass. Taking the serialised form on purpose: the card must be
    built from what was PUBLISHED, so it cannot describe a value the artifact
    does not contain.
    """
    cmp_ = dec.get("comparison") or {}
    exe = dec.get("executability") or {}
    thresholds = dec.get("thresholds") or {}
    action = dec.get("action")

    # --- margin: the edge, with both intervals named ----------------------
    if cmp_:
        ci = cmp_.get("delta_ci95")
        rng = cmp_.get("delta_range_p10_p90")
        margin: dict[str, Any] = {
            "available": True,
            "value": cmp_.get("delta"),
            "unit": "expected points",
            "ci95": ci,
            "interval_type": cmp_.get("delta_ci95_interval_type", "monte_carlo"),
            "interval_means": ("simulation error on the mean edge; it shrinks "
                               "as more scenarios are drawn"),
            "p_beats_hold": cmp_.get("p_move_beats_hold"),
            "measured_in": f"{cmp_.get('simulations')} shared fixture scenarios",
            "domain": (cmp_.get("domain") or {}).get("delta"),
        }
        if rng:
            margin["realistic_range"] = rng
            margin["realistic_range_interval_type"] = cmp_.get(
                "delta_range_interval_type", "prediction")
    else:
        margin = _absent("no move was scored against the hold this week")

    # --- upside / downside: football, not simulation error ----------------
    rng = cmp_.get("delta_range_p10_p90") if cmp_ else None
    if rng:
        upside = {
            "available": True,
            "value": rng[1],
            "means": ("in the best tenth of weeks this move beats holding by "
                      "about this much"),
            "interval_type": "prediction",
        }
        downside = {
            "available": True,
            "value": rng[0],
            "means": ("in the worst tenth of weeks this move does this badly "
                      "against holding" + (" -- the hit is already inside the "
                                           "number" if cmp_.get("hit_cost")
                                           else "")),
            "interval_type": "prediction",
        }
    else:
        upside = _absent("no paired scenario range was published")
        downside = _absent("no paired scenario range was published")

    # --- strength ---------------------------------------------------------
    strength = {
        "action": action,
        "label": dec.get("confidence"),
        "basis": thresholds.get("basis", "policy"),
        "fitted": thresholds.get("fitted", False),
        "min_actionable_points": thresholds.get("min_actionable_points"),
        "min_actionable_probability": thresholds.get("min_actionable_probability"),
        "note": ("the bars a move must clear before it is called an action are "
                 "a conservative policy floor, not a fitted parameter"),
    }

    # --- what it beat -----------------------------------------------------
    alternatives: list[dict[str, Any]] = []
    if cmp_:
        alternatives.append({
            "option": "hold",
            "label": "make no transfer",
            "expected": cmp_.get("hold_expected"),
            "beaten_by": cmp_.get("delta"),
        })
    cand = dec.get("candidate_move")
    if isinstance(cand, dict):
        alternatives.append({
            "option": "future_plan_first_step",
            "label": cand.get("label") or "the multi-week plan's first step",
            "status": cand.get("status"),
            "reason": cand.get("reason"),
        })

    # --- cost -------------------------------------------------------------
    cost = {
        "hit_points": (cmp_ or {}).get("hit_cost", 0),
        "paid_transfers": exe.get("paid_transfers"),
        "free_transfers_before": exe.get("free_transfers_before"),
        "free_transfers_after": exe.get("free_transfers_after"),
        "bank_before": exe.get("bank_before"),
        "bank_after": exe.get("bank_after"),
        "affordable": exe.get("affordable"),
        "note": exe.get("reason") or "",
    }

    # --- sensitivity: what the answer turns on ----------------------------
    risk = dec.get("biggest_risk")
    sensitivity = {
        "available": bool(risk),
        "biggest_risk": risk or None,
        "assumptions": dec.get("assumptions") or [],
    } if risk or dec.get("assumptions") else _absent(
        "nothing was identified as load-bearing for this decision")

    # --- what would change it ---------------------------------------------
    changers: list[str] = []
    if cmp_ and isinstance(cmp_.get("delta"), (int, float)):
        floor = thresholds.get("min_actionable_points")
        if isinstance(floor, (int, float)):
            gap = round(float(floor) - float(cmp_["delta"]), 2)
            if action != "transfer" and gap > 0:
                changers.append(
                    f"{gap:+.2f} more expected points would take this over the "
                    f"{floor:.1f}-point actionable floor")
            elif action == "transfer":
                changers.append(
                    f"losing {abs(gap):.2f} expected points would drop this "
                    f"back below the {floor:.1f}-point actionable floor")
    p = cmp_.get("p_move_beats_hold") if cmp_ else None
    p_floor = thresholds.get("min_actionable_probability")
    if isinstance(p, (int, float)) and isinstance(p_floor, (int, float)):
        changers.append(
            f"the move beats holding in {p:.0%} of scenarios against a "
            f"{p_floor:.0%} floor")
    if risk:
        changers.append(risk)
    what_would_change_it = changers or _absent(
        "no threshold or risk was close enough to name")

    return _finish({
        "card_version": CARD_VERSION,
        "gameweek": gameweek,
        "recommendation": {
            "action": action,
            "headline": dec.get("headline"),
            "reason": dec.get("reason"),
            "transfers_out": [_player(p, resolve) for p in dec.get("transfers_out") or []],
            "transfers_in": [_player(p, resolve) for p in dec.get("transfers_in") or []],
            "captain": _player(dec.get("captain"), resolve),
            "vice": _player(dec.get("vice"), resolve),
        },
        "strength": strength,
        "alternatives": alternatives,
        "margin": margin,
        "horizon": {
            "gameweeks": horizon,
            "delta": (cmp_ or {}).get("horizon_delta"),
            "domain": ((cmp_.get("domain") or {}).get("horizon_delta")
                       if cmp_ else None),
        },
        "cost": cost,
        "upside": upside,
        "downside": downside,
        "sensitivity": sensitivity,
        "what_would_change_it": what_would_change_it,
        "league_effect": _league_effect(dec.get("league_effects")),
        "evidence_quality": dec.get("evidence_quality") or _absent(
            "evidence quality was not published with this decision"),
    })


def _finish(card: dict[str, Any]) -> dict[str, Any]:
    """Order the keys canonically and stamp the digest."""
    ordered = {k: card[k] for k in CARD_FIELDS}
    ordered[HASH_FIELD] = content_hash(ordered)
    return ordered
