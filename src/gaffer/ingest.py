"""Ingest the FPL API into SQLite.

Single free source of truth. The FPL bootstrap already carries Opta expected
stats (xG/xA/xGI and xG-conceded per 90) plus defensive-contribution per 90, so
no scraping is required. Pre-season, the per-90 rate fields mirror last season —
exactly the baseline the GW1 projection needs.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx

from gaffer import config
from gaffer.fpl.client import FplClient
from gaffer.store import db

_POSITION = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _set_piece_notes(e: dict[str, Any]) -> str:
    bits = []
    if (e.get("penalties_order") or 99) <= 2:
        bits.append(f"pens #{e['penalties_order']}")
    if (e.get("direct_freekicks_order") or 99) <= 2:
        bits.append(f"FK #{e['direct_freekicks_order']}")
    if (e.get("corners_and_indirect_freekicks_order") or 99) <= 2:
        bits.append(f"corners #{e['corners_and_indirect_freekicks_order']}")
    return ", ".join(bits)


def ingest_teams(conn: sqlite3.Connection, bootstrap: dict[str, Any]) -> int:
    rows = []
    for t in bootstrap["teams"]:
        rows.append(
            {
                "id": t["id"],
                "code": t.get("code"),
                "name": t["name"],
                "short": t["short_name"],
                "strength_att_home": t.get("strength_attack_home"),
                "strength_att_away": t.get("strength_attack_away"),
                "strength_def_home": t.get("strength_defence_home"),
                "strength_def_away": t.get("strength_defence_away"),
                "strength_overall": (
                    (t.get("strength_overall_home", 0) + t.get("strength_overall_away", 0)) // 2
                ),
            }
        )
    return db.upsert(conn, "teams", rows, ["id"])


def ingest_players(conn: sqlite3.Connection, bootstrap: dict[str, Any]) -> int:
    # Preserve enriched DEFCON: pre-season the bootstrap field is 0, so keep any
    # value we computed from history; let a non-zero (in-season) value override.
    existing_defcon = {
        r["id"]: r["defcon_per_90"]
        for r in conn.execute("SELECT id, defcon_per_90 FROM players")
    }
    rows = []
    for e in bootstrap["elements"]:
        rows.append(
            {
                "id": e["id"],
                "code": e.get("code"),
                "web_name": e["web_name"],
                "first_name": e.get("first_name"),
                "second_name": e.get("second_name"),
                "team_id": e["team"],
                "position": _POSITION[e["element_type"]],
                "price": e["now_cost"],
                "status": e.get("status"),
                "chance_playing": e.get("chance_of_playing_next_round"),
                "selected_by_pct": _f(e.get("selected_by_percent")),
                "transfers_in_event": e.get("transfers_in_event", 0),
                "transfers_out_event": e.get("transfers_out_event", 0),
                "cost_change_event": e.get("cost_change_event", 0),
                "minutes": e.get("minutes", 0),
                "starts": e.get("starts", 0),
                "form": _f(e.get("form")),
                "points_per_game": _f(e.get("points_per_game")),
                "ep_next": _f(e.get("ep_next")),
                "xg_per_90": _f(e.get("expected_goals_per_90")),
                "xa_per_90": _f(e.get("expected_assists_per_90")),
                "xgi_per_90": _f(e.get("expected_goal_involvements_per_90")),
                "xgc_per_90": _f(e.get("expected_goals_conceded_per_90")),
                "defcon_per_90": (
                    _f(e.get("defensive_contribution_per_90"))
                    or existing_defcon.get(e["id"], 0.0)
                ),
                "news": e.get("news") or "",
                "set_piece_notes": _set_piece_notes(e),
            }
        )
    return db.upsert(conn, "players", rows, ["id"])


def ingest_fixtures(conn: sqlite3.Connection, fixtures: list[dict[str, Any]]) -> int:
    rows = []
    for fx in fixtures:
        rows.append(
            {
                "id": fx["id"],
                "gw": fx.get("event"),
                "team_h": fx["team_h"],
                "team_a": fx["team_a"],
                "kickoff": fx.get("kickoff_time"),
                "fdr_h": fx.get("team_h_difficulty"),
                "fdr_a": fx.get("team_a_difficulty"),
                "finished": 1 if fx.get("finished") else 0,
            }
        )
    return db.upsert(conn, "fixtures", rows, ["id"])


def ingest_my_squad(
    conn: sqlite3.Connection, client: FplClient, entry_id: int, gw: int
) -> int:
    """Load the user's picks for the given GW. Returns 0 (and records why) if the
    picks aren't available yet (e.g. pre-season before the GW1 deadline)."""
    try:
        picks = client.entry_picks(entry_id, gw)
    except httpx.HTTPStatusError as exc:
        db.set_meta(conn, "squad_status", f"unavailable ({exc.response.status_code})")
        return 0

    conn.execute("DELETE FROM my_squad WHERE gw=?", (gw,))
    rows = []
    for p in picks.get("picks", []):
        rows.append(
            {
                "gw": gw,
                "player_id": p["element"],
                "is_captain": 1 if p.get("is_captain") else 0,
                "is_vice": 1 if p.get("is_vice_captain") else 0,
                "multiplier": p.get("multiplier", 1),
                "purchase_price": None,
                "selling_price": None,
            }
        )
    n = db.upsert(conn, "my_squad", rows, ["gw", "player_id"])
    eh = picks.get("entry_history", {})
    db.set_meta(conn, "bank", eh.get("bank", 0))
    db.set_meta(conn, "team_value", eh.get("value", 1000))
    db.set_meta(conn, "active_chip", picks.get("active_chip") or "")
    db.set_meta(conn, "squad_status", "loaded")
    return n


