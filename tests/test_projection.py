"""Projection-model behaviour tests."""

import pytest

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection

# --------------------------------------------------------------------------
# M2/M3 — a zero in the prior-season baseline is not always a measurement
#
# FPL back-fills `history_past` for seasons that predate a statistic with 0
# rather than omitting the key, so `base_starts == 0` and `base_xg90 == 0.0` each
# mean one of two opposite things. Verified against the live API on 2026-08-15:
# B.Fernandes' 2021/22 row reports 3110 minutes, 0 starts and 0.00 xG.
# --------------------------------------------------------------------------

def _ctx():
    """Two identical average teams, so every fixture effect is neutral and the
    only thing these tests can move is the baseline branch."""
    ids = (1, 2)
    return F.TeamContext.from_ratings(
        att_home={i: 1100.0 for i in ids}, att_away={i: 1050.0 for i in ids},
        def_home={i: 1100.0 for i in ids}, def_away={i: 1050.0 for i in ids},
        team_xgc={i: 1.3 for i in ids})


def _player(**over):
    base = {
        "position": "MID", "price": 90, "team_id": 1,
        "minutes": 0, "starts": None,
        "base_minutes": 0, "base_starts": 0, "base_xg90": 0.0, "base_xa90": 0.0,
        "base_season": "", "xg_per_90": 0.0, "xa_per_90": 0.0, "defcon_per_90": 0.0,
    }
    base.update(over)
    return base


def _rates(fixtures_played=0, **over):
    return projection.fixture_rates(
        _player(**over), F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3),
        _ctx(), avail=1.0, fixtures_played=fixtures_played)


@pytest.mark.parametrize("label,expected", [
    ("2021/22", False), ("2022/23", True), ("2025/26", True),
    ("", None), (None, None), ("garbage", None),
])
def test_which_seasons_could_report_starts_and_xg(label, expected):
    assert config.season_reports_advanced_stats(label) is expected


def test_a_zero_from_a_season_that_could_measure_it_is_believed():
    """326 minutes and no starts in a season that reported starts is a
    substitute, and the number to use is 0 — not a prior invented from price."""
    assert _rates(base_minutes=326, base_starts=0,
                  base_season="2025/26")["p_start"] == 0.0


def test_a_zero_from_a_season_that_could_not_measure_it_is_not():
    """The same numbers from 2021/22 say nothing at all: FPL had no `starts`
    column then. Falling back to the price prior is the honest answer."""
    p = _rates(base_minutes=326, base_starts=0, base_season="2021/22")["p_start"]
    assert p == pytest.approx(projection._start_prior("MID", 90), abs=1e-9)


def test_minutes_that_cannot_come_from_the_bench_override_the_zero():
    """The guard that works without provenance. 3110 minutes with no starts is
    not a career substitute, whatever the record says — a season is 38 games and
    a cameo is not 80 minutes long. This is what protects an existing database
    between the schema migration and the next enrichment run."""
    p = _rates(base_minutes=3110, base_starts=0, base_season="")["p_start"]
    assert p == pytest.approx(projection._start_prior("MID", 90), abs=1e-9)


def test_a_real_starter_is_unaffected():
    r = _rates(base_minutes=3017, base_starts=35, base_season="2024/25")
    assert r["p_start"] == pytest.approx(35 / 38.0, abs=1e-9)


def test_a_measured_zero_xg_outranks_the_positional_prior():
    """A midfielder with a full modern season and no goal threat has told us his
    rate. Substituting a positional average there discards the only evidence
    there is."""
    r = _rates(base_minutes=2000, base_starts=25, base_season="2024/25",
               base_xg90=0.0, base_xa90=0.0)
    assert r["exp_goals"] == 0.0
    assert r["exp_assists"] == 0.0


def test_an_unmeasurable_zero_xg_falls_back_to_the_prior():
    """The same zeros from 2021/22 are a missing column. Believing them would
    project a player who never threatens."""
    r = _rates(base_minutes=3110, base_starts=0, base_season="2021/22",
               base_xg90=0.0, base_xa90=0.0)
    assert r["exp_goals"] > 0.0
    assert r["exp_assists"] > 0.0


