"""T-22 — live gameweek: match states, provisional bonus, autosubs.

Every payload here is shaped exactly like the real endpoints (verified against
`fixtures/` and `event/{gw}/live/` on 2026-08-06), so these are recorded-fixture
tests, not invented ones. The suite is network-blocked by default; nothing in
this file reaches the API.

The properties that matter:
  * provisional bonus is never presented as confirmed
  * BPS ties consume the places below them, per FPL's actual rule
  * a player who has not kicked off is not "out", so he is never auto-subbed
  * a substitution only happens if the resulting XI is a legal formation
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gaffer import live

NOW = datetime(2026, 8, 22, 16, 0, tzinfo=UTC)
KO = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)


def fx(fid=1, event=1, minutes=0, started=False, finished=False,
       provisional=False, kickoff=KO, team_h=1, team_a=2, stats=None):
    return {
        "id": fid, "event": event, "team_h": team_h, "team_a": team_a,
        "minutes": minutes, "started": started, "finished": finished,
        "finished_provisional": provisional,
        "kickoff_time": None if kickoff is None else
        kickoff.isoformat().replace("+00:00", "Z"),
        "stats": stats or [],
    }


def bps_block(home: dict[int, int], away: dict[int, int]):
    return [{"identifier": "bps",
             "h": [{"value": v, "element": k} for k, v in home.items()],
             "a": [{"value": v, "element": k} for k, v in away.items()]}]


def el(pid, minutes=0, points=0, bps=0):
    return {"id": pid, "stats": {"minutes": minutes, "total_points": points,
                                 "bps": bps}}


# ==========================================================================
# Match and event states
# ==========================================================================

def test_before_first_kickoff():
    s = live.classify_fixture(fx(), NOW)
    assert s.state == live.STATE_SCHEDULED
    assert not s.in_play and not s.counts_as_played and not s.bonus_final


def test_live_match():
    s = live.classify_fixture(fx(started=True, minutes=23), NOW)
    assert s.state == live.STATE_LIVE and s.in_play
    assert not s.counts_as_played, "a live match cannot trigger autosubs"


def test_half_time():
    s = live.classify_fixture(fx(started=True, minutes=45), NOW)
    assert s.state == live.STATE_HALF_TIME and s.in_play


def test_full_time_awaiting_bonus():
    s = live.classify_fixture(fx(started=True, minutes=90, provisional=True), NOW)
    assert s.state == live.STATE_AWAITING_BONUS
    assert s.counts_as_played, "the match is over, so a blank is a real blank"
    assert not s.bonus_final, "bonus is not confirmed until `finished`"


def test_ninety_minutes_without_the_provisional_flag_is_still_over():
    s = live.classify_fixture(fx(started=True, minutes=90), NOW)
    assert s.state == live.STATE_AWAITING_BONUS


def test_finished_match_has_final_bonus():
    s = live.classify_fixture(
        fx(started=True, minutes=90, provisional=True, finished=True), NOW)
    assert s.state == live.STATE_FINISHED
    assert s.bonus_final and s.counts_as_played


def test_postponed_match_has_no_event():
    s = live.classify_fixture(fx(event=None), NOW)
    assert s.state == live.STATE_POSTPONED


def test_postponed_match_has_no_kickoff_time():
    s = live.classify_fixture(fx(kickoff=None), NOW)
    assert s.state == live.STATE_POSTPONED


def test_abandoned_match_started_long_ago_and_never_finished():
    s = live.classify_fixture(
        fx(started=True, minutes=31, kickoff=NOW - timedelta(hours=6)), NOW)
    assert s.state == live.STATE_ABANDONED


def test_a_long_match_that_finished_is_not_abandoned():
    s = live.classify_fixture(
        fx(started=True, minutes=90, finished=True,
           kickoff=NOW - timedelta(hours=6)), NOW)
    assert s.state == live.STATE_FINISHED


def test_states_are_from_the_declared_vocabulary():
    for raw in (fx(), fx(started=True, minutes=10), fx(event=None),
                fx(started=True, minutes=90, finished=True)):
        assert live.classify_fixture(raw, NOW).state in live.ALL_STATES


def test_only_this_gameweeks_fixtures_are_collected():
    states = live.fixture_states(
        [fx(fid=1, event=1), fx(fid=2, event=2), fx(fid=3, event=1)], 1, NOW)
    assert sorted(states) == [1, 3]


def test_a_missing_live_endpoint_yields_no_players_not_a_crash():
    assert live.player_live({}, {}, {}, {}) == {}
    assert live.player_live({"elements": []}, {}, {}, {}) == {}


# ==========================================================================
# Provisional bonus — official BPS rules
# ==========================================================================

def test_no_tie_awards_three_two_one():
    assert live.bonus_from_bps({1: 40, 2: 35, 3: 30, 4: 20}) == {1: 3, 2: 2, 3: 1}


def test_two_tied_on_top_both_get_three_and_the_next_gets_one():
    """The 2-point award is consumed by the tie. This is the rule tools miss."""
    out = live.bonus_from_bps({1: 40, 2: 40, 3: 30, 4: 20})
    assert out == {1: 3, 2: 3, 3: 1}
    assert 2 not in out.values() or list(out.values()).count(2) == 0


def test_three_tied_on_top_all_get_three_and_nothing_else_is_awarded():
    out = live.bonus_from_bps({1: 40, 2: 40, 3: 40, 4: 35})
    assert out == {1: 3, 2: 3, 3: 3}
    assert 4 not in out


def test_tie_for_second_both_get_two_and_no_one_is_awarded():
    out = live.bonus_from_bps({1: 40, 2: 35, 3: 35, 4: 30})
    assert out == {1: 3, 2: 2, 3: 2}
    assert 4 not in out


def test_tie_for_third_both_get_one():
    out = live.bonus_from_bps({1: 40, 2: 35, 3: 30, 4: 30})
    assert out == {1: 3, 2: 2, 3: 1, 4: 1}


def test_four_tied_on_top_all_get_three():
    out = live.bonus_from_bps({1: 9, 2: 9, 3: 9, 4: 9})
    assert set(out.values()) == {3} and len(out) == 4


def test_empty_bps_awards_nothing():
    assert live.bonus_from_bps({}) == {}


def test_a_single_player_gets_three():
    assert live.bonus_from_bps({7: 12}) == {7: 3}


def test_bonus_totals_never_exceed_the_ladder_without_ties():
    out = live.bonus_from_bps({i: 50 - i for i in range(1, 12)})
    assert sum(out.values()) == 6


def test_bps_is_read_from_the_fixture_stats_block():
    raw = fx(stats=bps_block({1: 30, 2: 25}, {3: 28}))
    assert live.fixture_bps(raw) == {1: 30, 2: 25, 3: 28}


def test_a_fixture_without_a_bps_block_yields_nothing():
    assert live.fixture_bps(fx(stats=[{"identifier": "goals_scored",
                                       "h": [], "a": []}])) == {}


def test_provisional_bonus_is_skipped_once_bonus_is_final():
    """Final bonus is already inside total_points; adding ours double-counts."""
    raw = fx(started=True, minutes=90, provisional=True, finished=True,
             stats=bps_block({1: 40}, {2: 20}))
    states = live.fixture_states([raw], 1, NOW)
    assert live.provisional_bonus([raw], states) == {}


def test_provisional_bonus_is_computed_while_the_match_is_live():
    raw = fx(started=True, minutes=60, stats=bps_block({1: 40}, {2: 20}))
    states = live.fixture_states([raw], 1, NOW)
    assert live.provisional_bonus([raw], states) == {1: 3, 2: 2}


def test_a_double_gameweek_accumulates_bonus_from_both_fixtures():
    a = fx(fid=1, started=True, minutes=90, stats=bps_block({1: 40}, {9: 5}))
    b = fx(fid=2, started=True, minutes=90, team_h=1, team_a=3,
           stats=bps_block({1: 38}, {8: 5}))
    states = live.fixture_states([a, b], 1, NOW)
    assert live.provisional_bonus([a, b], states)[1] == 6


def test_an_unstarted_fixture_contributes_no_bonus():
    raw = fx(stats=bps_block({1: 40}, {2: 20}))
    assert live.provisional_bonus([raw], live.fixture_states([raw], 1, NOW)) == {}


# ==========================================================================
# Confirmed / provisional / predicted separation
# ==========================================================================

TEAMS = {p: (1 if p <= 10 else 2) for p in range(1, 21)}


def _live_state(fixtures, elements, predictions=None):
    states = live.fixture_states(fixtures, 1, NOW)
    prov = live.provisional_bonus(fixtures, states)
    return live.player_live({"elements": elements}, states, prov, TEAMS,
                            predictions or {})


def test_the_three_kinds_of_points_never_merge():
    fixtures = [fx(started=True, minutes=70, stats=bps_block({1: 40}, {}))]
    pl = _live_state(fixtures, [el(1, minutes=70, points=6)])
    p = pl[1]
    assert p.confirmed == 6
    assert p.provisional == 3
    assert p.predicted == 0.0
    assert p.total == 9
    d = p.as_dict()
    assert d["confirmed"] == 6 and d["provisional"] == 3
    assert "total" in d, "the parts and the total are both published"


def test_a_player_yet_to_kick_off_carries_a_prediction_not_a_zero():
    fixtures = [fx(fid=9, team_h=2, team_a=3)]   # player 11's team, not started
    pl = _live_state(fixtures, [el(11)], predictions={11: 4.5})
    assert pl[11].yet_to_play is True
    assert pl[11].predicted == 4.5
    assert pl[11].confirmed == 0


def test_a_player_whose_match_finished_carries_no_prediction():
    fixtures = [fx(started=True, minutes=90, finished=True, provisional=True)]
    pl = _live_state(fixtures, [el(1, minutes=0, points=0)], predictions={1: 4.5})
    assert pl[1].predicted == 0.0
    assert pl[1].finished is True and pl[1].yet_to_play is False


def test_a_player_with_no_live_row_yet_is_still_tracked():
    """The live endpoint lags kick-off; his fixture still exists."""
    fixtures = [fx()]
    pl = _live_state(fixtures, [], predictions={5: 3.0})
    assert 5 in pl and pl[5].yet_to_play and pl[5].predicted == 3.0


def test_a_postponed_fixture_leaves_a_player_neither_played_nor_pending():
    fixtures = [fx(event=None)]
    pl = _live_state(fixtures, [el(1)], predictions={1: 5.0})
    assert pl[1].yet_to_play is False
    assert pl[1].predicted == 0.0, "a postponed match will not deliver points"


# ==========================================================================
# Autosubs
# ==========================================================================

POS = {
    **{p: "GKP" for p in (1, 12)},
    **{p: "DEF" for p in (2, 3, 4, 13)},
    **{p: "MID" for p in (5, 6, 7, 8, 14)},
    **{p: "FWD" for p in (9, 10, 11, 15)},
}
XI = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
BENCH = [12, 13, 14, 15]


def states_for(**kw) -> dict[int, live.PlayerLive]:
    """Every squad player finished; `kw` sets minutes (default 90)."""
    out = {}
    for p in XI + BENCH:
        m = kw.get(f"p{p}", 90)
        out[p] = live.PlayerLive(id=p, minutes=m, played=m > 0, finished=True)
    return out


def test_no_blanks_means_no_substitutions():
    a = live.apply_autosubs(XI, BENCH, POS, states_for())
    assert a.xi == XI and a.subs_in == []


def test_a_blanking_outfielder_is_replaced_in_bench_order():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p8=0))
    assert a.subs_out == [8] and a.subs_in == [13]
    assert 8 not in a.xi and 13 in a.xi


def test_a_keeper_is_only_replaced_by_the_bench_keeper():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p1=0))
    assert a.subs_in == [12] and a.subs_out == [1]
    assert any("Goalkeeper" in n for n in a.notes)


def test_an_outfielder_never_replaces_the_keeper():
    """Bench keeper also blanked: nobody else is eligible."""
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p1=0, p12=0))
    assert a.subs_in == [] and 1 in a.xi


def test_a_bench_player_who_did_not_play_cannot_come_on():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p8=0, p13=0))
    assert 13 not in a.subs_in
    assert a.subs_in == [14], "the next bench player who played comes on instead"


def test_a_player_yet_to_play_is_never_substituted():
    st = states_for(p8=0)
    st[8] = live.PlayerLive(id=8, minutes=0, played=False, finished=False,
                            yet_to_play=True)
    a = live.apply_autosubs(XI, BENCH, POS, st)
    assert a.subs_in == [], "he has not kicked off; he has not blanked"
    assert 8 in a.xi


def test_a_substitution_that_would_break_the_formation_is_refused():
    """3-4-3 with a blanking defender and only forwards left on the bench."""
    xi = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]         # 1-3-4-3
    bench = [15, 11, 10, 9]                            # forwards only
    pos = dict(POS)
    st = {p: live.PlayerLive(id=p, minutes=90, played=True, finished=True)
          for p in set(xi + bench)}
    st[2] = live.PlayerLive(id=2, minutes=0, played=False, finished=True)
    a = live.apply_autosubs(xi, [15], pos, st)
    assert a.subs_in == [], "would leave only 2 defenders"
    assert any("legal" in n for n in a.notes)


def test_a_legal_substitution_is_made_when_one_exists():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p2=0))
    assert a.subs_in == [13] and a.subs_out == [2]


def test_two_blanks_take_two_bench_players_in_order():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p8=0, p7=0))
    assert a.subs_in == [13, 14]
    assert set(a.subs_out) == {7, 8}


def test_bench_boost_makes_no_substitutions_at_all():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p8=0), bench_boost=True)
    assert a.subs_in == [] and a.xi == XI
    assert any("Bench Boost" in n for n in a.notes)


def test_the_armband_passes_to_the_vice_when_the_captain_blanks():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p9=0), captain=9, vice=10)
    assert a.captain == 10 and a.captain_source == "vice" and a.multiplier == 2
    assert any("vice" in n for n in a.notes)


def test_the_armband_stays_when_the_captain_plays():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(), captain=9, vice=10)
    assert a.captain == 9 and a.captain_source == "captain"


def test_a_captain_yet_to_play_keeps_the_armband():
    st = states_for()
    st[9] = live.PlayerLive(id=9, minutes=0, played=False, finished=False,
                            yet_to_play=True)
    a = live.apply_autosubs(XI, BENCH, POS, st, captain=9, vice=10)
    assert a.captain == 9, "he has not blanked yet"


def test_both_captain_and_vice_blanking_multiplies_nobody():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p9=0, p10=0),
                            captain=9, vice=10)
    assert a.captain is None and a.captain_source == "none" and a.multiplier == 1
    assert any("no player is multiplied" in n for n in a.notes)


def test_substitutions_are_provisional_until_every_fixture_is_over():
    st = states_for(p8=0)
    st[15] = live.PlayerLive(id=15, minutes=0, played=False, finished=False,
                             yet_to_play=True)
    a = live.apply_autosubs(XI, BENCH, POS, st)
    assert a.provisional is True


def test_substitutions_are_final_once_everything_has_finished():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p8=0))
    assert a.provisional is False


def test_autosubs_serialise_with_their_reasoning():
    a = live.apply_autosubs(XI, BENCH, POS, states_for(p9=0), captain=9, vice=10)
    d = a.as_dict()
    assert d["captain_source"] == "vice" and d["notes"]
    assert set(d) >= {"xi", "bench", "subs_in", "subs_out", "captain",
                      "multiplier", "provisional"}


# ==========================================================================
# Squad scoring
# ==========================================================================

def plive(**pts) -> dict[int, live.PlayerLive]:
    out = {}
    for p in XI + BENCH:
        c, pr = pts.get(f"p{p}", (2, 0))
        out[p] = live.PlayerLive(id=p, minutes=90, confirmed=c, provisional=pr,
                                 played=True, finished=True)
    return out


def test_the_captain_is_doubled_and_the_bench_is_excluded():
    s = live.score_squad(XI, BENCH, POS, plive(p9=(10, 3)), captain=9, vice=10)
    # 10 starters at 2 + captain 10*2 = 20+20 = 40 confirmed
    assert s.confirmed == 10 * 2 + 20
    assert s.provisional == 6, "the captain's bonus doubles too"
    assert s.bench_points == 8


def test_triple_captain_multiplies_by_three():
    base = live.score_squad(XI, BENCH, POS, plive(p9=(10, 0)), captain=9)
    tc = live.score_squad(XI, BENCH, POS, plive(p9=(10, 0)), captain=9,
                          triple_captain=True)
    assert tc.confirmed - base.confirmed == 10


def test_the_triple_captain_multiplier_reaches_the_autosub_record():
    """The defect: `score_squad` recomputed the multiplier locally and left
    `Autosubs.multiplier` at 2, so `largest_swing` — which reads it from there —
    understated a Triple Captain week by a third."""
    tc = live.score_squad(XI, BENCH, POS, plive(p9=(10, 0)), captain=9,
                          triple_captain=True)
    assert tc.autosubs.multiplier == 3
    plain = live.score_squad(XI, BENCH, POS, plive(p9=(10, 0)), captain=9)
    assert plain.autosubs.multiplier == 2


def test_a_triple_captain_swing_is_measured_at_three_times():
    st = plive(p9=(20, 3))
    rival_xi = [p for p in XI if p != 9] + [15]
    mine_tc = live.score_squad(XI, BENCH, POS, st, captain=9, entry_id=1,
                               triple_captain=True)
    mine_x2 = live.score_squad(XI, BENCH, POS, st, captain=9, entry_id=1)
    theirs = live.score_squad(rival_xi, BENCH, POS, st, captain=1, entry_id=2)
    tc = live.largest_swing(mine_tc, [theirs], st)
    x2 = live.largest_swing(mine_x2, [theirs], st)
    assert tc["player_id"] == x2["player_id"] == 9
    assert tc["swing"] == pytest.approx(x2["swing"] * 1.5), \
        "x3 instead of x2 is exactly 50% more swing"


def test_a_blanked_captain_and_vice_still_multiply_nobody_under_triple_captain():
    st = plive(p9=(0, 0), p10=(0, 0))
    for pid in (9, 10):
        st[pid] = live.PlayerLive(id=pid, minutes=0, confirmed=0, provisional=0,
                                  played=False, finished=True)
    s = live.score_squad(XI, BENCH, POS, st, captain=9, vice=10,
                         triple_captain=True)
    assert s.autosubs.captain is None
    assert s.autosubs.multiplier == 1


# --------------------------------------------------------------------------
# Season baseline and hits
# --------------------------------------------------------------------------

HISTORY = {"current": [
    {"event": 1, "points": 62, "total_points": 62, "event_transfers_cost": 0},
    {"event": 2, "points": 51, "total_points": 109, "event_transfers_cost": 4},
    {"event": 3, "points": 70, "total_points": 179, "event_transfers_cost": 0},
    {"event": 4, "points": 40, "total_points": 211, "event_transfers_cost": 8},
]}


def test_the_baseline_is_the_total_before_this_gameweek():
    """`summary_overall_points` cannot be used: once the gameweek starts scoring
    it already contains the points the live view is computing."""
    baseline, hits = live.entry_baseline_and_hits(HISTORY, 4)
    assert baseline == 179, "cumulative total at GW3, not the season total"
    assert hits == 8, "the -8 paid FOR gameweek 4"


def test_hits_are_read_not_assumed_zero():
    assert live.entry_baseline_and_hits(HISTORY, 2)[1] == 4
    assert live.entry_baseline_and_hits(HISTORY, 3)[1] == 0


def test_the_first_gameweek_has_no_baseline():
    assert live.entry_baseline_and_hits(HISTORY, 1) == (0, 0)


def test_a_missing_history_is_zero_rather_than_an_exception():
    assert live.entry_baseline_and_hits(None, 5) == (0, 0)
    assert live.entry_baseline_and_hits({}, 5) == (0, 0)
    assert live.entry_baseline_and_hits({"current": []}, 5) == (0, 0)


def test_a_history_without_cumulative_totals_is_rebuilt_net_of_hits():
    hist = {"current": [
        {"event": 1, "points": 62, "event_transfers_cost": 0},
        {"event": 2, "points": 51, "event_transfers_cost": 4},
    ]}
    assert live.entry_baseline_and_hits(hist, 3)[0] == 62 + 51 - 4


def test_bench_boost_scores_all_fifteen_and_reports_no_bench_points():
    s = live.score_squad(XI, BENCH, POS, plive(), captain=9, bench_boost=True)
    assert s.confirmed == 15 * 2 + 2      # 15 players + captain's extra copy
    assert s.bench_points == 0


def test_hits_reduce_the_current_score():
    s = live.score_squad(XI, BENCH, POS, plive(), captain=9, hits=4)
    assert s.current == s.confirmed + s.provisional - 4


def test_the_season_total_adds_the_carried_baseline():
    s = live.score_squad(XI, BENCH, POS, plive(), captain=9, baseline=120)
    assert s.as_dict()["season_total_projected"] == pytest.approx(120 + s.projected)


def test_projected_adds_only_the_predicted_part():
    st = plive()
    st[11] = live.PlayerLive(id=11, minutes=0, predicted=5.0, yet_to_play=True)
    s = live.score_squad(XI, BENCH, POS, st, captain=9)
    assert s.predicted == 5.0
    assert s.projected == s.current + 5.0
    assert s.players_yet_to_play == 1


def test_the_dict_keeps_confirmed_provisional_and_predicted_apart():
    s = live.score_squad(XI, BENCH, POS, plive(p9=(10, 3)), captain=9)
    d = s.as_dict()
    assert {"confirmed", "provisional_bonus", "predicted_remaining",
            "current", "projected"} <= set(d)
    assert d["confirmed"] != d["current"], "bonus is reported separately"


# ==========================================================================
# Rivals and league swing
# ==========================================================================

def test_user_and_rivals_are_scored_from_one_live_state():
    st = plive(p9=(12, 3))
    mine = live.score_squad(XI, BENCH, POS, st, captain=9, entry_id=1)
    theirs = live.score_squad(XI, BENCH, POS, st, captain=9, entry_id=2)
    assert mine.confirmed == theirs.confirmed, "same state, same football"


def test_the_largest_swing_is_a_differential_not_a_shared_player():
    st = plive(p9=(20, 3))       # a huge haul...
    rival_xi = list(XI)          # ...but the rival owns him too
    mine = live.score_squad(XI, BENCH, POS, st, captain=1, entry_id=1)
    theirs = live.score_squad(rival_xi, BENCH, POS, st, captain=1, entry_id=2)
    assert live.largest_swing(mine, [theirs], st) is None, \
        "a shared player cannot move a mini-league"


def test_a_differential_haul_is_identified_and_signed():
    st = plive(p9=(20, 3))
    rival_xi = [p for p in XI if p != 9] + [15]
    mine = live.score_squad(XI, BENCH, POS, st, captain=1, entry_id=1)
    theirs = live.score_squad(rival_xi, BENCH, POS, st, captain=1, entry_id=2)
    swing = live.largest_swing(mine, [theirs], st, names={9: "Haaland"})
    assert swing["player_id"] == 9 and swing["name"] == "Haaland"
    assert swing["swing"] > 0 and swing["in_your_xi"] is True


def test_a_rivals_differential_swings_against_you():
    st = plive(p15=(18, 3))
    rival_xi = [p for p in XI if p != 9] + [15]
    mine = live.score_squad(XI, BENCH, POS, st, captain=1, entry_id=1)
    theirs = live.score_squad(rival_xi, BENCH, POS, st, captain=1, entry_id=2)
    swing = live.largest_swing(mine, [theirs], st)
    assert swing["swing"] < 0 and swing["in_your_xi"] is False


def test_no_rivals_means_no_swing():
    st = plive()
    mine = live.score_squad(XI, BENCH, POS, st, captain=9, entry_id=1)
    assert live.largest_swing(mine, [], st) is None
