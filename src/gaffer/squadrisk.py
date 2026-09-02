"""Structural risk in a squad: a dead bench, a concentrated gameweek, and both
of them seen far enough ahead that a free transfer can still fix them.

**Why these three live together.** They are one failure mode wearing three
faces: a squad can be made of individually good picks and still be badly
built. Every player projecting well says nothing about whether eight of them
are in two fixtures, or whether four bench slots contain two players who cannot
score. Neither fact is visible anywhere in a points projection, and neither is
a modelling problem -- they are structural, deterministic, and knowable weeks
in advance.

**Why this is not a model.** Nothing here is fitted and nothing here predicts.
Every number is a count, a share, or a fixture identity read off the schedule.
That is deliberate: the 2026-09-02 congestion work established that the useful
form of this class of information is DESCRIPTION, not a correction applied to
somebody's start probability. See `backtest.CONGESTION_REFUSED`.

**The rule the concentration metric is written against.** "Never own opposing
players" is a bad rule and this does not implement it. Owning both sides of a
match is often correct -- two good players are two good players -- and the
sensible reading is not a prohibition but a description of the SHAPE of the
week: which outcomes are correlated, which cancel, and how much of the squad is
riding on one referee's afternoon.
"""
from __future__ import annotations

import sqlite3
from typing import Any

SQUADRISK_VERSION = "squadrisk-1"

# ---------------------------------------------------------------------------
# Bench robustness
# ---------------------------------------------------------------------------

#: Start probability below which a bench player is treated as having no
#: realistic route into the team. A DECLARED POLICY CHOICE, labelled as one
#: wherever it is published -- nothing fitted it.
#:
#: It sits at 0.10 because the question is not "might he start" but "is there a
#: route to minutes at all". A player under 10% is not a rotation risk; he is
#: cover for an injury that has not happened.
DEAD_START_P = 0.10

#: ...and the minutes floor that separates "has not played" from "does not
#: play". Two full matches is a season's worth of evidence by GW3 and no
#: evidence at all by GW30, which is why `matches_available` scales it.
DEAD_MINUTES_PER_MATCH = 15.0

BENCH_LIVE = "live"
BENCH_THIN = "thin"
BENCH_DEAD = "dead"

BENCH_MEANING = {
    BENCH_LIVE: "plays, or plausibly could; a real autosub and a real Bench "
                "Boost contributor",
    BENCH_THIN: "cheap and marginal -- some route to minutes, but not a "
                "player you would choose to need",
    BENCH_DEAD: "no realistic route to minutes. Cannot autosub in usefully "
                "and cannot contribute to a Bench Boost",
}


def _classify_bench_player(
    p_start: float | None, minutes: int, matches: int, status: str | None,
    position: str, first_choice_gk_minutes: int | None = None,
) -> tuple[str, str]:
    """One bench slot, and the sentence explaining it."""
    if status and status in ("i", "s", "u"):
        return BENCH_DEAD, {"i": "injured", "s": "suspended",
                            "u": "not registered / has left"}[status]

    # A backup goalkeeper is a special case and pretending otherwise flatters
    # every squad in the game. He is not a rotation risk: he is waiting for an
    # injury, and until it happens his expected contribution is zero.
    if position == "GKP" and first_choice_gk_minutes and minutes == 0:
        return BENCH_DEAD, ("backup goalkeeper behind an ever-present -- his "
                            "route to minutes is an injury, not a selection")

    per_match = minutes / matches if matches else 0.0
    if minutes == 0 and matches >= 2:
        return BENCH_DEAD, f"no minutes in {matches} matches"
    if p_start is not None and p_start < DEAD_START_P and per_match < DEAD_MINUTES_PER_MATCH:
        return BENCH_DEAD, (f"{p_start:.0%} start probability and "
                            f"{per_match:.0f} minutes a match")
    if p_start is not None and p_start < 0.35:
        return BENCH_THIN, f"{p_start:.0%} start probability"
    return BENCH_LIVE, "plays"


