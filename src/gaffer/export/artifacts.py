"""Write denormalised JSON artifacts for the static front-end.

The front-end reads these files only — it never touches the DB or the FPL API.
Keep the shapes stable; the Svelte app depends on them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gaffer import config
from gaffer.io import write_json_atomic
from gaffer.model.rationale import player_rationale, player_tags, xmins_badge
from gaffer.solver.optimize import Solution


def _teams(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    return {r["id"]: dict(r) for r in conn.execute("SELECT id, code, name, short FROM teams")}


def _price_pred(net: int) -> dict[str, Any]:
    """Momentum-based price-change signal from net transfers this GW.

    FPL's exact thresholds are secret; net transfers are the dominant driver, so
    we surface momentum + a directional flag when it's decisively one-way.
    """
    if net >= 60000:
        return {"dir": "up", "momentum": net}
    if net <= -60000:
        return {"dir": "down", "momentum": net}
    return {"dir": "stable", "momentum": net}


def build_meta(conn: sqlite3.Connection, model_version: str) -> dict[str, Any]:
    keys = [
        "current_gw", "gw_name", "deadline", "last_finished_gw", "squad_status",
        "entry_name", "manager_name", "overall_rank", "bank", "team_value", "active_chip",
        "free_transfers", "rule_budget", "rule_club_limit", "rule_squad_size",
        "rule_sell_on_fee", "rule_max_extra_ft", "rule_transfers_cap",
    ]
    meta = {}
    for k in keys:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        meta[k] = row["value"] if row else None
    meta["model_version"] = model_version
    meta["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    meta["season"] = config.SEASON
    return meta


def build_players(
    conn: sqlite3.Connection,
    from_gw: int,
    horizon: int,
    team_fixtures: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    teams = _teams(conn)
    team_fixtures = team_fixtures or {}
    # horizon sum + next-GW row per player
    horizon_sum: dict[int, float] = {}
    for r in conn.execute(
        "SELECT player_id, SUM(exp_points) s FROM projections "
        "WHERE gw>=? AND gw<? GROUP BY player_id",
        (from_gw, from_gw + horizon),
    ):
        horizon_sum[r["player_id"]] = round(r["s"], 2)

    # per-GW projection across the horizon (for the chip planner)
    per_gw: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT player_id, gw, exp_points FROM projections "
        "WHERE gw>=? AND gw<? ORDER BY gw",
        (from_gw, from_gw + horizon),
    ):
        per_gw.setdefault(r["player_id"], []).append(
            {"gw": r["gw"], "xp": round(r["exp_points"], 2)}
        )

    out = []
    q = """
        SELECT pl.*, pr.exp_points, pr.p_start, pr.confidence, pr.exp_goal_pts,
               pr.exp_assist_pts, pr.exp_cs_pts, pr.exp_defcon_pts, pr.exp_bonus_pts,
               pr.exp_appearance, pr.exp_minutes
        FROM players pl
        LEFT JOIN projections pr ON pr.player_id = pl.id AND pr.gw = ?
    """
    for r in conn.execute(q, (from_gw,)):
        t = teams.get(r["team_id"], {})
        short = t.get("short", "?")
        fixtures = team_fixtures.get(short, {}).get("fixtures", [])[:5]
        rat_input = {
            "position": r["position"],
            "price": r["price"] / 10.0,
            "p_start": round(r["p_start"] or 0, 2),
            "exp_minutes": r["exp_minutes"] or 0,
            "xgi90": round(r["xgi_per_90"], 2),
            "defcon90": round(r["defcon_per_90"], 2),
            "form": r["form"],
            "owned_by": r["selected_by_pct"],
            "set_pieces": r["set_piece_notes"] or "",
            "news": r["news"] or "",
            "status": r["status"],
            "cs_pts": r["exp_cs_pts"] or 0,
            "goal_pts": r["exp_goal_pts"] or 0,
            "assist_pts": r["exp_assist_pts"] or 0,
            "defcon_pts": r["exp_defcon_pts"] or 0,
            "xp_next": round(r["exp_points"] or 0, 2),
            "fixtures": fixtures,
        }
        out.append(
            {
                "id": r["id"],
                "code": r["code"],
                "name": r["web_name"],
                "full_name": f"{r['first_name'] or ''} {r['second_name'] or ''}".strip(),
                "team": short,
                "team_id": r["team_id"],
                "team_code": t.get("code"),
                "pos": r["position"],
                "net_transfers": (r["transfers_in_event"] or 0) - (r["transfers_out_event"] or 0),
                "cost_change_event": r["cost_change_event"] or 0,
                "price_pred": _price_pred(
                    (r["transfers_in_event"] or 0) - (r["transfers_out_event"] or 0)
                ),
                "price": r["price"] / 10.0,
                "owned_by": r["selected_by_pct"],
                "status": r["status"],
                "news": r["news"] or "",
                "set_pieces": r["set_piece_notes"] or "",
                "form": r["form"],
                "ict": round(r["ict_index"] or 0, 1),
                "xgi90": round(r["xgi_per_90"], 2),
                "defcon90": round(r["defcon_per_90"], 2),
                "next_gw_xp": round(r["exp_points"], 2) if r["exp_points"] is not None else 0.0,
                "horizon_xp": horizon_sum.get(r["id"], 0.0),
                "xp_window": horizon_sum.get(r["id"], 0.0),
                "gw_xp": per_gw.get(r["id"], []),
                "p_start": round(r["p_start"], 2) if r["p_start"] is not None else 0.0,
                "confidence": round(r["confidence"], 2) if r["confidence"] is not None else 0.0,
                "xmins_badge": xmins_badge(r["exp_minutes"] or 0, r["p_start"] or 0),
                "rationale": player_rationale(rat_input),
                "tags": player_tags(rat_input),
                "fixtures": fixtures,
                "breakdown": {
                    "appearance": round(r["exp_appearance"] or 0, 2),
                    "goals": round(r["exp_goal_pts"] or 0, 2),
                    "assists": round(r["exp_assist_pts"] or 0, 2),
                    "clean_sheet": round(r["exp_cs_pts"] or 0, 2),
                    "defcon": round(r["exp_defcon_pts"] or 0, 2),
                    "bonus": round(r["exp_bonus_pts"] or 0, 2),
                },
            }
        )
    out.sort(key=lambda p: p["next_gw_xp"], reverse=True)
    return out


def build_fixtures(conn: sqlite3.Connection, from_gw: int, horizon: int) -> dict[str, Any]:
    """Per-team upcoming fixtures with an xGC-based difficulty (not raw FDR)."""
    from gaffer.model.features import TeamContext

    ctx = TeamContext.build(conn)
    teams = _teams(conn)
    rows = conn.execute(
        "SELECT gw, team_h, team_a, fdr_h, fdr_a FROM fixtures "
        "WHERE gw>=? AND gw<? AND finished=0 ORDER BY gw",
        (from_gw, from_gw + horizon),
    ).fetchall()

    per_team: dict[int, list[dict[str, Any]]] = {tid: [] for tid in teams}

    def difficulty(team_id: int, opp: int, home: bool) -> int:
        """1 (easy) .. 5 (hard): expected goals conceded by this team vs opp."""
        lam = ctx.expected_conceded(team_id, opp, home)
        # map ~0.6..2.2 xGC onto 1..5
        return int(max(1, min(5, round(1 + (lam - 0.6) / 0.4))))

    for r in rows:
        sides = ((r["team_h"], r["team_a"], True), (r["team_a"], r["team_h"], False))
        for team_id, opp, home in sides:
            per_team[team_id].append(
                {
                    "gw": r["gw"],
                    "opp": teams[opp]["short"],
                    "home": home,
                    "difficulty": difficulty(team_id, opp, home),
                }
            )
    return {
        teams[tid]["short"]: {"team": teams[tid]["name"], "fixtures": fx}
        for tid, fx in per_team.items()
    }


def _player_card(conn: sqlite3.Connection, pid: int, players_by_id: dict[int, dict]) -> dict:
    return players_by_id.get(pid, {"id": pid, "name": "?"})


def build_recommendation(
    conn: sqlite3.Connection, sol: Solution, players_index: list[dict[str, Any]]
) -> dict[str, Any]:
    idx = {p["id"]: p for p in players_index}

    def card(pid: int) -> dict[str, Any]:
        p = idx.get(pid, {"id": pid, "name": "?"})
        return {
            "id": pid, "name": p.get("name"), "team": p.get("team"),
            "pos": p.get("pos"), "price": p.get("price"), "code": p.get("code"),
            "team_code": p.get("team_code"),
            "next_gw_xp": p.get("next_gw_xp"), "confidence": p.get("confidence"),
            "rationale": p.get("rationale"), "tags": p.get("tags"),
            "xmins_badge": p.get("xmins_badge"),
        }

    cap = card(sol.captain)
    summary = _summarise(sol, idx)
    return {
        "mode": sol.meta.get("mode"),
        "status": sol.status,
        "formation": sol.formation,
        "squad_value": round(sol.squad_value / 10.0, 1),
        "xi_expected": sol.xi_expected,
        "captain": cap,
        "vice": card(sol.vice),
        "starting": [card(i) for i in sol.starting],
        "bench": [card(i) for i in sol.bench],
        "transfers_in": [card(i) for i in sol.transfers_in],
        "transfers_out": [card(i) for i in sol.transfers_out],
        "hits": sol.hits,
        "summary": summary,
    }


def _summarise(sol: Solution, idx: dict[int, dict]) -> str:
    """Plain-English one-liner (templated for now; the AI layer replaces this)."""
    cap = idx.get(sol.captain, {}).get("name", "?")
    if sol.meta.get("mode") == "build":
        return (
            f"Optimal {sol.formation} squad for £{sol.squad_value / 10:.1f}m. "
            f"Captain {cap} ({idx.get(sol.captain, {}).get('next_gw_xp', 0)} xP). "
            f"Projected starting XI haul: {sol.xi_expected} pts."
        )
    if not sol.transfers_in:
        return f"Roll your transfer. No move beats the -4. Captain {cap}."
    ins = ", ".join(idx.get(i, {}).get("name", "?") for i in sol.transfers_in)
    outs = ", ".join(idx.get(i, {}).get("name", "?") for i in sol.transfers_out)
    hit = f" (-{sol.hits * config.HIT_COST})" if sol.hits else ""
    return f"Transfer: {outs} -> {ins}{hit}. Captain {cap}."


def build_my_team(
    conn: sqlite3.Connection, from_gw: int, players_index: list[dict[str, Any]]
) -> dict[str, Any] | None:
    owned = [r["player_id"] for r in conn.execute(
        "SELECT player_id FROM my_squad WHERE gw=?", (from_gw,))]
    if not owned:
        return None
    idx = {p["id"]: p for p in players_index}
    return {
        "gw": from_gw,
        "players": [idx.get(pid, {"id": pid}) for pid in owned],
    }


def write_all(
    conn: sqlite3.Connection, sol: Solution, from_gw: int,
    horizon: int, model_version: str, out_dir: Path | None = None,
) -> list[str]:
    out_dir = out_dir or config.DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures = build_fixtures(conn, from_gw, horizon)
    players = build_players(conn, from_gw, horizon, team_fixtures=fixtures)
    artifacts = {
        "meta.json": build_meta(conn, model_version),
        "players.json": players,
        "fixtures.json": fixtures,
        "recommendation.json": build_recommendation(conn, sol, players),
        "my_team.json": build_my_team(conn, from_gw, players),
    }
    written = []
    for fname, data in artifacts.items():
        path = out_dir / fname
        write_json_atomic(path, data)
        written.append(str(path))
    return written
