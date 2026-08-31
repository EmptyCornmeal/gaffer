"""The h=1 blend only runs when the external forecast carries information.

Two measurements, a season apart in character but the same failure.

Pre-season 2026/27, a week before the GW1 deadline: `ep_next` topped out at
exactly 4.0 across all 587 players, and a 15.5m striker, a 12.0m midfielder and
a 6.0m goalkeeper all held that same 4.0. Blending 70% of that collapsed the
recommended XI from the model's own 66.2 expected points to a published 43.6
and — because the deflation is uneven — reordered the players the decision
turns on.

After GW1, on 2026-08-31: `ep_next` was exactly equal to FPL's own
backward-looking `form` for 596 of 626 players (95.2%), equal to `ep_this` for
614, and took 30 distinct values across the whole game. It carries no fixture
adjustment and no team news — which is the only thing the deference argument
ever rested on. Forty players were published above their own simulated
90th-percentile ceiling, a backup goalkeeper at 7.27 against a ceiling of 2.0.
Both guards were switched off at the time by a `season_started` bypass.
"""
from __future__ import annotations

import pytest

from gaffer import config
from gaffer.model import projection as P


def pairs(eps, mods):
    return list(zip(eps, mods, strict=True))


# The real shape: FPL clipped at 4.0, Gaffer's own model spread up to ~7.6.
DEGENERATE = pairs(
    [1.0, 1.5, 1.7, 2.0, 2.1, 2.2, 2.5, 2.6, 3.2, 4.0, 4.0, 4.0],
    [0.8, 1.4, 1.9, 2.6, 3.1, 3.4, 4.2, 4.6, 5.2, 6.5, 7.0, 7.6],
)

# In-season: the external number spreads at least as widely as ours.
INFORMATIVE = pairs(
    [0.9, 1.6, 2.0, 2.7, 3.3, 3.9, 4.6, 5.2, 5.9, 6.8, 7.4, 8.6],
    [0.8, 1.4, 1.9, 2.6, 3.1, 3.4, 4.2, 4.6, 5.2, 6.5, 7.0, 7.6],
)

#: `form` values that agree with INFORMATIVE's ep_next only occasionally — what
#: a source that adds a fixture and a team-news adjustment to a form baseline
#: looks like.
DIVERGENT_FORM = [0.5, 1.6, 1.0, 2.7, 2.0, 4.5, 3.0, 6.0, 4.0, 8.0, 5.0, 9.5]


# --- the three degeneracy tests --------------------------------------------

def test_a_clipped_pre_season_forecast_is_not_blended():
    r = P.ep_next_regime(DEGENERATE, season_started=False)
    assert r["regime"] == P.REGIME_COMPONENT_ONLY
    assert r["blend_weight"] == 0.0
    assert "4" in r["reason"], "the reason should name the ceiling it found"


def test_an_informative_forecast_is_blended_at_the_configured_weight():
    r = P.ep_next_regime(INFORMATIVE, season_started=False)
    assert r["regime"] == P.REGIME_BLENDED
    assert r["blend_weight"] == pytest.approx(config.EP_NEXT_BLEND_WEIGHT)


def test_a_compressed_forecast_is_rejected_even_above_the_absolute_floor():
    """The relative test is the self-calibrating one: a source can top out high
    and still be unable to separate anybody."""
    compressed = pairs(
        [4.6, 4.7, 4.8, 4.9, 5.0, 5.0, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6],
        [0.8, 1.4, 1.9, 2.6, 3.1, 3.4, 4.2, 4.6, 5.2, 6.5, 7.0, 7.6],
    )
    r = P.ep_next_regime(compressed, season_started=False)
    assert r["regime"] == P.REGIME_COMPONENT_ONLY
    assert r["spread_ratio"] < P.EP_NEXT_MIN_SPREAD_RATIO


def test_too_small_a_sample_is_not_blended():
    r = P.ep_next_regime(pairs([4.0, 3.0], [5.0, 4.0]), season_started=False)
    assert r["regime"] == P.REGIME_COMPONENT_ONLY
    assert r["sample"] == 2


# --- the collapse-onto-form test (2026-08-31) -------------------------------

def test_ep_next_that_is_just_fpls_form_is_not_blended():
    """The live GW3 2026-27 shape: a source that passes both older guards — it
    tops out at 8.6 and spreads wider than the model — and is still not a
    forecast, because it IS the backward-looking average."""
    forms = [e for e, _ in INFORMATIVE]  # ep_next == form for everybody
    r = P.ep_next_regime(INFORMATIVE, season_started=True, forms=forms)
    assert r["regime"] == P.REGIME_COMPONENT_ONLY
    assert r["blend_weight"] == 0.0
    assert r["form_match"] == 1.0
    assert "form" in r["reason"]
    # the older guards would both have passed this payload
    assert r["ep_max"] > P.EP_NEXT_MIN_POPULATION_MAX
    assert r["spread_ratio"] >= P.EP_NEXT_MIN_SPREAD_RATIO


