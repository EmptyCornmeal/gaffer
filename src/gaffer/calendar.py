"""5.1 / 5.3 -- what is still to come before the deadline, and whether to wait.

**Honestly scoped, and the scope is the point.** This is time awareness and
money awareness. It is not football awareness, and a calendar that quietly
implied otherwise would be worse than none: a reader who sees "nothing pending"
would take it to mean "no team news is coming", which this cannot know.

What v1 knows, all of it published by FPL:

  - the deadline, and how long is left;
  - which of the player's own players have a price change DUE
    (`price_change_percent` past +/-100);
  - FPL's three-offset projection for each, with FPL's own likelihood grade;
  - which are price-LOCKED, and until when;
  - which schedule window the reader is in.

What v1 does NOT know, stated in the artifact rather than left to be inferred:
European fixtures, press conferences, predicted lineups, injury updates -- and,
established by measurement rather than assumed, **when** a due price change will
actually happen.

That last one matters. FPL's price system is now ROLLING, not the nightly
01:30 UTC event the folklore describes: the 58 currently locked players carry
lock timestamps at 09, 13, 14, 18, 19, 21 and 23 UTC. And
`price_change_hourly_rate` cannot be read as "percent per hour" -- Bailey's rate
of -17 sits beside a projection that moves -18.2 over one offset, while Osula's
-135 sits beside -11.5. The two do not reconcile in any units, so Gaffer
publishes the rate uninterpreted and refuses to derive a countdown from it.

5.3 -- the wait-versus-act comparison is a COMPARISON. Explicitly not an
expected-value-of-information integral: it does not price the information, it
puts the size of the edge next to the size of what is still to come and lets
the reader see whether one is inside the other. Calling it EVPI would be
exactly the kind of borrowed authority the Confidence rule exists to stop.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

CALENDAR_VERSION = "calendar-1"

#: |price_change_percent| at or beyond which FPL's own field says the change is
#: due. Not a threshold Gaffer chose -- the field is a percentage OF the change.
DUE_PCT = 100.0

#: ...and the point at which it is close enough to be worth a reader's
#: attention. A DECLARED POLICY CHOICE, like the minimum-actionable bars and
#: the evidence-quality floor: nothing fitted it, and it is labelled as a
#: choice wherever it is published.
#:
#: It earns its place. This evening Calafiori sits at 85.4% of the way to a
#: rise with 48 hours to the deadline, and Mbeumo at -79.3% of a fall. Neither
#: is "due", and a calendar that only spoke at 100 would have said nothing
#: about either -- while a reader deciding whether to act tonight or Friday is
#: asking precisely about them.
NEAR_PCT = 70.0

#: What this calendar cannot see. Published WITH it, every time, because the
#: dangerous reading of an empty calendar is "nothing is coming".
NOT_COVERED: tuple[dict[str, str], ...] = (
    {"what": "team news and press conferences",
     "why": "no source is ingested; this is the substrate for it, not the thing"},
    {"what": "the domestic cups (FA Cup, League Cup)",
     "why": "no source carries them for the current season; they are also what "
            "creates blanks and doubles, so this matters more from GW20"},
    {"what": "predicted lineups",
     "why": "no source is ingested"},
    {"what": "injury updates between now and the deadline",
     "why": "`news` is a snapshot of what FPL had when the pipeline ran, not a feed"},
    {"what": "WHEN a due price change will happen",
     "why": ("FPL's price system is rolling rather than nightly -- locks land "
             "across the day -- and `price_change_hourly_rate` does not "
             "reconcile with the published projections in any units, so no "
             "countdown can be derived from it honestly")},
)


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _players(conn: sqlite3.Connection, ids: list[int]) -> list[dict[str, Any]]:
    """Published price state for the players this decision actually touches."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT p.id, p.web_name, p.price, p.price_change_percent, "
            f"p.price_change_locked_until, p.price_change_projections, "
            f"t.short AS team "
            f"FROM players p LEFT JOIN teams t ON t.id = p.team_id "
            f"WHERE p.id IN ({marks})", ids).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        projections = []
        raw = r["price_change_projections"]
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    projections = parsed
            except ValueError:
                projections = []
        out.append({
            "id": r["id"], "name": r["web_name"], "team": r["team"],
            "price": (r["price"] or 0) / 10.0,
            "percent": r["price_change_percent"],
            "locked_until": r["price_change_locked_until"],
            "projections": projections,
        })
    return out