def enrich_defcon(
    conn: sqlite3.Connection, client: FplClient, min_minutes: int = 900
) -> int:
    """Populate ``defcon_per_90`` from last season's totals.

    The bootstrap zeroes defensive-contribution pre-season, so we read each
    regular's ``history_past`` (one cached call per player) and compute the rate.
    Capped to players with meaningful minutes to keep call volume sane (~250).
    """
    targets = conn.execute(
        "SELECT id FROM players WHERE minutes>=? AND defcon_per_90=0", (min_minutes,)
    ).fetchall()
    updated = 0
    for row in targets:
        pid = row["id"]
        try:
            summ = client.element_summary(pid)
        except httpx.HTTPStatusError:
            continue
        past = summ.get("history_past") or []
        if not past:
            continue
        last = past[-1]  # most recent prior season
        mins = last.get("minutes") or 0
        dc = last.get("defensive_contribution") or 0
        if mins >= 450 and dc:
            rate = round(dc / mins * 90, 3)
            conn.execute("UPDATE players SET defcon_per_90=? WHERE id=?", (rate, pid))
            updated += 1
    conn.commit()
    return updated


def ingest_entry_meta(conn: sqlite3.Connection, client: FplClient, entry_id: int) -> None:
    try:
        info = client.entry(entry_id)
    except httpx.HTTPStatusError:
        return
    db.set_meta(conn, "entry_name", info.get("name", ""))
    db.set_meta(conn, "manager_name", f"{info.get('player_first_name','')} "
                f"{info.get('player_last_name','')}".strip())
    db.set_meta(conn, "overall_rank", info.get("summary_overall_rank") or "")
    # bank/value from last deadline as a fallback when picks aren't available yet.
    if db.get_meta(conn, "bank") is None:
        db.set_meta(conn, "bank", info.get("last_deadline_bank", 0))
        db.set_meta(conn, "team_value", info.get("last_deadline_value", 1000))


def run(db_path=None, skip_enrich: bool = False) -> dict[str, int]:
    """Full ingest. Returns a small summary of row counts.

    Set ``skip_enrich`` (or env ``GAFFER_SKIP_ENRICH=1``) to skip the per-player
    DEFCON history calls during fast dev iterations.
    """
    import os

    config.ensure_dirs()
    conn = db.connect(db_path)
    db.init_schema(conn)
    settings = config.Settings.load()
    skip_enrich = skip_enrich or os.environ.get("GAFFER_SKIP_ENRICH") == "1"
    summary: dict[str, int] = {}
    with FplClient() as client:
        bootstrap = client.bootstrap()
        summary["teams"] = ingest_teams(conn, bootstrap)
        summary["players"] = ingest_players(conn, bootstrap)
        summary["fixtures"] = ingest_fixtures(conn, client.fixtures())
        gw = client.current_gw()
        db.set_meta(conn, "current_gw", gw)
        db.set_meta(conn, "last_finished_gw", client.last_finished_gw() or "")
        ev = next((e for e in bootstrap["events"] if e["id"] == gw), None)
        if ev:
            db.set_meta(conn, "deadline", ev.get("deadline_time") or "")
            db.set_meta(conn, "gw_name", ev.get("name") or f"Gameweek {gw}")
        if not skip_enrich:
            summary["defcon_enriched"] = enrich_defcon(conn, client)
        if settings.entry_id:
            ingest_entry_meta(conn, client, settings.entry_id)
            summary["my_squad"] = ingest_my_squad(conn, client, settings.entry_id, gw)
        else:
            db.set_meta(conn, "squad_status", "no_entry_id")
    conn.close()
    return summary


if __name__ == "__main__":
    print(run())