def test_the_collapse_test_tolerates_incidental_agreement():
    """Coarse one-decimal values collide by chance. A source that mostly moves
    off form is still blended."""
    r = P.ep_next_regime(INFORMATIVE, season_started=True, forms=DIVERGENT_FORM)
    assert r["form_match"] <= P.EP_NEXT_MAX_FORM_MATCH
    assert r["regime"] == P.REGIME_BLENDED


def test_the_measured_collapse_rate_fires_and_the_chance_rate_does_not():
    """Both ends of the threshold, against the numbers it was set from: 93%
    agreement measured live, 11% expected by chance from the same marginals."""
    n = 100
    eps = [1.0 + i * 0.1 for i in range(n)]
    mods = [0.5 + i * 0.07 for i in range(n)]
    ps = pairs(eps, mods)
    for rate, expected in ((0.93, P.REGIME_COMPONENT_ONLY),
                           (0.11, P.REGIME_BLENDED)):
        k = round(rate * n)
        forms = [eps[i] if i < k else eps[i] + 1.3 for i in range(n)]
        r = P.ep_next_regime(ps, season_started=True, forms=forms)
        assert r["regime"] == expected, (rate, r["form_match"], r["reason"])


def test_a_missing_form_does_not_silently_pass_the_collapse_test():
    r = P.ep_next_regime(INFORMATIVE, season_started=True, forms=None)
    assert r["regime"] == P.REGIME_BLENDED
    assert r["form_match"] is None
    assert "did not run" in r["reason"], (
        "a test that could not run must say so, not read as a pass")


def test_forms_must_be_parallel_to_pairs():
    with pytest.raises(ValueError):
        P.ep_next_regime(INFORMATIVE, season_started=True, forms=[1.0, 2.0])


def test_too_few_forms_leaves_the_collapse_rate_unmeasured():
    forms: list[float | None] = [e for e, _ in INFORMATIVE]
    keep = P.EP_NEXT_MIN_SAMPLE - 1
    forms[keep:] = [None] * (len(forms) - keep)
    r = P.ep_next_regime(INFORMATIVE, season_started=True, forms=forms)
    assert r["form_match"] is None
    assert r["form_sample"] < P.EP_NEXT_MIN_SAMPLE


# --- the season_started bypass must not come back ---------------------------

def test_a_completed_gameweek_does_not_disable_the_guards():
    """REGRESSION. `ep_next_regime` used to return BLENDED unconditionally once
    `season_started` was true, on the assumption that a completed gameweek makes
    ep_next "computed from real form and fixtures". It is computed from form and
    NOT from fixtures, and the bypass disabled both guards exactly when the
    season made them checkable — which is how 40 players came to be published
    above their own simulated ceiling."""
    r = P.ep_next_regime(DEGENERATE, season_started=True)
    assert r["regime"] == P.REGIME_COMPONENT_ONLY, (
        "the season_started bypass is back")
    assert r["blend_weight"] == 0.0

    forms = [e for e, _ in INFORMATIVE]
    collapsed = P.ep_next_regime(INFORMATIVE, season_started=True, forms=forms)
    assert collapsed["regime"] == P.REGIME_COMPONENT_ONLY, (
        "the season_started bypass is back")


def test_season_started_is_recorded_but_changes_no_verdict():
    off = P.ep_next_regime(INFORMATIVE, season_started=False)
    on = P.ep_next_regime(INFORMATIVE, season_started=True)
    assert off["season_started"] is False and on["season_started"] is True
    assert off["regime"] == on["regime"]
    assert off["blend_weight"] == on["blend_weight"]


def test_the_regime_reports_what_it_measured():
    r = P.ep_next_regime(DEGENERATE, season_started=False)
    assert r["sample"] == len(DEGENERATE)
    assert r["ep_max"] == 4.0
    assert r["spread_ratio"] is not None and r["model_spread"] is not None
    assert r["reason"], "a regime with no reason is not auditable"


def test_the_reason_describes_this_run_not_an_assumption():
    """The reason reaches meta.json and is the only audit trail a reader gets.
    It used to assert that ep_next "is computed from real form and fixtures",
    which was a claim about FPL, was never measured, and was false."""
    r = P.ep_next_regime(INFORMATIVE, season_started=True, forms=DIVERGENT_FORM)
    assert "measured this run" in r["reason"]
    assert "fixtures" not in r["reason"], (
        "the reason must not claim ep_next contains a fixture adjustment")
    assert f"{r['spread_ratio']:.2f}" in r["reason"]


