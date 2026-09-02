"""Backtest the SHIPPED projection on a held-out season.

The previous harness scored a substitute: it injected ``ml.py``'s fixture
multiplier (no gamma, a different clamp) and a different clean-sheet formula,
zeroed the last-season prior, DEFCON and availability, and filtered the
evaluation population on ``minutes > 0`` — i.e. it knew who had played before
picking a team. Every published accuracy number came from that path.

This version calls ``projection._project_one_fixture`` through a real
``TeamContext``, keeps zero-minute outcomes in the population, reconstructs each
decision point from pre-deadline information only, and evaluates horizons 1-6.

It establishes a baseline. It does not tune anything.

    python -m gaffer.backtest                 # prints, writes nothing
    python -m gaffer.backtest --write         # overwrites data/backtest.json
    python -m gaffer.backtest --horizons 1 3  # faster subset
    python -m gaffer.backtest --minutes-only  # the A11 minutes block alone
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from gaffer import config, histdata, leakage
from gaffer.io import write_json_atomic
from gaffer.model import features as F
from gaffer.model import projection

#: Backtest artifact schema. Bump when the shape changes; the Accuracy page
#: refuses to render anything it does not recognise rather than mis-labelling it.
#:   1 = legacy (ml-vs-heuristic, minutes>0 filtered) — REJECTED, never rendered
#:   2 = corrected harness, per-horizon
#:   3 = adds `baselines` (frozen model comparisons) and `ablations`
#:   4 = withdraws the `fpl_xp` / `ensemble` columns. They were computed from the
#:       archive's `xP`, which is inadmissible (see WITHDRAWN_BASELINES). Adds
#:       `withdrawn_baselines` and `rejected_models` so the withdrawal is visible
#:       rather than silent.
#:   5 = replaces the single `rejected_models` blob with `model_candidates`: one
#:       record per candidate, each with its own decision. Version 4 reported GBM
#:       only and concluded "trained models lose every decision metric", which is
#:       false — ridge beat the heuristic at h=1. `rejected` and `inconclusive`
#:       are different findings and the artifact now carries both.
#:   6 = evaluates GW1. Versions 1-5 started at GW2 while their own comment
#:       claimed GW1 was included, so no genuinely pre-season decision had ever
#:       been measured — the one regime in which a whole squad is picked from
#:       scratch. Adds `pre_season`, because averaging that gameweek into 37
#:       in-season ones hides it, and because the naive baseline does not exist
#:       there and must not appear to have been beaten.
#:   7 = reports on 2025-26 and re-cuts the split behind it (G-N), and removes
#:       2022-23 from that split (G-Q). Every figure in a version-6 artifact was
#:       measured on 2024-25 — a season in which `defensive_contribution` does
#:       not exist, so the projection's DEFCON term contributed exactly nothing
#:       to it. A 6 and a 7 are two different models measured on two different
#:       seasons and must never be diffed as though one were an improvement on
#:       the other. Adds `season_split`, so the artifact states its own
#:       train/select/test instead of leaving it to a comment that never ships.
#:   8 = adds `minutes_model` (A11). `p_start` gates every projection — it scales
#:       every rate, discounts `ep_next` through `rotation_scale`, drives the
#:       autosubs and the solver, and is what the NAILED / ROTATION / CAMEO?
#:       badge reports — and until this version it was the one major component
#:       with no measured error rate at all. It has one now, and when this
#:       version was cut it lost to a three-game rolling start rate at every
#:       horizon. Additive: nothing that reads a version-7 artifact changes
#:       meaning.
#:
#:       A18 then acted on that measurement — see `MINUTES_CANDIDATE_FIX`, which
#:       records the fix as SHIPPED — so a version-8 artifact generated before
#:       and after it are two different models. The SHAPE is unchanged, which is
#:       why the number is not bumped: `model_version` is the field that
#:       separates them, and it moved from heuristic-0.5 to heuristic-0.6.
SCHEMA_VERSION = 8

#: The chronological split. Disjoint, ordered, and no season used twice.
#:
#:   train   2023-24
#:   select  2024-25
#:   test    2025-26  — reported once; nothing has ever been fitted, swept or
#:                      rejected against it
#:
#: Exactly one season later than the split it replaces (train 2022-23 + 2023-24,
#: select 2023-24, report 2024-25). The shift buys three separate things.
#:
#: 1. DEFCON becomes measurable at all. 2025-26 is the only season in the
#:    archive carrying `defensive_contribution`; in every earlier season
#:    `defcon90_td` is identically zero. Ablated on 2025-26 — thresholds
#:    neutralised, which is the only ablation that still means "no DEFCON" now
#:    the rate is shrunk toward a positional prior — it is worth +3.4 legal-XI
#:    points per gameweek (49.3 with, 45.9 without), +0.90 captain points and
#:    +2.7pp captain accuracy, while making MAE 0.014 WORSE. The same ablation
#:    on 2024-25 moves the legal XI by 0.1 (50.6 against 50.7) in the WRONG
#:    direction: since heuristic-0.5 the term no longer switches itself off on a
#:    season with no DEFCON column, it falls back to `F.DEFCON_PRIOR` and awards
#:    a small fabricated contribution. Reported rather than smoothed — see the
#:    note on `_player_inputs`.
#: 2. The reporting season stops being one that selection had already touched.
#:    T-12's clamp sweep was fitted on 2023-24, but `projection.py`'s two
#:    rejected p_start corrections were measured on 2023-24 AND 2024-25 — so
#:    "reported on a season selection never saw" was true of one piece of
#:    parameter work and not of the other. Demoting 2024-25 to the selection
#:    season makes the claim true rather than nearly true.
#: 3. 2022-23 leaves the split, which the freed 2024-25 slot makes free. See
#:    `SEASON_SPLIT["excluded"]` — the reason is a measurement, not a preference.
#:
#: What the move does NOT buy, recorded here because a season change is exactly
#: where a project starts quoting only the half that improved. RE-MEASURED at
#: heuristic-0.6 (A18), which is why these are not the figures earlier revisions
#: of this comment carried. At h=1, legal XI, model against the naive baseline:
#: 2024-25 is 53.6 vs 51.3, a win by 2.3; 2025-26 is 50.1 vs 44.8, a win by 5.3,
#: and the model leads at all six horizons on both seasons (2025-26: +5.3, +2.8,
#: +3.9, +6.3, +2.3, +1.8). Captaincy still flips the other way on the test
#: season — 5.89 vs 5.97 — where 2024-25 is 8.92 vs 8.00. The naive baseline
#: STILL beats the model on MAE (1.114 vs 1.075) and rank correlation (0.626 vs
#: 0.692) on the test season, though A18 closed most of that gap: before it the
#: same comparison was 1.539 vs 1.075 and 0.455 vs 0.692.
#:
#: At heuristic-0.5 this paragraph read 50.6 vs 51.7 on 2024-25 (a LOSS by 1.1)
#: and 49.3 vs 44.6 on 2025-26. Two things moved between then and now and only
#: one of them is A18: re-running the identical harness at HEAD before A18 gives
#: 51.8 and 49.7, not 50.6 and 49.3. That ~1-point drift is NOT explained here
#: and is the honest reason to distrust the last decimal of any figure in this
#: block; the A18 deltas above are before-and-after of the SAME run and do not
#: inherit it.
#:
#: NOT re-measured at heuristic-0.6: the DEFCON ablation figures in point 1
#: below, and the heuristic-0.4 comparison two paragraphs down. Both are carried
#: forward from heuristic-0.5 and are stamped as such rather than silently
#: reprinted as current.
#:
#: The DEFCON and split figures in this block were measured at
#: `projection.MODEL_VERSION == "heuristic-0.5"`. G-L/G-M/G-P was landing DEFCON
#: shrinkage and xA calibration into that same version while this was measured:
#: under heuristic-0.4 the identical run gave 48.9 / 5.47 / rank corr 0.450 /
#: MAE 1.600. The direction of every conclusion survived the bump; the third
#: decimal did not, which is why the stamp is here rather than the reader's
#: assumption.
#:
#: `gaffer.fitting` carries its own TRAIN_SEASON / VALIDATION_SEASON /
#: TEST_SEASON triple, still holding the pre-G-N values, and its
#: `EXCLUDED_SEASONS` still explains 2022-23's exclusion by the absence of a
#: 2021-22 file that has since been fetched. That module was out of scope for
#: this change; the two are now inconsistent and THIS block is the intended
#: source of truth.
SEASON_SPLIT = {
    "train": ("2023-24",),
    "select": "2024-25",
    "test": "2025-26",
    #: The projection these figures were taken from. Anything that moves
    #: `projection.MODEL_VERSION` invalidates them, and
    #: `test_this_module_moved_nothing_it_measures` is the forcing function that
    #: makes a version bump re-open this block rather than quietly outdate it.
    "measured_at_model_version": "heuristic-0.6",
    "excluded": {
        "2021-22": "Predates FPL's `expected_goals` / `expected_assists` / "
                   "`starts` columns entirely. On disk only as 2022-23's prior "
                   "source, which since G-Q has exactly one caller — "
                   "`xp_leakage_diagnostic`.",
        "2022-23": "G-Q. Its `expected_goals` and `expected_assists` are "
                   "identically ZERO for GW1-15: the first gameweek carrying "
                   "any xG at all is 16, and the covered window holds 64.2% of "
                   "the season's minutes. The season-wide goals/xG of 1.419 is "
                   "entirely that gap — over GW16-38 alone it is 0.913, i.e. "
                   "ordinary, and assists behave the same way (A/xA 2.111 "
                   "season-wide, 1.357 over the covered window, against ~1.37 "
                   "in every other season). So the column is not mis-scaled, it "
                   "is 40% absent. Compounding it, the 2021-22 prior cannot "
                   "report xG either, so `base_xg90 > 0` holds for 0.0% of "
                   "2022-23 players against 35-39% elsewhere. Measured "
                   "end-to-end, the model scores h=1 rank correlation -0.050 on "
                   "that season, a legal XI of 26.8 pts/gw against the naive "
                   "baseline's 48.2, and 8.1% captain accuracy. That is not a "
                   "weak season. It is the model running on inputs that are not "
                   "there.",
    },
    #: The alternatives to dropping it, both measured before it was dropped.
    "rejected_fixes": {
        "rescale 2022-23's xG": "The only rescaling the data supports is to "
            "recompute the rate over the window the archive actually covers "
            "(GW16-38) rather than over 38 gameweeks — a flat multiplier cannot "
            "work, because zero times anything is still zero. It does fix the "
            "level: 2023-24's mean `base_xg90` moves 0.1074 -> 0.1609, into "
            "line with 2024-25's 0.1674. It does not pay for itself. On "
            "2023-24: rank correlation 0.4267 -> 0.4316 and captain points 5.45 "
            "-> 5.63, but MAE 1.5010 -> 1.5179 and the legal XI 46.9 -> 46.1. "
            "The decision metric — the one that decides things — got WORSE. "
            "Rejected on the measurement, and it would additionally have needed "
            "a per-season special case wired into "
            "`histdata._prior_season_baseline`.",
        "keep it with a caveat": "A caveat describes a limitation. A rank "
            "correlation of -0.050 is not a limitation of a season, it is a "
            "model reading a column that does not exist. Training on it teaches "
            "the shape of the gap.",
    },
    #: Dropping 2022-23 from the split does NOT remove it from the causal chain:
    #: 2023-24's `base_xg90` / `base_xa90` are still read from its totals, and
    #: are therefore still ~33% low. That residual was measured and left alone —
    #: it is precisely the `rescale` row above, and correcting it costs 0.8
    #: legal-XI points on 2023-24. The test season is untouched by any of this:
    #: 2025-26's priors come from 2024-25, which is clean.
    "residual": "2023-24's attacking priors still come from the degraded "
                "2022-23 file. Correcting them was measured and made the "
                "decision metric worse; see rejected_fixes.",
}

#: The season every published figure in this artifact was measured on. Derived,
#: so it cannot drift from the split above.
TEST_SEASON = SEASON_SPLIT["test"]
#: Decision gameweeks evaluated. GW1 has no season-to-date history, so the model
#: runs on its prior/price path there — which is exactly the live GW1 regime and
#: is therefore included rather than skipped.
#:
#: It really is 1. This constant read 2 for five schema versions while the
#: comment above said otherwise, and the code won: every number the project has
#: ever quoted about itself came from an evaluation that skipped the pre-season
#: decision entirely. Everything downstream handles GW1 without special-casing —
#: `shift(1)` makes all season-to-date features 0, `team_form_ratings` finds no
#: played matches and shrinks fully to its prior, and `team_xgc_to_date` returns
#: nothing and falls back to the league mean. That is not a degraded path, it is
#: the pre-season path, which is the point.
FIRST_DECISION_GW = 1
HORIZONS = (1, 2, 3, 4, 5, 6)
SQUAD_SIZE = 15
BUDGET = 1000  # tenths of a million, as the API reports prices
CLUB_LIMIT = 3
QUOTA = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}


# ---------------------------------------------------------------------------
# The production projection, driven from historical rows
# ---------------------------------------------------------------------------

def _player_inputs(row: Any) -> dict[str, Any]:
    """Map one pre-deadline feature row onto the model's player dict.

    Only season-to-date (shift(1)) aggregates and known-in-advance fields. The
    keys are exactly what the live pipeline passes.
    """
    return {
        "position": row.pos,
        "minutes": float(row.min_td),
        "starts": float(row.starts_td),
        "base_minutes": float(row.base_minutes),
        "base_starts": float(row.base_starts),
        "base_xg90": float(row.base_xg90),
        "base_xa90": float(row.base_xa90),
        # G-L. The projection reads a prior-season DEFCON baseline, so this
        # module has to pass one or it is not scoring the shipped model. It went
        # missing silently for exactly the reason the parity test below now
        # guards against: `projection._rate` answers 0.0 for an absent column, so
        # every backtested player fell to the "no prior season recorded" branch
        # and a positional average, and nothing raised. `histdata` had been
        # computing the column all along.
        #
        # Zero is not a measurement here and does not need to be filtered: a
        # season whose prior file predates `defensive_contribution` yields 0.0
        # and `histdata` also zeroes it for samples under BASE_SAMPLE_MINUTES,
        # and `fixture_rates` reads both as "not recorded" — the same branch it
        # takes live against an un-backfilled database.
        "base_defcon90": float(getattr(row, "base_defcon90", 0.0) or 0.0),
        # Provenance, so the backtest takes the same zero-vs-missing branch the
        # live projection takes rather than a more forgiving one.
        "base_season": getattr(row, "base_season", "") or "",
        "price": float(row.value),
        "xg_per_90": float(row.xg90_td),
        "xa_per_90": float(row.xa90_td),
        "defcon_per_90": float(row.defcon90_td),
        "team_id": int(row.team_id) if not pd.isna(row.team_id) else 0,
    }


def _recency_before(
    df: pd.DataFrame, decision_gw: int, last_n: int = 3,
) -> dict[int, dict[str, float]]:
    """Per-player start recency from STRICTLY PRIOR gameweeks.

    2A.2, and the leakage boundary is the whole point: the decision is taken
    before ``decision_gw`` kicks off, so only completed fixtures may be read.
    ``df["GW"] < decision_gw`` is that boundary and nothing here relaxes it.

    Mirrors ``features.start_recency_by_player``, which does the same job
    against the live ``player_gw`` table. Two implementations of one definition
    is a risk the parity test below is there to hold down; they are kept
    separate because one reads a DataFrame of a finished season and the other a
    SQLite table of a live one.
    """
    prior = df[df["GW"] < decision_gw]
    if prior.empty or "starts" not in prior.columns:
        return {}
    out: dict[int, dict[str, float]] = {}
    grouped = prior.sort_values(["GW"]).groupby("element")["starts"]
    for element, seq in grouped:
        vals = [float(v) for v in seq.tolist() if v is not None and not pd.isna(v)]
        if not vals:
            continue
        tail = vals[-last_n:]
        out[int(element)] = {
            "started_lag": vals[-1],
            "start_rate_r3": sum(tail) / len(tail),
        }
    return out


def project_rows(
    frame: pd.DataFrame, ctx: F.TeamContext,
    fixtures_played: Mapping[int, int] | int,
    recency: Mapping[int, dict[str, float]] | None = None,
) -> pd.Series:
    """Run the real projection over historical fixtures.

    A player with two fixtures in a gameweek (DGW) is summed; a player with none
    (BGW) never reaches here and scores zero by construction — the same shape
    ``projection.project`` produces live.

    ``fixtures_played`` is per team, because that is the denominator the shipped
    model uses for start probability. An int is accepted for single-team callers
    (the parity test), where a mapping would be noise.
    """
    out = np.zeros(len(frame))
    for i, row in enumerate(frame.itertuples(index=False)):
        player = _player_inputs(row)
        fx = F.Fixture(
            gw=int(row.GW), opponent_id=int(row.opponent_team),
            at_home=bool(row.was_home), fdr=3,
        )
        # Availability: the historical dataset carries no status/chance column,
        # so every player resolves to the model's "available" branch. This is a
        # documented limitation, not a silent substitution — see `limitations`.
        avail = projection._availability("a", None)
        played = (fixtures_played if isinstance(fixtures_played, int)
                  else fixtures_played.get(player["team_id"], 0))
        # 2A -- the backtest must score the model that SHIPS. `p_start` now
        # reads per-fixture recency, so omitting it here would grade a model
        # nobody runs, which is the cardinality failure this project has already
        # paid for once.
        rec = None if recency is None else recency.get(int(row.element))
        parts = projection._project_one_fixture(
            player, fx, ctx, avail, played, rec)
        out[i] = parts["exp_points"]
    return pd.Series(out, index=frame.index)


def _context_for(
    hist: histdata.SeasonHistory, decision_gw: int, *, season_end_ratings: bool = False
) -> F.TeamContext:
    """A real TeamContext built from information available before ``decision_gw``.

    ``season_end_ratings=True`` restores the Batch 2 behaviour (the dataset's
    end-of-season snapshot applied to every gameweek) so the two can be compared
    directly. It is a leak and is never the default.
    """
    r = (hist.team_ratings() if season_end_ratings
         else hist.team_form_ratings(decision_gw))
    return F.TeamContext.from_ratings(
        att_home=r["att_home"], att_away=r["att_away"],
        def_home=r["def_home"], def_away=r["def_away"],
        team_xgc=hist.team_xgc_to_date(decision_gw),
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _mae(pred: pd.Series, actual: pd.Series) -> float:
    return float((pred - actual).abs().mean())


def _rank_corr(df: pd.DataFrame, col: str, target: str = "actual") -> float:
    corrs = []
    for _, grp in df.groupby("target_gw"):
        if len(grp) >= 10 and grp[col].std() > 0:
            c = grp[col].rank().corr(grp[target].rank())
            if pd.notna(c):
                corrs.append(c)
    return float(np.mean(corrs)) if corrs else 0.0


def _calibration(df: pd.DataFrame, col: str, bins: int = 8) -> list[dict[str, float]]:
    d = df[[col, "actual"]].dropna()
    if len(d) < bins * 5 or d[col].std() == 0:
        return []
    try:
        d = d.assign(_b=pd.qcut(d[col], bins, duplicates="drop"))
    except ValueError:
        return []
    out = []
    for _, g in d.groupby("_b", observed=True):
        out.append({
            "pred": round(float(g[col].mean()), 2),
            "actual": round(float(g["actual"].mean()), 2),
            "haul_rate": round(float((g["actual"] >= 10).mean()) * 100, 1),
            "n": int(len(g)),
        })
    return sorted(out, key=lambda x: x["pred"])


def _calibration_by_position(df: pd.DataFrame, col: str) -> dict[str, list]:
    return {
        pos: _calibration(grp, col, bins=5)
        for pos, grp in df.groupby("pos") if len(grp) >= 40
    }


# ---------------------------------------------------------------------------
# The minutes model (A11)
# ---------------------------------------------------------------------------
#
# `p_start` gates everything above it. It scales every rate in `fixture_rates`,
# it is the `rotation_scale` input that discounts FPL's own `ep_next`, it is
# what `model.simulate` and `model.scenarios` sample against, and it is what the
# NAILED / ROTATION / CAMEO? badge on the player page reports. Until schema 8 it
# was the only major component of the projection with no measured error rate at
# all: the points model had MAE, rank correlation, a legal-XI decision metric
# and a naive baseline; the thing deciding whether the player is on the pitch
# had none of them, and every number that depended on it was published anyway.
#
# The target is FPL's own `starts` column — 1 when the player was in the
# starting XI. It is a post-match field and appears in
# `leakage.POST_MATCH_FIELDS` for that reason; it is legal here because it is
# the evaluation target and is never read back as a feature.
#
# WHY THESE METRICS, since none was mandated:
#
#   Brier, with a skill score.  `p_start` is a probability that multiplies
#     through a rate bundle, not a yes/no call, so the scoring rule has to be
#     proper — one that cannot be improved by shading a forecast. Accuracy at a
#     threshold is not: it would score 0.51 and 0.99 identically on a player who
#     starts, and the difference between those two numbers is the difference
#     between a projection of 2.1 points and 4.1. The skill score is not
#     optional decoration; the raw Brier of a rare-event population is
#     uninterpretable, and it is exactly what let the price-prior arm look
#     respectable for three seasons (0.09 — against 0.02 for saying nothing).
#
#   A calibration curve, twice.  The most actionable form the answer takes: not
#     "the model is wrong by X" but "when it says 0.90, he starts 86% of the
#     time". Reported over the whole player list AND over the most-owned 250,
#     because the two disagree in opposite directions and only the second is
#     about a decision anyone makes.
#
#   The three shipped badge bands.  The badge is the interface. Measuring the
#     probability without measuring the label would leave the one number the
#     user actually reads unscored.
#
#   MAE of expected minutes.  `exp_minutes` is what the rate bundle multiplies,
#     not `p_start`, so a calibration fix that left the minutes wrong would be a
#     fix to the wrong quantity.
#
#   AUC, in support.  It separates "mis-calibrated but correctly ordered", which
#     a recalibration fixes, from "cannot tell these players apart", which needs
#     a feature the model does not have.
#
# The unit is a FIXTURE, not a gameweek. `p_start` is a per-match property —
# `projection.py` takes the MAX across a double rather than summing it — so a
# double gameweek is two chances to be picked and is scored as two rows.


#: The badge bands, exactly as `model.rationale.xmins_badge` cuts them. That
#: function owns the thresholds and is not edited from here; this is a reporting
#: mirror, and `tests/test_backtest_minutes.py` fails if the two ever disagree.
START_BANDS: tuple[tuple[str, float, float], ...] = (
    ("NAILED", 0.85, 1.01),
    ("ROTATION", 0.60, 0.85),
    ("CAMEO?", -0.01, 0.60),
)

#: The population cut that turns the calibration table from a curiosity into a
#: number worth acting on.
#:
#: Every band is reported twice, and the two disagree in opposite directions.
#: Over the whole registered player list the CAMEO? band looks acceptable,
#: because most of that list is third-choice goalkeepers and academy names who
#: genuinely never play. They are trivially easy to call, there are thousands of
#: them, and they carry the aggregate. Nobody picks them. The rows a manager
#: actually decides between are the ones with ownership, and conditioning on
#: that flips the CAMEO? error from over-prediction to under-prediction — in
#: every season measured, and in the live gameweek audited below. A calibration
#: curve averaged over a population the reader will never choose from is a curve
#: about somebody else's problem.
#:
#: 250 is roughly the pool an FPL manager reads: 20 clubs, a first team and the
#: obvious rotation options. Nothing was fitted to it and no threshold was
#: swept; the 400 cut is reported alongside it so the reader can see a gradient
#: rather than a cliff at one chosen number.
CONSIDERED_OWNERSHIP_RANK = 250

#: The naive baselines. All three are strictly prior-gameweek quantities.
#:
#: The first is deliberately almost the model itself: the shipped gate's primary
#: arm IS `starts / fixtures_played`. Where the model takes that arm the two
#: agree by construction, so the gap between them measures exactly one thing —
#: what the OTHER two arms cost. That is the whole point of choosing it.
MINUTES_BASELINES = {
    "start_rate_td": "season-to-date starts over the team's completed fixtures. "
                     "The shipped gate's own primary arm, applied to everyone "
                     "rather than only to the players it lets through.",
    "started_lag": "did he start his last fixture — 1 or 0. The crudest "
                   "available answer, and the one a manager gives from memory.",
    "start_rate_r3": "the share of his last three fixtures he started. A "
                     "four-valued recency estimate: 0, 1/3, 2/3, 1.",
}

#: Columns the minutes evaluation reads, checked against the leakage contract on
#: every run the way `histdata.FEATURE_COLUMNS` is. `starts` and `minutes` are
#: deliberately absent: they are the targets.
MINUTES_FEATURE_COLUMNS = (
    "min_td", "starts_td", "base_minutes", "base_starts", "base_season",
    "value", "position", "team_id", "selected",
    "start_rate_td", "started_lag", "start_rate_r3", "mins_avg_td",
)

MINUTES_LIMITATIONS = [
    "Availability is never varied. The archive carries no `status` or "
    "`chance_of_playing_next_round` column, so every historical player resolves "
    "to the model's available branch and `p_start` is measured with its "
    "availability multiplier pinned at 1.0. This is the interesting half of the "
    "error and it is NOT in these numbers: a player who is injured, suspended "
    "or unregistered is exactly the case the badge gets catastrophically wrong, "
    "and the archive cannot see it. The live audit below can, and does.",
    "`starts` is the target, so a player who was in the squad and did not make "
    "the matchday eighteen scores the same 0 as one who was omitted for "
    "rotation. The model does not distinguish them either, which is the point "
    "of the `price_prior` finding, but it means a 'wrong' call here can be an "
    "unavailability the model was never told about.",
    "Team strength ratings are rebuilt per gameweek from completed matches, "
    "which is not how the live pipeline builds them. `p_start` does not read "
    "them at all, so this affects `exp_minutes` only through nothing — it is "
    "recorded for consistency with the points block rather than because it "
    "bites here.",
    "One season, one league, 38 gameweeks. The branch decomposition reproduces "
    "on 2023-24 and 2024-25 (train and select) and the direction of every "
    "finding holds there, but the headline figures are 2025-26 alone.",
    "This block measured, and A18 then acted on it — so unlike every earlier "
    "revision, these figures are NOT of the model the first measurement was "
    "taken from. `p_start` no longer requires current-season minutes before it "
    "will believe a season-to-date zero. See `candidate_fix`, which records "
    "the before and after, the points backtest run behind it, and the larger "
    "variant measured at the same time and refused.",
    "The largest remaining error is the one this block can least see. "
    "Availability is pinned at 1.0 throughout, and `p_start` now answers "
    "exactly 0.00 for any player whose team has completed three fixtures he "
    "has not featured in. On the archive that is right about 99% of the time, "
    "but it is an absolute statement, so the ~1% it is wrong about are wrong "
    "by the whole probability. Live, a returning long-term absentee is exactly "
    "that case, and the status column the archive does not have is what should "
    "rescue him.",
]

MINUTES_VERDICT = (
    "Measured, fixed, and now winning. Two readings of this block found the "
    "shipped `p_start` behind every naive baseline at h=1 -- worded that way "
    "deliberately, because a past-tense claim about a defeat reads to a "
    "keyword check exactly like a present one, and the artifact contract "
    "compares this prose against the table below it. The first found "
    "a third of every row taking its answer from a PRICE prior -- told a ~29% "
    "chance of starting, starting 2.3% of the time -- and A18 removed the gate "
    "that sent them there. The second found what remained: the gate below it, "
    "`fixtures_played >= 3`, made the current season INVISIBLE until a team had "
    "played three fixtures, so at GW1-GW3 every player in the game was graded "
    "on last season. On 2026-09-01 that published a NAILED badge and a 0.90 "
    "start probability for a player with 0 starts and 11 minutes, while six "
    "ever-presents were flagged as rotation risks -- and at GW4 the ranking "
    "inverted on no new information beyond a counter reaching three. "
    "Phase 2A replaced the gate with SHRINKAGE (the current season enters from "
    "the first fixture, weighted by how much of it there is) plus RECENCY (the "
    "last-three start share, and whether he started his last fixture) -- the "
    "estimator this block had been reporting as the winner for two revisions "
    "while the model ignored it. "
    "It now beats every baseline at every horizon on both Brier and AUC. At "
    "h=1: Brier 0.086 against 0.099 for the three-game rolling rate, 0.110 for "
    "season-to-date and 0.112 for a lagged start; AUC 0.937 against 0.907. At "
    "h=6 the margin narrows to 0.134 against 0.135 and the honest reading is "
    "that the baselines converge on it there, not that it pulls away. The "
    "price-prior arm is 1.3% of rows, from 4.1% and originally 36.3%, and 97% "
    "of rows now take a recency-informed branch with a skill score above 0.45. "
    "Two things it still does NOT do. `exp_minutes` MAE is 14.3 against 11.5 "
    "for a lagged start times ninety, so the MINUTES estimate still loses to a "
    "one-line rule even though the start PROBABILITY no longer does -- the "
    "bimodal ~78'-or-~3' shape is untouched and is the next thing to attack. "
    "And NAILED remains over-confident: 0.939 claimed against 0.884 realised."
)

#: A18 — the variant that was measured alongside the shipped fix and REFUSED.
#: Phase 2 Release B -- MEASURED AND REFUSED, 2026-09-02.
#:
#: `CLEAN_SHEET_CONTRADICTION` records that the projection holds two estimates
#: of one quantity -- how many goals the opposition scores -- and that the
#: bottom-up one (summed opposing `exp_goals`) is the better clean-sheet
#: forecaster in all three archive seasons, better calibrated above 0.35, while
#: what ships is barely distinguishable from quoting the league base rate.
#: A19 left it unfixed because reconciling needs a two-pass projection.
#:
#: The two-pass projection was BUILT: accumulate every side's attacking lambda,
#: then read `p_cs` off the opponent's entry, with the same pass added to the
#: backtest so it scored what would ship. The lambdas were checked as physically
#: sensible first -- median 1.36, mean 1.50 goals a side, against a real Premier
#: League average near 1.4 -- so what follows is a result and not a bug.
#:
#:     season           points MAE h=1        XI points per gameweek
#:                      Rel A -> Rel B        Rel A -> Rel B
#:     2023-24 train    1.0190 -> 1.0170      49.9 -> 49.1   (-0.8)
#:     2024-25 select   1.1340 -> 1.1140      55.9 -> 56.0   (+0.1)
#:     2025-26 TEST     1.0480 -> 1.0340      51.4 -> 50.2   (-1.2)
#:
#: Points MAE improved in ALL THREE seasons. The decision metric did not: it
#: lost on two of three including the held-out one, and the pattern across
#: seasons (-0.8, +0.1, -1.2) is the non-replication signature. The
#: pre-registered rule required XI points not to fall on the test season.
#: REFUSED, and reverted.
#:
#: This is the THIRD time in this codebase that a better input has not been a
#: better decision -- after E2 of the crossover programme and 2A.4's
#: conditional minutes -- and it is now a pattern rather than an anecdote. An
#: estimate that is closer on average across 600 players can still move the
#: fifteen a squad is built from the wrong way, because a squad is a selection
#: at the tail and MAE is a statement about the middle.
#:
#: WHAT THIS REFUSAL COSTS, stated rather than glossed: the contradiction stays.
#: `model.scenarios` still cannot be exact against both lambdas and still says
#: so, `p_cs` is still barely better than the base rate, and it is still
#: over-confident above 0.35. Refusing the fix does not make the defect go
#: away; it means the fix measured worse than the defect on the metric that
#: decides.
#:
#: One observation that is NOT a reason to ship it and IS a reason to look
#: again: captain points rose 5.92 -> 6.84 on the test season while the XI fell,
#: so XI + captain is nearly flat (57.3 -> 57.0). `xi_points_per_gw` excludes
#: the armband, and the real gameweek score does not. That may make it an
#: incomplete decision metric -- but changing the metric after seeing the
#: result is what invalidates a pre-registration, so it is recorded as a
#: question for a future revisit rather than used to reverse this one.
#:
#: TO REVISIT, pre-register BOTH first: a decision metric that includes the
#: armband, and a paired test over gameweeks rather than a comparison of means.
#: A different blend weight between the two lambdas is not a different
#: hypothesis.
CLEAN_SHEET_RECONCILIATION_REFUSED = {
    "candidate": "two_pass_p_cs_from_opponent_attack_sum",
    "decision": "measured, REFUSED",
    "measured_on": "2026-09-02",
    "change": ("accumulate each side's attacking lambda in a first pass, then "
               "read `p_cs` from the opponent's entry instead of from "
               "`ctx.expected_conceded`"),
    "rule": ("pre-registered: ship only if clean-sheet Brier improves AND "
             "points MAE does not worsen AND XI points per gameweek does not "
             "fall, on the test season"),
    "points_mae_h1": {
        "2023-24": {"rel_a": 1.0190, "rel_b": 1.0170},
        "2024-25": {"rel_a": 1.1340, "rel_b": 1.1140},
        "2025-26": {"rel_a": 1.0480, "rel_b": 1.0340},
    },
    "xi_points_per_gw": {
        "2023-24": {"rel_a": 49.9, "rel_b": 49.1},
        "2024-25": {"rel_a": 55.9, "rel_b": 56.0},
        "2025-26": {"rel_a": 51.4, "rel_b": 50.2},
    },
    "captain_points_per_gw": {
        "2025-26": {"rel_a": 5.92, "rel_b": 6.84},
    },
    "lambda_sanity": ("accumulated team attack lambdas: median 1.36, mean 1.50, "
                      "range 0.60-3.51 -- physically sensible, so this is a "
                      "result and not a bug"),
    "refused_because": (
        "points MAE improved in all three seasons and the DECISION metric lost "
        "on two of three including the held-out one. -0.8, +0.1, -1.2 is the "
        "non-replication signature."),
    "cost_of_refusing": (
        "the contradiction stays: two lambdas, a scenario engine that cannot be "
        "exact against both and says so, and a `p_cs` barely better than the "
        "league base rate and over-confident above 0.35."),
    "open_question": (
        "captain points rose 5.92 -> 6.84 while the XI fell, so XI + captain is "
        "nearly flat. `xi_points_per_gw` excludes the armband and a real "
        "gameweek score does not. Pre-register a metric that includes it, and a "
        "paired test over gameweeks, BEFORE revisiting."),
}


#: Phase 2A.4 -- MEASURED AND REFUSED, 2026-09-02.
#:
#: After Release A the START PROBABILITY beats every baseline at every horizon.
#: The MINUTES estimate does not: MAE 14.3 against 11.5 for a lagged start times
#: ninety. The bimodal shape is the obvious suspect --
#: ``p_start * START_MINUTES + cameo * CAMEO_MINUTES`` with both constants
#: global, so every player is either about 78 minutes or about 3 and a reliable
#: 60-minute player is in neither mode. Gaffer's own crossover programme named
#: this in 2026-08, and OpenFPL's published method uses the two-stage
#: alternative: appearance probability, then minutes CONDITIONAL on appearing.
#:
#: Built, and measured at all three levels the plan mandates, because a minutes
#: model can be mechanically better while making worse decisions and MAE on
#: minutes is not the objective:
#:
#:     level                       shipped   candidate
#:     exp_minutes MAE h=1          14.31      13.83     better
#:     points MAE h=1                1.048      1.046     level
#:     XI points per gameweek        51.4       51.0      WORSE
#:
#: The pre-registered rule required all three, and the third is the one that
#: decides. REFUSED, and reverted.
#:
#: This is the exact failure the three-level rule was written to catch, and it
#: is the second time this project has seen it: E2 of the crossover programme
#: demonstrated a better INPUT that did not become a better DECISION. A minutes
#: estimate half a minute closer on average, spread across 626 players, moved
#: the fifteen a squad is built from the wrong way.
#:
#: Honest caveat, recorded rather than used to soften the result: 0.4 points a
#: gameweek over 38 gameweeks is not a large margin and could be noise. The
#: rule was fixed before the number was read, and a rule that bends when the
#: result is close is not a rule. If it is revisited, the revisit needs a
#: paired test over gameweeks, not a re-reading of these means.
#:
#: DO NOT REOPEN by tuning MINUTES_SHRINK_N or the positional priors. A
#: different constant is not a different hypothesis. What would be: a
#: conditional-minutes model that also knows about substitution risk and game
#: state, which is a different object from a shrunk mean.
MINUTES_SHAPE_REFUSED = {
    "candidate": "two_stage_conditional_minutes",
    "decision": "measured, REFUSED",
    "measured_on": "2026-09-02",
    "change": ("replace the global START_MINUTES / CAMEO_MINUTES constants with "
               "the player's own mean minutes when he starts and when he comes "
               "on, each shrunk toward the constant it replaces"),
    "rule": ("pre-registered: ship only if exp_minutes MAE improves on the test "
             "season AND points MAE does not worsen AND XI points per gameweek "
             "does not fall"),
    "test_season": {
        "exp_minutes_mae": {"shipped": 14.31, "candidate": 13.83},
        "points_mae_h1": {"shipped": 1.048, "candidate": 1.046},
        "xi_points_per_gw": {"shipped": 51.4, "candidate": 51.0},
    },
    "exp_minutes_mae_by_season": {
        "2023-24": {"shipped": 15.195, "candidate": 14.765},
        "2024-25": {"shipped": 15.678, "candidate": 15.236},
        "2025-26": {"shipped": 14.701, "candidate": 14.235},
    },
    "refused_because": (
        "it fails the decision metric. The minutes estimate improved in all "
        "three seasons and the XI it produces got worse, which is the trade "
        "the three-level rule exists to catch."),
    "caveat": (
        "0.4 points a gameweek is not a large margin and could be noise. The "
        "rule was fixed before the number was read; a revisit needs a paired "
        "test over gameweeks, not a re-reading of these means."),
    "still_true": (
        "exp_minutes remains the weakest published quantity: 14.3 MAE against "
        "11.5 for a lagged start times ninety. The bimodal shape is untouched "
        "and the loss to a one-line rule is real and unfixed."),
    "harness": "scripts/run_minutes_shape.py",
}


#: Phase 1.6 -- MEASURED AND REFUSED, 2026-09-01.
#:
#: The shipped objective picks the starting XI on ``players[i].value``, a
#: horizon-decayed six-gameweek sum, while picking the captain on
#: ``next_gw_points``. The line above the captain term states the reason it must
#: be one week -- "Captaincy is re-chosen every week, so double on *next-GW*
#: points, not horizon" -- and the identical argument was never applied to the
#: eleven beside it. The roadmap carried this as a SCOPE defect worth about two
#: points a week, blocked because ``_decision_metrics`` runs only at h=1 and
#: picks squad AND XI on the same one-week column, so the harness could not see
#: the shipped behaviour at all.
#:
#: The harness gap is now closed (``scripts/run_xi_horizon.py`` reconstructs the
#: decayed column the solver actually uses) and the change was measured before
#: it was written. Squad selection is held fixed on the decayed value in both
#: arms; only the XI column moves.
#:
#: Pre-registered decision rule, fixed before any result was read: ship only if
#: the proposal wins on the TEST season and does not lose on either other one.
#:
#:     season              shipped   proposed   delta
#:     2023-24  train       49.684     49.974   +0.290
#:     2024-25  select      49.000     49.816   +0.816
#:     2025-26  TEST        47.368     47.211   -0.157
#:
#: It loses on the held-out season, so it is REFUSED. Two seasons agreeing and
#: the test season reversing is the same signature that killed market-derived
#: team strength, and the roadmap's "~2 pts/wk" was an intuition: the best case
#: measured is +0.8 and the honest case is negative.
#:
#: The likely mechanism, and it is a correction to the premise rather than to
#: the code: a decayed six-gameweek sum is a SHRUNK estimate of one gameweek.
#: It is noisier week to week than the horizon it averages, so selecting on it
#: buys variance reduction that outweighs the loss of specificity. "You re-pick
#: the XI every week for free, so pick it on this week" is true about the
#: DECISION and false about the ESTIMATOR.
#:
#: The Scope concern is real and survives the refusal. It is resolved by
#: DISCLOSURE rather than by a change: the XI is selected on the horizon and
#: now says so where it is presented. Stating the domain is what the contract
#: asks for; changing the domain to something measurably worse is not.
#:
#: DO NOT REOPEN without new data or a structurally different hypothesis. A
#: different decay constant is not a different hypothesis.
XI_SELECTION_REFUSED = {
    "candidate": "select_the_xi_on_next_gw_points_rather_than_horizon_value",
    "decision": "measured, REFUSED",
    "measured_on": "2026-09-01",
    "change": ("in `solver.optimize`, weight `start[i]` by `next_gw_points` "
               "instead of the decayed `value`, leaving squad selection and "
               "the captain term unchanged"),
    "rule": ("pre-registered: ship only if it wins on the test season and does "
             "not lose on either of the other two"),
    "xi_points_per_gw": {
        "2023-24": {"role": "train", "shipped": 49.684, "proposed": 49.974,
                    "delta": 0.290},
        "2024-25": {"role": "select", "shipped": 49.000, "proposed": 49.816,
                    "delta": 0.816},
        "2025-26": {"role": "test", "shipped": 47.368, "proposed": 47.211,
                    "delta": -0.157},
    },
    "refused_because": (
        "it loses on the held-out season. Two seasons agreeing while the test "
        "season reverses is the signature of a result that does not replicate, "
        "and it is the same shape that refuted market-derived team strength."),
    "mechanism": (
        "a decayed six-gameweek sum is a SHRUNK estimate of one gameweek, so "
        "selecting on it trades specificity for variance reduction and the "
        "trade is favourable. 'The XI is re-picked weekly for free' is true "
        "about the decision and false about the estimator."),
    "roadmap_estimate_was": "~2 points per week — an intuition, not a measurement",
    "scope_concern_resolved_by": (
        "disclosure. The XI is selected on the horizon and now says so where it "
        "is presented; the contract asks for the domain to be stated, not for "
        "it to be changed to something measurably worse."),
    "harness": "scripts/run_xi_horizon.py",
}


#: Recorded the way `WITHDRAWN_BASELINES` is: a measurement that did not ship is
#: still a result, and the next person should find the reason rather than the
#: intuition. Defined first because `MINUTES_CANDIDATE_FIX` carries it.
MINUTES_REFUSED_FIX = {
    "candidate": "believe_a_current_season_zero_from_one_fixture",
    "decision": "measured, REFUSED",
    "change": "as A18, and additionally drop the `fixtures_played >= 3` "
              "minimum, so the current-season arm fires as soon as a team has "
              "completed one fixture.",
    "brier_h1": {
        "2023-24": {"role": "train", "before": 0.1452, "after": 0.1139},
        "2024-25": {"role": "select", "before": 0.1495, "after": 0.1161},
        "2025-26": {"role": "test", "before": 0.1509, "after": 0.1114},
    },
    "better_than_shipped_on": "h=1 Brier in all three seasons, points MAE in "
                              "all three, rank correlation in all three, and "
                              "the legal XI on the selection season (54.9 "
                              "against 53.6).",
    "worse_than_shipped_on": "the legal XI on the train season (46.2 against "
                             "46.6) and captain points there (5.42 against "
                             "5.74). Mixed, on the metric that decides things.",
    "refused_because": "it reads `starts / 1` as a start probability. Checked "
                       "against the live game on the day A18 shipped — 2026-27 "
                       "with one finished gameweek and every team on one or "
                       "two completed fixtures — it sets `p_start` to exactly "
                       "0.00 for 267 of 626 players, including two of the "
                       "most-owned forwards in the game, both flagged "
                       "available and both merely absent for the opening "
                       "weekend. A 38-gameweek Brier over a population that is "
                       "61% never-appears cannot price that: two gameweeks of "
                       "catastrophically wrong calls on the players a manager "
                       "actually owns is a rounding error in it. The `>= 3` "
                       "guard exists precisely to require a sample, dropping "
                       "it was not the change that was filed, and no rationale "
                       "for dropping it was ever recorded.",
    "reopen_if": "the minutes model gains a per-fixture availability signal "
                 "(roadmap M6). The objection is to trusting a one-fixture "
                 "sample, not to believing a zero, and a model that could tell "
                 "an absence from a rotation would not need the sample guard "
                 "to do that work.",
}
#: A18 — SHIPPED. The correction below was measured in this module, recorded as
#: "measured, not shipped", and has since been made in `projection.fixture_rates`
#: at heuristic-0.6. This block is now the before-and-after record rather than a
#: proposal.
#:
#: `fixture_rates` already knew that a zero can be evidence. Its prior-season arm
#: says so at length — "zero starts off a full sample is the strongest bench
#: evidence there is" — and believes a `base_starts` of 0. The current-season arm
#: did the opposite: its gate was `fixtures_played >= 3 and cur_min and ...`, so
#: a player whose team had played eight and who had played none of them failed on
#: `cur_min` and fell through to a price prior that reads an expensive squad
#: player as a probable starter. The lesson had been learned once and applied to
#: one of the two arms. `cur_min` is gone; the three-fixture sample requirement
#: is not.
#:
#: TWO variants were measured and only the smaller one shipped, which matters
#: because the figures this record carried while it was a proposal — 0.1139 /
#: 0.1161 / 0.1114 — are NOT the figures the change it described produces. They
#: belong to the larger variant. Reproduced to four decimal places on all three
#: seasons, so this was a transcription fault in the record and not a difference
#: of opinion about it:
#:
#:                        train             select            test
#:     shipped  (>= 3)    0.1452 -> 0.1185  0.1495 -> 0.1204  0.1509 -> 0.1154
#:     refused  (>= 1)    0.1452 -> 0.1139  0.1495 -> 0.1161  0.1509 -> 0.1114
#:
#: Measured on train and select before the test season was looked at; the test
#: figure is confirmation rather than the finding. See `MINUTES_REFUSED_FIX` for
#: why the better-scoring variant is the one that did not ship.
MINUTES_CANDIDATE_FIX = {
    "candidate": "believe_a_current_season_zero",
    "decision": "SHIPPED at projection heuristic-0.6",
    "change": "in `projection.fixture_rates`, `cur_min` was dropped from the "
              "current-season gate, so a player with a full team sample and "
              "zero starts scores 0 rather than falling through to the price "
              "prior. The `fixtures_played >= 3` sample requirement is "
              "unchanged.",
    "rationale": "the prior-season arm already believed a zero, and said so in "
                 "its own comment. The current-season arm did not. The same "
                 "lesson had been applied to one of the two.",
    #: What actually shipped, measured before and after with one harness.
    "brier_h1": {
        "2023-24": {"role": "train", "before": 0.1452, "after": 0.1185},
        "2024-25": {"role": "select", "before": 0.1495, "after": 0.1204},
        "2025-26": {"role": "test", "before": 0.1509, "after": 0.1154},
    },
    "brier_h1_population": "every h=1 row, GW1 included — the population the "
                           "unshipped record used, kept so the two are "
                           "comparable. The `per_horizon` table drops GW1 and "
                           "any row a baseline cannot score, so its figures "
                           "differ in the fourth decimal: 0.1502 -> 0.1138 on "
                           "the test season.",
    "points_model_effect": {
        "note": "a minutes improvement that degraded points would not be a "
                "win, so the points backtest was re-run behind it rather than "
                "assumed. h=1, before -> after.",
        "mae": {"2023-24": [1.44, 1.082], "2024-25": [1.509, 1.184],
                "2025-26": [1.539, 1.114]},
        "mae_naive": {"2023-24": 1.036, "2024-25": 1.115, "2025-26": 1.075},
        "rank_corr": {"2023-24": [0.436, 0.589], "2024-25": [0.451, 0.593],
                      "2025-26": [0.455, 0.626]},
        "rank_corr_naive": {"2023-24": 0.652, "2024-25": 0.666,
                            "2025-26": 0.692},
        "xi_points_per_gw": {"2023-24": [46.6, 46.6], "2024-25": [51.8, 53.6],
                             "2025-26": [49.7, 50.1]},
        "captain_points_per_gw": {"2023-24": [5.74, 5.74],
                                  "2024-25": [8.89, 8.92],
                                  "2025-26": [5.89, 5.89]},
        "verdict": "MAE and rank correlation improve by a wide margin in all "
                   "three seasons, and the naive baseline's long-standing lead "
                   "on both nearly closes — on the test season MAE goes from "
                   "0.464 behind to 0.039 behind. The decision metrics move "
                   "less: the legal XI gains 1.8 points a gameweek on the "
                   "selection season and 0.4 on the test season, and is "
                   "unmoved on the train season. Captaincy is untouched to two "
                   "decimal places on train and test. Nothing got worse.",
    },
    "what_moved": {
        "season": "2025-26",
        "rows_h1": 29338,
        "rows_moved": 10715,
        "rows_moved_pct": 36.5,
        "players_moved_at_least_once": "471 of 841",
        "direction": "every single delta is NEGATIVE. The fix can only remove "
                     "an invented start probability, never add one.",
        "mean_delta_points": -1.19,
        "median_delta_points": -1.15,
        "largest_delta_points": -6.46,
        "top20_names_changed_per_gw": 0.29,
        "top20_gameweeks_with_any_change": "11 of 38",
        "top50_names_changed_per_gw": 1.1,
        "top200_names_changed_per_gw": 12.7,
        "top20_actual_points_scored": [4.19, 4.27],
        "reading": "the leaderboard a manager reads is almost untouched — one "
                   "name in twenty changes about once every three gameweeks, "
                   "and the top 20 goes on to score slightly MORE afterwards. "
                   "The change lands two hundred players deep, on absentees "
                   "who were being projected at 4-6 points while recording "
                   "zero minutes. That is where a transfer suggestion comes "
                   "from, which is why a near-invisible leaderboard is not the "
                   "same as a small change.",
    },
    "branch_shift": {
        "note": "share of h=1 rows by the arm that produced `p_start`, on the "
                "test season, before -> after.",
        "current_season": [56.4, 92.9],
        "price_prior": [36.3, 4.1],
        "prior_season": [7.3, 3.0],
        "price_prior_start_rate": [0.023, 0.175],
        "price_prior_brier_skill": [-3.0939, -0.0467],
    },
    "residual": "0.5%-1.2% of the moved rows did start. They are now called at "
                "0.00 instead of ~0.29, and that error is real and absolute. "
                "Before the fix the same population was called at ~0.29 and "
                "started 0.4%-1.2% of the time, so the model was wrong about "
                "99% of it. The trade is a large error on a large population "
                "for a total error on a very small one.",
    "superseded_figures": {
        "recorded_while_unshipped": {"2023-24": 0.1139, "2024-25": 0.1161,
                                     "2025-26": 0.1114},
        "belong_to": "believe_a_current_season_zero_from_one_fixture",
        "note": "the record's prose described the shipped change and its "
                "numbers came from the refused one. Both were re-run; the "
                "refused variant reproduces those three figures exactly.",
    },
    "refused_variant": MINUTES_REFUSED_FIX,
    "caveat": "even after the fix the model still loses to `start_rate_r3` at "
              "h=1 (0.1138 against 0.0986). It now BEATS it at h=2 through "
              "h=6, and it is no longer distinguishable from `start_rate_td` "
              "or `started_lag` at h=1 — the paired 95% interval against each "
              "now contains zero, where before it was a loss in 37 gameweeks "
              "out of 37. This closes the gap. It does not make the shipped "
              "gate the best available estimator of the next fixture.",
}


def _brier(pred: Any, actual: Any) -> float:
    """Mean squared error of a probability against a 0/1 outcome."""
    p = np.asarray(pred, dtype=float)
    y = np.asarray(actual, dtype=float)
    if not len(y):
        return float("nan")
    return float(np.mean((p - y) ** 2))


def _brier_skill(pred: Any, actual: Any) -> float:
    """Brier against this population's own base rate. 0 = no skill, <0 = worse.

    The raw score is unreadable alone. A population that is 2% starters scores
    0.022 by answering "no" to everything, so 0.09 on it is four times worse
    than saying nothing — which is what the price prior does, and what the raw
    number hid for three seasons.
    """
    y = np.asarray(actual, dtype=float)
    if not len(y):
        return float("nan")
    ref = _brier(np.full(len(y), float(y.mean())), y)
    if ref <= 0:
        return float("nan")
    return float(1.0 - _brier(pred, y) / ref)


def _auc(pred: Any, actual: Any) -> float:
    """P(a random starter outranks a random non-starter). Calibration-free."""
    y = np.asarray(actual, dtype=int)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    ranks = pd.Series(np.asarray(pred, dtype=float)).rank().to_numpy()
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _start_calibration(
    df: pd.DataFrame, col: str = "p_start", bins: int = 10,
) -> list[dict[str, float]]:
    """Predicted start probability against the realised start rate, by decile.

    Binned on the RANK rather than the value. `p_start` piles up on a handful of
    discrete points — `starts / fixtures_played` for small denominators, and a
    price prior that is a function of price alone — so value-binning collapses
    to three or four bins and hides the shape that matters.
    """
    d = df[[col, "started", "minutes"]].dropna()
    if len(d) < bins * 5 or float(d[col].std()) == 0:
        return []
    try:
        d = d.assign(_b=pd.qcut(d[col].rank(method="first"), bins, duplicates="drop"))
    except ValueError:
        return []
    out = []
    for _, g in d.groupby("_b", observed=True):
        out.append({
            "pred": round(float(g[col].mean()), 3),
            "actual": round(float(g["started"].mean()), 3),
            "appear_rate": round(float((g["minutes"] > 0).mean()), 3),
            "n": int(len(g)),
        })
    return sorted(out, key=lambda x: x["pred"])


def _start_bands(df: pd.DataFrame, col: str = "p_start") -> list[dict[str, Any]]:
    """The three shipped badges, scored on what the badged players then did."""
    out = []
    for name, lo, hi in START_BANDS:
        g = df[(df[col] >= lo) & (df[col] < hi)]
        if g.empty:
            continue
        out.append({
            "band": name,
            "n": int(len(g)),
            "claimed": round(float(g[col].mean()), 3),
            "start_rate": round(float(g["started"].mean()), 3),
            "appear_rate": round(float((g["minutes"] > 0).mean()), 3),
            "exp_minutes": round(float(g["exp_minutes"].mean()), 1),
            "actual_minutes": round(float(g["minutes"].mean()), 1),
        })
    return out


def _minutes_branch(rates: Mapping[str, Any]) -> str:
    """Which evidence produced this ``p_start``.

    2A -- this used to TRANSCRIBE the conditions in `projection.fixture_rates`,
    because that function returned a number and not the branch that made it,
    and its own docstring called the duplication a liability. It was: the gate
    it copied has been replaced by shrinkage plus recency, and a second copy
    would have gone on reporting arms the model no longer has.

    The estimator now names its own branch and this reads it. One definition.
    """
    return str(rates.get("start_branch") or "unknown")


def build_minutes_evaluation(
    season: str = TEST_SEASON, horizons: tuple[int, ...] = HORIZONS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Every start prediction and its outcome, one row per player-fixture.

    `p_start` is computed ONCE per (decision gameweek, player) and joined onto
    every target fixture. That is not a shortcut: the shipped gate reads only
    frozen season-to-date aggregates, the prior season, price, availability and
    the team's completed-fixture count, none of which varies with the opponent,
    the venue or the horizon — so the value really is identical across them.
    `test_p_start_does_not_vary_with_the_fixture` holds that true rather than
    leaving it as an assumption that a later change to `fixture_rates` could
    quietly break.

    Availability is the model's "available" branch throughout, because the
    archive carries no status column. That is the single most important thing to
    know about these numbers and it is first in `MINUTES_LIMITATIONS`.
    """
    hist = histdata.load_season(season)
    df = hist.frame
    leakage.assert_no_leakage(MINUTES_FEATURE_COLUMNS, context="minutes features")
    if "starts" not in df.columns:
        raise histdata.MissingHistoryError(
            f"{season} carries no `starts` column, so there is no start outcome "
            "to score against")
    max_gw = int(df["GW"].max())
    avail = projection._availability("a", None)
    coverage = {"decision_gws": 0, "rows": 0, "skipped_unpriced": 0}
    records: list[dict[str, Any]] = []
    for decision_gw in range(FIRST_DECISION_GW, max_gw + 1):
        snap = df[df["GW"] == decision_gw]
        if snap.empty:
            continue
        feat = snap.drop_duplicates("element").set_index("element")
        ctx = _context_for(hist, decision_gw)
        fixtures_played = hist.team_fixtures_played(decision_gw)
        recency = _recency_before(df, decision_gw)
        # Ownership at the freeze, ranked. `selected` is a pre-deadline field and
        # this is the value from the DECISION snapshot, not the target one.
        own_rank = feat["selected"].rank(ascending=False, method="first")
        # Strictly prior gameweeks. That is the entire leakage policy for the
        # baselines: they may read every completed fixture and nothing else.
        prior = df[df["GW"] < decision_gw]
        if len(prior):
            grouped = prior.sort_values("GW").groupby("element")["starts"]
            last_start = grouped.last()
            rate_r3 = grouped.apply(lambda s: float(s.tail(3).mean()))
        else:
            last_start = pd.Series(dtype=float)
            rate_r3 = pd.Series(dtype=float)
        frozen: dict[int, dict[str, Any]] = {}
        for element, row in feat.iterrows():
            player = _player_inputs(row)
            if not player["position"] or pd.isna(row.get("value")):
                coverage["skipped_unpriced"] += 1
                continue
            played = int(fixtures_played.get(player["team_id"], 0))
            opponent = row.get("opponent_team")
            fx = F.Fixture(
                gw=decision_gw,
                opponent_id=int(opponent) if not pd.isna(opponent) else 1,
                at_home=True, fdr=3,
            )
            rates = projection.fixture_rates(
                player, fx, ctx, avail, played, recency.get(int(element)))
            frozen[int(element)] = {
                "p_start": float(rates["p_start"]),
                "p_play": float(rates["p_play"]),
                "exp_minutes": float(rates["exp_minutes"]),
                "branch": _minutes_branch(rates),
                "pos": player["position"],
                "own_rank": float(own_rank.get(element, np.nan)),
                "start_rate_td": (player["starts"] / played) if played else np.nan,
                "mins_avg_td": (player["minutes"] / played) if played else np.nan,
                "started_lag": float(last_start.get(element, np.nan)),
                "start_rate_r3": float(rate_r3.get(element, np.nan)),
                # 2A.3 -- the gate's own INPUTS, so an alternative estimator can
                # be scored without re-running the projection.
                #
                # The shipped gate is `fixtures_played >= 3`, and it binds only
                # in GW1-3: about 8% of a season. Both candidates ever measured
                # for it were judged on season averages, where 35 of 38
                # gameweeks are indifferent to it, so the regime was diluted out
                # of its own evaluation and the estimator that wins at h=1
                # (recency) was never a candidate for it at all. Exposing these
                # four columns is what makes the GW1-3 regime measurable on its
                # own terms.
                "fixtures_played": played,
                "starts_td": float(player["starts"]),
                "base_starts": float(player["base_starts"]),
                "price": float(row.get("value") or 0.0),
            }
        coverage["decision_gws"] += 1
        for h in horizons:
            target_gw = decision_gw + h - 1
            if target_gw > max_gw:
                continue
            for r in df[df["GW"] == target_gw].itertuples(index=False):
                f = frozen.get(int(r.element))
                if f is None:
                    continue
                records.append({
                    "decision_gw": decision_gw, "target_gw": target_gw,
                    "horizon": h, "element": int(r.element),
                    "started": int(r.starts), "minutes": float(r.minutes), **f,
                })
    if not records:
        raise histdata.MissingHistoryError("no evaluable start rows were produced")
    ev = pd.DataFrame(records)
    coverage["rows"] = int(len(ev))
    return ev, coverage