def bench_robustness(
    conn: sqlite3.Connection, bench: list[int], gw: int,
) -> dict[str, Any]:
    """How much of this bench can actually score, and what that costs.

    Two consequences, kept separate because they bite at different times:

    * **autosub cover** -- a dead slot cannot replace a starter who does not
      play, so the safety net is thinner than four names suggest;
    * **Bench Boost readiness** -- the chip pays the bench's actual return, and
      a dead slot pays zero however cheap it was.
    """
    if not bench:
        return {"available": False, "reason": "no bench to describe"}
    marks = ",".join("?" * len(bench))
    try:
        rows = conn.execute(
            f"SELECT p.id, p.web_name, p.position, p.status, p.minutes, "
            f"p.price, pr.p_start "
            f"FROM players p "
            f"LEFT JOIN projections pr ON pr.player_id = p.id AND pr.gw = ? "
            f"WHERE p.id IN ({marks})", (gw, *bench)).fetchall()
    except sqlite3.Error:
        return {"available": False, "reason": "bench could not be read"}
    if not rows:
        return {"available": False, "reason": "no bench rows found"}

    matches = _matches_played(conn)
    gk_minutes = _first_choice_gk_minutes(conn, bench)
    order = {pid: i for i, pid in enumerate(bench)}
    out: list[dict[str, Any]] = []
    for r in rows:
        state, why = _classify_bench_player(
            r["p_start"], r["minutes"] or 0, matches, r["status"],
            r["position"], gk_minutes)
        out.append({
            "id": r["id"], "name": r["web_name"], "pos": r["position"],
            "price": (r["price"] or 0) / 10.0,
            "state": state, "why": why,
            "bench_order": order.get(r["id"]),
        })
    out.sort(key=lambda x: (x["bench_order"] is None, x["bench_order"]))
    dead = [p for p in out if p["state"] == BENCH_DEAD]
    live = [p for p in out if p["state"] == BENCH_LIVE]
    outfield_dead = [p for p in dead if p["pos"] != "GKP"]
    return {
        "available": True,
        "players": out,
        "dead": len(dead),
        "live": len(live),
        "of": len(out),
        "meanings": dict(BENCH_MEANING),
        "threshold_is_policy": (
            f"a slot is dead below a {DEAD_START_P:.0%} start probability and "
            f"{DEAD_MINUTES_PER_MATCH:.0f} minutes a match. A DECLARED POLICY "
            f"CHOICE, not a fitted threshold."),
        "autosub_cover": {
            "usable_outfield_substitutes": len(
                [p for p in out if p["pos"] != "GKP" and p["state"] != BENCH_DEAD]),
            "means": ("how many bench players could actually replace a starter "
                      "who does not play. A dead slot is not cover."),
        },
        "bench_boost_ready": len(dead) == 0,
        "verdict": _bench_verdict(dead, outfield_dead, out),
    }


def _bench_verdict(dead, outfield_dead, all_bench) -> str:
    if not dead:
        return "every bench slot can score. Bench Boost is playable on merit."
    names = ", ".join(p["name"] for p in dead)
    return (
        f"{len(dead)} of {len(all_bench)} bench slots cannot score: {names}. "
        f"That is {len(outfield_dead)} fewer outfield autosub than the bench "
        f"appears to offer, and a Bench Boost would pay for "
        f"{len(all_bench) - len(dead)} players rather than {len(all_bench)}.")


def _matches_played(conn: sqlite3.Connection) -> int:
    try:
        r = conn.execute(
            "SELECT MAX(gw) AS g FROM player_gw WHERE minutes IS NOT NULL"
        ).fetchone()
        return int(r["g"] or 0)
    except sqlite3.Error:
        return 0


def _first_choice_gk_minutes(
    conn: sqlite3.Connection, bench: list[int],
) -> int | None:
    """Minutes of the busiest keeper at the club of any benched keeper."""
    try:
        rows = conn.execute(
            "SELECT team_id FROM players WHERE position='GKP' AND id IN "
            f"({','.join('?' * len(bench))})", bench).fetchall()
        if not rows:
            return None
        team = rows[0]["team_id"]
        r = conn.execute(
            "SELECT MAX(minutes) AS m FROM players "
            "WHERE position='GKP' AND team_id=?", (team,)).fetchone()
        return int(r["m"] or 0)
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# Fixture concentration
# ---------------------------------------------------------------------------

#: Share of the squad in a single fixture above which the week is called
#: concentrated. A DECLARED PRESENTATION CHOICE. It is set where it is because
#: a third of a squad in one match means one referee, one red card or one
#: postponement moves a third of the week.
CONCENTRATED_SHARE = 0.30