def test_no_prior_sample_at_all_still_uses_the_prior():
    """Absence and zero must not converge: a player with no recorded season is
    not a player measured at zero."""
    r = _rates(base_minutes=0, base_season="")
    assert r["exp_goals"] > 0.0
    assert r["p_start"] == pytest.approx(projection._start_prior("MID", 90), abs=1e-9)


# --------------------------------------------------------------------------
# M3b — `starts` is a fixture count, so its denominator must be one too
#
# The old code divided by `last_finished_gw`, an EVENT count. The two agree only
# while every team plays exactly once per gameweek. Across 2024-25 they differed
# for at least one team in 17 of 37 decision gameweeks, covering 3.9% of
# evaluated rows — small in aggregate, wrong in kind.
# --------------------------------------------------------------------------

def test_the_start_rate_divides_fixtures_by_fixtures():
    """A double gameweek breaks an event-count denominator. Four starts from five
    fixtures is 0.80; against the four gameweeks those fixtures fell in it reads
    1.00, and the model calls a rotated player nailed."""
    assert _rates(minutes=400, starts=4, fixtures_played=5)["p_start"] == \
        pytest.approx(4 / 5, abs=1e-9)


def test_a_blank_does_not_punish_a_player_for_a_match_never_played():
    """The mirror case. The team played three of a possible four and the player
    started all three, so he is an ever-present — not a 75% starter. (0.98 is the
    model's ceiling on any start probability, applied here as everywhere.)"""
    assert _rates(minutes=270, starts=3, fixtures_played=3)["p_start"] == \
        pytest.approx(0.98, abs=1e-9)


def test_the_current_season_enters_from_the_first_fixture_by_weight():
    """2A.1 -- the `fixtures_played >= 3` GATE is gone, replaced by shrinkage.

    It used to be a hard switch: below three completed fixtures the current
    season was invisible and every player in the game was graded on
    `base_starts / 38`. Teams have played two at GW3, so on 2026-09-01 that
    published `p_start 0.90` and a NAILED badge for a player with 0 starts and
    11 minutes while six ever-presents were flagged as rotation risks -- and at
    GW4 the ranking inverted on no new information beyond a counter reaching
    three.

    The current season now enters from the FIRST fixture, weighted by how much
    of it there is, so there is no gameweek at which the answer jumps. Measured
    over three seasons before it was written: GW1-3 Brier 0.182 -> 0.123 on the
    held-out season, and better in all three.
    """
    prior = _rates(minutes=180, starts=2, fixtures_played=2,
                   base_minutes=2000, base_starts=19, base_season="2024/25")
    w = 2 / (2 + projection.START_SHRINK_K)
    blended = w * (2 / 2) + (1 - w) * (19 / 38.0)
    # No recency supplied, so the shrunk rate is the whole answer.
    assert prior["p_start"] == pytest.approx(blended, abs=1e-9)
    # ...and it sits strictly between the two samples it is made of.
    assert 19 / 38.0 < prior["p_start"] < 1.0


def test_there_is_no_gameweek_at_which_the_answer_jumps():
    """The cliff is the defect, not the threshold. An ever-present's start
    probability must rise monotonically as his sample grows, never step."""
    seq = [
        _rates(minutes=90 * n, starts=n, fixtures_played=n,
               base_minutes=2000, base_starts=19, base_season="2024/25")["p_start"]
        for n in range(1, 7)
    ]
    assert seq == sorted(seq), f"start probability must not step: {seq}"
    assert max(b - a for a, b in zip(seq, seq[1:], strict=False)) < 0.2, (
        "no single fixture may move it by a fifth")


def test_played_fixtures_counts_both_sides_of_every_finished_match(conn):
    from gaffer.model import features as FF

    conn.execute("UPDATE fixtures SET finished=1 WHERE id IN (1,2)")
    conn.commit()
    played = FF.played_fixtures_by_team(conn)
    # Fixtures 1 and 2 are (1 v 2) and (3 v 4); fixture 3 (5 v 6) is unfinished.
    assert played == {1: 1, 2: 1, 3: 1, 4: 1}, played
    assert 5 not in played and 6 not in played, "an unfinished match counts for no one"


