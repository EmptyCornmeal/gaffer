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
from gaffer.model.projection import shrunk_defcon90
from gaffer.model.rationale import player_rationale, player_tags, xmins_badge
from gaffer.solver.optimize import MarginReport, Solution, squad_margins
from gaffer.store import db


def _teams(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    return {r["id"]: dict(r) for r in conn.execute("SELECT id, code, name, short FROM teams")}


def _prior_season_label(season: str) -> str:
    """'2026-27' -> '2025/26' (the most recent completed season)."""
    try:
        start = int(season.split("-")[0])
        return f"{start - 1}/{str(start)[2:]}"
    except (ValueError, IndexError):
        return "last season"


_PRIOR_SEASON = _prior_season_label(config.SEASON)


def _col(r: Any, name: str, default: Any = None) -> Any:
    """One column, or ``default`` if this row predates it. `sqlite3.Row` raises
    IndexError on an unknown key, and a missing column must never take the export
    down."""
    try:
        return r[name]
    except (IndexError, KeyError):
        return default


def _last_season(r: Any) -> dict[str, Any] | None:
    """Prior-season baseline (the ``base_*`` we persist so projections survive the
    FPL stats reset), surfaced for the player card. None when there's no real
    sample — e.g. a player new to the PL.

    ``season`` is the season the sample ACTUALLY came from. It used to be
    computed from the calendar and stamped on every player alike, which turned a
    2021/22 cameo by a player who has since been abroad into "last season" on the
    card — stale data wearing the label of current evidence. Where the provenance
    was never recorded it is reported as unknown, never inferred.
    """
    bm = r["base_minutes"] or 0
    if bm < config.BASE_SAMPLE_MINUTES:
        return None
    recorded = str(_col(r, "base_season") or "").strip()
    return {
        # None means "we did not record which season this was", which is a
        # different statement from any particular year.
        "season": recorded or None,
        # True: the season just gone. False: older, so treat it as weaker
        # evidence. None: unrecorded, so no claim either way.
        "is_prior_season": (recorded == _PRIOR_SEASON) if recorded else None,
        "minutes": int(bm),
        "starts": int(r["base_starts"] or 0),
        "xg90": round(r["base_xg90"] or 0, 2),
        "xa90": round(r["base_xa90"] or 0, 2),
    }


def _price_pred(net: int, owned_pct: float = 0.0, total_players: int = 0) -> dict[str, Any]:
    """Estimated price-change signal from net transfers this GW.

    FPL's exact thresholds are secret, but the dominant driver is net transfers
    relative to how many managers own the player: a rise/fall needs movement
    proportional to the owner base. We approximate the threshold as a fraction of
    owner count (with a floor for low-owned players) and report `progress` — the
    share of that threshold covered so far — clearly as an estimate, not a promise.
    """
    owned = max(0.0, owned_pct) / 100.0 * (total_players or 0)
    # ~7.5% of the owner base of net movement per day is a rough public heuristic;
    # floor keeps very low-owned players from tripping on tiny absolute numbers.
    threshold = max(30000.0, owned * 0.075)
    progress = round(min(1.0, abs(net) / threshold), 2) if threshold else 0.0
    if net > 0 and progress >= 0.6:
        direction = "up"
    elif net < 0 and progress >= 0.6:
        direction = "down"
    else:
        direction = "stable"
    return {
        "dir": direction,
        "momentum": net,
        "progress": progress,  # 0..1 estimated share of the change threshold
        "threshold": int(threshold),
    }


def run_timestamp() -> str:
    """One UTC stamp per pipeline run, shared by every artifact it writes.

    Stable ISO 8601 with an explicit ``+00:00`` offset. Artifacts must agree so
    the contract can assert they came from the same run.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_meta(
    conn: sqlite3.Connection,
    model_version: str,
    generated_at: str | None = None,
    settings: config.Settings | None = None,
) -> dict[str, Any]:
    keys = [
        "current_gw", "gw_name", "deadline", "last_finished_gw", "squad_status",
        # T-05: the two events are distinct, and the squad's provenance is explicit.
        "projection_event", "squad_source_event", "squad_status_reason",
        "squad_retrieved_at",
        # T-11: executable team state and where each value came from.
        "bank_source", "bank_exact", "free_transfers_source",
        "selling_price_confidence", "selling_prices_exact", "selling_prices_total",
        "recommendation_executable", "team_state_reason",
        "entry_name", "manager_name", "overall_rank", "overall_points",
        "bank", "team_value", "active_chip",
        "free_transfers", "rule_budget", "rule_club_limit", "rule_squad_size",
        "rule_sell_on_fee", "rule_max_extra_ft", "rule_transfers_cap",
        # Which scoring table this run was checked against, and whether it
        # matched what the model encodes.
        "rule_scoring_source", "rule_scoring_status", "rule_scoring_drift",
        # G21 — and *which* rules went unchecked, not merely how many.
        "rule_scoring_unchecked",
        # Which h=1 number was published: Gaffer's component model alone, or a
        # blend with FPL's ep_next. The difference is large enough to reorder a
        # squad, so it travels with the artifact like a model version does.
        "projection_regime", "projection_regime_reason", "ep_next_blend_weight",
        "ep_next_sample", "ep_next_ep_max", "ep_next_spread_ratio",
    ]
    meta = {}
    for k in keys:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        v = row["value"] if row else None
        # str(None)/empty from unset entry fields → real null (never leak "None")
        meta[k] = None if v in (None, "", "None") else v
    meta["model_version"] = model_version
    meta["generated_at"] = generated_at or run_timestamp()
    meta["season"] = config.SEASON
    # Label the build explicitly so a generic squad can never read as personalised.
    settings = settings if settings is not None else config.Settings.load()
    meta["build_mode"] = settings.build_mode
    meta["entry_id"] = settings.entry_id
    meta["league_ids"] = list(settings.league_ids)
    return meta


def _defcon_view(
    pos: str, observed90: float, believed90: float, exp_defcon_pts: float,
) -> dict[str, Any] | None:
    """Projected DEFCON for the next GW: P(+2 hit), per-90 rate, and a 'near-hit'
    flag (≥80% of the position threshold but under it — one tactical tick away).

    Gated on the OBSERVED rate and rendered from the BELIEVED one, which are two
    different questions and were previously answered by the same number. A
    player who has never recorded a defensive contribution should not sprout a
    DEFCON block merely because the model falls back to a positional prior for
    him; a player who recorded one in a single minute should not be shown
    "90.0/90" beside a P(hit) of 0.000. Gating on observed keeps the first from
    happening, displaying believed keeps the second from happening.
    """
    thr = config.DEFCON_THRESHOLD.get(pos, 99)
    if thr >= 99 or not observed90:
        return None
    p_hit = round(min(1.0, (exp_defcon_pts or 0) / config.DEFCON_POINTS), 3)
    return {
        "p_hit": p_hit,
        "per90": round(believed90, 1),
        "threshold": thr,
        "near_hit": bool(0.8 * thr <= believed90 < thr),
    }


def build_players(
    conn: sqlite3.Connection,
    from_gw: int,
    horizon: int,
    team_fixtures: dict[str, Any] | None = None,
    distributions: dict[int, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    teams = _teams(conn)
    team_fixtures = team_fixtures or {}
    distributions = distributions or {}
    tp_row = conn.execute("SELECT value FROM meta WHERE key='total_players'").fetchone()
    total_players = int(tp_row["value"]) if tp_row and str(tp_row["value"]).isdigit() else 0
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
               pr.exp_appearance, pr.exp_minutes, pr.exp_conceded_pts,
               pr.exp_saves_pts, pr.exp_cards_pts, pr.exp_misc_pts,
               pr.exp_points_model, pr.exp_points_ep_next
        FROM players pl
        LEFT JOIN projections pr ON pr.player_id = pl.id AND pr.gw = ?
    """
    for r in conn.execute(q, (from_gw,)):
        t = teams.get(r["team_id"], {})
        short = t.get("short", "?")
        fixtures = team_fixtures.get(short, {}).get("fixtures", [])[:5]
        # What the model believes, not what the raw column says. See
        # `projection.shrunk_defcon90` for why those differ and why it matters.
        believed_dc = shrunk_defcon90(r)
        rat_input = {
            "position": r["position"],
            "price": r["price"] / 10.0,
            "p_start": round(r["p_start"] or 0, 2),
            "exp_minutes": r["exp_minutes"] or 0,
            "xgi90": round(r["xgi_per_90"], 2),
            "defcon90": round(believed_dc, 2),
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
                    (r["transfers_in_event"] or 0) - (r["transfers_out_event"] or 0),
                    r["selected_by_pct"] or 0.0,
                    total_players,
                ),
                "price": r["price"] / 10.0,
                "owned_by": r["selected_by_pct"],
                "status": r["status"],
                "news": r["news"] or "",
                "set_pieces": r["set_piece_notes"] or "",
                "form": r["form"],
                "ict": round(r["ict_index"] or 0, 1),
                "last_season": _last_season(r),
                "dist": distributions.get(r["id"]),
                "defcon": _defcon_view(
                    r["position"], r["defcon_per_90"], believed_dc, r["exp_defcon_pts"]),
                "xgi90": round(r["xgi_per_90"], 2),
                "defcon90": round(believed_dc, 2),
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
                # Gaffer's own component sum, and FPL's ep_next, published
                # beside the shipped number so a card can always reconcile what
                # it is showing. When the two differ the difference is the h=1
                # blend, and `meta.projection_regime` says why it was applied.
                "model_xp": (round(r["exp_points_model"], 2)
                             if r["exp_points_model"] is not None else None),
                "ep_next_xp": (round(r["exp_points_ep_next"], 2)
                               if r["exp_points_ep_next"] is not None else None),
                # Every component, not six of ten: `saves` and `other` were
                # missing, so the breakdown could not add up to anything even
                # before the blend was in the picture. It now sums to `model_xp`.
                "breakdown": {
                    "appearance": round(r["exp_appearance"] or 0, 2),
                    "goals": round(r["exp_goal_pts"] or 0, 2),
                    "assists": round(r["exp_assist_pts"] or 0, 2),
                    "clean_sheet": round(r["exp_cs_pts"] or 0, 2),
                    "defcon": round(r["exp_defcon_pts"] or 0, 2),
                    "bonus": round(r["exp_bonus_pts"] or 0, 2),
                    "saves": round(r["exp_saves_pts"] or 0, 2),
                    "other": round((r["exp_conceded_pts"] or 0)
                                   + (r["exp_cards_pts"] or 0)
                                   + (r["exp_misc_pts"] or 0), 2),
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

    def def_difficulty(team_id: int, opp: int, home: bool) -> int:
        """1 (easy) .. 5 (hard) to keep a clean sheet: expected goals conceded."""
        lam = ctx.expected_conceded(team_id, opp, home)
        return int(max(1, min(5, round(1 + (lam - 0.6) / 0.4))))

    def att_difficulty(opp: int, home: bool) -> int:
        """1 (easy) .. 5 (hard) to score: the opponent-defence multiplier, inverted
        (a high attack multiplier = a soft defence = easy = 1). Mapped over the full
        de-compressed multiplier range so every 1-5 bucket is reachable."""
        mult = ctx.attack_multiplier(opp, home)  # ~0.5 (hard) .. 1.85 (easy)
        return int(max(1, min(5, round(1 + (1.85 - mult) / 0.34))))

    for r in rows:
        sides = ((r["team_h"], r["team_a"], True), (r["team_a"], r["team_h"], False))
        for team_id, opp, home in sides:
            att = att_difficulty(opp, home)
            dfc = def_difficulty(team_id, opp, home)
            per_team[team_id].append(
                {
                    "gw": r["gw"],
                    "opp": teams[opp]["short"],
                    "home": home,
                    # `difficulty` stays the overall (attack+defence) blend so the
                    # per-player fixture strips are unchanged; att/def let the ticker
                    # split scoring vs clean-sheet ease (Scoriness/Porosity).
                    "difficulty": int(round((att + dfc) / 2)),
                    "att": att,
                    "def": dfc,
                }
            )
    return {
        teams[tid]["short"]: {"team": teams[tid]["name"], "fixtures": fx}
        for tid, fx in per_team.items()
    }


def _player_card(conn: sqlite3.Connection, pid: int, players_by_id: dict[int, dict]) -> dict:
    return players_by_id.get(pid, {"id": pid, "name": "?"})


def _rec_card(pid: int, idx: dict[int, dict[str, Any]]) -> dict[str, Any]:
    p = idx.get(pid, {"id": pid, "name": "?"})
    return {
        "id": pid, "name": p.get("name"), "team": p.get("team"),
        "pos": p.get("pos"), "price": p.get("price"), "code": p.get("code"),
        "team_code": p.get("team_code"),
        "next_gw_xp": p.get("next_gw_xp"), "confidence": p.get("confidence"),
        "horizon_xp": p.get("horizon_xp"), "owned_by": p.get("owned_by"),
        "rationale": p.get("rationale"), "tags": p.get("tags"),
        "xmins_badge": p.get("xmins_badge"), "fixtures": p.get("fixtures", []),
    }


def _risk_note() -> str:
    from gaffer.solver.optimize import RISK_NOTE

    return RISK_NOTE


def build_recommendation(
    conn: sqlite3.Connection, sol: Solution, players_index: list[dict[str, Any]],
    generated_at: str | None = None,
    margins: MarginReport | None = None,
) -> dict[str, Any]:
    idx = {p["id"]: p for p in players_index}

    def card(pid: int) -> dict[str, Any]:
        """A squad card, carrying its near-optimal margin when one was measured.

        The margin rides on the card rather than living only in the block below
        because the number belongs next to the name: "how much does this pick
        actually matter" is a per-row question, and a screen that has to join two
        structures to answer it will end up not answering it. Cards for players
        with no margin (a transfer-out, an unmeasured pool player) are unchanged,
        so nothing downstream may assume the key is present.
        """
        c = _rec_card(pid, idx)
        m = margins.get(pid) if margins is not None else None
        return {**c, "margin": m.as_dict()} if m is not None else c

    cap = card(sol.captain)
    summary = _summarise(sol, idx)
    return {
        "generated_at": generated_at or run_timestamp(),
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
        # Provenance for the per-card numbers above: which objective they were
        # measured against, what the baseline was, how long it took, and whether
        # the replay reproduced the squad being published. Null when the sweep
        # was skipped or had nothing honest to say.
        "margins": margins.as_dict() if margins is not None else None,
    }


_HORIZON_LABEL = {1: "This gameweek", 3: "Next 3 GWs", 5: "Next 5 GWs"}


def _fixture_phrase(card: dict[str, Any], n: int) -> str:
    """'vs COV (H)' for n=1, or 'BOU, TOT, MCI' run for n>1 — from the card strip."""
    fx = card.get("fixtures") or []
    if not fx:
        return ""
    if n == 1:
        f = fx[0]
        return f"{f['opp']} ({'H' if f['home'] else 'A'})"
    return ", ".join(f["opp"] for f in fx[:n])


def _avg_difficulty(card: dict[str, Any], n: int) -> float | None:
    fx = (card.get("fixtures") or [])[:n]
    ds = [f.get("difficulty") for f in fx if f.get("difficulty")]
    return round(sum(ds) / len(ds), 1) if ds else None


_RISK_STANCE = {
    "differential": "Differential stance — chases points-per-£ and leaves the crowd's "
    "template; higher variance vs the field.",
    "balanced": "Balanced stance — owns the essential template for rank protection while "
    "keeping value elsewhere.",
    "template": "Template stance — maximises ownership of the crowd's picks; lowest rank "
    "risk, lowest differential upside.",
}


def build_optimal_explanation(
    sol: Solution, idx: dict[int, dict[str, Any]], horizon: int, risk: str = "balanced"
) -> dict[str, Any]:
    """Plain-English, fact-grounded 'why these picks' for the optimal squad.

    Deterministic on purpose — it cites the model's own numbers (xP, fixtures,
    price, ownership) so a user can audit every claim, no LLM guesswork.
    """
    starters = [_rec_card(i, idx) for i in sol.starting]
    if not starters:
        return {"headline": "No optimal squad available yet.", "bullets": []}
    cap = _rec_card(sol.captain, idx)
    n = min(horizon, 5)
    # A phrase that reads naturally after "for" and "over": "this gameweek" /
    # "the next 3 GWs" / "the next 5 GWs".
    phrase = "this gameweek" if horizon == 1 else f"the next {horizon} GWs"

    def val(c: dict[str, Any]) -> float:
        return (c.get("horizon_xp") if horizon > 1 else c.get("next_gw_xp")) or 0.0

    ranked = sorted(starters, key=val, reverse=True)
    premiums = sorted(starters, key=lambda c: c.get("price") or 0, reverse=True)[:2]
    enablers = [c for c in starters if (c.get("price") or 0) <= 4.5]
    diffs = [c for c in starters if (c.get("owned_by") or 0) and float(c["owned_by"]) < 10]

    bullets: list[str] = []

    # Risk stance — name the effective-ownership trade-off, and the template picks
    # it owns or omits (the thing that most changes between stances).
    owned_template = sorted(
        (c for c in starters if (c.get("owned_by") or 0) and float(c["owned_by"]) >= 30),
        key=lambda c: float(c["owned_by"] or 0),
        reverse=True,
    )
    stance = _RISK_STANCE.get(risk, "")
    if owned_template:
        names = ", ".join(f"{c['name']} ({c.get('owned_by')}%)" for c in owned_template[:3])
        stance += f" Owns the template core: {names}."
    elif risk == "differential":
        stance += " Owns none of the >30%-owned crowd — a pure punt against the field."
    bullets.append(stance)

    # Captain
    cap_fx = _fixture_phrase(cap, 1)
    cap_line = f"Captain **{cap['name']}** ({cap.get('next_gw_xp')} xP this GW"
    cap_line += f", {cap_fx})" if cap_fx else ")"
    cap_line += " — the single highest expected haul once the armband doubles it."
    bullets.append(cap_line)

    # Premium anchors + their fixtures
    if premiums:
        parts = []
        for c in premiums:
            fx = _fixture_phrase(c, 1)
            parts.append(f"**{c['name']}** (£{c.get('price')}m{', ' + fx if fx else ''})")
        bullets.append(
            "Builds around " + " and ".join(parts)
            + f" — their projected returns over {phrase} justify the spend."
        )

    # Fixture-driven picks (the user's core question: who they play)
    soft = [c for c in ranked if (_avg_difficulty(c, n) or 5) <= 2.5][:3]
    if soft:
        names = ", ".join(
            f"{c['name']} ({_fixture_phrase(c, n)})" for c in soft
        )
        bullets.append(
            f"Leans into soft fixture runs — {names} — where the opponent strength "
            f"model rates the matchups easy over {phrase}."
        )

    # Value enablers
    if enablers:
        names = ", ".join(f"{c['name']} (£{c.get('price')}m)" for c in enablers[:3])
        bullets.append(
            f"Frees cash with budget enablers — {names} — who are projected to start, "
            "letting the money concentrate on the premiums above."
        )

    # Formation logic
    bullets.append(
        f"Plays {sol.formation}: the shape that maximised total projected points "
        "under the £100m / 3-per-club rules, not a fixed setup."
    )

    # Differentials
    if diffs:
        names = ", ".join(f"{c['name']} ({c.get('owned_by')}%)" for c in diffs[:3])
        bullets.append(f"Sub-10% differentials in the XI: {names}.")

    headline = (
        f"Optimal {sol.formation} for {phrase} — "
        f"£{round(sol.squad_value / 10.0, 1)}m of £100m, "
        f"{sol.xi_expected} projected pts this GW."
    )
    return {"headline": headline, "bullets": bullets}


def build_horizon_reco(
    sol: Solution, players_index: list[dict[str, Any]], horizon: int, risk: str = "balanced"
) -> dict[str, Any]:
    """A compact optimal-squad payload for one (horizon, risk-stance) combination."""
    idx = {p["id"]: p for p in players_index}
    return {
        "horizon": horizon,
        "label": _HORIZON_LABEL.get(horizon, f"Next {horizon} GWs"),
        "risk": risk,
        "status": sol.status,
        "formation": sol.formation,
        "squad_value": round(sol.squad_value / 10.0, 1),
        "xi_expected": sol.xi_expected,
        "captain": _rec_card(sol.captain, idx),
        "vice": _rec_card(sol.vice, idx),
        "starting": [_rec_card(i, idx) for i in sol.starting],
        "bench": [_rec_card(i, idx) for i in sol.bench],
        "explanation": build_optimal_explanation(sol, idx, horizon, risk),
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


def build_plan(
    plan: Any, players_index: list[dict[str, Any]], generated_at: str | None = None
) -> dict[str, Any] | None:
    """Serialise a multi-GW transfer path (solver.multiperiod.Plan) for the UI."""
    if plan is None or not getattr(plan, "steps", None):
        return None
    idx = {p["id"]: p for p in players_index}

    def cards(ids: list[int]) -> list[dict[str, Any]]:
        return [_rec_card(i, idx) for i in ids]

    steps = []
    for s in plan.steps:
        bench = [i for i in s.squad if i not in s.starting]
        steps.append({
            "gw": s.gw,
            "xi_expected": s.xi_expected,
            "free_transfers": s.free_transfers,
            "hits": s.hits,
            "captain": _rec_card(s.captain, idx),
            "vice": _rec_card(s.vice, idx),
            "transfers_in": cards(s.transfers_in),
            "transfers_out": cards(s.transfers_out),
            "starting": cards(s.starting),
            "bench": cards(bench),
        })
    return {
        "generated_at": generated_at or run_timestamp(),
        "status": plan.status,
        "mode": plan.meta.get("mode"),
        "horizon": plan.meta.get("horizon"),
        "total_expected": plan.total_expected,
        "steps": steps,
    }


def build_my_team(
    conn: sqlite3.Connection, from_gw: int, players_index: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The stored holdings baseline, or None when no squad is known.

    Reads whatever squad is stored rather than filtering on ``from_gw``: the
    squad comes from the last *readable* event, which is never the event being
    projected. Returning None means "we do not know the squad" — consumers must
    not read it as "owns nothing".
    """
    rows = conn.execute(
        "SELECT gw, player_id FROM my_squad ORDER BY gw DESC, player_id"
    ).fetchall()
    if not rows:
        return None
    source_gw = int(rows[0]["gw"])
    owned = [r["player_id"] for r in rows if int(r["gw"]) == source_gw]
    idx = {p["id"]: p for p in players_index}
    return {
        # `gw` stays for backwards compatibility with the front-end; the explicit
        # names say which event is which.
        "gw": source_gw,
        "source_event": source_gw,
        "projection_event": from_gw,
        "status": db_get_meta(conn, "squad_status"),
        "players": [idx.get(pid, {"id": pid}) for pid in owned],
    }


def _mini_card(pid: Any, idx: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Just enough to render a name and a shirt — the strategy artifact carries
    hundreds of these and must not duplicate players.json."""
    p = idx.get(pid) or {}
    return {"id": pid, "name": p.get("name", "?"), "team": p.get("team"),
            "pos": p.get("pos"), "price": p.get("price"), "code": p.get("code"),
            "team_code": p.get("team_code"), "next_gw_xp": p.get("next_gw_xp")}


def build_strategy(
    strategy: dict[str, Any], players_index: list[dict[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Resolve the strategy layer's player ids into cards for the front-end.

    The strategy module speaks in ids because it is a pure decision layer; the UI
    needs names and shirts. This is the only place the two are joined.
    """
    idx = {p["id"]: p for p in players_index}
    out = dict(strategy)
    out["generated_at"] = generated_at or strategy.get("generated_at") or run_timestamp()

    squad = dict(strategy.get("squad") or {})
    for key in ("starting", "bench"):
        squad[key] = [_mini_card(i, idx) for i in squad.get(key) or []]
    if squad.get("captain") is not None:
        squad["captain"] = _mini_card(squad["captain"], idx)
    out["squad"] = squad

    def decorate(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        rows = []
        for e in entries or []:
            row = dict(e)
            row["player"] = _mini_card(e.get("player_id"), idx)
            rows.append(row)
        return rows

    out["leagues"] = [
        {**lg,
         "shields": decorate(lg.get("shields")),
         "differentials": decorate(lg.get("differentials"))}
        for lg in strategy.get("leagues") or []
    ]
    return out


def build_decision(
    payload: dict[str, Any], players_index: list[dict[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Resolve the weekly decision's player ids into cards for the UI.

    The decision layer speaks in ids because it is a pure decision layer; the
    home screen needs names, shirts and prices. This is the only place they join.
    """
    idx = {p["id"]: p for p in players_index}
    out = dict(payload)
    out["generated_at"] = generated_at or payload.get("generated_at") or run_timestamp()

    dec = dict(payload.get("decision") or {})
    for key in ("starting", "bench", "transfers_in", "transfers_out"):
        dec[key] = [_rec_card(i, idx) for i in dec.get(key) or []]
    for key in ("captain", "vice"):
        dec[key] = _rec_card(dec[key], idx) if dec.get(key) is not None else None
    out["decision"] = dec

    squad = dict(payload.get("squad_state") or {})
    squad["players"] = [_mini_card(i, idx) for i in squad.get("squad") or []]
    out["squad_state"] = squad
    # The stored distribution is for the review, not the UI: it is hundreds of
    # floats and nothing on screen reads it.
    out.pop("outcome_distribution", None)
    return out


def db_get_meta(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    v = row["value"] if row else None
    return None if v in (None, "", "None") else v


def write_all(
    conn: sqlite3.Connection, sol: Solution, from_gw: int,
    horizon: int, model_version: str, out_dir: Path | None = None,
    horizon_solutions: dict[int, dict[str, Solution]] | None = None,
    distributions: dict[int, dict[str, float]] | None = None,
    plan: Any = None,
    generated_at: str | None = None,
    settings: config.Settings | None = None,
    strategy: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    live: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    notifications: dict[str, Any] | None = None,
    verify_paths: bool = True,
    margins: bool = True,
    dry_run: bool = False,
) -> list[str]:
    out_dir = Path(out_dir) if out_dir is not None else config.DATA_DIR
    # Guard before any write: publishing outside the checkout discards the run.
    if verify_paths:
        config.verify_publish_paths(data_dir=out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = generated_at or run_timestamp()
    settings = settings if settings is not None else config.Settings.load()
    fixtures = build_fixtures(conn, from_gw, horizon)
    players = build_players(
        conn, from_gw, horizon, team_fixtures=fixtures, distributions=distributions
    )
    # G-O: what each of the fifteen picks is actually worth — an exact forced-out
    # re-solve per player, measured against the same objective that chose them.
    # ~3s on the real 587-player pool against ~0.2s for the headline solve, so it
    # ships on by default; `margins=False` is the opt-out for a caller that wants
    # the artifacts and not the extra second-and-a-bit.
    #
    # Deliberately NOT computed for the `by_horizon` grid below: that is nine
    # further solves, so margins there would cost nine sweeps (~27s) to decorate
    # squads the user is comparing rather than fielding.
    #
    # Wrapped, because a margin is a nice-to-have and a run that stops publishing
    # is not. A failure becomes a null `margins` block, not a lost gameweek.
    margin_report: MarginReport | None = None
    if margins:
        try:
            margin_report = squad_margins(conn, sol, distributions=distributions)
        except Exception as exc:                              # noqa: BLE001
            margin_report = MarginReport.unavailable(
                f"margin sweep failed: {type(exc).__name__}: {exc}")
    reco = build_recommendation(conn, sol, players, generated_at=generated_at,
                                margins=margin_report)
    # Grid of optimal squads — planning window (this GW / next 3 / next 5) ×
    # risk stance (differential / balanced / template) — each with a fact-grounded
    # explanation, so the Planner can toggle both and show *why*.
    if horizon_solutions:
        reco["by_horizon"] = {
            str(h): {
                "horizon": h,
                "label": _HORIZON_LABEL.get(h, f"Next {h} GWs"),
                "default_risk": "balanced",
                "risk_note": _risk_note(),
                "by_risk": {
                    r: build_horizon_reco(s, players, h, r)
                    for r, s in risk_map.items()
                },
            }
            for h, risk_map in sorted(horizon_solutions.items())
        }
    artifacts = {
        "meta.json": build_meta(
            conn, model_version, generated_at=generated_at, settings=settings
        ),
        "players.json": players,
        "fixtures.json": fixtures,
        "recommendation.json": reco,
        "my_team.json": build_my_team(conn, from_gw, players),
        "plan.json": build_plan(plan, players, generated_at=generated_at),
    }
    # Strategy is optional by presence: a run with no leagues configured, or one
    # invoked with --skip-strategy, writes nothing rather than an empty shell that
    # would read as "no leagues found".
    if strategy is not None:
        artifacts["strategy.json"] = build_strategy(
            strategy, players, generated_at=generated_at
        )
    # Batch 5. Each is optional by PRESENCE, not by emptiness: a run with nothing
    # to say writes no file rather than an empty shell that reads as "nothing
    # happened". `decision.json` is the exception — it always exists, because
    # "we cannot advise you" is itself the week's answer.
    if decision is not None:
        artifacts["decision.json"] = build_decision(
            decision, players, generated_at=generated_at)
    if live is not None:
        artifacts["live.json"] = {**live, "generated_at": generated_at}
    if review is not None:
        artifacts["review.json"] = {**review, "generated_at":
                                    review.get("generated_at") or generated_at}
    if notifications is not None:
        artifacts["notifications.json"] = {**notifications,
                                           "generated_at": generated_at}
    # T-29: every artifact carries the season it describes. `fixtures.json` is a
    # bare team->fixtures map and `players.json` a bare list, so those two are
    # stamped in `meta.json` alone and the contract cross-checks the rest against
    # it. Without this a stale artifact from a previous season parses cleanly and
    # renders as current.
    stamped = db.get_meta(conn, "season") or config.SEASON
    for blob in artifacts.values():
        if isinstance(blob, dict) and "season" not in blob:
            blob["season"] = stamped

    announce_targets(out_dir, list(artifacts))
    if dry_run:
        print("[artifacts] DRY RUN — nothing was written")
        return []
    written = []
    for fname, data in artifacts.items():
        path = out_dir / fname
        write_json_atomic(path, data)
        written.append(str(path))
    return written


def announce_targets(out_dir: Path, names: list[str]) -> None:
    """Print every file about to be overwritten, before anything is written.

    The scheduled refresh writes into the tracked ``data/`` directory by design —
    that is how the site updates. What must never happen is it doing so
    *silently*: an operator running the pipeline by hand deserves to see the
    tracked files they are about to replace, and a run that clobbers a manually
    curated artifact should be visible in the log, not discovered later.
    """
    root = Path(config.REPO_ROOT).resolve()
    print(f"[artifacts] target directory: {out_dir}")
    for name in sorted(names):
        path = (out_dir / name).resolve()
        try:
            tracked = path.is_relative_to(root) and path.exists()
        except (OSError, ValueError):  # pragma: no cover - unresolvable path
            tracked = False
        state = "OVERWRITE (tracked)" if tracked else (
            "overwrite" if path.exists() else "create")
        print(f"[artifacts]   {state:20} {name}")