#: ...and the same question asked of the two largest fixtures together.
#:
#: One threshold on the single biggest fixture is not enough, and the live
#: squad on 2026-09-02 is why: at GW4 no single fixture held more than 27% of
#: it, so a single-fixture rule reported a clean week -- while EIGHT of fifteen
#: players sat in two matches. Also a declared presentation choice.
CONCENTRATED_TOP_TWO = 0.45


def fixture_concentration(
    conn: sqlite3.Connection, squad: list[int], gw: int,
    starting: list[int] | None = None,
) -> dict[str, Any]:
    """How much of the squad rides on how few matches, and where it collides.

    NOT a prohibition on owning both sides of a fixture. Two good players are
    two good players. What this describes is the SHAPE of the week: which
    outcomes move together, which cancel, and how much of the squad depends on
    one afternoon.
    """
    if not squad:
        return {"available": False, "reason": "no squad to describe"}
    marks = ",".join("?" * len(squad))
    try:
        rows = conn.execute(
            f"SELECT p.id, p.web_name, p.position, p.team_id, t.short AS team "
            f"FROM players p LEFT JOIN teams t ON t.id = p.team_id "
            f"WHERE p.id IN ({marks})", squad).fetchall()
        fixtures = conn.execute(
            "SELECT team_h, team_a FROM fixtures WHERE gw = ?", (gw,)).fetchall()
    except sqlite3.Error:
        return {"available": False, "reason": "squad or fixtures unreadable"}
    if not rows or not fixtures:
        return {"available": False, "reason": f"no fixtures stored for GW{gw}"}

    side: dict[int, tuple[int, int]] = {}
    for f in fixtures:
        side[f["team_h"]] = (f["team_h"], f["team_a"])
        side[f["team_a"]] = (f["team_h"], f["team_a"])

    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    unplaced: list[str] = []
    start_set = set(starting or [])
    for r in rows:
        key = side.get(r["team_id"])
        if key is None:
            unplaced.append(r["web_name"])
            continue
        buckets.setdefault(key, []).append({
            "id": r["id"], "name": r["web_name"], "pos": r["position"],
            "team": r["team"], "team_id": r["team_id"],
            "starting": r["id"] in start_set if starting else None,
        })

    groups = []
    for (h, a), players in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        teams = {p["team_id"] for p in players}
        groups.append({
            "fixture": _fixture_label(conn, h, a),
            "players": [p["name"] for p in players],
            "n": len(players),
            "share_of_squad": round(len(players) / len(squad), 3),
            "both_sides": len(teams) > 1,
            "opposed_pairs": _opposed(players) if len(teams) > 1 else [],
        })

    biggest = groups[0] if groups else None
    top_two = sum(g["n"] for g in groups[:2])
    return {
        "available": True,
        "gameweek": gw,
        "fixtures_covering_the_squad": len(groups),
        "largest": biggest,
        "top_two_share": round(top_two / len(squad), 3) if squad else 0.0,
        "groups": groups[:5],
        "unplaced": unplaced,
        "concentrated": bool(
            (biggest and biggest["share_of_squad"] >= CONCENTRATED_SHARE)
            or (squad and top_two / len(squad) >= CONCENTRATED_TOP_TWO)),
        "threshold_is_policy": (
            f"concentrated at {CONCENTRATED_SHARE:.0%} of the squad in one "
            f"fixture OR {CONCENTRATED_TOP_TWO:.0%} across the largest two. "
            f"Both are DECLARED PRESENTATION CHOICES, not fitted bars. The "
            f"second exists because a single-fixture rule called a week clean "
            f"while eight of fifteen players sat in two matches."),
        "reading": _concentration_reading(groups, len(squad), top_two),
        "not_a_rule": (
            "this is not 'do not own opposing players'. Owning both sides is "
            "often right; what matters is knowing which of your outcomes "
            "cancel and how much of the week rides on one match."),
    }


def _fixture_label(conn: sqlite3.Connection, h: int, a: int) -> str:
    try:
        rows = {r["id"]: r["short"] for r in
                conn.execute("SELECT id, short FROM teams").fetchall()}
    except sqlite3.Error:
        rows = {}
    return f"{rows.get(h, h)} v {rows.get(a, a)}"