# --------------------------------------------------------------------------
# M2 — the baseline says WHICH season it came from
# --------------------------------------------------------------------------

def test_the_schema_carries_the_baseline_season(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(players)")}
    assert "base_season" in cols, (
        "without provenance the export stamps the current prior season on every "
        "baseline, and a four-year-old cameo renders as last season")


def test_enrich_history_records_the_season_it_read(conn, monkeypatch):
    """`history_past[-1]` is the most recent season FPL HAS for a player, not
    last season. For anyone who has been abroad it can be years old, and the
    number is only interpretable alongside the year it came from."""
    from gaffer import ingest

    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE players SET price=100, base_minutes=0 WHERE id=?", (pid,))
    conn.execute("UPDATE players SET price=30, selected_by_pct=0 WHERE id<>?", (pid,))
    conn.commit()

    class Client:
        def element_summary(self, _pid):
            return {"history_past": [
                {"season_name": "2019/20", "minutes": 500, "starts": 4,
                 "expected_goals": "0.00", "expected_assists": "0.00"},
                {"season_name": "2021/22", "minutes": 326, "starts": 0,
                 "expected_goals": "0.00", "expected_assists": "0.00"},
            ]}

    assert ingest.enrich_history(conn, Client()) == 1
    row = conn.execute(
        "SELECT base_season, base_minutes, base_starts FROM players WHERE id=?",
        (pid,)).fetchone()
    assert row["base_season"] == "2021/22"
    assert row["base_minutes"] == 326


@pytest.mark.parametrize("recorded,season,is_prior", [
    ("2025/26", "2025/26", True),      # the season just gone
    ("2021/22", "2021/22", False),     # real numbers, four years stale
    ("", None, None),                  # never recorded — no claim either way
])
def test_the_player_card_reports_the_season_it_actually_has(
        conn, recorded, season, is_prior, monkeypatch):
    """The defect this replaces: `season` was computed from the calendar and
    stamped on every player alike, so an old cameo rendered as "Last season"."""
    from gaffer.export import artifacts

    monkeypatch.setattr(artifacts, "_PRIOR_SEASON", "2025/26")
    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute(
        "UPDATE players SET base_minutes=326, base_starts=0, base_xg90=0, "
        "base_xa90=0, base_season=? WHERE id=?", (recorded, pid))
    conn.commit()
    row = conn.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()

    card = artifacts._last_season(row)
    assert card["season"] == season
    assert card["is_prior_season"] is is_prior
    assert card["minutes"] == 326


def test_too_short_a_sample_is_still_no_card_at_all(conn):
    from gaffer.export import artifacts

    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE players SET base_minutes=? WHERE id=?",
                 (config.BASE_SAMPLE_MINUTES - 1, pid))
    conn.commit()
    row = conn.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()
    assert artifacts._last_season(row) is None


def test_a_projection_survives_a_row_without_the_provenance_column():
    """`base_season` was added after existing databases were created, and
    sqlite3.Row raises IndexError for a column it does not have. A migration that
    has not run yet must not take the projection down."""
    player = _player(base_minutes=2000, base_starts=25)
    del player["base_season"]
    out = projection.fixture_rates(
        player, F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3),
        _ctx(), avail=1.0, fixtures_played=0)
    assert out["p_start"] > 0


def test_project_writes_rows(conn):
    n = projection.project(conn, from_gw=1, horizon=1)
    assert n == conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]


def test_unavailable_player_scores_near_zero(conn):
    # mark one player injured
    conn.execute("UPDATE players SET status='i' WHERE id=7")
    conn.commit()
    projection.project(conn, from_gw=1, horizon=1)
    row = conn.execute(
        "SELECT exp_points, p_start FROM projections WHERE player_id=7 AND gw=1"
    ).fetchone()
    assert row["p_start"] == 0.0
    assert row["exp_points"] < 0.2