def _paired_brier_diff(
    sub: pd.DataFrame, model_col: str, base_col: str,
    n_boot: int = 4000, seed: int = 20260831,
) -> dict[str, Any]:
    """Model-minus-baseline Brier, bootstrapped over gameweeks.

    Paired by target gameweek and resampled at that level, because rows within a
    gameweek are not independent — one manager's rotation moves eleven of them
    at once, and resampling rows would give an interval an order of magnitude
    too tight. NEGATIVE is the model winning: lower Brier is better.
    """
    d = sub[[model_col, base_col, "started", "target_gw"]].dropna()
    per_gw = []
    for _, g in d.groupby("target_gw"):
        if len(g) < 10:
            continue
        per_gw.append(_brier(g[model_col], g["started"])
                      - _brier(g[base_col], g["started"]))
    if len(per_gw) < 5:
        return {"gameweeks": len(per_gw), "diff": None,
                "note": "too few gameweeks to pair"}
    arr = np.asarray(per_gw, dtype=float)
    rng = np.random.default_rng(seed)
    draws = arr[rng.integers(0, len(arr), size=(n_boot, len(arr)))].mean(axis=1)
    return {
        "gameweeks": int(len(arr)),
        "diff": round(float(arr.mean()), 4),
        "ci95": [round(float(np.percentile(draws, 2.5)), 4),
                 round(float(np.percentile(draws, 97.5)), 4)],
        "gameweeks_model_better": int((arr < 0).sum()),
        "gameweeks_model_worse": int((arr > 0).sum()),
    }


