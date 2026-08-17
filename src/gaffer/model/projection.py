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

# 0.2 = T-13: goals conceded, saves, cards, OG, pens, bonus rate.
# 0.3 = M3: a zero in the prior-season baseline is read as a measurement only
#       when the season could have measured it. Numbers move for every player
#       whose baseline records a credible zero, so the version moves with them.
# 0.4 = M3b: the start rate divides fixtures by FIXTURES. It used to divide a
#       fixture-level `starts` tally by an event count, which agree only while
#       every team plays exactly once per gameweek. In-season only, so no effect
#       before GW1 — but it moves real numbers, so it moves the version.
# 0.5 = G-L/G-M/G-P: `defcon_per_90` is empirical-Bayes shrunk like every other
#       rate instead of being read raw, the NegBin dispersion behind it is
#       fitted rather than guessed, and xA is calibrated to FPL's assist
#       definition per position. Every projected DEFCON and assist number moves,
#       so the version moves with them.
MODEL_VERSION = "heuristic-0.5"

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

# --- h=1 blend regime -------------------------------------------------------
# The shipped one-week number blends FPL's own `ep_next` at
# `config.EP_NEXT_BLEND_WEIGHT`. In-season that is defensible: FPL sees team news
# Gaffer does not. Out of season it is not a forecast at all.
#
# Measured on the live 2026/27 pre-season payload, one week before the GW1
# deadline: `ep_next` topped out at exactly 4.0 across all 587 players, and
# Haaland (15.5m, 6.8 ppg), B.Fernandes (6.7), Gabriel (6.5) and a 6.0m
# goalkeeper (4.4) all held that same 4.0. Blending 70% of that collapsed the
# recommended XI from the model's own 66.2 expected points to a published 43.6,
# and — because the deflation is uneven — reordered the players the decision
# turns on. A goalkeeper outranked a premium forward.
#
# So the weight is gated on the external source actually carrying information.
# The gate is measured, recorded in meta.json, and lifts by itself.

#: Below this the whole population tops out too low to be a one-week points
#: forecast. A real premium's one-week expectation is comfortably above it.
EP_NEXT_MIN_POPULATION_MAX = 4.5
#: Below this the external forecast is compressed relative to the model it is
#: being blended into, so it cannot separate the players a decision turns on.
#: Self-calibrating — it asks "does this source spread the way ours does?",
#: not "is this number large?". Measured at 0.28 on the pre-season payload.
EP_NEXT_MIN_SPREAD_RATIO = 0.5
#: Fewer paired players than this and neither statistic means anything.
EP_NEXT_MIN_SAMPLE = 10

#: The published h=1 number is the blend of the component model with `ep_next`.
REGIME_BLENDED = "blended"
#: The published h=1 number is Gaffer's component model alone.
REGIME_COMPONENT_ONLY = "component_only"