def test_defcon_contributes_for_ballwinner(conn):
    projection.project(conn, from_gw=1, horizon=1)
    # DEF players in the fixture have defcon_per_90=11 (>10 threshold) -> a DEFCON term
    row = conn.execute(
        "SELECT exp_defcon_pts FROM projections pr JOIN players pl ON pl.id=pr.player_id "
        "WHERE pl.position='DEF' AND pl.defcon_per_90>10 AND pr.gw=1 LIMIT 1"
    ).fetchone()
    assert row["exp_defcon_pts"] > 0.5


def test_clean_sheet_only_for_defensive_positions(conn):
    projection.project(conn, from_gw=1, horizon=1)
    fwd = conn.execute(
        "SELECT exp_cs_pts FROM projections pr JOIN players pl ON pl.id=pr.player_id "
        "WHERE pl.position='FWD' AND pr.gw=1 LIMIT 1"
    ).fetchone()
    assert fwd["exp_cs_pts"] == 0.0


# --------------------------------------------------------------------------
# G-L — `defcon_per_90` is a division, and it was read raw
#
# Every attacking rate above it is empirical-Bayes shrunk; this one was not. In
# the shipped 2026/27 pre-season artifact that put two players at exactly 90.0
# defensive contributions per 90 — one contribution in one minute of football —
# reading as P(hit) 0.945 and 0.952 with "elite defensive volume" printed on a
# card that also said CAMEO? ~29'.
#
# The fix cannot simply be `shrink(defcon_per_90, minutes, prior)`. FPL resets
# `minutes` at the season rollover but KEEPS its per-90 fields, so out of season
# that rate came from last season while `minutes` is 0, and shrinking one
# against the other would discard the best DEFCON evidence in the system. So the
# target is an explicit `base_defcon90` and the sample size is the minutes that
# actually generated the rate.
# --------------------------------------------------------------------------

def _defcon_rate(r):
    """The shrunk per-90 rate behind a projection, recovered from its own mu."""
    return r["defcon_mu"] / r["mins_frac"]


@pytest.mark.parametrize("label,expected", [
    ("2023/24", False),   # the column did not exist yet
    ("2024/25", True), ("2025/26", True),
    ("", None), (None, None), ("garbage", None),
])
def test_which_seasons_could_report_defensive_contributions(label, expected):
    assert config.season_reports_defcon(label) is expected


def test_a_one_minute_contribution_does_not_read_as_elite():
    """Mheuka and Fredricson, by name. Both shipped at 90.0 per 90 with no
    prior-season sample at all, and both must fall to their positional prior."""
    fwd = _rates(position="FWD", price=45, defcon_per_90=90.0)
    assert _defcon_rate(fwd) == pytest.approx(F.DEFCON_PRIOR["FWD"], abs=1e-9)
    assert fwd["defcon_p_hit"] < 0.01, "was 0.945 in the shipped artifact"

    dfn = _rates(position="DEF", price=40, defcon_per_90=90.0)
    assert _defcon_rate(dfn) == pytest.approx(F.DEFCON_PRIOR["DEF"], abs=1e-9)
    assert dfn["defcon_p_hit"] < 0.01, "was 0.952 in the shipped artifact"


def test_a_full_season_ball_winner_is_not_flattened():
    """The regression the naive fix would have shipped. Elliot Anderson's
    2025/26: 13.91 per 90 over 3,332 minutes. Pre-season his current-season
    minutes are 0, so shrinking against THEM would replace the best DEFCON
    evidence in the system with a positional average. Against the minutes that
    produced the rate he is unmoved to the decimal."""
    r = _rates(position="MID", price=65, defcon_per_90=13.91,
               base_defcon90=13.91, base_minutes=3332, base_starts=37,
               base_season="2025/26")
    assert _defcon_rate(r) == pytest.approx(13.91, rel=1e-9)
    assert r["defcon_p_hit"] > 0.4


def test_an_unbackfilled_baseline_still_protects_the_ball_winner():
    """The migration window. A database written before `base_defcon90` existed
    carries the right rate in `defcon_per_90` and nothing in the new column, so
    the target degrades to the positional prior — but the SAMPLE SIZE is still
    last season's minutes, so the rate survives almost intact instead of
    collapsing. Anderson keeps 13.47 of 13.91."""
    r = _rates(position="MID", price=65, defcon_per_90=13.91,
               base_minutes=3332, base_starts=37, base_season="2025/26")
    assert _defcon_rate(r) == pytest.approx(13.47, abs=0.05)
    assert _defcon_rate(r) > 0.95 * 13.91