def _not_covered(euro_cov: dict[str, Any]) -> list[dict[str, str]]:
    """What this calendar cannot see, for THIS run.

    European fixtures are the entry that moves: covered when the source carries
    the season, invisible when it does not. Saying otherwise in either
    direction is the exact failure this module exists to prevent.
    """
    out = [dict(x) for x in NOT_COVERED]
    if not euro_cov.get("available"):
        missing = ", ".join(euro_cov.get("missing", {})) or "this season"
        out.insert(1, {
            "what": "European fixtures",
            "why": (f"no source carries {missing}, so a club playing in Europe "
                    f"looks the same here as one that is not. This is the gap "
                    f"that produced the GW7 Triple Captain error"),
        })
    return out


def build(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    deadline: datetime | None,
    window: str,
    squad_ids: list[int] | None = None,
    move_ids: list[int] | None = None,
    gameweek: int | None = None,
    season: str | None = None,
    squad_teams: Iterable[str] | None = None,
) -> dict[str, Any]:
    """The information calendar for this decision.

    Scoped to the players the reader owns or is being told to buy and sell.
    A calendar of all 651 players' price movements is a database dump, not a
    calendar: what makes an event an event is that it could change THIS answer.
    """
    squad_ids = list(squad_ids or [])
    move_ids = list(move_ids or [])
    relevant = sorted(set(squad_ids) | set(move_ids))
    events: list[dict[str, Any]] = []

    if deadline is not None:
        left = deadline - now
        events.append({
            "kind": "deadline",
            "at": deadline.isoformat(),
            "in_hours": round(left.total_seconds() / 3600, 1),
            "passed": left.total_seconds() < 0,
            "certainty": "published",
            "changes": "everything: after this the squad is locked for the gameweek",
        })

    for p in _players(conn, relevant):
        pct = p["percent"]
        in_move = p["id"] in move_ids
        lock = _parse(p["locked_until"])
        if lock is not None and lock > now:
            events.append({
                "kind": "price_locked",
                "at": p["locked_until"],
                "in_hours": round((lock - now).total_seconds() / 3600, 1),
                "player": {"id": p["id"], "name": p["name"], "team": p["team"]},
                "certainty": "published",
                "in_this_move": in_move,
                "changes": (f"{p['name']}'s price cannot move until then, so "
                            f"waiting costs nothing on his price"),
            })
        if (isinstance(pct, (int, float))
                and NEAR_PCT <= abs(pct) < DUE_PCT
                and (lock is None or lock <= now)):
            events.append({
                "kind": "price_change_near",
                "at": None,
                "player": {"id": p["id"], "name": p["name"], "team": p["team"],
                           "price": p["price"]},
                "direction": "rise" if pct > 0 else "fall",
                "percent": round(float(pct), 1),
                "projections": p["projections"],
                # The distinction that keeps this honest. FPL says how far
                # along it is; it does not say it will arrive.
                "certainty": (f"{abs(round(float(pct)))}% of the way, by FPL's "
                              f"own figure. NOT due, and no timing is implied"),
                "threshold_is_policy": (
                    f"reported from {NEAR_PCT:.0f}%, a declared presentation "
                    f"choice, not a fitted or published threshold"),
                "in_this_move": in_move,
                "owned": p["id"] in squad_ids,
                "changes": f"{p['name']} may move 0.1m before the deadline",
            })
        if isinstance(pct, (int, float)) and abs(pct) >= DUE_PCT:
            rising = pct > 0
            events.append({
                "kind": "price_change_due",
                # Deliberately no `at`. See NOT_COVERED: the timing is not
                # derivable, and inventing one would be the most useful-looking
                # lie in the artifact.
                "at": None,
                "player": {"id": p["id"], "name": p["name"], "team": p["team"],
                           "price": p["price"]},
                "direction": "rise" if rising else "fall",
                "percent": round(float(pct), 1),
                "projections": p["projections"],
                "certainty": "FPL says this change is due; it does not say when",
                "in_this_move": in_move,
                "owned": p["id"] in squad_ids,
                "changes": (
                    f"{p['name']} costs 0.1m more if you wait" if rising and in_move
                    else f"{p['name']} sells for 0.1m less if you wait"
                    if not rising and p["id"] in squad_ids
                    else f"{p['name']}'s price moves 0.1m"),
            })

    # Congestion the league creates for itself, and the breaks in between.
    # Derived, not sourced: it costs nothing and cannot go stale.
    shape = schedule_shape(gameweek_starts(conn), gameweek or 0)

    # ...and the fixtures Gaffer's own data cannot see. `coverage` travels with
    # them ALWAYS: an empty list from a season the source does not carry is not
    # the same claim as an empty list from a season it does.
    euro: list[dict[str, Any]] = []
    euro_cov: dict[str, Any] = {"available": False, "reason": "not attempted"}
    if season and squad_teams:
        euro, euro_cov = european_events(season, squad_teams, now)

    events.extend(euro)
    events.sort(key=lambda e: (e.get("in_hours") is None, e.get("in_hours") or 0))
    return {
        "calendar_version": CALENDAR_VERSION,
        "generated_for": now.isoformat(),
        "window": window,
        "scope": ("the reader's own squad and the players in this "
                  "recommendation, not the whole player list"),
        "events": events,
        "schedule_shape": shape,
        "european_coverage": euro_cov,
        # Both lists are DYNAMIC on purpose. European fixtures move between
        # them depending on whether the source carried this season, and a
        # static list would quietly claim coverage a run did not have --
        # which `test_the_things_it_cannot_see` caught the moment it was tried.
        "covers": [x for x in [
            "the deadline",
            "price changes that FPL says are due",
            f"price changes past {NEAR_PCT:.0f}% of the way, marked as not due",
            "price locks",
            "international breaks and midweek rounds, derived from the schedule",
            ("European fixtures for the clubs you own"
             if euro_cov.get("available") else None),
            "which schedule window this is",
        ] if x],
        "does_not_cover": _not_covered(euro_cov),
        "honesty": (
            "This is TIME and MONEY awareness. It is not football awareness. "
            "An empty calendar means Gaffer knows of nothing pending in the "
            "things it can see -- it does not mean no team news is coming."),
    }


