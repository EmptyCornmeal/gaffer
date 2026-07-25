"""Turn a projection into a plain-English *why* + tags.

Deterministic and data-driven — every claim traces to a number the model already
computed. The Phase 3 AI layer can later replace ``player_rationale`` with richer
prose behind the same field, but this keeps "explain every pick" true from day 1.
"""

from __future__ import annotations

from gaffer import config

_POS_NOUN = {"GKP": "keeper", "DEF": "defender", "MID": "midfielder", "FWD": "forward"}


def xmins_badge(exp_minutes: float, p_start: float | None = None) -> dict[str, str]:
    """NAILED / ROTATION / CAMEO minutes read (mirrors v1's badgeForXMins).

    Label is driven by start probability (a nailed starter reads NAILED even
    though projected minutes top out ~82'); the hint shows projected minutes.
    """
    hint = f"~{round(exp_minutes or 0)}'"
    p = p_start if p_start is not None else (exp_minutes or 0) / 90.0
    if p >= 0.85:
        return {"label": "NAILED", "kind": "good", "hint": hint}
    if p >= 0.6:
        return {"label": "ROTATION", "kind": "warn", "hint": hint}
    return {"label": "CAMEO?", "kind": "bad", "hint": hint}


def player_tags(p: dict) -> list[dict[str, str]]:
    """Short chips summarising the case for/against a player."""
    pos = p["position"]
    tags: list[dict[str, str]] = []

    # availability
    if p.get("news") or (p.get("status") and p["status"] != "a"):
        tags.append({"label": "fitness doubt", "kind": "bad"})

    # minutes
    ps = p.get("p_start", 0)
    if ps >= 0.85:
        tags.append({"label": "nailed", "kind": "good"})
    elif ps < 0.6:
        tags.append({"label": "rotation risk", "kind": "warn"})

    # defensive contribution
    thr = config.DEFCON_THRESHOLD.get(pos, 99)
    if p.get("defcon90", 0) >= thr < 99:
        tags.append({"label": "elite DEFCON", "kind": "good"})

    # clean sheets
    if p.get("cs_pts", 0) >= 1.2:
        tags.append({"label": "clean-sheet source", "kind": "good"})

    # attacking
    if pos in ("MID", "FWD") and p.get("xgi90", 0) >= 0.55:
        tags.append({"label": "high xGI", "kind": "good"})
    if p.get("set_pieces"):
        tags.append({"label": "set-pieces", "kind": "info"})
    if (p.get("form") or 0) >= 5:
        tags.append({"label": "in form", "kind": "good"})

    # ownership
    owned = p.get("owned_by") or 0
    if owned >= 30:
        tags.append({"label": "template", "kind": "info"})
    elif 0 < owned < 5 and p.get("xp_next", 0) >= 3.5:
        tags.append({"label": "differential", "kind": "info"})

    # fixtures
    diff = [f.get("difficulty", 3) for f in p.get("fixtures", [])]
    if diff:
        avg = sum(diff) / len(diff)
        if avg <= 2.3:
            tags.append({"label": "soft fixtures", "kind": "good"})
        elif avg >= 3.7:
            tags.append({"label": "tough run", "kind": "warn"})
    return tags


def player_rationale(p: dict) -> str:
    """One-line justification, led by whatever matters most for the position."""
    pos = p["position"]
    noun = _POS_NOUN.get(pos, "player")
    ps = p.get("p_start", 0)

    # minutes lead
    if ps >= 0.85:
        lead = f"Nailed {noun}"
    elif ps >= 0.6:
        lead = f"Likely-starting {noun} (rotation risk)"
    else:
        lead = f"Rotation/bench {noun}"

    reasons: list[str] = []
    if pos in ("DEF", "GKP"):
        if p.get("cs_pts", 0) >= 0.8:
            reasons.append("strong clean-sheet odds")
        thr = config.DEFCON_THRESHOLD.get(pos, 99)
        if p.get("defcon90", 0) >= thr < 99:
            reasons.append(f"elite defensive volume ({p['defcon90']:.1f}/90 → +2 most weeks)")
        if p.get("goal_pts", 0) + p.get("assist_pts", 0) >= 0.6:
            reasons.append("a genuine attacking threat from the back")
    else:
        if p.get("xgi90", 0) >= 0.4:
            reasons.append(f"real underlying threat ({p['xgi90']:.2f} xGI/90)")
        if p.get("set_pieces"):
            reasons.append(f"on {p['set_pieces']}")
        thr = config.DEFCON_THRESHOLD.get(pos, 99)
        if p.get("defcon90", 0) >= thr < 99:
            reasons.append(f"reliable DEFCON points ({p['defcon90']:.1f}/90 → +2 most weeks)")

    if (p.get("form") or 0) >= 5:
        reasons.append(f"in form ({p['form']:.1f})")

    diff = [f.get("difficulty", 3) for f in p.get("fixtures", [])]
    if diff:
        avg = sum(diff) / len(diff)
        if avg <= 2.3:
            reasons.append("a soft fixture run")
        elif avg >= 3.7:
            reasons.append("despite a tough run")

    if p.get("news"):
        reasons.append(f"but note: {p['news']}")

    if not reasons:
        reasons.append(f"projected {p.get('xp_next', 0):.1f} pts next GW")

    body = reasons[0]
    if len(reasons) > 1:
        body = ", ".join(reasons[:-1]) + f" and {reasons[-1]}"
    return f"{lead} with {body}."