def test_a_zero_defcon_baseline_is_never_read_as_a_measurement():
    """Unlike `base_xg90`, a zero here is ALWAYS a column that was not read. 392
    outfielders cleared BASE_SAMPLE_MINUTES in 2025-26 and not one recorded zero
    defensive contributions; the floor is 2.25 per 90. Believing the zero would
    send every ball-winner in an un-enriched database to nothing."""
    r = _rates(position="DEF", price=55, defcon_per_90=11.0, base_defcon90=0.0,
               base_minutes=3000, base_starts=34, base_season="2025/26")
    assert _defcon_rate(r) > 10.0


def test_a_baseline_from_before_defcon_existed_is_not_a_target():
    """`defensive_contribution` arrived in 2024/25. A 2023/24 baseline reports 0
    for every player alive, which is the same trap `base_xg90` has for seasons
    that predated expected goals — and the same three-valued answer."""
    r = _rates(position="DEF", price=55, defcon_per_90=11.0, base_defcon90=0.0,
               base_minutes=3000, base_starts=34, base_season="2023/24")
    assert _defcon_rate(r) > 10.0


def test_in_season_the_current_rate_is_shrunk_toward_last_season():
    """One match played, twelve contributions in it, against a 3,000-minute
    baseline of 8.0. The single match is worth 90/(90+300) of the answer, so the
    rate reads 8.92 rather than the 12.0 the raw column would have given."""
    r = _rates(fixtures_played=1, position="MID", price=65, minutes=90, starts=1,
               defcon_per_90=12.0, base_defcon90=8.0, base_minutes=3000,
               base_starts=34, base_season="2025/26")
    assert _defcon_rate(r) == pytest.approx(8.923, abs=0.01)


def test_goalkeepers_are_left_alone_entirely():
    """`DEFCON_THRESHOLD["GKP"]` is 999 and keepers recorded no defensive
    contributions at all in the measured season, so the branch must not run even
    when the column contains nonsense."""
    r = _rates(position="GKP", price=45, defcon_per_90=90.0)
    assert r["defcon_mu"] == 0.0
    assert r["defcon_p_hit"] == 0.0


def test_the_schema_carries_the_defcon_baseline(conn):
    """Nullable on purpose: NULL is "no prior season has been read for this
    player" and 0.0 is "read, and he made none". `ingest.enrich_history` needs
    both to backfill an existing database exactly once."""
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(players)")}
    assert "base_defcon90" in cols
    assert cols["base_defcon90"]["dflt_value"] is None, (
        "a DEFAULT 0 would make every already-enriched player look permanently "
        "unread, and the backfill would run forever or never")


def test_enrich_history_backfills_the_defcon_baseline_exactly_once(conn):
    """The migration path. An existing database has base_minutes > 0 already, so
    the old `AND base_minutes=0` gate alone would never revisit those players and
    the new column would stay empty for exactly the ball-winners it protects."""
    from gaffer import ingest

    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE players SET price=100 WHERE id=?", (pid,))
    conn.execute("UPDATE players SET price=30, selected_by_pct=0 WHERE id<>?", (pid,))
    conn.commit()
    # The fixture player already has a prior-season sample and no DEFCON baseline
    # — precisely the state this migration has to reach.
    row = conn.execute(
        "SELECT base_minutes, base_defcon90 FROM players WHERE id=?", (pid,)).fetchone()
    assert row["base_minutes"] >= config.BASE_SAMPLE_MINUTES
    assert row["base_defcon90"] is None

    class Client:
        def element_summary(self, _pid):
            return {"history_past": [
                {"season_name": "2025/26", "minutes": 3000, "starts": 34,
                 "expected_goals": "3.00", "expected_assists": "6.00",
                 "defensive_contribution": 300},
            ]}

    assert ingest.enrich_history(conn, Client()) == 1
    got = conn.execute(
        "SELECT base_defcon90, defcon_per_90 FROM players WHERE id=?",
        (pid,)).fetchone()
    assert got["base_defcon90"] == pytest.approx(9.0, abs=1e-6)
    assert got["defcon_per_90"] == pytest.approx(9.0, abs=1e-6)
    # ...and the player no longer matches either arm, so the ~350 cached calls
    # are paid once rather than on every run.
    assert ingest.enrich_history(conn, Client()) == 0