def live_start_audit(
    outcomes: Mapping[int, Mapping[str, Any]],
    snapshot_path: Path | None = None,
    target_gw: int = 1,
    considered_rank: int = CONSIDERED_OWNERSHIP_RANK,
) -> dict[str, Any]:
    """Score a FROZEN pre-deadline `p_start` snapshot against what happened.

    The archive cannot measure the availability multiplier, because it has no
    status column — and availability is where the badge fails worst. The live
    pipeline writes `data/state/projections.ndjson` with `is_pre_deadline = 1`
    and an `as_of` timestamp before the deadline, so those rows ARE the shipped
    minutes model with availability included, frozen before kickoff. Joining
    them to the finished gameweek is the only honest way to see that half.

    ``outcomes`` maps element id to a mapping with ``starts`` and ``minutes``,
    and optionally ``own_rank``. It is passed in rather than fetched: this module
    does not make network calls, and a backtest that needed one could not run in
    CI. Reproduce it from ``event/{gw}/live/`` and ``bootstrap-static/``.
    """
    path = snapshot_path or (config.DATA_DIR / "state" / "projections.ndjson")
    if not Path(path).exists():
        return {"status": "unavailable",
                "reason": f"no frozen snapshot at {path}"}
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("target_gw") != target_gw or not rec.get("is_pre_deadline"):
            continue
        outcome = outcomes.get(int(rec["player_id"]))
        if outcome is None:
            continue
        rows.append({
            "element": int(rec["player_id"]),
            "p_start": float(rec["p_start"]),
            "exp_minutes": float(rec["exp_minutes"]),
            "availability": float(rec.get("availability", 1.0)),
            "as_of": rec.get("as_of"),
            "started": int(outcome.get("starts", 0)),
            "minutes": float(outcome.get("minutes", 0.0)),
            "own_rank": float(outcome.get("own_rank", np.nan)),
        })
    if len(rows) < 20:
        return {"status": "unavailable",
                "reason": f"only {len(rows)} frozen rows matched an outcome"}
    ev = pd.DataFrame(rows)
    considered = ev[ev["own_rank"] <= considered_rank]
    nailed = ev[ev["p_start"] >= START_BANDS[0][1]]
    cameo = ev[ev["p_start"] < START_BANDS[2][2]]
    out: dict[str, Any] = {
        "status": "measured",
        "target_gw": target_gw,
        "as_of": ev["as_of"].iloc[0],
        "n": int(len(ev)),
        "start_rate": round(float(ev["started"].mean()), 4),
        "brier": round(_brier(ev["p_start"], ev["started"]), 4),
        "brier_skill": round(_brier_skill(ev["p_start"], ev["started"]), 4),
        "auc": round(_auc(ev["p_start"], ev["started"]), 4),
        "exp_minutes_mae": round(_mae(ev["exp_minutes"], ev["minutes"]), 2),
        "bands": _start_bands(ev),
        "nailed_that_did_not_start": int((nailed["started"] == 0).sum()),
        "nailed_n": int(len(nailed)),
        "cameo_that_started": int(cameo["started"].sum()),
        "cameo_n": int(len(cameo)),
    }
    if len(considered) >= 20:
        c_cameo = considered[considered["p_start"] < START_BANDS[2][2]]
        out["considered"] = {
            "rank_cut": considered_rank,
            "n": int(len(considered)),
            "brier": round(_brier(considered["p_start"], considered["started"]), 4),
            "bands": _start_bands(considered),
            "cameo_that_started": int(c_cameo["started"].sum()),
            "cameo_n": int(len(c_cameo)),
        }
    return out