# ---------------------------------------------------------------------------
# The schedule around the deadline: breaks, midweek rounds, Europe
# ---------------------------------------------------------------------------

#: A gap this long between deadlines is an international break.
#:
#: Thirteen days, because a normal round is seven and a break pushes it to
#: fourteen or twenty-one. Derived rather than sourced: FPL publishes every
#: deadline, so the breaks are already in data Gaffer ingests and need no
#: feed at all. Measured on 2026-27: GW6 (21 days), GW11 (14), GW31 (20).
BREAK_DAYS = 13

#: ...and a gap this short is a midweek round, which is congestion the league
#: creates for itself. 2026-27 has seven: GW13, 14, 18, 19, 20, 25 and 28.
MIDWEEK_DAYS = 4


def gameweek_starts(conn: sqlite3.Connection) -> list[tuple[int, datetime]]:
    """The first kick-off of each gameweek, from the fixtures already stored.

    Deadlines would do as well and Gaffer stores only the current one, so this
    reads first kick-offs instead. The gap between consecutive rounds is the
    same either way, which is the only thing the caller wants.
    """
    try:
        rows = conn.execute(
            "SELECT gw, MIN(kickoff) AS k FROM fixtures "
            "WHERE kickoff IS NOT NULL GROUP BY gw ORDER BY gw").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = _parse(r["k"])
        if d is not None:
            out.append((int(r["gw"]), d))
    return out