def test_a_prior_season_that_recorded_no_defcon_is_still_marked_as_read(conn):
    """0.0 written rather than left NULL. Otherwise a player whose most recent
    FPL season predates the stat would be re-fetched on every single run."""
    from gaffer import ingest

    pid = conn.execute("SELECT id FROM players LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE players SET price=100 WHERE id=?", (pid,))
    conn.execute("UPDATE players SET price=30, selected_by_pct=0 WHERE id<>?", (pid,))
    conn.commit()

    class Client:
        def element_summary(self, _pid):
            return {"history_past": [
                {"season_name": "2021/22", "minutes": 3110, "starts": 0,
                 "expected_goals": "0.00", "expected_assists": "0.00"},
            ]}

    assert ingest.enrich_history(conn, Client()) == 1
    got = conn.execute(
        "SELECT base_defcon90 FROM players WHERE id=?", (pid,)).fetchone()
    assert got["base_defcon90"] == 0.0
    assert ingest.enrich_history(conn, Client()) == 0


def test_a_projection_survives_a_row_without_the_defcon_baseline_column():
    """Same contract as `base_season`: a migration that has not run yet must not
    take the projection down."""
    player = _player(base_minutes=2000, base_starts=25, defcon_per_90=9.0)
    assert "base_defcon90" not in player
    out = projection.fixture_rates(
        player, F.Fixture(gw=1, opponent_id=2, at_home=True, fdr=3),
        _ctx(), avail=1.0, fixtures_played=0)
    assert out["defcon_mu"] > 0


# --------------------------------------------------------------------------
# G-P — the xA factors are measured and recorded, and deliberately NOT applied
# --------------------------------------------------------------------------

def test_the_xa_factors_are_per_position_and_ordered_by_the_measurement():
    """FPL awards 22% (DEF), 36% (MID) and 111% (FWD) more assists than Opta xA
    over 2023-24 + 2024-25, while goals track xG to within 2% over the same
    rows — so the gap is definitional and specific to assists. A blanket 1.400
    would under-correct forwards by a third, which is why the constant is a
    table and not a number."""
    f = config.XA_TO_ASSIST
    assert set(f) == {"GKP", "DEF", "MID", "FWD"}
    assert f["FWD"] > f["MID"] > f["DEF"] > 1.0
    assert f["GKP"] == 1.0, "1.4 xA and 5 assists in two seasons is not a sample"
    assert config.XA_TO_ASSIST_FIT_SEASONS == ("2023-24", "2024-25")
    assert config.XA_TO_ASSIST_HELDOUT_SEASON == "2025-26"


def test_the_xa_factors_are_not_applied_to_the_projection():
    """Held out on 2025-26 the correction moves paired per-gameweek rank
    correlation by -0.0005 (t=-1.37, 24 of 38 gameweeks worse) and best-legal-XI
    points by -0.87 per gameweek; pooled over three seasons the rank-correlation
    loss is t = -4.2 and every one of the six deltas is negative. It degrades
    the ordering the solver consumes, so it is measured, recorded and left out.

    Pinned structurally rather than by asserting a number: a player whose xG and
    xA baselines are identical must project identical expected goals and
    assists, because neither carries a positional multiplier the other lacks.
    Any per-position assist factor separates them, so this fails the moment one
    is wired in without the evidence above being revisited."""
    assert config.XA_TO_ASSIST_APPLIED is False
    for pos in ("DEF", "MID", "FWD"):
        r = _rates(position=pos, base_minutes=2500, base_starts=30,
                   base_season="2025/26", base_xg90=0.20, base_xa90=0.20)
        assert r["exp_goals"] == pytest.approx(r["exp_assists"], rel=1e-9)