#: The live gameweek, measured. FROZEN, and re-runnable rather than merely
#: asserted: `live_start_audit` recomputes every figure below from
#: `data/state/projections.ndjson` plus the two public endpoints named in
#: `method`. It is recorded here because this module makes no network calls, so
#: the published artifact would otherwise carry no measurement of the one thing
#: the archive cannot see — the availability multiplier.
#:
#: It exists because of a specific claim. After GW1 2026-27 the reported
#: experience was that the cameo calls "went 1-for-7", that three badged cameos
#: started and produced most of the points, and that a player badged
#: "NAILED 0.90, ~74'" did not play at all. Every part of that is checkable
#: against a snapshot frozen two and a half hours before the deadline, so it was
#: checked rather than repeated.
LIVE_GW1_START_AUDIT: dict[str, Any] = {
    "status": "measured",
    "season": "2026-27",
    "target_gw": 1,
    "snapshot": "data/state/projections.ndjson, is_pre_deadline = 1",
    "as_of": "2026-08-21T17:00:28+00:00",
    "deadline": "2026-08-21T17:30:00Z",
    "method": "join the frozen snapshot to `event/1/live/` for `starts` and "
              "`minutes`, and to `bootstrap-static/` for ownership rank. "
              "Reproduce with `live_start_audit(outcomes)`.",
    "why_it_exists": "the archive has no status column, so the backtest above "
                     "measures `p_start` with availability pinned at 1.0. This "
                     "is the same model with availability included, frozen two "
                     "and a half hours before the deadline, against a finished "
                     "gameweek.",
    "n": 600,
    "start_rate": 0.3667,
    "brier": 0.1837,
    "brier_skill": 0.2091,
    "auc": 0.766,
    "exp_minutes_mae": 26.39,
    "bands": [
        {"band": "NAILED", "n": 52, "claimed": 0.928, "start_rate": 0.808,
         "appear_rate": 0.885, "exp_minutes": 76.5, "actual_minutes": 72.0},
        {"band": "ROTATION", "n": 72, "claimed": 0.743, "start_rate": 0.694,
         "appear_rate": 0.847, "exp_minutes": 62.7, "actual_minutes": 62.0},
        {"band": "CAMEO?", "n": 476, "claimed": 0.256, "start_rate": 0.269,
         "appear_rate": 0.422, "exp_minutes": 24.3, "actual_minutes": 24.0},
    ],
    "nailed_that_did_not_start": 10,
    "nailed_n": 52,
    "cameo_that_started": 128,
    "cameo_n": 476,
    "considered": {
        "rank_cut": 250,
        "n": 249,
        "brier": 0.2367,
        "bands": [
            {"band": "NAILED", "n": 47, "claimed": 0.933, "start_rate": 0.851,
             "appear_rate": 0.915, "exp_minutes": 76.8, "actual_minutes": 76.0},
            {"band": "ROTATION", "n": 61, "claimed": 0.751, "start_rate": 0.754,
             "appear_rate": 0.836, "exp_minutes": 63.2, "actual_minutes": 65.1},
            {"band": "CAMEO?", "n": 141, "claimed": 0.339, "start_rate": 0.574,
             "appear_rate": 0.723, "exp_minutes": 31.6, "actual_minutes": 50.0},
        ],
        "cameo_that_started": 81,
        "cameo_n": 141,
    },
    #: The claim this was built to check, and what the snapshot says about it.
    "reported_claim": {
        "as_stated": "the cameo calls went 1-for-7; three badged cameos started "
                     "and produced 21 of 39 points; and a player badged "
                     "NAILED 0.90, ~74' was not registered to play.",
        "verdict": "substantially confirmed, and the counts are slightly worse "
                   "than remembered rather than better.",
        "on_the_squad": "six of the fifteen picked for GW1 were badged CAMEO?, "
                        "and FIVE of them started — so 1-for-6, not 1-for-7. "
                        "Four were in the XI and contributed 22 of its 50 "
                        "points.",
        "the_nailed_player": "Konsa, badged NAILED at p_start 0.895 and "
                             "exp_minutes 73.8 — which renders as `~74'` — with "
                             "availability 1.0, played 0 minutes. Dubravka was "
                             "badged the same way at 0.921 / ~76' and also "
                             "played 0. Both were on the bench, so neither cost "
                             "points directly; both were live autosub cover.",
        "but_not_bad_luck": "across all 600 players the CAMEO? band was very "
                            "nearly calibrated — 0.256 claimed against a 0.269 "
                            "realised start rate — so the pool-wide badge was "
                            "not broken that week. Restricted to the 250 "
                            "most-owned players it claims 0.339 and realises "
                            "0.574. The badge is calibrated on players nobody "
                            "picks and wrong on the players everybody picks, "
                            "and a fifteen-man squad is drawn entirely from the "
                            "second group. That is a selection effect in the "
                            "MEASUREMENT, not a run of bad luck in the week.",
        "reproduces_in_the_archive": "not a one-off. At GW1 among the 250 "
                                     "most-owned players the CAMEO? band claims "
                                     "0.334 and realises 0.433 on 2025-26, and "
                                     "the same cut on 2024-25 gives 0.35 "
                                     "against 0.43. The pre-season regime "
                                     "under-calls starts for exactly the "
                                     "players a manager is choosing between, in "
                                     "every season it can be measured on.",
    },
    "limitations": [
        "One gameweek, 600 players, and the first gameweek of a season — the "
        "regime with the least information and the widest error. It is a spot "
        "check on the half of the model the archive cannot see, not a second "
        "backtest.",
        "115 of the 600 carried an availability multiplier below 1.0, so the "
        "availability path is exercised here. It is exercised ONCE.",
        "Frozen. These numbers are not recomputed on a backtest run, because "
        "this module makes no network calls. `live_start_audit` reproduces them "
        "from the same snapshot given the outcomes.",
    ],
}