def schedule_shape(starts: list[tuple[int, datetime]], from_gw: int,
                   ahead: int = 4) -> list[dict[str, Any]]:
    """International breaks and midweek rounds, from the schedule itself.

    No external source. The whole signal is the gap between one round and the
    next, which Gaffer already has -- so this is the part of congestion
    awareness that costs nothing and cannot go stale.
    """
    rows = []
    prev = None
    for gw, d in starts:
        if prev is not None and from_gw <= gw <= from_gw + ahead:
            gap = (d - prev).days
            raw = d.isoformat()
            if gap >= BREAK_DAYS:
                rows.append({
                    "kind": "international_break",
                    "gameweek": gw, "at": raw, "gap_days": gap,
                    "certainty": "published: derived from FPL's own deadlines",
                    "changes": (f"a {gap}-day gap before GW{gw}. Players travel, "
                                f"and the ones who travel furthest come back "
                                f"latest"),
                })
            elif gap <= MIDWEEK_DAYS:
                rows.append({
                    "kind": "midweek_round",
                    "gameweek": gw, "at": raw, "gap_days": gap,
                    "certainty": "published: derived from FPL's own deadlines",
                    "changes": (f"only {gap} days after GW{gw - 1}. Rotation is "
                                f"more likely across the whole league"),
                })
        prev = d
    return rows