# --------------------------------------------------------------------------
# A18 — a season-to-date zero is evidence, on the same terms as a prior-season
# zero. The gate used to read `fixtures_played >= 3 and cur_min and ...`, and
# that `cur_min` sent every player with a full team sample and no minutes to a
# price prior which reads an expensive squad player as a probable starter.
#
# Measured before it shipped: h=1 Brier on `starts` 0.1452 -> 0.1185 (train),
# 0.1495 -> 0.1204 (select), 0.1509 -> 0.1154 (test), and h=1 points MAE on the
# test season 1.539 -> 1.114. `backtest.MINUTES_CANDIDATE_FIX` is the record.
# --------------------------------------------------------------------------


def test_a_full_team_sample_with_no_minutes_is_believed():
    """Eight completed fixtures and no appearance in any of them is the
    strongest bench evidence there is, and it now scores 0 — not a number
    invented from price."""
    assert _rates(minutes=0, starts=0, fixtures_played=8)["p_start"] == 0.0


def test_a_current_season_zero_outranks_a_prior_season_of_starting():
    """The precedence question, which is the half of A18 that is not obvious.

    A player who started 30 of last season's 38 and has not featured in his
    team's first eight is not a 79% starter. The current season is the more
    recent sample and it wins.
    """
    r = _rates(minutes=0, starts=0, fixtures_played=8,
               base_minutes=2700, base_starts=30, base_season="2024/25")
    # 2A.1 -- shrinkage, so the prior season is not erased at a stroke; but with
    # eight completed fixtures it is heavily outweighed, and the answer is much
    # closer to the current season's zero than to last season's 0.79.
    prior_rate = 30 / 38.0
    assert r["p_start"] < 0.3
    assert r["p_start"] < prior_rate / 2


def test_the_zero_is_not_a_zero_projection():
    """`p_start` 0 must not switch the player off entirely: the cameo arm is a
    separate probability and a benched player can still come on. A hard zero
    here would hand the autosubs and the solver a certainty the model does not
    have.
    """
    r = _rates(minutes=0, starts=0, fixtures_played=8)
    assert r["p_start"] == 0.0
    assert 0.0 < r["p_play"] < 0.5
    assert r["exp_minutes"] > 0.0


def test_a_thin_sample_is_weighted_down_rather_than_ignored_or_believed():
    """The two variants measured for the old gate were both wrong at the ends.

    `>= 3` ignored the current season entirely below three fixtures -- the
    defect this replaces. `>= 1` believed it completely, reading `starts / 1` as
    a probability and calling every player who missed the opener a certainty
    not to start; it was refused for that reason and the refusal was sound.

    Shrinkage is the third option neither of them was: a player who has missed
    both of his team's fixtures after starting 30 of last season's 38 is
    neither a 79% starter nor a 0% one.
    """
    r = _rates(minutes=0, starts=0, fixtures_played=2,
               base_minutes=2700, base_starts=30, base_season="2024/25")
    prior_rate = 30 / 38.0
    assert 0.0 < r["p_start"] < prior_rate, (
        "must be pulled down by the current season, but not to a certainty")
    w = 2 / (2 + projection.START_SHRINK_K)
    assert r["p_start"] == pytest.approx((1 - w) * prior_rate, abs=1e-9)
    # With no prior season either, it is the price prior and not a zero.
    bare = _rates(minutes=0, starts=0, fixtures_played=2)
    assert bare["p_start"] == pytest.approx(0.0, abs=1e-9) or bare["p_start"] > 0.0


def test_an_unreadable_starts_column_is_still_not_a_zero():
    """`starts is None` means the column was never read, which is the one case
    the price prior is still for. Absence and zero must not converge — the same
    distinction the prior-season arm draws for seasons that predate `starts`.
    """
    r = _rates(minutes=0, starts=None, fixtures_played=8)
    assert r["p_start"] == pytest.approx(
        projection._start_prior("MID", 90), abs=1e-9)


def test_a_player_who_has_featured_is_completely_unaffected():
    """A18 can only move rows whose `starts` is zero, and it can only move them
    down. Anyone with minutes took the current-season arm before the change and
    takes it afterwards, at the same number.
    """
    assert _rates(minutes=720, starts=8, fixtures_played=11)["p_start"] == \
        pytest.approx(8 / 11, abs=1e-9)
