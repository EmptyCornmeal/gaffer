"""Assemble the weekly decision and persist its snapshot (T-21).

The seam between the solver, the scenario engine, the strategy layer and the
front-end. Its whole job is to turn "here are fifteen rows and a plan object"
into one sentence a person can act on before a deadline, together with the
evidence for it and the single most likely way it is wrong.

The hold baseline is constructed here rather than taken from the solver, because
the solver never produces one: its "do nothing" case is implicit. Making it
explicit — same scenarios, same objective, same team state, same horizon, same
chip state — is what turns a recommendation into a comparison.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from gaffer import config, decision, snapshots
from gaffer.model import projection
from gaffer.model import scenarios as SC
from gaffer.solver import objective as OBJ
from gaffer.solver import optimize

WEEKLY_VERSION = "weekly-1.0"

#: A legal XI must satisfy these; used when re-deriving the hold XI so that the
#: baseline is a squad you could actually field, not the 11 highest projections.
_MIN = config.FORMATION_MIN


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    v = r["value"] if r else None
    return None if v in (None, "", "None") else v


def _int_meta(conn: sqlite3.Connection, key: str) -> int | None:
    v = _meta(conn, key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def held_squad(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The user's last readable picks. None means unknown — never "owns nothing"."""
    rows = conn.execute(
        "SELECT gw, player_id, is_captain, is_vice, multiplier FROM my_squad "
        "WHERE gw = (SELECT MAX(gw) FROM my_squad)"
    ).fetchall()
    if not rows:
        return None
    return {
        "source_event": int(rows[0]["gw"]),
        "squad": [r["player_id"] for r in rows],
        "starting": [r["player_id"] for r in rows if (r["multiplier"] or 0) > 0],
        "bench": [r["player_id"] for r in rows if (r["multiplier"] or 0) == 0],
        "captain": next((r["player_id"] for r in rows if r["is_captain"]), None),
        "vice": next((r["player_id"] for r in rows if r["is_vice"]), None),
    }


def hold_baseline(
    conn: sqlite3.Connection, squad: list[int], from_gw: int, horizon: int,
) -> dict[str, Any]:
    """The best *legal* XI and captain from the squad you already own.

    Deliberately re-derived rather than reusing last week's lineup: holding your
    transfer does not mean holding a stale bench order, and comparing a move
    against a badly-set XI would flatter every move.
    """
    players = optimize.load_players(conn, from_gw, horizon)
    owned = [p for p in squad if p in players]
    if len(owned) < 11:
        return {"starting": [], "bench": [], "captain": None, "vice": None,
                "horizon_value": 0.0, "legal": False}
    xi = optimize._pick_xi(players, owned)
    bench = sorted(
        (i for i in owned if i not in xi),
        # G-G -- this gameweek's xP, not the decayed horizon value: the bench
        # order is an autosub queue for the match about to be played.
        key=lambda i: (players[i].position != "GKP", -players[i].next_gw_points))
    ranked = sorted(xi, key=lambda i: -players[i].next_gw_points)
    captain = ranked[0] if ranked else None
    vice = ranked[1] if len(ranked) > 1 else captain
    return {
        "starting": xi, "bench": bench, "captain": captain, "vice": vice,
        "horizon_value": round(sum(players[i].value for i in xi), 3),
        "legal": True,
    }


def move_horizon_value(
    conn: sqlite3.Connection, starting: list[int], from_gw: int, horizon: int,
) -> float:
    players = optimize.load_players(conn, from_gw, horizon)
    return round(sum(players[i].value for i in starting if i in players), 3)


