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


#: 4.7 -- which measured population the badge quotes.
#:
#: `overall` is every registered player, and 22,490 of its 29,757 rows are
#: CAMEO?: players nobody is choosing between, trivially easy to call, and they
#: carry the aggregate. `considered` is the same rows cut to the most-owned --
#: the ones a manager actually picks between -- and the CAMEO? error changes
#: SIGN between the two. A badge shown next to a player being considered must
#: quote the population that player belongs to.
BADGE_CALIBRATION_POPULATION = "considered"

#: Where the numbers come from. Never computed here: this module owns the
#: thresholds, `backtest.START_BANDS` mirrors them, and the measurement is the
#: backtest's.
BADGE_CALIBRATION_SOURCE = "backtest.json -> minutes_model.bands.considered"


def badge_calibration(backtest: dict | None) -> dict:
    """What each badge claimed and what the badged players then did.

    A badge is a one-word confidence statement and was the least qualified
    output in the product: NAILED asserts a near-certainty in capital letters
    and, measured, over-claims by five points. This attaches the measurement
    to the claim.

    Read from the backtest artifact rather than restated here. A constant
    would drift the moment the model changed, and a stale calibration is worse
    than none: it would be a false reassurance carrying a measurement's
    authority.
    """
    bands = (((backtest or {}).get("minutes_model") or {})
             .get("bands") or {}).get(BADGE_CALIBRATION_POPULATION)
    if not isinstance(bands, list) or not bands:
        return {"available": False,
                "reason": f"no measured bands at {BADGE_CALIBRATION_SOURCE}"}
    out: dict = {
        "available": True,
        "population": BADGE_CALIBRATION_POPULATION,
        "source": BADGE_CALIBRATION_SOURCE,
        "means": ("`claimed` is the mean start probability of the players who "
                  "wore this badge in the archive; `start_rate` is how often "
                  "they then started. The gap is the badge's own error."),
        "bands": {},
    }
    for row in bands:
        name = row.get("band")
        if not name:
            continue
        claimed, actual = row.get("claimed"), row.get("start_rate")
        entry = {
            "claimed": claimed,
            "start_rate": actual,
            "appear_rate": row.get("appear_rate"),
            "n": row.get("n"),
        }
        if isinstance(claimed, (int, float)) and isinstance(actual, (int, float)):
            entry["over_claims_by"] = round(claimed - actual, 3)
        out["bands"][name] = entry
    return out


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