def minutes_report(
    season: str = TEST_SEASON, horizons: tuple[int, ...] = HORIZONS,
) -> dict[str, Any]:
    """The published minutes-model block.

    Ordered so it reads in one pass: how good is the number, against what, where
    the error lives, and what the badge is worth on the players a manager would
    actually pick.
    """
    ev, coverage = build_minutes_evaluation(season, horizons)
    methods = {"gaffer": "p_start", **{k: k for k in MINUTES_BASELINES}}

    # In-season only for the paired table. At GW1 none of the three baselines
    # exists — there are no completed fixtures to average — so a GW1 row would
    # be the model measured against nothing, dressed as a comparison. GW1 gets
    # its own block below, exactly as the points model's pre-season one does.
    per_horizon: dict[str, Any] = {}
    for h in horizons:
        sub = ev[(ev["horizon"] == h) & (ev["decision_gw"] > 1)]
        if sub.empty:
            continue
        paired = sub.dropna(subset=[*methods.values(), "mins_avg_td"])
        if paired.empty:
            continue
        block: dict[str, Any] = {
            "n": int(len(paired)),
            "start_rate": round(float(paired["started"].mean()), 4),
            "brier": {k: round(_brier(paired[c], paired["started"]), 4)
                      for k, c in methods.items()},
            "brier_skill": {k: round(_brier_skill(paired[c], paired["started"]), 4)
                            for k, c in methods.items()},
            "auc": {k: round(_auc(paired[c], paired["started"]), 4)
                    for k, c in methods.items()},
            # `exp_minutes` is what the rate bundle multiplies, not `p_start`, so
            # a calibration fix that left this wrong would fix the wrong thing.
            "exp_minutes_mae": {
                "gaffer": round(_mae(paired["exp_minutes"], paired["minutes"]), 2),
                "mins_avg_td": round(_mae(paired["mins_avg_td"], paired["minutes"]), 2),
                "started_lag_x90": round(
                    _mae(paired["started_lag"] * 90.0, paired["minutes"]), 2),
            },
        }
        if h == 1:
            block["paired_vs_baseline"] = {
                b: _paired_brier_diff(paired, "p_start", b) for b in MINUTES_BASELINES
            }
        per_horizon[str(h)] = block

    h1 = ev[ev["horizon"] == 1]
    in_season = h1[h1["decision_gw"] > 1]
    gw1 = h1[h1["decision_gw"] == 1]

    branches = []
    for name, g in h1.groupby("branch"):
        branches.append({
            "branch": str(name),
            "n": int(len(g)),
            "share_pct": round(100.0 * len(g) / len(h1), 1),
            "mean_p_start": round(float(g["p_start"].mean()), 3),
            "start_rate": round(float(g["started"].mean()), 3),
            "brier": round(_brier(g["p_start"], g["started"]), 4),
            "brier_skill": round(_brier_skill(g["p_start"], g["started"]), 4),
        })
    branches.sort(key=lambda b: -b["n"])

    return {
        "measured": True,
        "target": "FPL `starts` == 1, per player-fixture. A post-match column, "
                  "legal here as the evaluation target and never as a feature.",
        "unit": "player-fixture. A double gameweek is two rows, because "
                "`p_start` is a per-match property and the projection takes the "
                "max across a double rather than summing it.",
        "season": season,
        "model_version": projection.MODEL_VERSION,
        "decision_gameweeks": f"GW{FIRST_DECISION_GW}-GW{int(ev['decision_gw'].max())}",
        "why_these_metrics": (
            "Brier because `p_start` is a multiplier, not a yes/no call, and a "
            "proper scoring rule is the only kind that cannot be gamed by "
            "shading the forecast. A skill score beside it because a raw Brier "
            "on a rare-event population is unreadable — 0.09 looks respectable "
            "until you notice that saying nothing scores 0.02. A calibration "
            "curve because it is the only form of the answer a reader can act "
            "on. The badge bands because the badge is the interface. Minutes "
            "MAE because `exp_minutes`, not `p_start`, is what multiplies "
            "through the rates."
        ),
        "coverage": {
            **coverage,
            "start_rate": round(float(ev["started"].mean()), 4),
            "zero_minute_share_pct": round(
                100.0 * float((ev["minutes"] == 0).mean()), 1),
        },
        "leakage_check": {
            "enforced": True,
            "post_match_fields_in_features": leakage.check_features(
                MINUTES_FEATURE_COLUMNS),
            "policy": "features are frozen season-to-date aggregates and strictly "
                      "prior-gameweek rolls; the target is post-match by "
                      "definition and is never read back as a feature",
        },
        "baselines": MINUTES_BASELINES,
        "per_horizon": per_horizon,
        "calibration": {
            "overall": _start_calibration(in_season),
            "considered": _start_calibration(
                in_season[in_season["own_rank"] <= CONSIDERED_OWNERSHIP_RANK]),
            "considered_rank_cut": CONSIDERED_OWNERSHIP_RANK,
            "note": "`overall` is every registered player. Most of that list "
                    "never plays, they are trivially easy to call, and they "
                    "carry the aggregate. `considered` is the same rows cut to "
                    "the most-owned players — the ones a manager is choosing "
                    "between — and the CAMEO? error changes SIGN between the "
                    "two. Read the second one.",
        },
        "bands": {
            "overall": _start_bands(h1),
            "considered": _start_bands(h1[h1["own_rank"] <= CONSIDERED_OWNERSHIP_RANK]),
            "considered_400": _start_bands(h1[h1["own_rank"] <= 400]),
            "pre_season": _start_bands(gw1),
            "pre_season_considered": _start_bands(
                gw1[gw1["own_rank"] <= CONSIDERED_OWNERSHIP_RANK]),
            "note": "`claimed` is the mean `p_start` of the players wearing that "
                    "badge; `start_rate` is how often they then started. The "
                    "thresholds live in `model.rationale.xmins_badge` and are "
                    "mirrored here, never redefined.",
        },
        "branches": branches,
        "branch_note": "Which arm of the shipped gate produced `p_start`. "
                       "`price_prior` fires when a player has no minutes this "
                       "season and no usable prior season, and it answers with "
                       "a function of his PRICE. It is a third of the "
                       "population and its skill score is deeply negative: not "
                       "merely uninformative, but worse than quoting that "
                       "group's own base rate to every one of them.",
        "pre_season": {
            "decision_gw": 1,
            "n": int(len(gw1)),
            "regime": "no season-to-date history exists, so `p_start` comes from "
                      "last season's `starts / 38` or the price prior alone",
            "brier": round(_brier(gw1["p_start"], gw1["started"]), 4),
            "brier_skill": round(_brier_skill(gw1["p_start"], gw1["started"]), 4),
            "auc": round(_auc(gw1["p_start"], gw1["started"]), 4),
            "naive_baseline": "does not exist. Every baseline here averages over "
                              "completed fixtures and before GW1 there are none. "
                              "The model's GW1 figure is unopposed and must not "
                              "be read as a win.",
        },
        "candidate_fix": MINUTES_CANDIDATE_FIX,
        "limitations": MINUTES_LIMITATIONS,
        "verdict": MINUTES_VERDICT,
        "live_audit": LIVE_GW1_START_AUDIT,
    }


def _select_squad(grp: pd.DataFrame, col: str) -> list[int] | None:
    """A legal 15 maximising ``col`` under budget, quota and the club limit.

    An exact MILP, so "the model's team" is a team you could actually have
    fielded — the previous harness picked an unconstrained top-11 leaderboard
    costing up to £101.5m with four players from one club.
    """
    import pulp

    ids = list(grp.index)
    if not ids:
        return None
    prob = pulp.LpProblem("bt_squad", pulp.LpMaximize)
    x = {i: pulp.LpVariable(f"x{i}", cat="Binary") for i in ids}
    proj = grp[col].to_dict()
    price = grp["value"].to_dict()
    pos = grp["pos"].to_dict()
    club = grp["team_id"].to_dict()

    prob += pulp.lpSum(x[i] * float(proj[i]) for i in ids)
    prob += pulp.lpSum(x[i] for i in ids) == SQUAD_SIZE
    for p, n in QUOTA.items():
        prob += pulp.lpSum(x[i] for i in ids if pos[i] == p) == n
    prob += pulp.lpSum(x[i] * float(price[i]) for i in ids) <= BUDGET
    for c in {club[i] for i in ids}:
        prob += pulp.lpSum(x[i] for i in ids if club[i] == c) <= CLUB_LIMIT
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None
    return [i for i in ids if x[i].value() and x[i].value() > 0.5]


def _best_xi(grp: pd.DataFrame, squad: list[int], col: str) -> list[int]:
    """The highest-``col`` legal XI from a 15 (formation minima and maxima)."""
    sub = grp.loc[squad].sort_values(col, ascending=False)
    chosen: list[int] = []
    counts = {p: 0 for p in XI_MIN}
    for p in XI_MIN:
        for idx in sub.index[sub["pos"] == p][: XI_MIN[p]]:
            chosen.append(idx)
            counts[p] += 1
    for idx, row in sub.iterrows():
        if len(chosen) >= 11:
            break
        if idx in chosen:
            continue
        if counts[row["pos"]] < XI_MAX[row["pos"]]:
            chosen.append(idx)
            counts[row["pos"]] += 1
    return chosen


def _pre_season_block(ev: pd.DataFrame) -> dict[str, Any]:
    """The GW1 decision, reported on its own.

    Not merely the first data point — a different regime. There is no
    season-to-date anything, so the model runs entirely on prior-season rates and
    the price prior, which is its exact state on the one evening of the year when
    a whole squad is picked from scratch. Averaged into 37 in-season gameweeks it
    is one thirty-eighth of a number and cannot be read at all.
    """
    sub = ev[(ev["decision_gw"] == FIRST_DECISION_GW) & (ev["horizon"] == 1)]
    if sub.empty:
        return {}

    # Cumulative season-to-date PPG is 0 for every player before a ball is
    # kicked. Reporting a rank correlation against a constant, or a decision made
    # by ranking one, would manufacture a baseline that does not exist.
    naive_defined = bool(sub["naive"].std() > 0)

    out: dict[str, Any] = {
        "decision_gw": FIRST_DECISION_GW,
        "n": int(len(sub)),
        "regime": "prior-season rates and the price prior only — no "
                  "season-to-date history exists yet",
        "mae": {"gaffer": round(_mae(sub["pred"], sub["actual"]), 3)},
        "rank_corr": {"gaffer": round(_rank_corr(sub, "pred"), 3)},
        "decisions": {"gaffer": _decision_metrics(sub, "pred")},
        # A single gameweek renders `captain_accuracy_pct` as 0 or 100 and
        # `xi_points_per_gw` as one observation. Both look like the rates they
        # are named after, and a page showing "100% captain accuracy" from one
        # decision would be worse than showing nothing.
        "decisions_caveat": "ONE gameweek, not an average. captain_accuracy_pct "
                            "here can only be 0 or 100 and is not a rate; "
                            "xi_points_per_gw is a single score. Read these as "
                            "what happened on one evening, never as performance.",
        "zero_minute_share_pct": round(100.0 * (sub["minutes"] == 0).mean(), 1),
    }
    # The Accuracy page prints this immediately after its own "No baseline to
    # beat." lede, so it has to be a sentence rather than a status word: opening
    # on a bare UNDEFINED read as a stringification bug on the one page whose
    # whole job is looking rigorous.
    out["naive_baseline"] = "defined" if naive_defined else (
        "The naive baseline here is cumulative season-to-date points-per-game, "
        "which does not exist before a ball is kicked: it predicts 0 for every "
        "player. Its rank correlation is undefined against that zero variance, "
        "and its MAE would measure only how many players failed to appear, so "
        "neither is reported. That absence is itself the finding."
    )
    if naive_defined:
        out["mae"]["naive"] = round(_mae(sub["naive"], sub["actual"]), 3)
        out["rank_corr"]["naive"] = round(_rank_corr(sub, "naive"), 3)
    return out


def _decision_metrics(df: pd.DataFrame, col: str) -> dict[str, Any]:
    """Constrained squad/XI/captain performance and regret vs perfect hindsight."""
    xi_pts, xi_best, cap_pts, cap_best, cap_hits, weeks = [], [], [], [], 0, 0
    for _, grp in df.groupby("target_gw"):
        g = grp.dropna(subset=["value", "team_id"]).copy()
        if len(g) < 40:
            continue
        squad = _select_squad(g, col)
        if squad is None:
            continue
        xi = _best_xi(g, squad, col)
        if len(xi) != 11:
            continue
        weeks += 1
        xi_pts.append(float(g.loc[xi, "actual"].sum()))

        # Perfect-hindsight legal XI, for regret.
        ideal_squad = _select_squad(g, "actual")
        if ideal_squad is not None:
            ideal_xi = _best_xi(g, ideal_squad, "actual")
            xi_best.append(float(g.loc[ideal_xi, "actual"].sum()))

        cap = g.loc[xi, col].idxmax()
        cap_pts.append(float(g.loc[cap, "actual"]))
        best_cap = g.loc[xi, "actual"].idxmax()
        cap_best.append(float(g.loc[best_cap, "actual"]))
        cap_hits += int(cap == best_cap)

    if not weeks:
        return {}
    return {
        "gameweeks_scored": weeks,
        "xi_points_per_gw": round(float(np.mean(xi_pts)), 1),
        "xi_regret_per_gw": (
            round(float(np.mean(xi_best) - np.mean(xi_pts)), 1) if xi_best else None
        ),
        "captain_points_per_gw": round(float(np.mean(cap_pts)), 2),
        "captain_regret_per_gw": round(float(np.mean(cap_best) - np.mean(cap_pts)), 2),
        "captain_accuracy_pct": round(100.0 * cap_hits / weeks, 1),
    }