def build(
    conn: sqlite3.Connection, *, sol: Any, from_gw: int, horizon: int,
    scen: SC.ScenarioSet | None, settings: config.Settings,
    strategy: dict[str, Any] | None = None,
    params: OBJ.ObjectiveParams | None = None,
) -> decision.Decision:
    """Produce the week's single decision."""
    params = params or OBJ.DEFAULT
    held = held_squad(conn)

    if held is None or not held["starting"]:
        return decision.Decision(
            action=decision.ACTION_UNAVAILABLE,
            headline="We do not know your squad yet",
            reason=(_meta(conn, "squad_status_reason")
                    or "no squad could be read from the public API"),
            captain=sol.captain or None, vice=sol.vice or None,
            starting=list(sol.starting), bench=list(sol.bench),
            confidence="unknown",
            biggest_risk="Everything below describes a suggested squad, not "
                         "yours — no transfer advice is possible until FPL "
                         "publishes your picks.",
            assumptions=["The squad shown is Gaffer's recommended build, not "
                         "your team."],
        )

    hold = hold_baseline(conn, held["squad"], from_gw, horizon)

    # G2 -- the unknown-hold blowout. `hold_baseline` returns `legal: False`
    # with an empty XI and `horizon_value: 0.0` when fewer than eleven of the
    # owned players could be projected. Nothing checked the flag, so that 0.0
    # went into `decision.compare` as if it were a real score, and *any* move
    # beat it by the whole value of a squad. The output was not a wrong number,
    # it was a confident recommendation to transfer, invented out of a missing
    # projection -- and `decision.py` waives the probability gate once the delta
    # is decisive enough, so the usual safety net let it through too.
    #
    # A hold we cannot price is not a hold worth zero. Say so.
    if not hold.get("legal", True):
        missing = 11 - len(hold.get("starting") or [])
        return decision.Decision(
            action=decision.ACTION_UNAVAILABLE,
            headline="We cannot price holding your squad this week",
            reason=(
                f"{missing} of your eleven could not be projected, so there is "
                "no legal XI to hold and nothing to measure a transfer against"
            ),
            captain=sol.captain or None, vice=sol.vice or None,
            starting=list(sol.starting), bench=list(sol.bench),
            confidence="unknown",
            biggest_risk="Comparing a move against a squad we cannot price "
                         "would make every move look decisive, whatever it is.",
            assumptions=["The squad shown is Gaffer's recommended build. No "
                         "transfer is being recommended, because holding could "
                         "not be scored."],
        )

    move_xi = list(sol.starting)
    hit_cost = int(sol.hits or 0) * params.hit_cost

    cmp_ = decision.compare(
        scen,
        move_xi=move_xi, move_captain=sol.captain or None,
        hold_xi=hold["starting"], hold_captain=hold["captain"],
        hit_cost=hit_cost,
        move_horizon=move_horizon_value(conn, move_xi, from_gw, horizon),
        hold_horizon=hold["horizon_value"],
    )

    ft = _int_meta(conn, "free_transfers") or settings.free_transfers
    bank = _int_meta(conn, "bank")
    exe = decision.executability(
        conn, list(sol.transfers_in), list(sol.transfers_out), ft, bank)

    horizon_driven = cmp_.horizon_delta > cmp_.delta + 0.5
    action, reason = decision.classify(cmp_)

    # A move nobody can pay for is not a recommendation, whatever it projects.
    if action == decision.ACTION_TRANSFER and sol.transfers_in and not exe.affordable:
        action = decision.ACTION_UNAVAILABLE
        reason = f"the recommended move is not executable: {exe.reason}"

    # The solver proposing nothing is a roll, not a "transfer worth 0".
    if action == decision.ACTION_TRANSFER and not sol.transfers_in:
        action = decision.ACTION_ROLL
        reason = ("no transfer beats holding, so the free transfer is worth more "
                  "kept than spent")

    headline = _headline(conn, action, sol, cmp_, exe)
    league_note = _league_note(strategy)
    chip = (strategy or {}).get("chips")

    return decision.Decision(
        action=action, headline=headline, reason=reason,
        transfers_out=list(sol.transfers_out), transfers_in=list(sol.transfers_in),
        captain=sol.captain or None, vice=sol.vice or None,
        starting=move_xi if action == decision.ACTION_TRANSFER else hold["starting"],
        bench=list(sol.bench) if action == decision.ACTION_TRANSFER else hold["bench"],
        comparison=cmp_, executability=exe, chip=chip, league_note=league_note,
        confidence=decision.confidence_band(cmp_),
        biggest_risk=decision.biggest_risk(
            conn, list(sol.transfers_in), sol.captain or None, horizon_driven),
        assumptions=_assumptions(conn, scen, horizon, hold, held),
    )


def _name(conn: sqlite3.Connection, pid: int | None) -> str:
    if pid is None:
        return "?"
    r = conn.execute("SELECT web_name FROM players WHERE id=?", (pid,)).fetchone()
    return r["web_name"] if r else str(pid)


def _headline(
    conn: sqlite3.Connection, action: str, sol: Any,
    cmp_: decision.Comparison, exe: decision.Executability,
) -> str:
    if action == decision.ACTION_UNAVAILABLE:
        return "No executable recommendation this week"
    if action == decision.ACTION_ROLL:
        return f"Roll your transfer — captain {_name(conn, sol.captain)}"
    if action == decision.ACTION_TOO_CLOSE:
        return f"Too close to call — captain {_name(conn, sol.captain)}"
    outs = ", ".join(_name(conn, p) for p in sol.transfers_out)
    ins = ", ".join(_name(conn, p) for p in sol.transfers_in)
    hit = f" (-{exe.paid_transfers * config.HIT_COST})" if exe.paid_transfers else ""
    return f"{outs} → {ins}{hit} — captain {_name(conn, sol.captain)}"


def _league_note(strategy: dict[str, Any] | None) -> str:
    if not strategy:
        return ""
    diverging = [lg for lg in strategy.get("leagues") or []
                 if lg.get("differs_from_neutral")]
    if not diverging:
        return ""
    first = diverging[0]
    return (f"{first.get('name', 'a league')} argues for a "
            f"{(first.get('posture') or {}).get('stance', 'different')} posture: "
            f"{first.get('difference_reason', '')}").strip()