# --- rotation attenuation ---------------------------------------------------

def test_rotation_scale_ramps_between_the_configured_points():
    lo = config.EP_NEXT_ROTATION_ZERO_P_START
    hi = config.EP_NEXT_ROTATION_FULL_P_START
    assert P.rotation_scale(0.0) == 0.0
    assert P.rotation_scale(lo) == 0.0
    assert P.rotation_scale(hi) == 1.0
    assert P.rotation_scale(1.0) == 1.0
    assert P.rotation_scale((lo + hi) / 2) == pytest.approx(0.5)


def test_a_missing_p_start_does_not_silently_kill_the_blend():
    assert P.rotation_scale(None) == 1.0


def test_a_backup_keeper_does_not_receive_full_external_weight():
    """Tzolakis, 2026-08-31: p_start 0.30, model 0.90, ep_next 10.0 (his GW1
    haul, straight through FPL's `form`), published at 7.27 against his own
    simulated 90th-percentile ceiling of 2.0."""
    rows = [
        {"player_id": 1, "gw": 1, "exp_points": 0.9, "exp_points_model": 0.9,
         "exp_points_ep_next": 10.0, "p_start": 0.30},
    ] + [
        {"player_id": i + 2, "gw": 1, "exp_points": m, "exp_points_model": m,
         "exp_points_ep_next": e, "p_start": 0.9}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    avail = {r["player_id"]: 1.0 for r in rows}
    regime = P.apply_ep_next_blend(rows, from_gw=1, availability=avail,
                                   season_started=True, form=None)
    assert regime["regime"] == P.REGIME_BLENDED
    assert rows[0]["exp_points"] == pytest.approx(0.9), (
        "a 30%-to-start keeper must get none of the external weight")
    nailed = rows[-1]
    w = config.EP_NEXT_BLEND_WEIGHT
    assert nailed["exp_points"] == pytest.approx(
        round((1 - w) * nailed["exp_points_model"]
              + w * nailed["exp_points_ep_next"], 3))


def test_rotation_risk_attenuates_between_the_two_ends():
    mid = (config.EP_NEXT_ROTATION_ZERO_P_START
           + config.EP_NEXT_ROTATION_FULL_P_START) / 2
    rows = [
        {"player_id": i + 1, "gw": 1, "exp_points": m, "exp_points_model": m,
         "exp_points_ep_next": e, "p_start": mid}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    P.apply_ep_next_blend(rows, from_gw=1,
                          availability={r["player_id"]: 1.0 for r in rows},
                          season_started=True)
    w = config.EP_NEXT_BLEND_WEIGHT * 0.5
    for r in rows:
        assert r["exp_points"] == pytest.approx(
            round((1 - w) * r["exp_points_model"]
                  + w * r["exp_points_ep_next"], 3), abs=1e-3)


def test_availability_and_rotation_both_bite():
    rows = [
        {"player_id": i + 1, "gw": 1, "exp_points": m, "exp_points_model": m,
         "exp_points_ep_next": e, "p_start": 1.0}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    avail = {r["player_id"]: 1.0 for r in rows}
    avail[1] = 0.5
    P.apply_ep_next_blend(rows, from_gw=1, availability=avail,
                          season_started=True)
    w = config.EP_NEXT_BLEND_WEIGHT * 0.5
    assert rows[0]["exp_points"] == pytest.approx(
        round((1 - w) * rows[0]["exp_points_model"]
              + w * rows[0]["exp_points_ep_next"], 3))


def test_the_applied_weight_is_reported_not_just_the_nominal_one():
    rows = [
        {"player_id": i + 1, "gw": 1, "exp_points": m, "exp_points_model": m,
         "exp_points_ep_next": e, "p_start": 0.2}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    regime = P.apply_ep_next_blend(
        rows, from_gw=1, availability={r["player_id"]: 1.0 for r in rows},
        season_started=True)
    assert regime["blend_weight"] == pytest.approx(config.EP_NEXT_BLEND_WEIGHT)
    assert regime["blend_weight_applied_mean"] == 0.0
    assert regime["blend_weight_zeroed"] == len(rows)
    assert "falling to zero for" in regime["reason"]
    for r in rows:
        assert r["exp_points"] == pytest.approx(r["exp_points_model"])


# --- apply_ep_next_blend ----------------------------------------------------

def _rows(n=12, gw=1, p_start=1.0):
    """Rows shaped like `projection.project` builds them."""
    eps = [e for e, _ in DEGENERATE][:n]
    mods = [m for _, m in DEGENERATE][:n]
    return [
        {"player_id": i + 1, "gw": gw, "exp_points": mods[i],
         "exp_points_model": mods[i], "exp_points_ep_next": eps[i],
         "p_start": p_start}
        for i in range(n)
    ]


def test_when_the_guard_fires_the_published_number_is_the_model_number():
    rows = _rows()
    avail = {r["player_id"]: 1.0 for r in rows}
    regime = P.apply_ep_next_blend(rows, from_gw=1, availability=avail,
                                   season_started=False)
    assert regime["regime"] == P.REGIME_COMPONENT_ONLY
    for r in rows:
        assert r["exp_points"] == r["exp_points_model"]


def test_when_the_guard_lifts_the_published_number_moves_toward_ep_next():
    rows = [
        {"player_id": i + 1, "gw": 1, "exp_points": m,
         "exp_points_model": m, "exp_points_ep_next": e, "p_start": 1.0}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    avail = {r["player_id"]: 1.0 for r in rows}
    regime = P.apply_ep_next_blend(rows, from_gw=1, availability=avail,
                                   season_started=False)
    assert regime["regime"] == P.REGIME_BLENDED
    w = config.EP_NEXT_BLEND_WEIGHT
    for r in rows:
        expected = round((1 - w) * r["exp_points_model"]
                         + w * r["exp_points_ep_next"], 3)
        assert r["exp_points"] == pytest.approx(expected)


def test_the_blend_never_touches_later_gameweeks():
    rows = _rows() + [
        {"player_id": 1, "gw": 2, "exp_points": 4.0,
         "exp_points_model": 4.0, "exp_points_ep_next": 9.0, "p_start": 1.0}
    ]
    P.apply_ep_next_blend(rows, from_gw=1, availability={1: 1.0},
                          season_started=True)
    later = [r for r in rows if r["gw"] == 2][0]
    assert later["exp_points"] == 4.0, "ep_next is a one-week number"


def test_an_unavailable_player_is_not_resurrected_by_the_blend():
    rows = [
        {"player_id": i + 1, "gw": 1, "exp_points": m,
         "exp_points_model": m, "exp_points_ep_next": e, "p_start": 1.0}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    avail = {r["player_id"]: 1.0 for r in rows}
    avail[1] = 0.0  # ruled out by our own availability read
    P.apply_ep_next_blend(rows, from_gw=1, availability=avail,
                          season_started=False)
    assert rows[0]["exp_points"] == pytest.approx(rows[0]["exp_points_model"])


def test_the_collapse_check_reads_form_for_the_eligible_players_only():
    """The forms list is built alongside the pairs, so a player excluded from
    the paired population must not shift the match rate."""
    rows = [
        {"player_id": i + 1, "gw": 1, "exp_points": m,
         "exp_points_model": m, "exp_points_ep_next": e, "p_start": 1.0}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    rows.append({"player_id": 99, "gw": 1, "exp_points": 3.0,
                 "exp_points_model": 3.0, "exp_points_ep_next": 0.0,
                 "p_start": 1.0})
    form = {r["player_id"]: r["exp_points_ep_next"] for r in rows}
    form[99] = 999.0  # ineligible, and would break the rate if it were read
    regime = P.apply_ep_next_blend(
        rows, from_gw=1,
        availability={r["player_id"]: 1.0 for r in rows},
        season_started=True, form=form)
    assert regime["form_sample"] == len(INFORMATIVE)
    assert regime["form_match"] == 1.0
    assert regime["regime"] == P.REGIME_COMPONENT_ONLY


def test_the_regime_is_recorded_where_the_artifact_can_read_it(tmp_path):
    from gaffer.store import db
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    regime = P.ep_next_regime(DEGENERATE, season_started=False)
    P.record_regime(conn, regime)
    stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert stored["projection_regime"] == P.REGIME_COMPONENT_ONLY
    assert stored["projection_regime_reason"]
    assert float(stored["ep_next_blend_weight"]) == 0.0
    assert "ep_next_form_match" in stored
    conn.close()


def test_the_recorded_reason_names_the_collapse_when_that_is_what_fired(tmp_path):
    from gaffer.store import db
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    forms = [e for e, _ in INFORMATIVE]
    regime = P.ep_next_regime(INFORMATIVE, season_started=True, forms=forms)
    P.record_regime(conn, regime)
    stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert "form" in stored["projection_regime_reason"]
    assert float(stored["ep_next_form_match"]) == 1.0
    conn.close()


# --- the whole path, on rows `project` actually builds -----------------------

def test_projected_rows_carry_the_p_start_the_blend_needs():
    """The rotation scaler reads `p_start` off the projection row. If the row
    ever stopped carrying it the scaler would silently return 1.0 and the defect
    would come back without a single test failing."""
    from dataclasses import fields
    assert "p_start" in {f.name for f in fields(P.GwProjection)}