def _transfer_regret(df: pd.DataFrame, col: str) -> dict[str, Any]:
    """One free transfer per week, chosen on projection, versus holding.

    G18 -- this had three faults and each one inflated it.

    **The swap enforced nothing.** It took the worst projected player out and
    the best projected same-position player in, checking neither budget nor club
    limit nor whether the money existed. Measured, the active arm finished the
    season at £107.7m against a £100.0m budget: a squad nobody could field, so
    its points were not a score anybody could have had.

    **The arms scored different numbers of gameweeks.** Each one independently
    skipped a week it could not field eleven from (``continue``), and the totals
    were then differenced anyway while ``gameweeks`` reported ``len(gws) - 1``
    regardless. The arms actually scored 35 and 36 weeks and the artifact
    published 37.

    **And the passive arm rotted.** Never replacing anyone, it decayed to 20.6
    points a gameweek -- a figure no real FPL XI produces -- so most of the
    "gain" was one arm falling apart rather than the other improving.

    What made it undeniable: the *same code* reported ``gaffer.gain: -24.0`` on
    2024-25 and ``+727.0`` on 2025-26. A metric that swings from "a transfer a
    week costs you 24 points a season" to "it gains you 727" between adjacent
    seasons is not measuring transfer value.

    Now: transfers respect budget, club limit and the money in the bank; both
    arms are scored only on gameweeks where **both** can field a legal eleven;
    and that count is what gets published. The sell price is the player's
    current value -- this harness does not track purchase prices, which is a
    simplification and is recorded in ``limitations`` rather than hidden.
    """
    gws = sorted(df["target_gw"].unique())
    if len(gws) < 3:
        return {}
    first = df[df["target_gw"] == gws[0]].dropna(subset=["value", "team_id"])
    squad = _select_squad(first, col)
    if squad is None:
        return {}

    held = {int(first.loc[i, "element"]) for i in squad}
    spent = float(first.loc[squad, "value"].sum())
    bank = float(BUDGET) - spent

    active, passive = set(held), set(held)
    active_pts = passive_pts = 0.0
    scored = 0
    swaps = 0
    blocked = {"budget": 0, "club": 0}

    for gw in gws[1:]:
        g = df[df["target_gw"] == gw].dropna(subset=["value", "team_id"])
        by_el = g.set_index("element")

        a_avail = [e for e in active if e in by_el.index]
        p_avail = [e for e in passive if e in by_el.index]
        # Both arms or neither. Differencing totals built from different
        # gameweeks is what published a 37 that neither arm ever played.
        if len(a_avail) >= 11 and len(p_avail) >= 11:
            indexed = by_el.reset_index().set_index("element")
            a_xi = _best_xi(indexed, a_avail, col)
            p_xi = _best_xi(indexed, p_avail, col)
            active_pts += float(by_el.loc[a_xi, "actual"].sum())
            passive_pts += float(by_el.loc[p_xi, "actual"].sum())
            scored += 1

        # One free transfer for the active squad, and it has to be legal.
        cand = by_el[~by_el.index.isin(active)]
        mine = by_el[by_el.index.isin(active)]
        if cand.empty or mine.empty:
            continue
        out_el = mine[col].idxmin()
        out_pos = mine.loc[out_el, "pos"]
        out_price = float(mine.loc[out_el, "value"])
        same_pos = cand[cand["pos"] == out_pos].sort_values(col, ascending=False)
        if same_pos.empty:
            continue

        clubs: dict[Any, int] = {}
        for e in active:
            if e in by_el.index and e != out_el:
                c = by_el.loc[e, "team_id"]
                clubs[c] = clubs.get(c, 0) + 1

        for in_el, row in same_pos.iterrows():
            if float(row[col]) <= float(mine.loc[out_el, col]):
                break  # sorted, so nothing further is an improvement either
            in_price = float(row["value"])
            if bank + out_price < in_price:
                blocked["budget"] += 1
                continue
            if clubs.get(row["team_id"], 0) + 1 > CLUB_LIMIT:
                blocked["club"] += 1
                continue
            active.discard(out_el)
            active.add(int(in_el))
            bank += out_price - in_price
            swaps += 1
            break

    if scored < 2:
        return {}
    return {
        "with_transfers": round(active_pts, 1),
        "hold_squad": round(passive_pts, 1),
        "gain": round(active_pts - passive_pts, 1),
        "gameweeks": scored,
        "transfers_made": swaps,
        "transfers_blocked": blocked,
        "bank_remaining_tenths": round(bank, 1),
        "basis": ("one free transfer per gameweek, budget/club-limit/bank "
                  "enforced, both arms scored only on gameweeks where both "
                  "could field a legal XI; sell price is current value"),
    }


# ---------------------------------------------------------------------------
# T-26 — what was withdrawn, and what was tried and rejected
# ---------------------------------------------------------------------------

#: Baselines this dataset cannot measure. Recorded rather than deleted: a number
#: that quietly disappears looks like it was never there.
WITHDRAWN_BASELINES = {
    "fpl_xp": {
        "withdrawn_in_schema": 4,
        "previously_reported": {"rank_corr_h1": 0.760, "mae_h1": 0.942,
                                "xi_points_per_gw": 84.2},
        "reason": "computed from the archive's `xP` column. The archive cannot "
                  "certify this value as the pre-deadline forecast managers saw, "
                  "and the upstream dataset explicitly warns that it may contain "
                  "post-match information. It is therefore inadmissible for this "
                  "backtest.",
        "provenance": "The upstream data dictionary states `xP` is FPL's "
                      "`ep_this`, scraped AFTER each gameweek has ended, with an "
                      "undocumented update cadence, and advises shifting or "
                      "dropping it. That is the grounds for exclusion.",
        "corroboration": "Not proof of timing, but consistent with it: within a "
                         "player, across single-fixture gameweeks he completed "
                         "60+ minutes of, `xP` deviates by sd~1.75 points and "
                         "correlates +0.40..+0.47 with the deviation in what he "
                         "then scored, against +0.09 and -0.13 for two quantities "
                         "that are pre-deadline by construction. Reproduce with "
                         "`python -m gaffer.backtest --xp-diagnostic`.",
    },
    "ensemble": {
        "withdrawn_in_schema": 4,
        "previously_reported": {"rank_corr_h1": 0.699, "mae_h1": 1.043,
                                "xi_points_per_gw": 84.6},
        "reason": "0.7 of it was `fpl_xp`, so it inherited the same inflation.",
    },
    "consequence": "EP_NEXT_BLEND_WEIGHT (0.7) was chosen against these numbers. "
                   "The weight is UNCHANGED — moving it on withdrawn evidence "
                   "would be as unfounded as setting it was — but it is now "
                   "labelled a policy choice, not a fitted one. It becomes "
                   "fittable once projection_snapshots and player_gw accumulate "
                   "live ep_next values against real outcomes.",
}

#: T-26's experiment, one record per candidate.
#:
#: The first version of this block reported GBM only and concluded "trained
#: models lose every decision metric". That is false as written: ridge beat the
#: heuristic at h=1 on legal-XI points and on captaincy. It is not a *win* — the
#: interval spans zero and it does not hold up past h=1 — but "not selected" and
#: "rejected" are different findings, and flattening them hid real evidence.
#:
#: FROZEN AT 2024-25, and G-N is why this now has to be said out loud. Every
#: number below was measured on the pre-G-N split (train 2022-23 + 2023-24,
#: select 2023-24, report 2024-25) against a code path that no longer exists:
#: the same batch that recorded this experiment deleted `src/gaffer/ml.py` and
#: the joblib. So the candidate rows cannot be re-measured on the current split
#: without rebuilding a trainer, and re-labelling them "2025-26" would be a
#: fabrication in the opposite direction to the one they are guarding against.
#: `heuristic_reference` therefore KEEPS its 2024-25 values — the candidate
#: rows are paired against it gameweek by gameweek, and swapping in 2025-26
#: figures would compare a 2024-25 model against a 2025-26 baseline and
#: manufacture a result. What the shipped heuristic does on the CURRENT test
#: season is `current_split_reference`, measured fresh for G-N and paired with
#: nothing.
MODEL_CANDIDATES = {
    "evaluation_version": "candidates-1.1",
    "outcome": "no trained points model ships",
    "measured_on_season": "2024-25",
    "status": "FROZEN at 2024-25, on the pre-G-N split, against a removed code "
              "path (`src/gaffer/ml.py`). Not re-runnable. Nothing here "
              "describes what ships today.",
    "protocol": "Features from the same leakage-checked adapter; trained on "
                "2022-23 + 2023-24 (2021-22 supplies 2022-23's priors); selected "
                "on 2023-24; reported once on 2024-25, untouched until selection "
                "closed. Decision metric = legal 15 under budget, quota and the "
                "three-per-club limit, best legal XI from it, scored on what "
                "actually happened; paired by gameweek against the shipped "
                "heuristic with a 4000-sample bootstrap. NO LONGER THE PROJECT'S "
                "SPLIT — see `season_split`. Two things in it are wrong rather "
                "than merely superseded: selection ran on 2023-24, which was "
                "also in the training set; and 2022-23 has no `expected_goals` "
                "or `expected_assists` at all before GW16 (G-Q).",
    "heuristic_reference": {
        "measured_on_season": "2024-25",
        "why_not_restated": "The candidate rows are paired against these by "
                            "gameweek. Restating them on 2025-26 would pair a "
                            "2024-25 model with a 2025-26 baseline.",
        "xi_points_per_gw": {"1": 50.86, "2": 52.58, "3": 51.03,
                             "4": 51.15, "5": 48.91, "6": 49.47},
        "captain_accuracy_pct_h1": 29.7,
        "captain_regret_per_gw_h1": 5.59,
        "rank_corr": {"1": 0.440, "3": 0.413, "6": 0.397},
        "mae": {"1": 1.566, "3": 1.585, "6": 1.605},
    },
    #: The shipped heuristic on the CURRENT test season. Here so that
    #: `heuristic_reference` can never be read as a description of the live
    #: configuration — which is exactly what it had become.
    "current_split_reference": {
        "measured_on_season": "2025-26",
        "measured_at_model_version": "heuristic-0.5",
        "note": "Re-measured for G-N. No trained candidate was ever run on this "
                "season, so nothing here is paired, and none of it may be "
                "differenced against the candidate rows.",
        "xi_points_per_gw": {"1": 49.3, "2": 47.8, "3": 46.6,
                             "4": 47.1, "5": 45.6, "6": 44.3},
        "captain_points_per_gw_h1": 5.87,
        "captain_accuracy_pct_h1": 21.1,
        "captain_regret_per_gw_h1": 6.03,
        "rank_corr": {"1": 0.447, "2": 0.433, "3": 0.420,
                      "4": 0.413, "5": 0.406, "6": 0.402},
        "mae": {"1": 1.592, "2": 1.606, "3": 1.613,
                "4": 1.628, "5": 1.633, "6": 1.637},
        "naive_baseline": {
            "xi_points_per_gw": {"1": 44.6, "2": 45.3, "3": 41.5,
                                 "4": 40.2, "5": 42.6, "6": 43.5},
            "rank_corr": {"1": 0.692, "2": 0.677, "3": 0.664,
                          "4": 0.653, "5": 0.647, "6": 0.640},
            "mae": {"1": 1.075, "2": 1.091, "3": 1.104,
                    "4": 1.126, "5": 1.133, "6": 1.138},
            "captain_points_per_gw_h1": 5.97,
            "reading": "The model leads legal-XI points at every horizon "
                       "(+4.7, +2.5, +5.1, +6.9, +3.0, +0.8) — a first. It "
                       "still loses captaincy (5.87 to 5.97), and loses MAE and "
                       "rank correlation heavily: 61.4% of rows are zero-minute "
                       "and a rolling points average predicts non-appearance far "
                       "better. On 2024-25 it ran the other way: 50.6 to 51.7 on "
                       "the XI, 8.76 to 8.00 on the captain.",
        },
        "defcon_ablation_h1": {
            "method": "DEFCON_THRESHOLD neutralised to 999 everywhere. Zeroing "
                      "`defcon_per_90` is no longer an ablation: since "
                      "heuristic-0.5 the rate shrinks toward `F.DEFCON_PRIOR`, "
                      "so zero still yields a contribution.",
            "with": {"xi_points_per_gw": 49.3, "rank_corr": 0.447, "mae": 1.592,
                     "captain_points_per_gw": 5.87,
                     "captain_accuracy_pct": 21.1},
            "without": {"xi_points_per_gw": 45.9, "rank_corr": 0.442,
                        "mae": 1.578, "captain_points_per_gw": 4.97,
                        "captain_accuracy_pct": 18.4},
            "reading": "+3.4 legal-XI points per gameweek: about 72% of the "
                       "model's whole 4.7-point margin over the naive baseline, "
                       "from the one component measurable for exactly one "
                       "season. It also makes MAE 0.014 worse. On 2024-25 it "
                       "moves the XI 0.1 the WRONG way (50.6 with, 50.7 "
                       "without) — that season has no `defensive_contribution` "
                       "column, so the term's contribution there is fabricated "
                       "from a positional prior.",
        },
    },
    "candidates": [
        {
            "candidate": "gbm",
            "label": "Gradient-boosted trees",
            "detail": "sklearn HistGradientBoostingRegressor, 300 iterations, "
                      "depth 6, leaf 60, l2 1.0, fixed seed",
            "decision": "rejected",
            "reason": "Worse than the shipped heuristic on legal-XI points at "
                      "every horizon, and far worse on captaincy (13.5% accuracy "
                      "against 29.7%). Four of six paired intervals exclude zero "
                      "against it.",
            "worse_at_every_horizon": True,
            "per_horizon": {
                "1": {"candidate_xi": 46.22, "diff": -4.65, "ci95": [-10.03, 0.84],
                      "p_better": 0.048},
                "2": {"candidate_xi": 46.36, "diff": -6.22, "ci95": [-11.92, -0.05],
                      "p_better": 0.024},
                "3": {"candidate_xi": 44.97, "diff": -6.06, "ci95": [-11.71, -0.31],
                      "p_better": 0.020},
                "4": {"candidate_xi": 42.97, "diff": -8.18, "ci95": [-13.88, -2.41],
                      "p_better": 0.002},
                "5": {"candidate_xi": 44.76, "diff": -4.15, "ci95": [-9.33, 1.12],
                      "p_better": 0.069},
                "6": {"candidate_xi": 44.78, "diff": -4.69, "ci95": [-12.0, 2.69],
                      "p_better": 0.105},
            },
            "statistical": {"rank_corr": {"1": 0.638, "3": 0.610, "6": 0.589},
                            "mae": {"1": 1.14, "3": 1.16, "6": 1.19}},
            "captain_accuracy_pct_h1": 13.5,
            "captain_regret_per_gw_h1": 6.32,
            "limitations": [
                "Wins every statistical metric and loses every decision metric. "
                "58% of rows are zero-minute, so MAE and rank correlation mostly "
                "reward predicting who does NOT play; a squad needs the top tail, "
                "and a squared-error objective on a zero-inflated target "
                "regresses hard to the mean.",
            ],
        },
        {
            "candidate": "ridge",
            "label": "Regularised linear model",
            "detail": "standardised, with position one-hots, alpha 10",
            "decision": "inconclusive",
            "reason": "Beat the heuristic at h=1 — +2.70 legal-XI points per "
                      "gameweek, 22 gameweeks to 14, and better captaincy (32.4% "
                      "accuracy against 29.7%, regret 4.43 against 5.59). But the "
                      "paired interval spans zero, and the advantage does not "
                      "survive past h=1: negative at h=2 through h=6, every one "
                      "of those intervals also spanning zero. Not selected — an "
                      "edge that is neither material nor durable does not justify "
                      "a training pipeline, a model artifact, a serialisation "
                      "format, an integrity check and a fallback path. This is "
                      "NOT the same finding as gbm.",
            "worse_at_every_horizon": False,
            "per_horizon": {
                "1": {"candidate_xi": 53.57, "diff": 2.70, "ci95": [-1.38, 6.89],
                      "p_better": 0.898, "wins": 22, "losses": 14},
                "2": {"candidate_xi": 52.00, "diff": -0.58, "ci95": [-4.22, 3.17],
                      "p_better": 0.369, "wins": 15, "losses": 18},
                "3": {"candidate_xi": 48.69, "diff": -2.34, "ci95": [-6.6, 1.83],
                      "p_better": 0.141, "wins": 13, "losses": 21},
                "4": {"candidate_xi": 50.56, "diff": -0.59, "ci95": [-5.18, 4.09],
                      "p_better": 0.406, "wins": 17, "losses": 14},
                "5": {"candidate_xi": 47.61, "diff": -1.30, "ci95": [-5.18, 2.52],
                      "p_better": 0.256, "wins": 17, "losses": 15},
                "6": {"candidate_xi": 48.53, "diff": -0.94, "ci95": [-6.5, 4.12],
                      "p_better": 0.374, "wins": 17, "losses": 14},
            },
            "statistical": {"rank_corr": {"1": 0.640, "3": 0.609, "6": 0.586},
                            "mae": {"1": 1.146, "3": 1.169, "6": 1.192}},
            "captain_accuracy_pct_h1": 32.4,
            "captain_regret_per_gw_h1": 4.43,
            "limitations": [
                "37 gameweeks is a small sample for a paired per-gameweek "
                "comparison; the h=1 interval is [-1.38, +6.89].",
                "h=1 was not pre-registered as the primary endpoint. Selecting "
                "it after the fact is how a coin flip becomes a finding.",
            ],
        },
        {
            "candidate": "xp_models",
            "label": "Models using the archive's xP column",
            "detail": "gbm and ridge with `xP` as a feature, plus per-horizon and "
                      "stacked variants",
            "decision": "invalid_experiment",
            "reason": "The only configurations that looked like a decisive win "
                      "(+10.73 legal-XI points per gameweek on the selection "
                      "season). The advantage came almost entirely from `xP`, "
                      "which is inadmissible: the archive cannot certify it as "
                      "the pre-deadline forecast managers saw, and the upstream "
                      "dataset explicitly warns it may contain post-match "
                      "information. The experiment cannot be scored — that is "
                      "different from losing.",
            "worse_at_every_horizon": None,
            "per_horizon": {},
            "statistical": {},
            "limitations": [
                "Recorded so the result is not rediscovered and mistaken for a "
                "finding.",
                "`xP` was never carried past h=1: it is a one-week number, and "
                "forwarding it would manufacture data.",
            ],
        },
    ],
    "not_ruled_out": "The trained models ARE much better at predicting "
                     "appearances — fringe-player MAE 0.285 against the shipped "
                     "0.504, rank correlation 0.462 against 0.325. A minutes-only "
                     "classifier feeding the existing `p_start` gate is the "
                     "version worth testing next; a points regressor is not. That "
                     "is a hypothesis, not a plan, and nothing here implements it.",
    "cost": {"fit_seconds": 6.95, "predict_seconds": 0.485,
             "train_rows": 279899, "predict_rows": 146073,
             "peak_train_mb": 176.8},
    "removed": ["src/gaffer/ml.py", "data/model/gaffer_gbm.joblib",
                "the `ml` extra in pyproject.toml"],
}


