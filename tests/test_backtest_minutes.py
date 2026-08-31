"""A11 — the minutes model must be measured, and measured honestly.

`p_start` gates every projection Gaffer publishes: it scales every rate, it
discounts FPL's own `ep_next` through `rotation_scale`, it drives the autosubs
and the solver, and it is what the NAILED / ROTATION / CAMEO? badge reports. For
seven schema versions it was the one major component with no reported error rate
at all.

These tests pin the measurement to the shipped code path (the discipline
`test_backtest_parity.py` applies to the points model), pin the reported bands to
the badge a user actually sees, and pin the honesty rules: no post-match feature,
no baseline invented where none exists, and no sentence in the artifact that the
numbers beside it contradict.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from gaffer import backtest, config, histdata, leakage
from gaffer.model import features as F
from gaffer.model import projection
from gaffer.model.rationale import xmins_badge

# ---------------------------------------------------------------------------
# The bands reported must be the bands shipped
# ---------------------------------------------------------------------------


def test_reported_bands_match_the_shipped_badge_exactly():
    """`START_BANDS` mirrors `rationale.xmins_badge`'s thresholds.

    A mirror that drifts would publish an accuracy figure for a badge nobody
    sees. Swept in 0.005 steps rather than spot-checked, because the failure
    mode is an edge — `>= 0.85` against `> 0.85` is one player in a band of
    three thousand, and it changes nothing until it changes the headline.
    """
    for p in np.arange(0.0, 1.0001, 0.005):
        p = round(float(p), 4)
        shipped = xmins_badge(p * 90.0, p)["label"]
        reported = next(n for n, lo, hi in backtest.START_BANDS if lo <= p < hi)
        assert reported == shipped, (
            f"p={p}: the artifact says {reported}, the badge says {shipped}")


def test_the_bands_partition_the_whole_probability_range():
    """Every p in [0, 1] falls in exactly one band. No gaps, no overlap."""
    for p in np.arange(0.0, 1.0001, 0.001):
        hits = [n for n, lo, hi in backtest.START_BANDS if lo <= float(p) < hi]
        assert len(hits) == 1, f"p={p} landed in {hits}"


# ---------------------------------------------------------------------------
# Parity with the shipped gate
# ---------------------------------------------------------------------------

_BASE = {
    "position": "MID", "minutes": 900.0, "starts": 10.0,
    "base_minutes": 2400.0, "base_starts": 28.0,
    "base_xg90": 0.31, "base_xa90": 0.22, "base_defcon90": 9.5,
    "base_season": "2024-25",
    "price": 75.0, "xg_per_90": 0.42, "xa_per_90": 0.18,
    "defcon_per_90": 4.5, "team_id": 1,
}


def _ctx() -> F.TeamContext:
    return F.TeamContext.from_ratings(
        att_home={1: 1200.0, 2: 1050.0}, att_away={1: 1150.0, 2: 1000.0},
        def_home={1: 1180.0, 2: 1020.0}, def_away={1: 1120.0, 2: 980.0},
        team_xgc={1: 1.25, 2: 1.55})


def test_p_start_does_not_vary_with_the_fixture():
    """`build_minutes_evaluation` depends on this being true.

    `p_start` is computed once per (decision gameweek, player) and carried onto
    every target fixture at every horizon. That is only legitimate because the
    minutes gate reads no fixture input at all. If a future change to
    `fixture_rates` gives it one, this fails here rather than silently
    publishing a horizon-6 number that is really a horizon-1 number.
    """
    ctx = _ctx()
    avail = projection._availability("a", None)
    seen = set()
    for opponent in (1, 2):
        for home in (True, False):
            for gw in (5, 20, 38):
                fx = F.Fixture(gw=gw, opponent_id=opponent, at_home=home, fdr=3)
                r = projection.fixture_rates(_BASE, fx, ctx, avail, 11)
                seen.add((round(r["p_start"], 12), round(r["exp_minutes"], 12)))
    assert len(seen) == 1, f"the minutes gate moved with the fixture: {seen}"


def test_the_branch_label_names_the_formula_that_produced_p_start():
    """`_minutes_branch` transcribes conditions that live in `projection.py`.

    Duplicated logic is a liability, so it is checked against behaviour rather
    than against the source it was copied from: recompute all three arms over a
    grid of players and assert `p_start` equals the one the label names.
    """
    fx = F.Fixture(gw=5, opponent_id=2, at_home=True, fdr=3)
    ctx = _ctx()
    checked = set()
    for fixtures_played in (0, 2, 3, 11, 38):
        for cur_minutes in (0.0, 1.0, 900.0):
            for base_minutes in (0.0, 100.0, 2400.0):
                for base_starts in (0.0, 28.0):
                    player = {**_BASE, "minutes": cur_minutes,
                              "base_minutes": base_minutes,
                              "base_starts": base_starts, "starts": 10.0}
                    rates = projection.fixture_rates(
                        player, fx, ctx, 1.0, fixtures_played)
                    branch = backtest._minutes_branch(player, fixtures_played)
                    checked.add(branch)
                    expected = {
                        "current_season": min(
                            player["starts"] / max(fixtures_played, 1), 0.98),
                        "prior_season": min(base_starts / 38.0, 0.98),
                        "price_prior": projection._start_prior(
                            player["position"], player["price"]),
                    }[branch]
                    assert rates["p_start"] == pytest.approx(expected, abs=1e-12), (
                        f"branch={branch} fixtures={fixtures_played} "
                        f"cur={cur_minutes} base={base_minutes}/{base_starts}")
    assert checked == {"current_season", "prior_season", "price_prior"}, (
        f"the grid never exercised every arm: {checked}")


def test_the_price_prior_branch_reads_price_and_nothing_else():
    """The finding this block turned on, stated as a test.

    What this arm answers is a function of PRICE: two players with different
    histories get the same number as long as they cost the same and neither has
    a usable sample. A18 shrank the population it fires on from a third of every
    row to 4.1% — it now needs `starts` to be UNREADABLE, not merely zero — so
    both players here carry `starts: None`, which is what a row predating the
    column looks like.
    """
    a = {**_BASE, "minutes": 0.0, "starts": None,
         "base_minutes": 0.0, "base_starts": 0.0}
    b = {**_BASE, "minutes": 0.0, "starts": None,
         "base_minutes": 100.0, "base_starts": 0.0,
         "xg_per_90": 0.0, "xa_per_90": 0.0}
    fx = F.Fixture(gw=5, opponent_id=2, at_home=True, fdr=3)
    ra = projection.fixture_rates(a, fx, _ctx(), 1.0, 8)
    rb = projection.fixture_rates(b, fx, _ctx(), 1.0, 8)
    assert backtest._minutes_branch(a, 8) == "price_prior"
    assert backtest._minutes_branch(b, 8) == "price_prior"
    assert ra["p_start"] == pytest.approx(rb["p_start"])


def test_a_readable_zero_no_longer_reaches_the_price_prior():
    """A18, from the measuring side.

    The same player with `starts: 0` instead of `starts: None` is not a missing
    column — it is eight fixtures of evidence that he does not start — and both
    the projection and the branch label must now say so.
    """
    p = {**_BASE, "minutes": 0.0, "starts": 0.0,
         "base_minutes": 2400.0, "base_starts": 28.0}
    fx = F.Fixture(gw=5, opponent_id=2, at_home=True, fdr=3)
    assert backtest._minutes_branch(p, 8) == "current_season"
    assert projection.fixture_rates(p, fx, _ctx(), 1.0, 8)["p_start"] == 0.0
    # ...and below the sample threshold it still is not evidence.
    assert backtest._minutes_branch(p, 2) == "prior_season"
    assert projection.fixture_rates(p, fx, _ctx(), 1.0, 2)["p_start"] == \
        pytest.approx(28 / 38.0, abs=1e-12)

def test_the_minutes_features_are_leak_free():
    assert leakage.check_features(backtest.MINUTES_FEATURE_COLUMNS) == []


@pytest.mark.parametrize("col", ["starts", "minutes"])
def test_the_targets_are_post_match_and_are_not_features(col):
    """`starts` is the target. It must stay one."""
    assert leakage.is_post_match(col)
    assert col not in backtest.MINUTES_FEATURE_COLUMNS


def test_every_baseline_is_named_and_described():
    """A baseline with no description is a column nobody can audit."""
    for name, why in backtest.MINUTES_BASELINES.items():
        assert not leakage.is_post_match(name), name
        assert len(why) > 30, f"{name} has no usable description"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_a_perfect_forecast_scores_zero_brier():
    y = [1, 0, 1, 1, 0]
    assert backtest._brier(y, y) == pytest.approx(0.0)


def test_brier_skill_is_zero_at_the_base_rate_and_negative_below_it():
    """Why the skill score is published beside the raw one.

    A constant equal to the base rate is the definition of no skill, and
    anything worse must read as negative — or the price prior's 0.09 goes on
    looking respectable next to a population that scores 0.022 by saying
    nothing.
    """
    y = np.array([1] * 20 + [0] * 80, dtype=float)
    assert backtest._brier_skill(np.full(100, 0.2), y) == pytest.approx(0.0, abs=1e-9)
    assert backtest._brier_skill(np.full(100, 0.6), y) < 0
    assert backtest._brier_skill(y, y) == pytest.approx(1.0)


def test_auc_is_calibration_free():
    """A forecast can be hopelessly mis-scaled and still order perfectly.

    That distinction decides what a fix would even be — a recalibration or a new
    feature — so the two metrics must be able to move apart.
    """
    y = np.array([0, 0, 1, 1])
    good = np.array([0.1, 0.2, 0.8, 0.9])
    shifted = good / 10.0  # same order, wildly wrong scale
    assert backtest._auc(good, y) == pytest.approx(1.0)
    assert backtest._auc(shifted, y) == pytest.approx(1.0)
    assert backtest._brier(shifted, y) > backtest._brier(good, y)


def test_auc_is_undefined_rather_than_wrong_on_a_one_class_population():
    assert np.isnan(backtest._auc([0.4, 0.6], [1, 1]))


def _frame(p, started, minutes=None, **cols) -> pd.DataFrame:
    return pd.DataFrame({
        "p_start": list(p), "started": list(started),
        "minutes": list(minutes) if minutes is not None else [90 * s for s in started],
        "exp_minutes": [90.0 * x for x in p],
        **cols,
    })


def test_bands_report_the_claim_and_the_outcome_separately():
    df = _frame([0.95, 0.90, 0.70, 0.65, 0.30, 0.10], [1, 0, 1, 0, 1, 0])
    bands = {b["band"]: b for b in backtest._start_bands(df)}
    assert bands["NAILED"]["n"] == 2
    assert bands["NAILED"]["claimed"] == pytest.approx(0.925)
    assert bands["NAILED"]["start_rate"] == pytest.approx(0.5)
    assert bands["ROTATION"]["n"] == 2
    assert bands["CAMEO?"]["n"] == 2


def test_a_band_with_no_players_is_omitted_rather_than_reported_as_zero():
    df = _frame([0.95, 0.92], [1, 1])
    assert [b["band"] for b in backtest._start_bands(df)] == ["NAILED"]


def test_calibration_bins_on_rank_so_a_piled_up_forecast_still_shows_a_curve():
    """`p_start` clusters on a handful of discrete values.

    `starts / fixtures_played` for a small denominator, and a price prior that
    is a function of price alone. Value-binning collapses that to three or four
    bins and hides the shape, which is why the curve is cut on the rank.
    """
    p = [0.25] * 90 + list(np.linspace(0.3, 0.98, 10))
    y = [0] * 90 + [1] * 10
    bins = backtest._start_calibration(_frame(p, y), bins=10)
    assert len(bins) == 10
    assert bins == sorted(bins, key=lambda b: b["pred"])
    assert sum(b["n"] for b in bins) == 100


def test_calibration_reports_nothing_rather_than_a_flat_line_on_no_variance():
    assert backtest._start_calibration(_frame([0.5] * 100, [1] * 50 + [0] * 50)) == []


def test_the_paired_interval_is_over_gameweeks_not_rows():
    """Rows inside a gameweek are not independent — one rotation moves eleven.

    A row-level bootstrap would give an interval an order of magnitude too
    tight, so the pairing unit is asserted rather than assumed.
    """
    rows = [{"p_start": 0.5, "base": 0.9, "started": i % 2, "target_gw": gw}
            for gw in range(1, 7) for i in range(20)]
    out = backtest._paired_brier_diff(pd.DataFrame(rows), "p_start", "base",
                                      n_boot=200)
    assert out["gameweeks"] == 6
    assert out["diff"] < 0  # 0.5 beats 0.9 on a coin flip
    assert out["ci95"][0] <= out["diff"] <= out["ci95"][1]


def test_too_few_gameweeks_reports_no_interval_rather_than_a_meaningless_one():
    rows = [{"p_start": 0.5, "base": 0.9, "started": 1, "target_gw": g}
            for g in range(3) for _ in range(20)]
    out = backtest._paired_brier_diff(pd.DataFrame(rows), "p_start", "base")
    assert out["diff"] is None and "note" in out


# ---------------------------------------------------------------------------
# The live audit — the half the archive cannot see
# ---------------------------------------------------------------------------


def _snapshot(tmp_path, rows):
    p = tmp_path / "projections.ndjson"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_the_live_audit_reads_only_pre_deadline_rows(tmp_path):
    """A snapshot taken after kickoff is not a forecast.

    The pipeline writes rows both before and after a deadline; scoring the
    post-deadline ones would report the model's hindsight as its accuracy.
    """
    rows = []
    for i in range(1, 41):
        rows.append({"player_id": i, "target_gw": 1, "is_pre_deadline": 1,
                     "p_start": 0.9, "exp_minutes": 75.0, "availability": 1.0,
                     "as_of": "2026-08-21T17:00:00+00:00"})
        rows.append({"player_id": i, "target_gw": 1, "is_pre_deadline": 0,
                     "p_start": 0.0, "exp_minutes": 0.0, "availability": 1.0,
                     "as_of": "2026-08-21T21:00:00+00:00"})
    outcomes = {i: {"starts": 1, "minutes": 90} for i in range(1, 41)}
    out = backtest.live_start_audit(
        outcomes, snapshot_path=_snapshot(tmp_path, rows), target_gw=1)
    assert out["status"] == "measured"
    assert out["n"] == 40, "post-deadline rows leaked into the audit"
    assert out["brier"] == pytest.approx((1 - 0.9) ** 2, abs=1e-9)


def test_the_live_audit_ignores_other_gameweeks(tmp_path):
    rows = [{"player_id": i, "target_gw": gw, "is_pre_deadline": 1,
             "p_start": 0.5, "exp_minutes": 45.0, "availability": 1.0,
             "as_of": "x"} for gw in (1, 2) for i in range(1, 31)]
    outcomes = {i: {"starts": 0, "minutes": 0} for i in range(1, 31)}
    out = backtest.live_start_audit(
        outcomes, snapshot_path=_snapshot(tmp_path, rows), target_gw=2)
    assert out["n"] == 30


def test_the_live_audit_says_unavailable_rather_than_inventing_a_number(tmp_path):
    assert backtest.live_start_audit(
        {}, snapshot_path=tmp_path / "nope.ndjson")["status"] == "unavailable"
    rows = [{"player_id": 1, "target_gw": 1, "is_pre_deadline": 1,
             "p_start": 0.5, "exp_minutes": 45.0, "as_of": "x"}]
    out = backtest.live_start_audit(
        {1: {"starts": 1, "minutes": 90}}, snapshot_path=_snapshot(tmp_path, rows))
    assert out["status"] == "unavailable"


def test_the_frozen_live_audit_agrees_with_itself():
    """The recorded claim must not contradict the numbers recorded beside it.

    The point of this block is that a remembered result was checked against a
    frozen snapshot. If the prose and the table ever disagree, the prose is what
    drifted.
    """
    a = backtest.LIVE_GW1_START_AUDIT
    bands = {b["band"]: b for b in a["bands"]}
    assert a["cameo_n"] == bands["CAMEO?"]["n"]
    assert a["nailed_n"] == bands["NAILED"]["n"]
    assert a["nailed_that_did_not_start"] <= a["nailed_n"]
    assert a["cameo_that_started"] == pytest.approx(
        bands["CAMEO?"]["start_rate"] * bands["CAMEO?"]["n"], abs=1.0)
    considered = {b["band"]: b for b in a["considered"]["bands"]}
    # The finding: near-calibrated pool-wide, badly wrong on the players anyone
    # picks. If the recorded numbers ever stop saying that, the paragraph
    # asserting it has to go with them.
    assert abs(bands["CAMEO?"]["claimed"] - bands["CAMEO?"]["start_rate"]) < 0.05
    assert considered["CAMEO?"]["start_rate"] - considered["CAMEO?"]["claimed"] > 0.15
    assert "1-for-6" in a["reported_claim"]["on_the_squad"]


# ---------------------------------------------------------------------------
# The report's own honesty
# ---------------------------------------------------------------------------


def test_the_candidate_fix_records_that_it_shipped():
    """It improved the score in all three seasons, and it has now been made.

    This test used to assert the opposite — `decision == "measured, not
    shipped"` — because an unshipped improvement described as a fix is a false
    statement about a running system. The inverse is just as false: a record
    still reading "not shipped" beside a projection that has made the change
    would be worse than no record at all, so the assertion flips rather than
    being deleted.
    """
    fix = backtest.MINUTES_CANDIDATE_FIX
    assert "SHIPPED" in fix["decision"]
    assert projection.MODEL_VERSION in fix["decision"], (
        "the record must name the projection version that carries the change, "
        "or a reader cannot tell which artifacts predate it")
    seasons = fix["brier_h1"]
    assert {v["role"] for v in seasons.values()} == {"train", "select", "test"}
    for season, v in seasons.items():
        assert v["after"] < v["before"], f"{season} does not improve"
    assert fix["caveat"], "an improvement with no remaining gap named is a claim"

def test_the_candidate_fix_was_not_measured_on_the_test_season_first():
    """Train and select must both be present.

    A single number on the reporting season is a result fitted to the season it
    is reported on — the mistake `SEASON_SPLIT` exists to prevent.
    """
    roles = {v["role"]: s
             for s, v in backtest.MINUTES_CANDIDATE_FIX["brier_h1"].items()}
    assert roles["test"] == backtest.SEASON_SPLIT["test"]
    assert roles["select"] == backtest.SEASON_SPLIT["select"]
    assert roles["train"] in backtest.SEASON_SPLIT["train"]


def test_the_limitations_name_the_unmeasured_availability_path():
    """The most important caveat, and the one most likely to be dropped.

    Availability is pinned at 1.0 across the whole archive, so the historical
    figures cannot see the failure that hurts most: a badged starter who is
    injured, suspended or not registered.
    """
    text = " ".join(backtest.MINUTES_LIMITATIONS).lower()
    assert "availability" in text
    assert "status" in text


def test_the_schema_version_moved_with_the_shape():
    """Publishing a new field without bumping the version is how a page comes
    to render a shape it does not understand."""
    assert backtest.SCHEMA_VERSION >= 8


def test_the_minutes_block_never_reports_a_withdrawn_baseline():
    """The rule `test_ml_removed.py` enforces on the points block, applied here.

    `fpl_xp` and the ensemble containing it were withdrawn for leakage. A new
    measurement is exactly the sort of place they could quietly come back.
    """
    banned = {"fpl_xp", "ensemble", "ml", "xP"}
    assert not (set(backtest.MINUTES_BASELINES) & banned)
    assert not (set(backtest.MINUTES_FEATURE_COLUMNS) & banned)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_measurement_reaches_the_published_artifact(monkeypatch):
    """A measurement that does not ship changed nothing.

    The points half is stubbed out — it is slow and it is already covered — so
    this asserts one thing: `run` puts the minutes block in the artifact under a
    key, and omits it rather than nulling it when it was not computed.
    """
    ev = pd.DataFrame({
        "element": range(12), "horizon": [1] * 12, "decision_gw": [2] * 12,
        "target_gw": [2] * 12, "pred": np.linspace(1, 6, 12),
        "actual": np.linspace(0, 8, 12), "minutes": [90] * 12,
        "naive": np.linspace(1, 5, 12), "pos": ["MID"] * 12,
        "value": [50] * 12, "team_id": [1] * 12,
    })
    monkeypatch.setattr(backtest, "build_evaluation",
                        lambda *a, **k: (ev, {"decision_gws": 1, "rows": 12,
                                              "skipped_no_fixture": 0}))
    monkeypatch.setattr(backtest, "_pre_season_block", lambda _ev: {})
    monkeypatch.setattr(backtest, "_decision_metrics", lambda *a, **k: {})
    monkeypatch.setattr(backtest, "_transfer_regret", lambda *a, **k: {})
    monkeypatch.setattr(backtest, "minutes_report",
                        lambda *a, **k: {"measured": True, "sentinel": 42})

    out = backtest.run(horizons=(1,), write=False)
    assert out["minutes_model"]["sentinel"] == 42
    assert out["schema_version"] == backtest.SCHEMA_VERSION

    bare = backtest.run(horizons=(1,), write=False, with_minutes=False)
    assert "minutes_model" not in bare


def test_a_missing_archive_is_recorded_as_unmeasured_not_omitted(monkeypatch):
    """`measured: False` with a reason, never a silently absent block.

    A missing measurement and a measurement that found nothing look identical
    once the key is gone.
    """
    ev = pd.DataFrame({
        "element": range(12), "horizon": [1] * 12, "decision_gw": [2] * 12,
        "target_gw": [2] * 12, "pred": np.linspace(1, 6, 12),
        "actual": np.linspace(0, 8, 12), "minutes": [90] * 12,
        "naive": np.linspace(1, 5, 12), "pos": ["MID"] * 12,
        "value": [50] * 12, "team_id": [1] * 12,
    })
    monkeypatch.setattr(backtest, "build_evaluation",
                        lambda *a, **k: (ev, {"decision_gws": 1, "rows": 12,
                                              "skipped_no_fixture": 0}))
    monkeypatch.setattr(backtest, "_pre_season_block", lambda _ev: {})
    monkeypatch.setattr(backtest, "_decision_metrics", lambda *a, **k: {})
    monkeypatch.setattr(backtest, "_transfer_regret", lambda *a, **k: {})

    def _boom(*a, **k):
        raise histdata.MissingHistoryError("no `starts` column")

    monkeypatch.setattr(backtest, "minutes_report", _boom)
    out = backtest.run(horizons=(1,), write=False)
    assert out["minutes_model"]["measured"] is False
    assert "starts" in out["minutes_model"]["reason"]


def test_a_season_without_a_starts_column_is_refused_not_approximated(monkeypatch):
    """No `starts` means no start outcome. Say so, rather than scoring
    `minutes > 0` and labelling it the minutes model."""
    class _Stub:
        frame = pd.DataFrame({"GW": [1], "element": [1], "minutes": [90]})

    monkeypatch.setattr(histdata, "load_season", lambda season: _Stub())
    with pytest.raises(histdata.MissingHistoryError, match="starts"):
        backtest.build_minutes_evaluation("1999-00", (1,))


# ---------------------------------------------------------------------------
# End to end, on the real archive
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def report():
    try:
        return backtest.minutes_report(backtest.TEST_SEASON, horizons=(1,))
    except Exception as exc:  # pragma: no cover - archive-free checkout
        pytest.skip(f"historical archive unavailable: {exc}")


def test_the_report_measures_the_test_season_with_no_leakage(report):
    assert report["measured"] is True
    assert report["season"] == backtest.SEASON_SPLIT["test"]
    assert report["leakage_check"]["post_match_fields_in_features"] == []
    assert report["model_version"] == projection.MODEL_VERSION


def test_gameweek_one_is_evaluated_and_kept_apart(report):
    """GW1 is a different regime AND the one with no baseline.

    Averaging it into the paired table would report the model as unbeaten there
    because nothing was standing on the other side.
    """
    assert report["pre_season"]["decision_gw"] == 1
    assert report["pre_season"]["n"] > 0
    assert "does not exist" in report["pre_season"]["naive_baseline"]
    assert report["per_horizon"]["1"]["n"] > 0


def test_the_published_verdict_matches_the_published_numbers(report):
    """The prose says the model loses. The table has to agree.

    This is the check that would have caught the points model's old summary,
    which claimed a result its own columns contradicted.
    """
    h1 = report["per_horizon"]["1"]
    assert "loses" in report["verdict"].lower()
    assert h1["brier"]["gaffer"] > min(
        v for k, v in h1["brier"].items() if k != "gaffer")
    assert h1["auc"]["gaffer"] < max(
        v for k, v in h1["auc"].items() if k != "gaffer")


def test_every_baseline_is_actually_scored(report):
    h1 = report["per_horizon"]["1"]
    for name in backtest.MINUTES_BASELINES:
        assert name in h1["brier"] and name in h1["auc"]


def test_the_price_prior_branch_is_no_longer_a_third_of_the_population(report):
    """A18, from the reporting side.

    This test used to assert `share_pct > 20` — "a minority branch could not
    carry the headline" — because the arm was a third of every row, claimed ~29%
    and realised 2.3%. The fix did not improve that arm; it stopped sending
    players to it. Both halves are asserted, so a regression that reopens the
    old gate fails here as well as in `test_projection.py`.
    """
    branches = {b["branch"]: b for b in report["branches"]}
    pp = branches["price_prior"]
    assert pp["share_pct"] < 10, (
        "A18 shrank this arm from ~36% of rows to ~4%; a third of the "
        "population back on the price prior means the gate regressed")
    assert branches["current_season"]["share_pct"] > 85
    # Still the weakest arm, and still worse than quoting its own base rate. It
    # is simply no longer the largest source of error in the model.
    assert pp["brier_skill"] < 0
    assert pp["brier_skill"] > -1.0, (
        "before A18 this was -3.09, because the arm was answering ~0.29 to a "
        "population that started 2.3% of the time")

def test_the_badge_is_measured_on_the_pool_a_manager_picks_from(report):
    """Both populations must ship, because they disagree.

    Publishing only the pool-wide table is how the CAMEO? band came to look
    calibrated while being wrong about every player anyone owns. A18 flipped the
    SIGN of the pool-wide error — CAMEO? claimed 0.259 against a realised 0.147
    before it and claims 0.126 against 0.144 now — so the direction of the
    overall gap is no longer the invariant. The disagreement is, and the owned
    sub-population has been the under-called one throughout.
    """
    overall = {b["band"]: b for b in report["bands"]["overall"]}
    considered = {b["band"]: b for b in report["bands"]["considered"]}
    assert overall and considered
    assert considered["CAMEO?"]["start_rate"] >= considered["CAMEO?"]["claimed"]
    considered_gap = (considered["CAMEO?"]["start_rate"]
                      - considered["CAMEO?"]["claimed"])
    overall_gap = abs(overall["CAMEO?"]["start_rate"]
                      - overall["CAMEO?"]["claimed"])
    assert considered_gap > overall_gap, (
        "the owned pool must remain the worse-called one, or this pair of "
        "tables has stopped earning its place")

def test_nailed_is_reported_as_over_confident_rather_than_rounded_up(report):
    """~0.94 claimed, ~0.84 realised. The gap is the number that matters."""
    nailed = next(b for b in report["bands"]["overall"] if b["band"] == "NAILED")
    assert nailed["claimed"] > nailed["start_rate"]
    assert nailed["n"] > 500


def test_expected_minutes_is_scored_against_a_naive_alternative(report):
    """`exp_minutes`, not `p_start`, is what multiplies through the rates, so it
    needs its own baseline — a probability cannot be scored against minutes."""
    mae = report["per_horizon"]["1"]["exp_minutes_mae"]
    assert set(mae) >= {"gaffer", "mins_avg_td", "started_lag_x90"}
    assert all(v > 0 for v in mae.values())


def test_the_paired_comparison_reports_how_many_gameweeks_it_lost(report):
    """An aggregate that lost narrowly and one that lost every single week are
    different findings, and only the second is worth acting on."""
    paired = report["per_horizon"]["1"]["paired_vs_baseline"]
    for name, block in paired.items():
        assert block["gameweeks"] > 5, name
        assert "ci95" in block, name


def test_the_considered_cut_is_declared_in_the_artifact(report):
    """A population cut nobody can see is a filter, not a measurement."""
    assert report["calibration"]["considered_rank_cut"] == \
        backtest.CONSIDERED_OWNERSHIP_RANK
    assert "considered" in report["calibration"]["note"]


def test_this_module_moved_nothing_it_measures():
    """It measures. It must not have changed what it measures."""
    assert config.BASE_SAMPLE_MINUTES > 0
    assert backtest.SEASON_SPLIT["measured_at_model_version"] == \
        projection.MODEL_VERSION, (
            "the minutes figures are stamped at the split's model version; a "
            "projection change invalidates them and this is the reminder")


# ---------------------------------------------------------------------------
# A18 — the recorded decision
# ---------------------------------------------------------------------------


def test_the_shipped_figures_are_not_the_ones_the_proposal_carried():
    """The proposal's prose described the change that shipped and its numbers
    came from a larger variant that did not. Both are kept, and they must stay
    distinguishable — a later edit that quietly replaces one with the other
    would restore exactly the fault this pair exists to record.
    """
    cf = backtest.MINUTES_CANDIDATE_FIX
    superseded = cf["superseded_figures"]["recorded_while_unshipped"]
    refused = backtest.MINUTES_REFUSED_FIX
    assert cf["superseded_figures"]["belong_to"] == refused["candidate"]
    for season, value in superseded.items():
        assert refused["brier_h1"][season]["after"] == value
        assert cf["brier_h1"][season]["after"] != value


def test_the_refused_variant_ships_in_the_artifact_with_its_reason():
    """`WITHDRAWN_BASELINES` set the precedent: a measurement that did not ship
    is still a result, and it belongs in the artifact rather than in a commit
    message nobody reads.
    """
    refused = backtest.MINUTES_CANDIDATE_FIX["refused_variant"]
    assert refused is backtest.MINUTES_REFUSED_FIX
    assert "REFUSED" in refused["decision"]
    assert refused["refused_because"]
    assert refused["reopen_if"]
    # It scored BETTER and was refused anyway. If that ever stops being true the
    # record has been rewritten to flatter the decision.
    for season, block in refused["brier_h1"].items():
        assert block["after"] < \
            backtest.MINUTES_CANDIDATE_FIX["brier_h1"][season]["after"], season


def test_the_points_effect_is_recorded_beside_the_minutes_effect():
    """`p_start` gates every rate, so a minutes win that cost points would not be
    a win. The artifact has to carry both or the claim is unfalsifiable.
    """
    eff = backtest.MINUTES_CANDIDATE_FIX["points_model_effect"]
    for key in ("mae", "rank_corr", "xi_points_per_gw"):
        assert set(eff[key]) == {"2023-24", "2024-25", "2025-26"}
        for season, (before, after) in eff[key].items():
            if key == "mae":
                assert after < before, f"{key} {season}"
            else:
                assert after >= before, f"{key} {season}"


# ---------------------------------------------------------------------------
# C1 / A19 — measurements that did not become changes
# ---------------------------------------------------------------------------


def test_the_captaincy_refusal_is_recorded_with_its_numbers():
    """A refusal nobody can read is indistinguishable from never having looked.

    `WITHDRAWN_BASELINES` set the precedent and `MINUTES_CANDIDATE_FIX` followed
    it. C1 measured nineteen armband rules against the shipped one and shipped
    none of them, so the evidence has to be in the artifact rather than in a
    commit message.
    """
    c = backtest.CAPTAINCY_CANDIDATE
    assert "REFUSED" in c["decision"]
    assert c["rules_tried"] >= 10
    assert c["refused_because"] and c["reopen_if"] and c["limitations"]
    # The headroom must be real, or the refusal is a refusal to solve nothing.
    for season, shipped in c["headroom"]["shipped_captain_points_per_gw"].items():
        assert c["headroom"]["perfect_hindsight_per_gw"][season] > shipped


def test_the_captaincy_refusal_names_the_rule_that_lost_in_every_season():
    """`boom` is the rule the whole idea rests on — P(haul) for a decision that
    doubles one player — and it is the one rule with a consistent sign. If a
    later edit ever records it as a win, this fails.
    """
    boom = backtest.CAPTAINCY_CANDIDATE["vs_expected_points_per_gw"]["boom"]
    assert set(boom) == {"2023-24", "2024-25", "2025-26"}
    for season, delta in boom.items():
        assert delta < 0, f"{season}: boom is recorded as beating expected points"


def test_the_captaincy_refusal_records_why_and_not_only_that():
    """The mechanism is the part worth keeping: `boom` cannot beat expected
    points at ordering captains because it does not order hauls any better.
    Asserted on the numbers, so the sentence cannot drift from them.
    """
    auc = backtest.CAPTAINCY_CANDIDATE["haul_discrimination_auc"]
    test = auc[backtest.SEASON_SPLIT["test"]]
    assert abs(test["boom"] - test["expected_points"]) < 0.01, (
        "the recorded mechanism is that the two are indistinguishable at "
        "picking hauls; a real gap there would reopen the question")


def test_the_clean_sheet_contradiction_is_recorded_as_measured_not_fixed():
    """Two estimates of one quantity, and `p_cs` reads the one that loses.

    Recorded rather than corrected, because reading the other one needs a
    two-pass projection and moves every defender and goalkeeper in the product.
    The record has to carry the direction AND the reason it was not acted on.
    """
    a = backtest.CLEAN_SHEET_CONTRADICTION
    assert a["decision"] == "measured, NOT fixed"
    assert a["not_fixed_because"] and a["next_step"]
    w = a["which_is_right"]
    for season in ("2023-24", "2024-25", "2025-26"):
        assert w["brier_bottom_up"][season] < w["brier_shipped_top_down"][season], (
            f"{season}: the record claims the unread lambda is better; if that "
            "stops being true the finding has changed")
        assert w["auc_bottom_up"][season] >= w["auc_shipped_top_down"][season]


def test_the_shipped_clean_sheet_is_recorded_as_barely_beating_the_base_rate():
    """The number that makes this worth a record rather than a footnote: on the
    test season the shipped `p_cs` scores 0.1899 against a league base rate of
    0.1901, and on the train season it is WORSE than the base rate.
    """
    w = backtest.CLEAN_SHEET_CONTRADICTION["which_is_right"]
    test = backtest.SEASON_SPLIT["test"]
    margin = w["brier_league_base_rate"][test] - w["brier_shipped_top_down"][test]
    assert 0 <= margin < 0.005, (
        "a shipped clean-sheet model that clearly beat the base rate would "
        "make this a tuning question rather than a modelling one")
    train = backtest.SEASON_SPLIT["train"][0]
    assert w["brier_shipped_top_down"][train] > w["brier_league_base_rate"][train]


def test_the_over_confident_clean_sheet_bands_are_published():
    """Calibration is the actionable half. Below 0.35 the shipped `p_cs` is
    honest; above it, it is not — and the live artifact prints numbers well
    above 0.35."""
    bands = backtest.CLEAN_SHEET_CONTRADICTION[
        "calibration_of_the_shipped_p_cs"]["bands"]
    assert len(bands) >= 5
    low = [b for b in bands if b["claimed"] <= 0.25]
    high = [b for b in bands if b["claimed"] >= 0.45]
    assert low and high
    assert all(abs(b["claimed"] - b["realised"]) < 0.05 for b in low)
    assert all(b["claimed"] - b["realised"] > 0.15 for b in high), (
        "the recorded fault is over-confidence at the top of the range")


def test_both_refusals_reach_the_published_artifact(report):
    """They are constants; what matters is that `run()` publishes them. The
    minutes report is the cheap fixture here, so the artifact assembly is
    checked directly rather than by re-running the whole backtest.
    """
    assert report  # the archive is present, so `run()` would produce a block
    import inspect
    src = inspect.getsource(backtest.run)
    assert '"captaincy_candidate": CAPTAINCY_CANDIDATE' in src
    assert '"clean_sheet_contradiction": CLEAN_SHEET_CONTRADICTION' in src
