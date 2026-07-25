"""Heuristic, component-based expected-points model (Phase 1).

Every projection decomposes into the same visible parts —
appearance + goals + assists + clean sheet + DEFCON + bonus — each gated by an
explicit minutes estimate, and carries a confidence read. Phase 2 swaps the
internals for a trained model behind the same interface.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from gaffer import config
from gaffer.model import features as F
from gaffer.model.features import TeamContext, clamp

MODEL_VERSION = "heuristic-0.1"

# Availability status -> baseline multiplier on the chance of featuring.
_STATUS_MULT = {"a": 1.0, "d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}
# Approx minutes for a nailed starter and for a cameo appearance.
_START_MINUTES = 82.0
_CAMEO_MINUTES = 20.0


@dataclass
class GwProjection:
    player_id: int
    gw: int
    p_start: float
    exp_minutes: float
    exp_goal_pts: float
    exp_assist_pts: float
    exp_cs_pts: float
    exp_defcon_pts: float
    exp_bonus_pts: float
    exp_appearance: float
    exp_points: float
    confidence: float
    model_version: str = MODEL_VERSION
    generated_at: str = ""


def _availability(status: str | None, chance: int | None) -> float:
    base = _STATUS_MULT.get(status or "a", 1.0)
    if chance is not None:  # explicit % overrides the coarse status bucket
        base = chance / 100.0
    return clamp(base, 0.0, 1.0)


def _start_prior(position: str, price: int) -> float:
    """Fallback start probability for players with no usable PL history.

    Leans on price as a proxy for expected role (pricier => more nailed).
    """
    frac = clamp((price - 40) / 60.0, 0.0, 1.0)  # £4.0m..£10.0m -> 0..1
    ceiling = {"GKP": 0.9, "DEF": 0.85, "MID": 0.8, "FWD": 0.8}[position]
    return 0.25 + frac * (ceiling - 0.25)


def _project_one_fixture(
    player: sqlite3.Row, fx: F.Fixture, ctx: TeamContext, avail: float, games_played: int = 0
) -> dict[str, float]:
    pos = player["position"]
    cur_min = player["minutes"] or 0
    base_min = player["base_minutes"] or 0

    # --- minutes gate ---------------------------------------------------
    # start prob: current-season starts/games once enough games; else last-season
    # starts/38; else a price-based prior. (starts/38 mid-season is wrong.)
    if games_played >= 3 and cur_min and player["starts"] is not None:
        base_start = clamp(player["starts"] / games_played, 0.0, 0.98)
    elif base_min > 90 and player["base_starts"]:
        base_start = clamp(player["base_starts"] / 38.0, 0.0, 0.98)
    else:
        base_start = _start_prior(pos, player["price"])
    p_start = clamp(base_start * avail, 0.0, 0.98)
    p_play = clamp(p_start + (1 - p_start) * 0.35 * avail, 0.0, 0.99)  # inc. cameo chance
    exp_minutes = p_start * _START_MINUTES + (p_play - p_start) * _CAMEO_MINUTES
    p60 = p_start  # starters are the ones who reach 60'

    # --- attacking ------------------------------------------------------
    # Shrink current-season rate toward the LAST-SEASON rate (survives the FPL
    # stats reset), falling back to a flat position prior for players with none.
    prior = F.XGI_PRIOR[pos]
    tgt_xg = player["base_xg90"] or (prior * 0.55)
    tgt_xa = player["base_xa90"] or (prior * 0.45)
    xg90 = F.shrink(player["xg_per_90"] or 0, cur_min, tgt_xg)
    xa90 = F.shrink(player["xa_per_90"] or 0, cur_min, tgt_xa)
    att_mult = ctx.attack_multiplier(fx.opponent_id, fx.at_home)
    mins_frac = exp_minutes / 90.0
    exp_goals = xg90 * mins_frac * att_mult
    exp_assists = xa90 * mins_frac * att_mult
    exp_goal_pts = exp_goals * config.GOAL_POINTS[pos]
    exp_assist_pts = exp_assists * config.ASSIST_POINTS

    # --- clean sheet ----------------------------------------------------
    exp_cs_pts = 0.0
    if config.CS_POINTS[pos] > 0:
        lam = ctx.expected_conceded(player["team_id"], fx.opponent_id, fx.at_home)
        p_cs = F.poisson_p0(lam)
        exp_cs_pts = p_cs * config.CS_POINTS[pos] * p60

    # --- DEFCON ---------------------------------------------------------
    exp_defcon_pts = 0.0
    thr = config.DEFCON_THRESHOLD[pos]
    if player["defcon_per_90"] and thr < 99:
        mu = player["defcon_per_90"] * mins_frac
        p_hit = F.poisson_sf(thr, mu)
        exp_defcon_pts = p_hit * config.DEFCON_POINTS

    # --- appearance -----------------------------------------------------
    exp_appearance = p60 * 2.0 + (p_play - p60) * 1.0

    # --- bonus (light proxy: bonus tracks returns + defensive workload) --
    exp_bonus_pts = 0.55 * (exp_goals + exp_assists) + 0.25 * exp_defcon_pts
    if pos in ("GKP", "DEF"):
        exp_bonus_pts += 0.35 * exp_cs_pts / max(config.CS_POINTS[pos], 1)

    exp_points = (
        exp_appearance
        + exp_goal_pts
        + exp_assist_pts
        + exp_cs_pts
        + exp_defcon_pts
        + exp_bonus_pts
    )
    return {
        "p_start": p_start,
        "exp_minutes": exp_minutes,
        "exp_goal_pts": exp_goal_pts,
        "exp_assist_pts": exp_assist_pts,
        "exp_cs_pts": exp_cs_pts,
        "exp_defcon_pts": exp_defcon_pts,
        "exp_bonus_pts": exp_bonus_pts,
        "exp_appearance": exp_appearance,
        "exp_points": exp_points,
    }


def _confidence(player: sqlite3.Row, avail: float) -> float:
    """0-1: how much to trust this projection. Driven by minutes reliability,
    availability certainty, and news flags."""
    rel = max(player["minutes"] or 0, player["base_minutes"] or 0)
    minutes_rel = rel / (rel + F.XGI_SHRINK_K)
    conf = 0.55 * minutes_rel + 0.35 * avail + 0.10
    if player["news"]:
        conf *= 0.85
    return round(clamp(conf, 0.05, 0.98), 3)


def project(conn: sqlite3.Connection, from_gw: int, horizon: int | None = None) -> int:
    """Compute and store projections for all players across the horizon.

    A blank gameweek yields a zero row; a double stacks both fixtures.
    Returns the number of (player, gw) rows written.
    """
    horizon = horizon or config.PROJECTION_HORIZON
    ctx = TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, from_gw, horizon)
    players = conn.execute("SELECT * FROM players").fetchall()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    # current-season games played (for start-probability denominator); 0 pre-season
    lf = conn.execute("SELECT value FROM meta WHERE key='last_finished_gw'").fetchone()
    games_played = int(lf["value"]) if lf and str(lf["value"]).isdigit() else 0

    rows: list[dict] = []
    for p in players:
        avail = _availability(p["status"], p["chance_playing"])
        conf = _confidence(p, avail)
        team_fx = fixtures.get(p["team_id"], {})
        # group this team's fixtures by gw (handles doubles/blanks)
        by_gw: dict[int, list[F.Fixture]] = {}
        for fx in team_fx:
            by_gw.setdefault(fx.gw, []).append(fx)
        additive = [
            "exp_goal_pts", "exp_assist_pts", "exp_cs_pts", "exp_defcon_pts",
            "exp_bonus_pts", "exp_appearance", "exp_points", "exp_minutes",
        ]
        for gw in range(from_gw, from_gw + horizon):
            parts = [
                _project_one_fixture(p, fx, ctx, avail, games_played)
                for fx in by_gw.get(gw, [])
            ]
            acc = {k: sum(part[k] for part in parts) for k in additive}
            # p_start is a per-match property, not additive across a double.
            acc["p_start"] = max((part["p_start"] for part in parts), default=0.0)
            proj = GwProjection(
                player_id=p["id"], gw=gw, confidence=conf, generated_at=now,
                **{k: round(v, 3) for k, v in acc.items()},
            )
            rows.append(asdict(proj))

    conn.execute("DELETE FROM projections")
    from gaffer.store import db
    return db.upsert(conn, "projections", rows, ["player_id", "gw"])