#: C1 — captaincy on the distribution. MEASURED, REFUSED.
#:
#: `solver/optimize.py` decided the armband on expected points and justified it
#: as UI consistency: "so every surface agrees on it". That is a reason to keep a
#: rule and not a reason to have chosen one, and captaincy is the largest
#: addressable number in this artifact — 6.05 points of regret per gameweek at
#: h=1 against a perfect-hindsight ceiling of 11.94, with the model BELOW the
#: naive baseline on captain accuracy (21.1% against 26.3%).
#:
#: Captaincy is a max-order-statistic problem — you double ONE player — so the
#: quantity that should decide it is P(haul), not E[points], and `model.simulate`
#: already publishes `boom`, `ceiling`, `floor` and `std` per player. Nineteen
#: rules were scored with squad and XI selection held fixed on expected points,
#: so that the armband was the only thing that varied.
#:
#: The simulation IS reconstructible on the archive, which had been flagged as
#: the thing that might stop this being measurable at all: `_sample_fixture`
#: takes the rate bundle `projection.fixture_rates` returns, and the backtest
#: already builds that bundle. Parity was checked rather than assumed — the
#: simulated mean correlates with the point estimate at 0.9998.
CAPTAINCY_CANDIDATE = {
    "candidate": "captain_on_the_distribution",
    "decision": "measured, REFUSED",
    "question": "the armband doubles ONE player, so P(haul) should decide it "
                "rather than E[points]. Does any rule built on the published "
                "distribution beat expected points?",
    "protocol": "squad and XI chosen on expected points in every arm, so only "
                "the armband varies. Rules selected on 2024-25 with 2023-24 as "
                "corroboration; 2025-26 reported once. Three Monte-Carlo "
                "configurations (n=3000 seed A, n=3000 seed B, n=20000) because "
                "a captaincy rule that changes two armbands a season can be "
                "moved by sampling noise alone.",
    "rules_tried": 19,
    "headroom": {
        "shipped_captain_points_per_gw": {"2023-24": 5.74, "2024-25": 8.92,
                                          "2025-26": 5.89},
        "perfect_hindsight_per_gw": {"2023-24": 12.48, "2024-25": 14.03,
                                     "2025-26": 11.94},
        "note": "the ceiling is the best actual scorer in the XI, which is not "
                "attainable. It is the size of the prize, not a target.",
    },
    #: Points per gameweek MINUS the shipped rule. Positive is the challenger
    #: winning. n=3000 at the first seed; see `seed_stability`.
    "vs_expected_points_per_gw": {
        "boom": {"2023-24": -0.474, "2024-25": -1.105, "2025-26": -0.447},
        "ceiling": {"2023-24": -0.237, "2024-25": 0.395, "2025-26": -0.026},
        "floor": {"2023-24": -0.316, "2024-25": -2.026, "2025-26": -1.105},
        "std": {"2023-24": -0.342, "2024-25": -0.342, "2025-26": -0.132},
        "sim_mean": {"2023-24": 0.184, "2024-25": -0.158, "2025-26": 0.0},
        "blend_upside_0.1": {"2023-24": 0.211, "2024-25": 0.079,
                             "2025-26": 0.079},
        "blend_upside_1.5": {"2023-24": -1.026, "2024-25": 0.237,
                             "2025-26": -0.132},
    },
    "seed_stability": "the only rule with a consistent sign across all three "
                      "Monte-Carlo configurations is `boom`, and its sign is "
                      "NEGATIVE in all nine season-configuration cells. Every "
                      "rule that looked like a win changed sign with the seed: "
                      "`blend_upside_0.1` runs +0.211 / -0.053 / -0.053 on the "
                      "train season across the three configurations, and "
                      "`blend_boom_0.1` runs +0.211 / -0.316 / +0.342 on the "
                      "test season. The selection-season winner, `ceiling` at "
                      "+0.395, decays to +0.132 at n=20000 while losing the "
                      "train and test seasons in every configuration.",
    "mechanism": "the distribution is a deterministic function of the SAME "
                 "eleven rates the point estimate is built from, so inside an "
                 "eleven-man XI it carries almost no ORDERING information the "
                 "mean does not already have. On the test season the AUC for "
                 "predicting an actual double-digit haul is 0.5876 for expected "
                 "points and 0.5863 for `boom` — indistinguishable. `boom` does "
                 "not know which player hauls, so doubling the player with the "
                 "highest P(haul) cannot help. On the same season the simulated "
                 "mean moves the armband in ZERO of 38 gameweeks.",
    "haul_discrimination_auc": {
        "note": "AUC against `actual >= 10`, over captain-eligible XI rows.",
        "2023-24": {"expected_points": 0.6291, "boom": 0.6918,
                    "ceiling": 0.6833, "std": 0.6734},
        "2024-25": {"expected_points": 0.6947, "boom": 0.6882,
                    "ceiling": 0.7160, "std": 0.7112},
        "2025-26": {"expected_points": 0.5876, "boom": 0.5863,
                    "ceiling": 0.5913, "std": 0.5889},
    },
    "armbands_moved_of_38": {
        "note": "how many captains a rule changes at all. A rule worth a "
                "per-gameweek figure has to move more than one.",
        "sim_mean": {"2023-24": 3, "2024-25": 2, "2025-26": 0},
        "boom": {"2023-24": 11, "2024-25": 10, "2025-26": 13},
        "ceiling": {"2023-24": 8, "2024-25": 4, "2025-26": 11},
        "blend_upside_0.1": {"2023-24": 3, "2024-25": 2, "2025-26": 1},
    },
    "refused_because": "no rule beats expected points on the selection season "
                       "with corroboration on the train season, and the one "
                       "that wins the selection season alone (`ceiling`, "
                       "+0.395/gw) loses the train and test seasons and shrinks "
                       "as the Monte-Carlo error shrinks. That is the shape of "
                       "a result fitted to one season, which this project has "
                       "produced once before and shipped once too often.",
    "reopen_if": "the distribution stops being a deterministic re-reading of "
                 "the point estimate — a separate haul model, a bonus-point "
                 "model, or minute-level variance that the rate bundle does not "
                 "already contain. The blocker is not the decision rule, it is "
                 "that `boom` and `exp_points` are the same information twice.",
    "limitations": [
        "The archive carries no bonus, card, own-goal or penalty rates, so the "
        "simulated distribution here is missing exactly the tail events that "
        "make a haul. The point estimate is missing them too, so the ARMS are "
        "comparable; the absolute `boom` level is not the live one.",
        "Captains are chosen from an XI selected on expected points. A rule "
        "that also picked a different XI was not tried, and would not be a "
        "captaincy change.",
        "Availability is pinned at 1.0, as everywhere in this harness.",
    ],
}

#: A19 — the projection contradicts itself about one number. MEASURED, NOT FIXED.
#:
#: Found by `model.scenarios` while making the sampler agree with the projection:
#: having removed the sampler's own divergence, the projection still disagreed
#: with itself. `projection.fixture_rates` carries two estimates of "how many
#: goals does the opposition score in this fixture" — the top-down
#: `ctx.expected_conceded`, which is the only one `p_cs` reads, and the bottom-up
#: sum of the opposing side's players' `exp_goals`, which is what every attacking
#: projection is built from.
#:
#: Measured end-to-end on all three split seasons, one row per fixture-side.
CLEAN_SHEET_CONTRADICTION = {
    "finding": "two estimates of one quantity, and `p_cs` reads the weaker one",
    "decision": "measured, NOT fixed",
    "unit": "fixture-side: 760 per season, 2280 in all",
    "disagreement": {
        "mean_abs_goals": {"2023-24": 0.350, "2024-25": 0.360,
                           "2025-26": 0.363},
        "max_abs_goals": {"2023-24": 4.564, "2024-25": 2.038,
                          "2025-26": 2.694},
        "correlation": {"2023-24": 0.6845, "2024-25": 0.7449,
                        "2025-26": 0.6392},
        "within_25_pct_of_each_other": "53.6% of fixture-sides. One lambda is "
                                       "more than double the other in 3.8%.",
        "by_stage": {
            "note": "the gap is WORST in the regime the live product occupies "
                    "in early September, which is why this surfaced now.",
            "GW1-3": {"n": 178, "mean_abs_gap": 0.670, "correlation": 0.262,
                      "max_p_cs": 0.835},
            "GW4-8": {"n": 302, "mean_abs_gap": 0.434, "correlation": 0.556,
                      "max_p_cs": 0.709},
            "GW9+": {"n": 1800, "mean_abs_gap": 0.314, "correlation": 0.761,
                     "max_p_cs": 0.785},
        },
    },
    "which_is_right": {
        "note": "clean sheets actually kept, scored against each lambda. The "
                "bottom-up one wins on every metric in every season, which is "
                "the opposite of the arm that ships.",
        "brier_shipped_top_down": {"2023-24": 0.1643, "2024-25": 0.1794,
                                   "2025-26": 0.1899},
        "brier_bottom_up": {"2023-24": 0.1603, "2024-25": 0.1714,
                            "2025-26": 0.1853},
        "brier_league_base_rate": {"2023-24": 0.1639, "2024-25": 0.1794,
                                   "2025-26": 0.1901},
        "auc_shipped_top_down": {"2023-24": 0.6500, "2024-25": 0.6345,
                                 "2025-26": 0.6123},
        "auc_bottom_up": {"2023-24": 0.6617, "2024-25": 0.6586,
                          "2025-26": 0.6144},
        "goals_mae_shipped_top_down": {"2023-24": 0.9854, "2024-25": 0.9269,
                                       "2025-26": 0.9121},
        "goals_mae_bottom_up": {"2023-24": 0.9771, "2024-25": 0.9583,
                                "2025-26": 0.9203},
        "reading": "on the clean sheet itself — which is what `p_cs` is for — "
                   "the bottom-up lambda wins the Brier and the AUC in all "
                   "three seasons. On raw goals conceded the top-down lambda "
                   "wins two of three, so this is not a claim that one lambda "
                   "is better at everything. What ships is barely "
                   "distinguishable from quoting the league clean-sheet rate "
                   "to every side: 0.1899 against a base rate of 0.1901 on the "
                   "test season, a dead tie on the selection season, and WORSE "
                   "than the base rate on the train season.",
    },
    "calibration_of_the_shipped_p_cs": {
        "note": "claimed -> realised, pooled over the three seasons. Honest "
                "below 0.35 and over-confident above it.",
        "bands": [
            {"claimed": 0.100, "realised": 0.098, "n": 407},
            {"claimed": 0.201, "realised": 0.185, "n": 567},
            {"claimed": 0.298, "realised": 0.251, "n": 589},
            {"claimed": 0.395, "realised": 0.319, "n": 420},
            {"claimed": 0.490, "realised": 0.286, "n": 182},
            {"claimed": 0.610, "realised": 0.435, "n": 115},
        ],
        "at_or_above_0.70": {"n": 10, "claimed": 0.758, "realised": 0.600},
        "bottom_up_equivalent": [
            {"claimed": 0.102, "realised": 0.114, "n": 536},
            {"claimed": 0.198, "realised": 0.204, "n": 712},
            {"claimed": 0.298, "realised": 0.276, "n": 562},
            {"claimed": 0.395, "realised": 0.352, "n": 321},
            {"claimed": 0.487, "realised": 0.353, "n": 119},
            {"claimed": 0.590, "realised": 0.433, "n": 30},
        ],
        "and_it_is_more_conservative": "the bottom-up lambda's largest clean-"
                                       "sheet probability over three seasons is "
                                       "0.693, against 0.835 for the one that "
                                       "ships, and it puts 30 fixture-sides "
                                       "above 0.55 where the shipped arm puts "
                                       "115.",
    },
    "live_consequence": "the artifact published a 0.760 clean-sheet probability "
                        "for one club at home after ONE finished gameweek. That "
                        "is not a sampler artefact — it is what the projection "
                        "says — and it sits above anything three seasons of "
                        "archive validate: the ten fixture-sides that ever "
                        "cleared 0.70 realised 0.60, and the whole 0.55+ band "
                        "realises 0.435. Two players in the owner's real squad "
                        "are priced on it.",
    "not_fixed_because": "`fixture_rates` is per-player and cannot see the "
                         "opposing team's squad, so reading the bottom-up "
                         "lambda needs a two-pass projection: accumulate every "
                         "team's attacking lambda, then project. That is a "
                         "structural change to `projection.project`, it moves "
                         "every defender and goalkeeper in the product, and it "
                         "needs the points backtest run behind it the way A18 "
                         "did. Characterised rather than rushed.",
    "next_step": "two passes, then re-run the points backtest. The measurement "
                 "above says which lambda to keep for `p_cs`; it does not say "
                 "what that does to the legal XI, and a clean-sheet improvement "
                 "that degrades the decision is not a win. Note also that the "
                 "over-confidence above 0.35 is a calibration fault present in "
                 "BOTH lambdas, so reconciling them is necessary and not "
                 "sufficient.",
    "exposed_as": "`model.scenarios.ScenarioSet.diagnostics"
                  "['clean_sheet_contradiction']`, per run.",
}


def candidate(name: str) -> dict[str, Any]:
    """One candidate's record. Raises rather than returning a silent default."""
    for c in MODEL_CANDIDATES["candidates"]:
        if c["candidate"] == name:
            return c
    raise KeyError(f"no model candidate named {name!r}")


def candidate_decisions() -> dict[str, str]:
    """`candidate -> decision`. These distinctions must survive summarising."""
    return {c["candidate"]: c["decision"] for c in MODEL_CANDIDATES["candidates"]}


