"""The h=1 blend only runs when the external forecast carries information.

Measured on the live 2026/27 pre-season payload a week before the GW1 deadline:
`ep_next` topped out at exactly 4.0 across all 587 players, and a 15.5m striker,
a 12.0m midfielder and a 6.0m goalkeeper all held that same 4.0. Blending 70% of
that collapsed the recommended XI from the model's own 66.2 expected points to a
published 43.6 and — because the deflation is uneven — reordered the players the
decision turns on.
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


def test_the_guard_cannot_fire_once_a_gameweek_has_completed():
    """This is what makes the restoration automatic rather than something a
    human has to remember to undo."""
    r = P.ep_next_regime(DEGENERATE, season_started=True)
    assert r["regime"] == P.REGIME_BLENDED
    assert r["blend_weight"] == pytest.approx(config.EP_NEXT_BLEND_WEIGHT)


def test_too_small_a_sample_is_not_blended():
    r = P.ep_next_regime(pairs([4.0, 3.0], [5.0, 4.0]), season_started=False)
    assert r["regime"] == P.REGIME_COMPONENT_ONLY
    assert r["sample"] == 2


def test_the_regime_reports_what_it_measured():
    r = P.ep_next_regime(DEGENERATE, season_started=False)
    assert r["sample"] == len(DEGENERATE)
    assert r["ep_max"] == 4.0
    assert r["spread_ratio"] is not None and r["model_spread"] is not None
    assert r["reason"], "a regime with no reason is not auditable"


def _rows(n=12, gw=1):
    """Rows shaped like `projection.project` builds them."""
    eps = [e for e, _ in DEGENERATE][:n]
    mods = [m for _, m in DEGENERATE][:n]
    return [
        {"player_id": i + 1, "gw": gw, "exp_points": mods[i],
         "exp_points_model": mods[i], "exp_points_ep_next": eps[i]}
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
         "exp_points_model": m, "exp_points_ep_next": e}
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
         "exp_points_model": 4.0, "exp_points_ep_next": 9.0}
    ]
    P.apply_ep_next_blend(rows, from_gw=1, availability={1: 1.0},
                          season_started=True)
    later = [r for r in rows if r["gw"] == 2][0]
    assert later["exp_points"] == 4.0, "ep_next is a one-week number"


def test_an_unavailable_player_is_not_resurrected_by_the_blend():
    rows = [
        {"player_id": i + 1, "gw": 1, "exp_points": m,
         "exp_points_model": m, "exp_points_ep_next": e}
        for i, (e, m) in enumerate(INFORMATIVE)
    ]
    avail = {r["player_id"]: 1.0 for r in rows}
    avail[1] = 0.0  # ruled out by our own availability read
    P.apply_ep_next_blend(rows, from_gw=1, availability=avail,
                          season_started=False)
    assert rows[0]["exp_points"] == pytest.approx(rows[0]["exp_points_model"])


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
    conn.close()
