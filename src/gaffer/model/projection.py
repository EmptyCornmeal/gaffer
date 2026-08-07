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
from typing import Any

from gaffer import config, gameweek
from gaffer import season as season_mod
from gaffer.model import features as F
from gaffer.model.features import TeamContext, clamp

MODEL_VERSION = "heuristic-0.2"  # T-13: goals conceded, saves, cards, OG, pens, bonus rate

# Availability status -> baseline multiplier on the chance of featuring.
_STATUS_MULT = {"a": 1.0, "d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}
# Approx minutes for a nailed starter and for a cameo appearance.
_START_MINUTES = 82.0
_CAMEO_MINUTES = 20.0
#: League-average saves per goal conceded, used only when a keeper has no
#: history of his own. PL keepers face roughly this many shots on target per
#: goal shipped.
_SAVES_PER_GOAL = 2.2
#: Weight on a player's own historical bonus rate vs the returns-driven proxy.
#: Bonus is BPS-driven and BPS is post-match, so the rate (a prior-gameweeks
#: aggregate) is the only pre-deadline signal available.
_BONUS_HISTORY_WEIGHT = 0.5


def _rate(player: Any, key: str) -> float:
    """A per-90 rate from the player row, tolerating absent columns.

    Historical frames and test fixtures do not always carry every rate; a
    missing rate means "no evidence", which must read as zero contribution
    rather than raising.
    """
    try:
        v = player[key]
    except (KeyError, IndexError, TypeError):
        return 0.0
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


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
    exp_conceded_pts: float
    exp_saves_pts: float
    exp_cards_pts: float
    exp_misc_pts: float
    exp_points: float
    confidence: float
    exp_points_model: float = 0.0            # Gaffer's own component sum
    exp_points_ep_next: float | None = None  # FPL's ep_next, where it exists
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


def fixture_rates(
    player: sqlite3.Row, fx: F.Fixture, ctx: TeamContext, avail: float, games_played: int = 0
) -> dict[str, float]:
    """The underlying per-fixture rate bundle the projection is built from.

    Exposed so the Monte-Carlo layer (``model.simulate``) samples from the *same*
    rates the deterministic projection sums — the point estimate and the
    distribution can never drift apart.
    """
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
    mins_frac = exp_minutes / 90.0

    # --- attacking ------------------------------------------------------
    # Shrink current-season rate toward the LAST-SEASON rate (survives the FPL
    # stats reset), falling back to a flat position prior for players with none.
    prior = F.XGI_PRIOR[pos]
    tgt_xg = player["base_xg90"] or (prior * 0.55)
    tgt_xa = player["base_xa90"] or (prior * 0.45)
    xg90 = F.shrink(player["xg_per_90"] or 0, cur_min, tgt_xg)
    xa90 = F.shrink(player["xa_per_90"] or 0, cur_min, tgt_xa)
    att_mult = ctx.attack_multiplier(fx.opponent_id, fx.at_home)
    exp_goals = xg90 * mins_frac * att_mult
    exp_assists = xa90 * mins_frac * att_mult

    # --- clean sheet ----------------------------------------------------
    p_cs = 0.0
    if config.CS_POINTS[pos] > 0:
        lam = ctx.expected_conceded(player["team_id"], fx.opponent_id, fx.at_home)
        p_cs = F.poisson_p0(lam)

    # --- DEFCON ---------------------------------------------------------
    thr = config.DEFCON_THRESHOLD[pos]
    defcon_mu = 0.0
    p_hit = 0.0
    if player["defcon_per_90"] and thr < 99:
        defcon_mu = player["defcon_per_90"] * mins_frac
        p_hit = F.nbinom_sf(thr, defcon_mu, F.DEFCON_NB_DISPERSION)

    # --- goals conceded / saves (T-13) ----------------------------------
    # Both derive from the SAME expected-goals-conceded figure that drives the
    # clean sheet, so the two cannot disagree about how leaky the fixture is.
    lam_conceded = ctx.expected_conceded(player["team_id"], fx.opponent_id, fx.at_home)
    conceded_units = 0.0
    if pos in config.CONCEDED_POSITIONS:
        # Only goals shipped while on the pitch count; scale the rate by the
        # share of the match played, not the whole 90.
        conceded_units = F.expected_floor_div(
            lam_conceded * mins_frac, config.CONCEDED_PER_PENALTY)

    save_units = 0.0
    if pos == "GKP":
        rate = _rate(player, "saves_per_90")
        if rate <= 0:
            # No history: fall back to the league relationship between goals
            # conceded and shots faced rather than assuming a keeper never saves.
            rate = lam_conceded * _SAVES_PER_GOAL
        save_units = F.expected_floor_div(rate * mins_frac, config.SAVES_PER_POINT)

    return {
        "pos": pos,
        "p_start": p_start,
        "p_play": p_play,
        "p60": p60,
        "exp_minutes": exp_minutes,
        "mins_frac": mins_frac,
        "exp_goals": exp_goals,
        "exp_assists": exp_assists,
        "goal_pts_per": float(config.GOAL_POINTS[pos]),
        "assist_pts_per": float(config.ASSIST_POINTS),
        "p_cs": p_cs,
        "cs_pts_per": float(config.CS_POINTS[pos]),
        "defcon_mu": defcon_mu,
        "defcon_thr": float(thr),
        "defcon_p_hit": p_hit,
        "defcon_pts": float(config.DEFCON_POINTS),
        "lam_conceded": lam_conceded,
        "conceded_units": conceded_units,
        "save_units": save_units,
        "yellow_rate": _rate(player, "yellow_per_90"),
        "red_rate": _rate(player, "red_per_90"),
        "og_rate": _rate(player, "og_per_90"),
        "pen_save_rate": _rate(player, "pen_save_per_90"),
        "pen_miss_rate": _rate(player, "pen_miss_per_90"),
        "bonus_rate": _rate(player, "bonus_per_90"),
    }


def _project_one_fixture(
    player: sqlite3.Row, fx: F.Fixture, ctx: TeamContext, avail: float, games_played: int = 0
) -> dict[str, float]:
    r = fixture_rates(player, fx, ctx, avail, games_played)
    pos = r["pos"]

    exp_goal_pts = r["exp_goals"] * r["goal_pts_per"]
    exp_assist_pts = r["exp_assists"] * r["assist_pts_per"]
    exp_cs_pts = r["p_cs"] * r["cs_pts_per"] * r["p60"]
    exp_defcon_pts = r["defcon_p_hit"] * r["defcon_pts"]

    # --- appearance -----------------------------------------------------
    exp_appearance = r["p60"] * 2.0 + (r["p_play"] - r["p60"]) * 1.0

    # --- goals conceded (T-13) ------------------------------------------
    # Negative counterpart to the clean sheet, from the same lambda: a defender
    # at a leaky club is no longer rewarded for the fixture and spared its cost.
    exp_conceded_pts = r["conceded_units"] * config.CONCEDED_PENALTY

    # --- goalkeeper saves (T-13) ----------------------------------------
    exp_saves_pts = r["save_units"] * config.SAVE_POINTS

    # --- discipline and rare events (T-13) ------------------------------
    # Scaled by time on the pitch. Rates are per-90 season aggregates, so these
    # are expectations, not predictions of a specific booking.
    mf = r["mins_frac"]
    exp_cards_pts = (
        r["yellow_rate"] * mf * config.YELLOW_POINTS
        + r["red_rate"] * mf * config.RED_POINTS
    )
    exp_misc_pts = (
        r["og_rate"] * mf * config.OWN_GOAL_POINTS
        + r["pen_miss_rate"] * mf * config.PENALTY_MISS_POINTS
        + (r["pen_save_rate"] * mf * config.PENALTY_SAVE_POINTS if pos == "GKP" else 0.0)
    )

    # --- bonus ------------------------------------------------------------
    # BPS is post-match, so it cannot be a feature. Blend the player's own
    # historical bonus rate (a prior-gameweeks aggregate) with the returns-driven
    # proxy, rather than inserting realised BPS.
    proxy = 0.55 * (r["exp_goals"] + r["exp_assists"]) + 0.25 * exp_defcon_pts
    if pos in ("GKP", "DEF"):
        proxy += 0.35 * exp_cs_pts / max(r["cs_pts_per"], 1)
    hist = r["bonus_rate"] * mf
    exp_bonus_pts = (
        (1 - _BONUS_HISTORY_WEIGHT) * proxy + _BONUS_HISTORY_WEIGHT * hist
        if hist > 0 else proxy
    )

    exp_points = (
        exp_appearance
        + exp_goal_pts
        + exp_assist_pts
        + exp_cs_pts
        + exp_defcon_pts
        + exp_bonus_pts
        + exp_conceded_pts
        + exp_saves_pts
        + exp_cards_pts
        + exp_misc_pts
    )
    return {
        "p_start": r["p_start"],
        "exp_minutes": r["exp_minutes"],
        "exp_goal_pts": exp_goal_pts,
        "exp_assist_pts": exp_assist_pts,
        "exp_cs_pts": exp_cs_pts,
        "exp_defcon_pts": exp_defcon_pts,
        "exp_bonus_pts": exp_bonus_pts,
        "exp_appearance": exp_appearance,
        "exp_conceded_pts": exp_conceded_pts,
        "exp_saves_pts": exp_saves_pts,
        "exp_cards_pts": exp_cards_pts,
        "exp_misc_pts": exp_misc_pts,
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
    # Microsecond precision: snapshots are keyed by `as_of`, and two runs in
    # the same second would otherwise collide and overwrite each other.
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    # current-season games played (for start-probability denominator); 0 pre-season
    lf = conn.execute("SELECT value FROM meta WHERE key='last_finished_gw'").fetchone()
    games_played = int(lf["value"]) if lf and str(lf["value"]).isdigit() else 0

    rows: list[dict] = []
    avail_by_player: dict[int, float] = {}
    for p in players:
        avail = _availability(p["status"], p["chance_playing"])
        avail_by_player[p["id"]] = avail
        conf = _confidence(p, avail)
        team_fx = fixtures.get(p["team_id"], {})
        # group this team's fixtures by gw (handles doubles/blanks)
        by_gw: dict[int, list[F.Fixture]] = {}
        for fx in team_fx:
            by_gw.setdefault(fx.gw, []).append(fx)
        additive = [
            "exp_goal_pts", "exp_assist_pts", "exp_cs_pts", "exp_defcon_pts",
            "exp_bonus_pts", "exp_appearance", "exp_points", "exp_minutes",
            "exp_conceded_pts", "exp_saves_pts", "exp_cards_pts", "exp_misc_pts",
        ]
        for gw in range(from_gw, from_gw + horizon):
            parts = [
                _project_one_fixture(p, fx, ctx, avail, games_played)
                for fx in by_gw.get(gw, [])
            ]
            acc = {k: sum(part[k] for part in parts) for k in additive}
            # p_start is a per-match property, not additive across a double.
            acc["p_start"] = max((part["p_start"] for part in parts), default=0.0)
            # T-15: blend FPL's own expected points for the NEXT gameweek only.
            # `ep_next` is a one-week-ahead number and does not exist for later
            # gameweeks, so h>=2 stays pure Gaffer. The model's own estimate is
            # retained separately so the component breakdown still adds up and
            # the external number is never presented as Gaffer's own.
            model_points = acc["exp_points"]
            blended = model_points
            ep = p["ep_next"] if "ep_next" in p.keys() else None
            if gw == from_gw and ep is not None and float(ep) > 0:
                # Scale the external weight by OUR availability read. FPL's
                # ep_next does not always reflect fresh injury news, and without
                # this an unavailable player would be resurrected by the blend.
                w = config.EP_NEXT_BLEND_WEIGHT * avail
                blended = (1.0 - w) * model_points + w * float(ep)
            acc["exp_points"] = blended
            proj = GwProjection(
                player_id=p["id"], gw=gw, confidence=conf, generated_at=now,
                exp_points_model=round(model_points, 3),
                exp_points_ep_next=round(float(ep), 3) if ep is not None else None,
                **{k: round(v, 3) for k, v in acc.items()},
            )
            rows.append(asdict(proj))

    from gaffer.store import db

    # Snapshot BEFORE the destructive replace. `projections` is wiped every run,
    # so without this there is no record to score the model against once the
    # results land.
    snapshot_projections(
        conn, rows, from_gw=from_gw, generated_at=now,
        availability=avail_by_player,
    )

    conn.execute("DELETE FROM projections")
    return db.upsert(conn, "projections", rows, ["player_id", "gw"])


def snapshot_projections(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    from_gw: int,
    generated_at: str,
    availability: dict[int, float] | None = None,
    season: str | None = None,
    deadlines: dict[int, str] | None = None,
) -> int:
    """Retain this run's projections keyed by (season, target_gw, player, as_of).

    ``is_pre_deadline`` records whether the snapshot was taken before the target
    event's deadline. Only pre-deadline snapshots are a fair basis for scoring —
    a projection computed after kickoff has seen team news the decision could
    not have. The flag is written once, at snapshot time, and never recomputed.
    """
    from gaffer.store import db

    season = season or season_mod.current(conn)
    availability = availability or {}
    if deadlines is None:
        deadlines = {
            int(r["gw"]): r["kickoff"]
            for r in conn.execute(
                "SELECT gw, MIN(kickoff) AS kickoff FROM fixtures "
                "WHERE kickoff IS NOT NULL GROUP BY gw"
            )
            if r["gw"] is not None
        }

    now_dt = gameweek.parse_deadline(generated_at)
    snaps = []
    for r in rows:
        target = int(r["gw"])
        deadline_raw = deadlines.get(target)
        deadline_dt = gameweek.parse_deadline(deadline_raw)
        # Unknown deadline -> assume pre-deadline only when the target event is
        # at or beyond the event being projected from.
        if deadline_dt is not None and now_dt is not None:
            pre = now_dt <= deadline_dt
        else:
            pre = target >= from_gw
        snaps.append({
            "season": season,
            "target_gw": target,
            "player_id": r["player_id"],
            "as_of": generated_at,
            "model_version": MODEL_VERSION,
            "horizon": target - from_gw,
            "is_pre_deadline": 1 if pre else 0,
            "deadline_time": deadline_raw,
            "p_start": r.get("p_start"),
            "exp_minutes": r.get("exp_minutes"),
            "exp_goal_pts": r.get("exp_goal_pts"),
            "exp_assist_pts": r.get("exp_assist_pts"),
            "exp_cs_pts": r.get("exp_cs_pts"),
            "exp_defcon_pts": r.get("exp_defcon_pts"),
            "exp_bonus_pts": r.get("exp_bonus_pts"),
            "exp_appearance": r.get("exp_appearance"),
            "exp_points": r.get("exp_points"),
            "confidence": r.get("confidence"),
            "availability": availability.get(r["player_id"]),
        })
    if not snaps:
        return 0
    return db.upsert(
        conn, "projection_snapshots", snaps,
        ["season", "target_gw", "player_id", "as_of"],
    )


def latest_pre_deadline_snapshot(
    conn: sqlite3.Connection, target_gw: int, season: str | None = None
) -> dict[int, dict]:
    """The snapshot a fair evaluation must use for ``target_gw``.

    Deterministic rule: among snapshots marked pre-deadline for that event, take
    the LATEST ``as_of`` — the last projection that could still have informed the
    decision. Post-deadline snapshots are never returned.
    """
    season = season or season_mod.current(conn)
    rows = conn.execute(
        "SELECT * FROM projection_snapshots WHERE season=? AND target_gw=? "
        "AND is_pre_deadline=1 ORDER BY as_of",
        (season, target_gw),
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:  # ordered ascending, so the last write per player wins
        out[r["player_id"]] = dict(r)
    return out