def _quantile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank quantile. Deliberately not interpolated: these are decision
    thresholds, and an exact tie should read as the value that is actually there."""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))
    return sorted_values[idx]


def ep_next_regime(
    pairs: list[tuple[float, float]], *, season_started: bool,
) -> dict[str, Any]:
    """Decide whether FPL's ``ep_next`` is worth blending into h=1 this run.

    ``pairs`` is ``(ep_next, model_points)`` for every player carrying both.

    Two independent degeneracy tests, either of which disables the blend:

    1. **Absolute** — the population maximum is at or below
       ``EP_NEXT_MIN_POPULATION_MAX``. Nothing that tops out at 4.0 across every
       player in the game is a one-week points forecast.
    2. **Relative** — the source's upper spread is less than
       ``EP_NEXT_MIN_SPREAD_RATIO`` of the model's own, so it cannot discriminate
       where the model can.

    Both are skipped entirely once a gameweek has completed: from then on
    ``ep_next`` is computed from real form and fixtures, and the guard must not be
    able to fire. That is what makes the restoration automatic rather than a
    thing somebody has to remember.
    """
    eps = sorted(e for e, _ in pairs)
    mods = sorted(m for _, m in pairs)
    stats: dict[str, Any] = {
        "sample": len(pairs),
        "ep_max": round(eps[-1], 3) if eps else None,
        "ep_spread": None,
        "model_spread": None,
        "spread_ratio": None,
        "season_started": season_started,
    }
    full = config.EP_NEXT_BLEND_WEIGHT

    def out(regime: str, weight: float, reason: str) -> dict[str, Any]:
        return {**stats, "regime": regime, "blend_weight": round(weight, 4),
                "reason": reason}

    if season_started:
        return out(REGIME_BLENDED, full,
                   "a gameweek has been completed, so ep_next is computed from "
                   "real form and fixtures rather than a pre-season placeholder")
    if len(pairs) < EP_NEXT_MIN_SAMPLE:
        return out(REGIME_COMPONENT_ONLY, 0.0,
                   f"only {len(pairs)} player(s) carry both an ep_next and a "
                   "model projection, which is too few to judge whether the "
                   "external forecast carries any information")

    ep_spread = _quantile(eps, 0.95) - _quantile(eps, 0.50)
    model_spread = _quantile(mods, 0.95) - _quantile(mods, 0.50)
    ratio = (ep_spread / model_spread) if model_spread > 0 else 0.0
    stats["ep_spread"] = round(ep_spread, 3)
    stats["model_spread"] = round(model_spread, 3)
    stats["spread_ratio"] = round(ratio, 3)

    if stats["ep_max"] is not None and stats["ep_max"] <= EP_NEXT_MIN_POPULATION_MAX:
        return out(REGIME_COMPONENT_ONLY, 0.0,
                   f"ep_next tops out at {stats['ep_max']:g} across all "
                   f"{len(pairs)} projected players, which is a clipped "
                   "pre-season placeholder rather than a one-week forecast")
    if ratio < EP_NEXT_MIN_SPREAD_RATIO:
        return out(REGIME_COMPONENT_ONLY, 0.0,
                   f"ep_next spreads only {ratio:.2f}x as widely as Gaffer's own "
                   "projection over the same players, so blending it would "
                   "compress the ranking rather than inform it")
    return out(REGIME_BLENDED, full,
               f"ep_next spreads {ratio:.2f}x as widely as Gaffer's own "
               "projection, so it carries usable one-week information")


def apply_ep_next_blend(
    rows: list[dict], *, from_gw: int, availability: dict[int, float],
    season_started: bool,
) -> dict[str, Any]:
    """Blend ``ep_next`` into the h=1 rows, unless the source is degenerate.

    Mutates ``rows`` in place and returns the regime record. Rows keep
    ``exp_points_model`` and ``exp_points_ep_next`` untouched either way, so the
    component breakdown always adds up and the two inputs stay auditable.
    """
    pairs = [
        (float(r["exp_points_ep_next"]), float(r["exp_points_model"]))
        for r in rows
        if r["gw"] == from_gw
        and r.get("exp_points_ep_next") is not None
        and float(r["exp_points_ep_next"]) > 0
        and float(r["exp_points_model"]) > 0
    ]
    regime = ep_next_regime(pairs, season_started=season_started)
    base = regime["blend_weight"]
    if base <= 0:
        return regime
    for r in rows:
        if r["gw"] != from_gw:
            continue
        ep = r.get("exp_points_ep_next")
        if ep is None or float(ep) <= 0:
            continue
        # Scale the external weight by OUR availability read. FPL's ep_next does
        # not always reflect fresh injury news, and without this an unavailable
        # player would be resurrected by the blend.
        w = base * availability.get(r["player_id"], 1.0)
        r["exp_points"] = round(
            (1.0 - w) * float(r["exp_points_model"]) + w * float(ep), 3)
    return regime


def record_regime(conn: sqlite3.Connection, regime: dict[str, Any]) -> None:
    """Stamp the active projection regime into ``meta`` so it reaches meta.json.

    The regime is the difference between "these are Gaffer's numbers" and "these
    are 70% somebody else's". It travels with the artifact for the same reason
    the model version does.
    """
    from gaffer.store import db

    db.set_meta(conn, "projection_regime", regime.get("regime"))
    db.set_meta(conn, "projection_regime_reason", regime.get("reason") or "")
    db.set_meta(conn, "ep_next_blend_weight", regime.get("blend_weight"))
    for key in ("sample", "ep_max", "spread_ratio"):
        val = regime.get(key)
        db.set_meta(conn, f"ep_next_{key}", "" if val is None else val)


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


#: The most minutes a player could accumulate in a season without ever starting:
#: 38 appearances at the model's own cameo length. Above this, a `base_starts` of
#: 0 is a column the source did not have, not a career on the bench — and unlike
#: the season check this holds even when the provenance was never recorded.
_MAX_UNSTARTED_MINUTES = 38 * _CAMEO_MINUTES


def _field(player: Any, key: str, default: Any = None) -> Any:
    """One optional input, whatever the row type. ``sqlite3.Row`` raises
    IndexError for an unknown column and a dict raises KeyError; a column added
    after a database was created must not take the projection down."""
    try:
        return player[key]
    except (KeyError, IndexError):
        return default


def shrunk_defcon90(player: Any) -> float:
    """The DEFCON rate the projection actually believes, per 90.

    Lifted out of ``fixture_rates`` so a player card cannot quote a different
    number from the one the model scores. ``export.artifacts`` published
    ``players.defcon_per_90`` straight off the row, so once the shrinkage landed
    a card could read *"reliable DEFCON points (90.0/90 → +2 most weeks)"*
    directly above a P(hit) of 0.000 — two numbers on one card, describing the
    same player, disagreeing by two orders of magnitude. The badge is the half
    of this defect a reader can actually see, so it must come from here rather
    than from a second copy of the arithmetic that can drift.

    Returns 0.0 where DEFCON does not score, so callers may keep reading a zero
    as "not applicable" exactly as they did before.
    """
    pos = player["position"]
    if config.DEFCON_THRESHOLD.get(pos, 99) >= 99:
        return 0.0
    cur_min = player["minutes"] or 0
    base_min = player["base_minutes"] or 0
    have_base = base_min >= config.BASE_SAMPLE_MINUTES
    base_dc = _rate(player, "base_defcon90")
    dc_recorded = config.season_reports_defcon(
        _field(player, "base_season")) is not False
    tgt_dc = (base_dc if (have_base and dc_recorded and base_dc > 0)
              else F.DEFCON_PRIOR[pos])
    # Whichever season produced the rate is the season that sized it.
    dc_minutes = cur_min if cur_min > 0 else base_min
    return F.shrink(_rate(player, "defcon_per_90"), dc_minutes, tgt_dc,
                    F.DEFCON_SHRINK_K)


def fixture_rates(
    player: sqlite3.Row, fx: F.Fixture, ctx: TeamContext, avail: float,
    fixtures_played: int = 0,
) -> dict[str, float]:
    """The underlying per-fixture rate bundle the projection is built from.

    Exposed so the Monte-Carlo layer (``model.simulate``) samples from the *same*
    rates the deterministic projection sums — the point estimate and the
    distribution can never drift apart.
    """
    pos = player["position"]
    cur_min = player["minutes"] or 0
    base_min = player["base_minutes"] or 0
    base_starts = _field(player, "base_starts") or 0
    # Whether a prior season was RECORDED at all. Everything below turns on this
    # rather than on truthiness, because `base_*` is 0 in two situations that
    # mean opposite things: no sample exists, or a real sample measured zero.
    # Both writers gate on the same figure, so the test is exact.
    have_base = base_min >= config.BASE_SAMPLE_MINUTES
    # ...and whether a zero in that sample can be believed. FPL back-fills old
    # seasons with 0 instead of omitting the field, so a zero is only evidence
    # when the season was capable of reporting it. None means unrecorded, which
    # is treated as "believe it" — the physical check below is what protects the
    # unrecorded case.
    zero_is_evidence = config.season_reports_advanced_stats(
        _field(player, "base_season")) is not False

    # --- minutes gate ---------------------------------------------------
    # start prob: current-season starts/games once enough games; else last-season
    # starts/38; else a price-based prior. (starts/38 mid-season is wrong.)
    #
    # Zero starts off a full sample is the strongest bench evidence there is, and
    # the old truthiness test threw it away — sending exactly those players to a
    # price prior that reads an expensive squad player as a probable starter. One
    # start scores 1/38 and is believed; nought is believed on the same terms,
    # but only when it is credible:
    #   * the season could report `starts` at all, and
    #   * the minutes are physically reachable without ever starting. A season of
    #     substitute appearances cannot exceed 38 cameos; more than that with no
    #     starts is a missing column, whatever the provenance says.
    zero_starts_possible = base_min <= _MAX_UNSTARTED_MINUTES
    # `starts` counts FIXTURES, so the denominator counts fixtures too — the
    # team's own completed fixtures, not the number of gameweeks that have
    # elapsed. See `features.played_fixtures_by_team`.
    if fixtures_played >= 3 and cur_min and player["starts"] is not None:
        base_start = clamp(player["starts"] / fixtures_played, 0.0, 0.98)
    elif have_base and (base_starts or (zero_is_evidence and zero_starts_possible)):
        # `base_starts / 38` — and the denominator really is 38, which is known
        # to conflate two different things. It assumes the player was available
        # for every match, so a season missed through injury is recorded as a
        # season of not being picked. Saka started 25 of 38 because he spent
        # three months injured, and this reads 0.66.
        #
        # Two corrections were built and MEASURED, and neither survived:
        #
        #   symmetric blend with the price prior — wrong mechanism. A £4.5m
        #   price prior is 0.30, so it dragged Shaw from 0.98 to 0.65 and Raya
        #   from 0.97 to 0.72. Cheap does not mean benched. It improved the
        #   aggregate while degrading the best-known estimates, which is exactly
        #   the trade an average hides.
        #
        #   upward-only price floor — right mechanism, no effect. Held out on
        #   2024-25, XI points were flat at 50.7/gw for every floor weight in
        #   [0, 0.75] while rank correlation and MAE got monotonically WORSE as
        #   the floor rose. 2023-24 agreed. Measured, not guessed.
        #
        # The likely reason, and it is a correction to the premise rather than
        # to the code: absence predicts absence. A player who missed three
        # months is more likely to miss time again, so conflating injury with
        # rotation is crude but not the free win it looks like. Separating them
        # needs per-fixture history — the run-length of a player's zero-minute
        # gameweeks distinguishes an injury from rotation, and season aggregates
        # cannot. That is roadmap M6, not a constant.
        base_start = clamp(base_starts / 38.0, 0.0, 0.98)
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
    # A measured zero outranks a prior. A holding midfielder with 2,000 minutes
    # and no goals has told us what his xG rate is; substituting a positional
    # average there is not caution, it is discarding the only evidence available.
    # But a season that predated expected-goals reports 0.00 for everyone, and
    # believing THAT would project Bruno Fernandes as a man who never threatens.
    base_xg = player["base_xg90"] or 0.0
    base_xa = player["base_xa90"] or 0.0
    use_base_xgi = have_base and (zero_is_evidence or base_xg or base_xa)
    tgt_xg = base_xg if use_base_xgi else prior * 0.55
    tgt_xa = base_xa if use_base_xgi else prior * 0.45
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
    # G-L. This read the rate raw while both attacking rates above it were
    # shrunk, and a per-90 rate is a division: two players in the shipped 2026/27
    # pre-season artifact carried exactly 90.0 defensive contributions per 90 —
    # one contribution in one minute of football — and the model answered
    # P(hit) = 0.945 and 0.952 and printed "elite defensive volume" on a card
    # that said CAMEO? ~29' three lines further up.
    #
    # The obvious fix ships a worse bug. `defcon_per_90` does not mean what
    # `xg_per_90` means. FPL resets `minutes` at the season rollover but KEEPS
    # its per-90 fields, and `ingest.ingest_players` additionally falls back to
    # the enriched last-season figure when the bootstrap ships a zero — so out of
    # season this column holds a rate derived from ~3,000 prior-season minutes
    # while `cur_min` is 0. Shrinking that against `cur_min` would throw away the
    # best DEFCON evidence in the system and answer with a positional average.
    #
    # So two things are made explicit rather than one:
    #
    #   the TARGET is `base_defcon90`, the prior-season rate, mirroring
    #   `base_xg90` exactly (schema + `ingest.enrich_history`; `histdata` has
    #   computed the column all along for the backtest path);
    #
    #   the SAMPLE SIZE is the minutes that actually generated the rate, which
    #   is last season's whenever the current season has none yet. Without this
    #   second half, an existing database — where `base_defcon90` has not been
    #   backfilled yet but `defcon_per_90` is already correct — would send every
    #   elite ball-winner to a positional average on the first run after the
    #   migration. With it they lose about 3% instead: Anderson 13.91 -> 13.47,
    #   Gabriel 9.06 -> 8.93. Once the backfill runs they are exactly unmoved.
    #
    # A zero in `base_defcon90` is never read as a measurement. 392 outfielders
    # cleared `BASE_SAMPLE_MINUTES` in 2025-26 and not one recorded zero
    # defensive contributions; the floor is 2.25 per 90. Defensive contributions
    # are a high-frequency count, so a zero over a real sample is a column that
    # was not read — the same distinction `base_xg90` draws for seasons that
    # predated expected goals, and `season_reports_defcon` draws it against
    # DEFCON's own later cutoff.
    #
    # Verified on the live artifact: Mheuka 90.0 -> 4.7 (P(hit) 0.945 -> 0.000)
    # and Fredricson 90.0 -> 7.7 (0.952 -> 0.000), while Anderson (13.91),
    # Senesi (11.47), Tarkowski (10.16), Rice (10.94) and Gabriel (9.06) keep
    # their rate to the decimal and move only by the dispersion refit.
    thr = config.DEFCON_THRESHOLD[pos]
    defcon_mu = 0.0
    p_hit = 0.0
    if thr < 99:
        defcon_mu = shrunk_defcon90(player) * mins_frac
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
    player: sqlite3.Row, fx: F.Fixture, ctx: TeamContext, avail: float,
    fixtures_played: int = 0,
) -> dict[str, float]:
    r = fixture_rates(player, fx, ctx, avail, fixtures_played)
    pos = r["pos"]

    exp_goal_pts = r["exp_goals"] * r["goal_pts_per"]
    exp_assist_pts = r["exp_assists"] * r["assist_pts_per"]
    exp_cs_pts = r["p_cs"] * r["cs_pts_per"] * r["p60"]
    exp_defcon_pts = r["defcon_p_hit"] * r["defcon_pts"]

    # --- appearance -----------------------------------------------------
    exp_appearance = (
        r["p60"] * config.APPEARANCE_LONG
        + (r["p_play"] - r["p60"]) * config.APPEARANCE_SHORT
    )

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
    # Two different counts, deliberately kept apart. `games_played` answers "has
    # the season started", which is an event-level question. `played_by_team`
    # answers "how many matches has THIS team completed", which is the only
    # correct denominator for a fixture-level `starts` tally.
    lf = conn.execute("SELECT value FROM meta WHERE key='last_finished_gw'").fetchone()
    games_played = int(lf["value"]) if lf and str(lf["value"]).isdigit() else 0
    played_by_team = F.played_fixtures_by_team(conn)

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
                _project_one_fixture(p, fx, ctx, avail,
                                     played_by_team.get(p["team_id"], 0))
                for fx in by_gw.get(gw, [])
            ]
            acc = {k: sum(part[k] for part in parts) for k in additive}
            # p_start is a per-match property, not additive across a double.
            acc["p_start"] = max((part["p_start"] for part in parts), default=0.0)
            # T-15: FPL's own expected points for the NEXT gameweek are blended
            # in afterwards, in one pass over every row, because the decision to
            # blend at all depends on the whole population (see
            # `apply_ep_next_blend`). `ep_next` is a one-week-ahead number and
            # does not exist for later gameweeks, so h>=2 is always pure Gaffer.
            # The model's own estimate is retained separately so the component
            # breakdown still adds up and the external number is never presented
            # as Gaffer's own.
            model_points = acc["exp_points"]
            ep = p["ep_next"] if "ep_next" in p.keys() else None
            proj = GwProjection(
                player_id=p["id"], gw=gw, confidence=conf, generated_at=now,
                exp_points_model=round(model_points, 3),
                exp_points_ep_next=round(float(ep), 3) if ep is not None else None,
                **{k: round(v, 3) for k, v in acc.items()},
            )
            rows.append(asdict(proj))

    # The h=1 blend, decided over the whole population rather than per player:
    # an external forecast that cannot separate anybody must not be allowed to
    # flatten a ranking. Runs before the snapshot so what is retained for later
    # scoring is exactly what was published.
    regime = apply_ep_next_blend(
        rows, from_gw=from_gw, availability=avail_by_player,
        season_started=games_played > 0,
    )
    record_regime(conn, regime)

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