def european_events(
    season: str, teams: Iterable[str], now: datetime, window_days: int = 21,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Non-league fixtures for the reader's own clubs, and what was missing.

    Returns the coverage report ALWAYS. An empty list from a season the source
    does not carry is not the same claim as an empty list from a season it
    does, and a caller that cannot tell them apart will read "no European
    football" off a missing file.
    """
    from gaffer import competitions as COMP

    try:
        fixtures, cov = COMP.load_season(season)
    except Exception as exc:  # noqa: BLE001 -- any failure is the same answer
        return [], {"available": False,
                    "reason": f"{type(exc).__name__} reading the source"}
    rows = COMP.english_fixtures(fixtures)
    want = set(teams)
    out = []
    for r in rows:
        if r["team"] not in want:
            continue
        delta = (datetime(r["day"].year, r["day"].month, r["day"].day,
                          tzinfo=UTC) - now).total_seconds() / 86400
        if not (-3 <= delta <= window_days):
            continue
        out.append({
            "kind": "european_fixture",
            "at": r["day"].isoformat(),
            "in_days": round(delta, 1),
            "team": r["team"],
            "competition": r["competition"],
            "opponent": r["opponent"],
            "home": r["home"],
            "certainty": "published fixture list",
            "changes": (f"{r['team']} play {r['opponent']} in the "
                        f"{r['competition']}. Rotation risk, not a points "
                        f"projection -- Gaffer does not model this match"),
        })
    cov["available"] = bool(cov.get("found"))
    return sorted(out, key=lambda x: x["in_days"]), cov


# ---------------------------------------------------------------------------
# 5.3 -- wait versus act
# ---------------------------------------------------------------------------

#: Money, in points. There is no exchange rate between a tenth of a million and
#: an expected point, and pretending there is would be the whole failure this
#: task is meant to avoid. So the comparison is reported in BOTH units and
#: converted in neither.
def wait_vs_act(
    decision: dict[str, Any], cal: dict[str, Any],
) -> dict[str, Any]:
    """Put the size of the edge next to the size of what is still to come.

    A COMPARISON, not an expected-value-of-information calculation. It does not
    price the information and does not claim an optimal stopping time; it says
    what is pending, what that is worth in the units it is actually worth in,
    and whether the edge is small enough that the pending thing could matter.

    The football term is missing and is named as missing. On most weeks it is
    the larger one, and a verdict that ignored it while sounding decisive would
    be worse than no verdict.
    """
    cmp_ = decision.get("comparison") or {}
    edge = cmp_.get("delta")
    action = decision.get("action")
    events = cal.get("events") or []

    due = [e for e in events if e.get("kind") == "price_change_due"]
    near = [e for e in events if e.get("kind") == "price_change_near"]
    in_move = [e for e in due if e.get("in_this_move")]
    locked_in_move = [e for e in events
                      if e.get("kind") == "price_locked" and e.get("in_this_move")]

    # Money, in tenths of a million, signed from the reader's point of view.
    cost_of_waiting = 0
    detail: list[str] = []
    for e in in_move:
        if e.get("direction") == "rise":
            cost_of_waiting += 1
            detail.append(f"{e['player']['name']} is due to rise: waiting costs 0.1m")
        else:
            cost_of_waiting -= 1
            detail.append(f"{e['player']['name']} is due to fall: waiting saves 0.1m")
    for e in due:
        if e.get("owned") and not e.get("in_this_move") and e.get("direction") == "fall":
            cost_of_waiting += 1
            detail.append(
                f"{e['player']['name']} is due to fall and you own him: "
                f"his selling price drops 0.1m while you wait")

    # Who could move against the reader if he waits: someone he is buying who
    # might rise, or someone he owns who might fall.
    near_at_risk = {
        e["player"]["name"] for e in near
        if (e.get("in_this_move") and e.get("direction") == "rise")
        or (e.get("owned") and e.get("direction") == "fall")
    }

    deadline_ev = next((e for e in events if e.get("kind") == "deadline"), None)
    hours_left = deadline_ev.get("in_hours") if deadline_ev else None

    if action not in ("transfer", "too_close"):
        verdict = "nothing to wait on"
        reason = ("this week's answer is not a transfer, so there is no "
                  "purchase whose timing matters")
    elif cost_of_waiting > 0:
        verdict = "act"
        reason = (f"waiting costs {cost_of_waiting / 10:.1f}m of team value on "
                  f"changes FPL says are already due")
    elif cost_of_waiting < 0:
        verdict = "waiting is cheaper"
        reason = (f"waiting saves {abs(cost_of_waiting) / 10:.1f}m on changes "
                  f"FPL says are already due -- on money alone")
    elif locked_in_move:
        verdict = "no rush"
        reason = ("every player in this move is price-locked, so waiting "
                  "cannot cost you money")
    elif near_at_risk:
        # Deliberately weaker language than the `act` branch above. Nothing
        # here is due; something here is close. Saying "act" on a maybe would
        # be borrowing the certainty of the branch that measured one.
        names = ", ".join(sorted(near_at_risk))
        verdict = "money may be at stake"
        reason = (f"{names} is close to a price change by FPL's own figure but "
                  f"is not due, and no timing can be derived -- so this is a "
                  f"risk of waiting, not a cost of it")
    else:
        verdict = "no money either way"
        reason = "no price change in this move is due"

    return {
        "kind": "comparison",
        "not_an_evpi": (
            "This does not price the information and does not compute an "
            "optimal time to act. It puts the edge beside what is pending and "
            "reports both in their own units."),
        "verdict": verdict,
        "reason": reason,
        "hours_to_deadline": hours_left,
        "edge_points": edge,
        "edge_interval_type": cmp_.get("delta_ci95_interval_type"),
        "money_cost_of_waiting_m": round(cost_of_waiting / 10, 1),
        "detail": detail,
        "pending_price_changes": len(due),
        "near_price_changes": len(near),
        "near_at_risk_if_you_wait": sorted(near_at_risk),
        "no_exchange_rate": (
            "Money and points are reported separately and deliberately not "
            "combined: 0.1m of team value has no fixed worth in expected "
            "points, and inventing a rate to produce one number would be a "
            "made-up precision."),
        "missing_term": (
            "The football information -- team news, press conferences, "
            "predicted lineups -- is NOT in this comparison, and on most weeks "
            "it is the larger term. See the calendar's `does_not_cover`."),
    }