def xp_leakage_diagnostic(
    seasons: tuple[str, ...] = ("2022-23", "2023-24", "2024-25", "2025-26"),
) -> dict[str, Any]:
    """Reproduce the measurement that withdrew `fpl_xp`.

    Within one player, across gameweeks he played 60+ minutes of and his team
    played exactly once, how far does each quantity move from that player's own
    average, and how well does the move predict the move in his points?

    A pre-deadline forecast can only move with the fixture and the team news.
    Anything that moves with the *result* is not one.

    2025-26 was added to the default when it became the test season, and it
    WEAKENS the corroboration rather than strengthening it: `xP` correlates
    +0.147 with the points deviation there, against +0.45 / +0.46 / +0.40 on
    2022-23 / 2023-24 / 2024-25, while its within-player spread is the largest of
    the four (sd 1.86). Recorded rather than dropped — a season is not excluded
    from a diagnostic for disagreeing with it. The withdrawal does not move,
    because it never rested on this measurement: the grounds are that the archive
    cannot certify `xP` as the pre-deadline forecast and the upstream dataset
    warns it may contain post-match information. What 2025-26 does show is that
    the correlation is not a stable property of the column, which is one more
    reason not to have leant on it.
    """
    out: dict[str, Any] = {"method": xp_leakage_diagnostic.__doc__, "seasons": {}}
    for season in seasons:
        hist = histdata.load_season(season)
        df = hist.frame
        if "xP" not in df:
            continue
        df = df.copy()
        df["_fx"] = df.groupby(["team_id", "GW"])["fixture"].transform("nunique")
        # `ppg_td` is the control: points-per-game so far, pre-deadline by
        # construction (shift(1)), and the closest thing the archive has to an
        # honest forecast of the same quantity.
        df["ppg_td"] = df["pts_td"] / df["games_td"].clip(lower=1)
        d = df[(df["minutes"] >= 60) & (df["_fx"] == 1)].copy()
        n = d.groupby("element")["GW"].transform("size")
        d = d[n >= 15]
        if len(d) < 500:
            continue
        d["_dy"] = d["total_points"] - d.groupby("element")["total_points"].transform("mean")
        row: dict[str, Any] = {"n_rows": int(len(d))}
        for col, kind in (("xP", "the archive's expected-points column"),
                          ("ppg_td", "CONTROL — pre-deadline by construction"),
                          ("expected_goals", "REFERENCE — post-match by definition")):
            if col not in d:
                continue
            dev = d[col] - d.groupby("element")[col].transform("mean")
            row[col] = {"kind": kind,
                        "sd_within_player": round(float(dev.std()), 2),
                        "corr_with_points_deviation": round(float(dev.corr(d["_dy"])), 3)}
        out["seasons"][season] = row
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def build_evaluation(
    season: str = TEST_SEASON, horizons: tuple[int, ...] = HORIZONS,
    *, season_end_ratings: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Every (decision_gw, target_gw, player) prediction, with its actual.

    For a decision made before gameweek ``d``, features are frozen at ``d-1`` and
    the model projects ``d .. d+5``. Horizon 6 therefore uses exactly the same
    information horizon 1 did — which is the point of measuring them separately.
    """
    hist = histdata.load_season(season)
    df = hist.frame
    leakage.assert_no_leakage(histdata.FEATURE_COLUMNS, context="adapter features")

    max_gw = int(df["GW"].max())
    records: list[pd.DataFrame] = []
    coverage = {"decision_gws": 0, "rows": 0, "skipped_no_fixture": 0}

    for decision_gw in range(FIRST_DECISION_GW, max_gw + 1):
        # Features frozen the instant before this deadline.
        snap = df[df["GW"] == decision_gw]
        if snap.empty:
            continue
        feat = snap.drop_duplicates("element").set_index("element")
        ctx = _context_for(hist, decision_gw, season_end_ratings=season_end_ratings)
        # Per team, not `decision_gw - 1`: after a double the two disagree, and
        # `starts` is a fixture count.
        fixtures_played = hist.team_fixtures_played(decision_gw)
        recency = _recency_before(df, decision_gw)
        coverage["decision_gws"] += 1

        for h in horizons:
            target_gw = decision_gw + h - 1
            if target_gw > max_gw:
                continue
            tgt = df[df["GW"] == target_gw].copy()
            if tgt.empty:
                continue
            # Carry the frozen features onto the target fixture rows.
            cols = ["min_td", "starts_td", "xg90_td", "xa90_td", "defcon90_td",
                    "base_minutes", "base_starts", "base_xg90", "base_xa90",
                    "base_defcon90", "base_season", "value", "pos", "team_id"]
            for c in cols:
                tgt[c] = tgt["element"].map(feat[c])
            tgt = tgt.dropna(subset=["pos", "value", "opponent_team", "team_id"])
            if tgt.empty:
                coverage["skipped_no_fixture"] += 1
                continue
            tgt["pred"] = project_rows(tgt, ctx, fixtures_played, recency)
            # A DGW is two fixture rows; sum them, as the live projection does.
            # `xP` is deliberately NOT carried through. It is the archive's own
            # expected-points column, it is not FPL's pre-deadline `ep_next`, and
            # it fails the leakage contract — see leakage.POST_MATCH_FIELDS.
            agg = tgt.groupby("element").agg(
                pred=("pred", "sum"),
                actual=("total_points", "sum"),
                minutes=("minutes", "sum"),
                pos=("pos", "first"),
                value=("value", "first"),
                team_id=("team_id", "first"),
            ).reset_index()
            agg["decision_gw"] = decision_gw
            agg["target_gw"] = target_gw
            agg["horizon"] = h
            agg["naive"] = agg["element"].map(
                feat["pts_td"] / feat["games_td"].replace(0, np.nan)
            ).fillna(0.0)
            records.append(agg)
            coverage["rows"] += len(agg)

    if not records:
        raise histdata.MissingHistoryError("no evaluable rows were produced")
    ev = pd.concat(records, ignore_index=True)
    return ev, coverage


def run(
    data_dir: Path | None = None, season: str = TEST_SEASON,
    horizons: tuple[int, ...] = HORIZONS, write: bool = False,
    out_path: Path | None = None,
    baselines: dict[str, Any] | None = None,
    ablations: list[dict[str, Any]] | None = None,
    season_end_ratings: bool = False,
    with_minutes: bool = True,
) -> dict[str, Any]:
    """Evaluate and (only on request) persist.

    ``write`` defaults to False: an exploratory run must never silently rewrite
    the tracked ``data/backtest.json`` that the Accuracy page serves.
    """
    data_dir = data_dir or config.DATA_DIR
    ev, coverage = build_evaluation(
        season, horizons, season_end_ratings=season_end_ratings)

    # Only models this dataset can measure honestly. `fpl_xp` and the `ensemble`
    # that contains it were withdrawn in schema 4: both were computed from the
    # archive's `xP`, which is not FPL's pre-deadline `ep_next` and carries
    # same-gameweek information. What ships at h=1 is still the ep_next blend —
    # it is simply no longer claimed to be measured. See `withdrawn_baselines`.
    methods = {"gaffer": "pred", "naive": "naive"}
    have = {k: c for k, c in methods.items() if c in ev and ev[c].notna().any()}

    per_horizon: dict[str, Any] = {}
    for h in horizons:
        sub = ev[ev["horizon"] == h]
        if sub.empty:
            continue
        # FPL's own xP is a ONE-WEEK-AHEAD number published before each event's
        # own deadline. At h>=2 it therefore knows things the model's frozen
        # h-step forecast cannot, so comparing them there flatters it. (Its
        # near-flat rank correlation across horizons is the giveaway.) Report it
        # only where the comparison is fair.
        # Beyond h=1 neither ep_next nor the ensemble exists (ep_next is a
        # one-week-ahead number), so both are omitted rather than faked.
        usable = have
        block: dict[str, Any] = {
            "n": int(len(sub)),
            "mae": {k: round(_mae(sub[c], sub["actual"]), 3) for k, c in usable.items()},
            "rank_corr": {k: round(_rank_corr(sub, c), 3) for k, c in usable.items()},
        }
        if h == 1:  # decision metrics are only meaningful for the imminent week
            block["decisions"] = {
                k: _decision_metrics(sub, c) for k, c in usable.items()
            }
            block["transfers"] = {
                k: _transfer_regret(sub, c) for k, c in usable.items()
            }
        per_horizon[str(h)] = block

    h1 = ev[ev["horizon"] == 1]
    zero_min = int((ev["minutes"] == 0).sum())
    pre_season = _pre_season_block(ev)
    # A11. Measured on the same season, through the same leakage policy, and
    # reported in the same artifact — because a component that gates every
    # number above it should not be the one component with no error bar.
    minutes: dict[str, Any] | None = None
    if with_minutes:
        try:
            minutes = minutes_report(season, horizons)
        except histdata.MissingHistoryError as exc:
            # A season with no `starts` column has no start outcome to score
            # against. Say that, rather than measuring something adjacent.
            minutes = {"measured": False, "reason": str(exc)}

    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model_version": projection.MODEL_VERSION,
        "dataset": "vaastav/Fantasy-Premier-League merged_gw",
        "season": season,
        # The artifact states its own split. A reader who only ever sees the
        # published JSON should not have to trust that some comment in the
        # repository still matches it.
        "season_split": SEASON_SPLIT,
        "decision_gameweeks": f"GW{FIRST_DECISION_GW}-GW{int(ev['decision_gw'].max())}",
        "horizons": list(horizons),
        "coverage": {
            **coverage,
            "rows_evaluated": int(len(ev)),
            "zero_minute_rows_retained": zero_min,
            "zero_minute_share_pct": round(100.0 * zero_min / max(len(ev), 1), 1),
            "excluded": {
                "missing_position_price_or_fixture": coverage["skipped_no_fixture"],
                "note": "no row is excluded on post-match data",
            },
        },
        "leakage_check": {
            "enforced": True,
            "post_match_fields_in_features": leakage.check_features(
                histdata.FEATURE_COLUMNS
            ),
            "policy": "features use shift(1) season-to-date aggregates only",
        },
        "per_horizon": per_horizon,
        "pre_season": pre_season,
        # G20 — two curves, because the single one was a picture of the wrong
        # thing. `overall` runs over every player-gameweek including the ~61%
        # where nobody played, so its middle octiles go non-monotonic and the
        # page reads as though the model is anti-correlated with itself. That
        # dip is the minutes model (M9), rendered under a heading that says
        # points model. `appeared` restricts to players who actually featured
        # and isolates what the points model does once someone is on the pitch.
        # Publishing both is the honest answer: the first is what a manager
        # experiences, the second is what this panel claims to be about.
        "calibration": {
            "overall": _calibration(h1, "pred"),
            "appeared": _calibration(h1[h1["minutes"] > 0], "pred"),
            "by_position": _calibration_by_position(h1, "pred"),
            "note": (
                "`overall` includes player-gameweeks with zero minutes, which "
                "are the majority of the population. A player correctly "
                "projected at 1.3 who is then left out scores 0, so the middle "
                "of the curve measures whether we knew who would play, not "
                "whether we knew what they would score. `appeared` conditions "
                "on having played and is the like-for-like read of the points "
                "model."
            ),
        },
        "shipped_projection": {
            "model_version": projection.MODEL_VERSION,
            "next_gameweek": f"blend of Gaffer and FPL ep_next at w="
                             f"{config.EP_NEXT_BLEND_WEIGHT}, scaled by availability",
            "later_gameweeks": "Gaffer only — ep_next does not exist beyond h=1",
            "next_gameweek_status": "POLICY, NOT MEASURED. The blend weight cannot "
                                    "be fitted on this dataset because the archive "
                                    "has no faithful copy of the live ep_next.",
        },
        "withdrawn_baselines": WITHDRAWN_BASELINES,
        "model_candidates": MODEL_CANDIDATES,
        # Two measurements that did not become changes. Published for the same
        # reason `withdrawn_baselines` is: a refusal nobody can read is
        # indistinguishable from never having looked.
        "captaincy_candidate": CAPTAINCY_CANDIDATE,
        "clean_sheet_contradiction": CLEAN_SHEET_CONTRADICTION,
        "limitations": [
            "Team strength ratings are rebuilt each gameweek from matches already "
            "played (T-12). They are NOT the same construction the live pipeline "
            "uses, which reads FPL's published strength fields — so fixture-driven "
            "numbers here are indicative rather than exactly reproducible live.",
            "The dataset carries no status/chance-of-playing column, so every "
            "player resolves to the model's available branch; the availability "
            "path is exercised but never varied.",
            "DEFCON is measured here for the first time, and it carries most of "
            "the headline. 2025-26 is the only season in the archive with a "
            "`defensive_contribution` column, so every backtest before this one "
            "ran with `defcon90_td` identically zero. Ablated here (thresholds "
            "neutralised) it is worth +3.4 legal-XI points per gameweek (49.3 "
            "with, 45.9 without) and +0.90 captain points, while making MAE "
            "0.014 WORSE — so ~72% of the model's 4.7-point margin over the "
            "naive baseline comes from the one component measurable for exactly "
            "one season. One season is one season; read the margin accordingly.",
            "Transfer regret is a one-free-transfer greedy sequence, not the "
            "shipped multi-period solver.",
            "Captain regret is the largest addressable number on this page and "
            "it was attacked and not moved. Nineteen armband rules built on "
            "the published distribution — `boom`, `ceiling`, `floor`, `std` "
            "and blends — were scored with squad and XI held fixed, and none "
            "survived selection on 2024-25 with corroboration on 2023-24. "
            "`boom` is worse than expected points in every season at every "
            "Monte-Carlo seed. See `captaincy_candidate`: the reason is that "
            "the distribution is a re-reading of the same rates the point "
            "estimate uses, so it does not know which player hauls either.",
            "The clean-sheet probability behind every defender and goalkeeper "
            "on this page is barely distinguishable from the league clean-"
            "sheet rate — Brier 0.1899 against a base rate of 0.1901 — and it "
            "is over-confident above 0.35: a claimed 0.49 realises 0.29. The "
            "projection additionally holds a SECOND, better-calibrated "
            "estimate of the same quantity that `p_cs` does not read. Measured "
            "and not fixed; see `clean_sheet_contradiction`.",
            "Squad selection maximises projected points under budget, quota and "
            "the club limit. It is close to, but not identical to, the shipped "
            "optimiser, which also carries a ceiling term and a goalkeeper "
            "spend penalty.",
            "The 'gaffer' column is the standalone component model. What ships "
            "for the NEXT gameweek is a blend of it with FPL's live ep_next, and "
            "that blend is unmeasurable here — see `withdrawn_baselines`. Beyond "
            "h=1 the shipped projection is exactly this column.",
            "An in-sample gain is never evidence. T-12's clamp sweep was fitted "
            "on 2023-24, and the two p_start corrections below were rejected on "
            "2023-24 AND 2024-25 — now the train and select seasons. Nothing has "
            "ever been fitted, swept or rejected against 2025-26. That is a "
            "stronger claim than this line used to make: while 2024-25 was the "
            "reporting season, a rejection had already looked at it.",
            "`p_start` from a prior season is `starts / 38`, which cannot "
            "separate rotation from injury absence — the denominator assumes "
            "the player was available for all 38. Two price-prior corrections "
            "were measured and both were rejected: a symmetric blend degraded "
            "nailed cheap players, and an upward-only floor left XI points flat "
            "while worsening rank correlation and MAE at every weight on both "
            "2023-24 and 2024-25 — which are now the train and select seasons, "
            "not this one. Absence appears to predict absence, so the conflation is "
            "crude rather than simply wrong. A real fix needs per-fixture "
            "history, not a constant.",
            "The minutes model was measured for the first time in this "
            "schema version and it LOST to every naive baseline it was given. "
            "A18 then acted on that: `p_start` no longer needs current-season "
            "minutes before it will believe a season-to-date zero. At h=1 it "
            "now scores a Brier of 0.114 where it scored 0.150, beats the "
            "three-game rolling start rate at h=2 through h=6, and is "
            "indistinguishable from two of the three baselines at h=1. It is "
            "still beaten by `start_rate_r3` at h=1. See `minutes_model` and "
            "`minutes_model.candidate_fix`.",
            "Every number on this page moved with it, because `p_start` "
            "multiplies through every rate in `fixture_rates`. On this season "
            "at h=1, MAE went 1.539 -> 1.114 against the naive baseline's "
            "1.075, and rank correlation 0.455 -> 0.626 against 0.692. The "
            "naive baseline still wins both; it used to win them by an order "
            "more.",
            "The PRICE prior — the arm that fires when a player has no minutes "
            "this season and no usable prior season — was a third of every "
            "backtested row before A18. Those rows were told they had a ~29% "
            "chance of starting and started 2.3% of the time, in each of the "
            "three most recent seasons. It is now 4.1% of rows, and the rows "
            "left in it start 17.5% of the time. It was the single largest "
            "source of minutes error and it is no longer the largest anything.",
            "This artifact reports 2025-26; the one before it reported 2024-25. "
            "They are not two readings of one instrument. 2025-26 has a DEFCON "
            "column and 2024-25 has none, and zero-minute share is 61.4% "
            "against 58.0%. At heuristic-0.6 the model wins legal-XI points on "
            "both (50.1 to 44.8 here, 53.6 to 51.3 there) and leads at all six "
            "horizons on both, while still losing captaincy on this season "
            "(5.89 to 5.97) and winning it on 2024-25 (8.92 to 8.00). The "
            "naive baseline still beats it on MAE and rank correlation in "
            "both. At heuristic-0.5 the XI result INVERTED between the two "
            "seasons; A18 is what removed that, and a finding this artifact "
            "once led on turning out to be one projection change deep is "
            "itself worth knowing.",
            "GW1 is included, and the naive baseline does not exist there: it is "
            "cumulative season-to-date points-per-game, which is 0 for everyone "
            "before a ball is kicked. `rank_corr` skips zero-variance "
            "gameweeks, so the naive figure in `per_horizon` averages 37 "
            "gameweeks where the model's averages 38. Read `pre_season` for the "
            "GW1 regime on its own; do not read the two aggregates as a "
            "like-for-like comparison there.",
        ],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # Optional evidence blocks. `baselines` compares this model against frozen
    # earlier versions; `ablations` records which changes earned their place.
    if baselines:
        out["baselines"] = baselines
    if ablations:
        out["ablations"] = ablations
    # Omitted rather than nulled when `--no-minutes` was asked for: a key
    # present and empty reads as "measured, and it found nothing".
    if minutes is not None:
        out["minutes_model"] = minutes
    if write:
        target = Path(out_path) if out_path else data_dir / "backtest.json"
        # Say exactly what is being overwritten, before it happens.
        print(f"[backtest] WRITING {target}", file=sys.stderr)
        write_json_atomic(target, out)
        out["_written_to"] = str(target)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Backtest the shipped projection (read-only by default)")
    ap.add_argument("--season", default=TEST_SEASON)
    ap.add_argument("--horizons", type=int, nargs="*", default=list(HORIZONS))
    ap.add_argument(
        "--write", action="store_true",
        help="persist the result. Without --out this overwrites the tracked "
             "data/backtest.json that the Accuracy page serves.")
    ap.add_argument("--out", default=None,
                    help="explicit output path (implies --write)")
    ap.add_argument("--xp-diagnostic", action="store_true",
                    help="reproduce the measurement that withdrew the fpl_xp "
                         "baseline, and exit")
    ap.add_argument("--minutes-only", action="store_true",
                    help="print the minutes-model block alone and exit. Writes "
                         "nothing, and skips the points backtest, which is the "
                         "slow half.")
    ap.add_argument("--no-minutes", action="store_true",
                    help="omit the minutes-model block. It is part of the "
                         "artifact; this exists for a fast points-only run.")
    args = ap.parse_args(argv)
    if args.minutes_only:
        try:
            print(json.dumps(minutes_report(
                args.season, tuple(args.horizons)), indent=2))
        except histdata.MissingHistoryError as exc:
            print(f"minutes backtest unavailable: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.xp_diagnostic:
        try:
            print(json.dumps(xp_leakage_diagnostic(), indent=2))
        except histdata.MissingHistoryError as exc:
            print(f"diagnostic unavailable: {exc}", file=sys.stderr)
            return 2
        return 0
    write = args.write or args.out is not None
    try:
        out = run(season=args.season, horizons=tuple(args.horizons),
                  write=write, out_path=Path(args.out) if args.out else None,
                  with_minutes=not args.no_minutes)
    except histdata.MissingHistoryError as exc:
        print(f"backtest unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