def _assumptions(
    conn: sqlite3.Connection, scen: Any, horizon: int,
    hold: dict[str, Any], held: dict[str, Any],
) -> list[str]:
    out = [
        f"Move and hold are scored in the same {getattr(scen, 'n_sims', 0)} "
        "fixture scenarios, with the same projections, objective and team state.",
        f"The hold baseline re-optimises your XI and captain from the squad you "
        f"already own (source: GW{held['source_event']}), so it is the best "
        "version of doing nothing.",
    ]
    conf = _meta(conn, "selling_price_confidence")
    if conf and conf != "exact":
        out.append(
            f"Selling prices are {conf}, so the affordable set may be wrong.")
    if horizon > 1:
        out.append(
            "Horizon value uses gameweeks 2-6, where Gaffer's mean projections "
            "are materially weaker than its one-week ones.")
    out.append(
        f"The bar for calling a move an action ("
        f"{decision.MIN_ACTIONABLE_POINTS} points and "
        f"{int(decision.MIN_ACTIONABLE_PROBABILITY * 100)}% chance of beating "
        f"the hold) is a conservative policy choice, not a fitted one. It "
        f"becomes fittable once ~6 gameweeks of decision snapshots and reviews "
        f"exist.")
    return out


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def snapshot_payload(
    conn: sqlite3.Connection, dec: decision.Decision, *, from_gw: int,
    horizon: int, scen: Any, settings: config.Settings,
    strategy: dict[str, Any] | None, generated_at: str,
    params: OBJ.ObjectiveParams | None = None,
) -> dict[str, Any]:
    """The versioned record of what Gaffer advised, and on what basis."""
    params = params or OBJ.DEFAULT
    held = held_squad(conn)
    leagues = [
        {"league_id": lg.get("league_id"), "name": lg.get("name"),
         "target_position": lg.get("target_position"),
         "p_first": (lg.get("placing") or {}).get("p_first"),
         "p_target": (lg.get("placing") or {}).get("p_target"),
         "available": (lg.get("placing") or {}).get("available"),
         "stance": (lg.get("posture") or {}).get("stance")}
        for lg in (strategy or {}).get("leagues") or []
    ]
    return {
        "weekly_version": WEEKLY_VERSION,
        "decision_version": decision.DECISION_VERSION,
        "generated_at": generated_at,
        "gameweek": from_gw,
        "horizon": horizon,
        "squad_state": {
            "known": held is not None,
            "status": _meta(conn, "squad_status"),
            "source_event": (held or {}).get("source_event"),
            "squad": (held or {}).get("squad", []),
            "captain": (held or {}).get("captain"),
            "vice": (held or {}).get("vice"),
        },
        "decision": dec.as_dict(),
        "versions": {
            "model_version": projection.MODEL_VERSION,
            "objective_version": OBJ.OBJECTIVE_VERSION,
            "sim_version": getattr(scen, "meta", {}).get("sim_version",
                                                         SC.SIM_VERSION),
            "n_sims": int(getattr(scen, "n_sims", 0) or 0),
            "seed": int(getattr(scen, "seed", 0) or 0),
            "objective_params": params.as_dict(),
        },
        "chip": (strategy or {}).get("chips"),
        "leagues": leagues,
        "freshness": {
            "generated_at": generated_at,
            "squad_retrieved_at": _meta(conn, "squad_retrieved_at"),
            "bank_source": _meta(conn, "bank_source"),
            "free_transfers_source": _meta(conn, "free_transfers_source"),
            "selling_price_confidence": _meta(conn, "selling_price_confidence"),
        },
        "overrides": {
            "free_transfers": settings.sources.get("free_transfers"),
            "bank": settings.sources.get("bank"),
            "purchase_prices": settings.sources.get("purchase_prices"),
            "manual_purchase_price_count": len(settings.purchase_prices),
        },
    }


def persist(
    conn: sqlite3.Connection, payload: dict[str, Any], *, entry_id: int | None,
    target_event: int, deadline: str | None, now: datetime | None = None,
) -> dict[str, Any]:
    """Store the snapshot if — and only if — the deadline has not passed."""
    if entry_id is None:
        return {"outcome": "no_entry_id", "as_of": None}
    if not deadline:
        return {"outcome": "no_deadline", "as_of": None}
    now = now or datetime.now(UTC)
    snap, outcome = snapshots.record(
        conn, entry_id=entry_id, target_event=target_event, deadline=deadline,
        payload=payload, now=now)
    return {
        "outcome": outcome,
        "as_of": snap.as_of if snap else None,
        "content_hash": snap.content_hash if snap else None,
    }