def _opposed(players: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Pairs whose returns directly cancel: a clean sheet against a goal.

    A defender or keeper on one side and an attacker on the other cannot both
    have a good afternoon. That is not a reason to avoid it -- it is variance
    reduction, and sometimes exactly what a leader wants -- but it must be
    visible, because it silently caps the ceiling of the week.
    """
    out = []
    for a in players:
        for b in players:
            if a["team_id"] >= b["team_id"]:
                continue
            back = {"GKP", "DEF"}
            fwd = {"MID", "FWD"}
            if (a["pos"] in back and b["pos"] in fwd) or \
               (b["pos"] in back and a["pos"] in fwd):
                keeper = a if a["pos"] in back else b
                attacker = b if a["pos"] in back else a
                out.append({
                    "clean_sheet_side": keeper["name"],
                    "attacking_side": attacker["name"],
                    "means": (f"{attacker['name']} scoring is a goal against "
                              f"{keeper['name']}"),
                })
    return out


def _concentration_reading(groups, squad_n, top_two) -> str:
    if not groups:
        return "no fixture could be matched to this squad"
    g = groups[0]
    parts = [
        f"{g['n']} of {squad_n} players are in {g['fixture']}"
        f" ({g['share_of_squad']:.0%} of the squad)."]
    if len(groups) > 1:
        parts.append(f"{top_two} of {squad_n} sit in two fixtures.")
    opposed = [p for grp in groups for p in grp["opposed_pairs"]]
    if opposed:
        pair = opposed[0]
        parts.append(
            f"{len(opposed)} directly opposed pair(s) -- {pair['means']} -- "
            f"which cancels rather than compounds.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The planning horizon
# ---------------------------------------------------------------------------

#: How many gameweeks ahead structural problems are looked for.
#:
#: Three, because that is the number a free transfer can still solve. A warning
#: that arrives on deadline day is a complaint; the same warning three weeks out
#: is a plan. Nothing here is a projection -- the fixture list is published and
#: a dead bench slot is dead today.
HORIZON_GWS = 3


def horizon_warnings(
    conn: sqlite3.Connection, squad: list[int], bench: list[int],
    from_gw: int, horizon: int = HORIZON_GWS,
) -> dict[str, Any]:
    """Structural problems far enough ahead that a free transfer can fix them.

    Deliberately NOT a deadline-week warning. Everything here is knowable now:
    the fixture list is published weeks out, and a bench slot with no route to
    minutes does not acquire one by Saturday.
    """
    out: list[dict[str, Any]] = []
    for gw in range(from_gw, from_gw + horizon):
        conc = fixture_concentration(conn, squad, gw)
        if not conc.get("available"):
            continue
        if conc["concentrated"]:
            out.append({
                "gameweek": gw,
                "kind": "fixture_concentration",
                "gameweeks_away": gw - from_gw,
                "detail": conc["reading"],
                "largest": conc["largest"]["fixture"] if conc["largest"] else None,
                "fixable_by": _fixable_by(gw, from_gw),
            })

    bench_state = bench_robustness(conn, bench, from_gw)
    if bench_state.get("available") and bench_state["dead"]:
        out.append({
            "gameweek": from_gw,
            "kind": "dead_bench",
            "gameweeks_away": 0,
            "detail": bench_state["verdict"],
            "fixable_by": ("a transfer in any gameweek -- this does not resolve "
                           "itself, and it costs a little every week until it is "
                           "fixed"),
        })

    return {
        "available": True,
        "version": SQUADRISK_VERSION,
        "from_gameweek": from_gw,
        "horizon": horizon,
        "warnings": out,
        "clear": not out,
        "why_early": (
            f"looked for {horizon} gameweeks ahead because that is how far a "
            f"free transfer can still reach. A structural problem announced on "
            f"deadline day is a complaint; the same problem three weeks out is "
            f"a plan."),
        "not_a_projection": (
            "every warning here is read off the published fixture list and the "
            "squad as it stands. Nothing is forecast."),
    }


def _fixable_by(gw: int, from_gw: int) -> str:
    n = gw - from_gw
    if n == 0:
        return "this week's transfer, if you have not used it"
    if n == 1:
        return "one free transfer, if you roll this week's"
    return f"{n} free transfers, which you have if you roll from now"

