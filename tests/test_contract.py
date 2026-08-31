"""T-04 — the artifact contract.

Before this existed the only pre-publish check was ``git status --porcelain``,
which detects difference, not validity. These tests pin the failure cases that
must block a publish.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gaffer import config, contract, gameweek

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
STAMP = NOW.isoformat(timespec="seconds")


def _player(pid: int) -> dict:
    return {"id": pid, "name": f"P{pid}", "pos": "MID", "team": "C1",
            "price": 5.0, "next_gw_xp": 3.0}


def _card(pid: int) -> dict:
    return {"id": pid, "name": f"P{pid}"}


def write_valid_set(d, *, generated_at=STAMP, n_players=450,
                    build_mode="personalised", entry_id=1066421, my_team=True,
                    squad_status=gameweek.STATUS_LOADED, squad_source_event=1,
                    squad_status_reason="picks read for GW1"):
    d.mkdir(parents=True, exist_ok=True)

    def dump(name, obj):
        (d / name).write_text(json.dumps(obj), encoding="utf-8")

    dump("players.json", [_player(i) for i in range(1, n_players + 1)])
    dump("fixtures.json", {"C1": {"team": "C1", "fixtures": []}})
    dump("meta.json", {
        "current_gw": "2", "generated_at": generated_at, "season": "2026-27",
        "model_version": "heuristic-0.1", "build_mode": build_mode,
        "entry_id": entry_id, "league_ids": [271619],
        "projection_event": 2,
        "squad_status": squad_status,
        "squad_source_event": squad_source_event,
        "squad_status_reason": squad_status_reason,
        "squad_retrieved_at": generated_at,
    })
    dump("recommendation.json", {
        "generated_at": generated_at, "mode": "build", "status": "Optimal",
        "formation": "4-4-2",
        "captain": _card(1), "vice": _card(2),
        "starting": [_card(i) for i in range(1, 12)],
        "bench": [_card(i) for i in range(12, 16)],
    })
    dump("plan.json", {"generated_at": generated_at, "status": "Optimal", "steps": [{}]})
    dump("my_team.json", {"gw": 1, "players": [_player(1)]} if my_team else None)
    return d


@pytest.fixture
def art(tmp_path):
    return write_valid_set(tmp_path / "data")


def validate(d, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("expected_entry_id", 1066421)
    kw.setdefault("require_personalised", True)
    return contract.validate(d, **kw)


def fields(report):
    return {v.field for v in report.violations}


# --------------------------------------------------------------------------

def test_valid_set_passes(art):
    r = validate(art)
    assert r.ok, r.render()
    assert "meta.json" in r.checked and "players.json" in r.checked
    assert "OK" in r.render()


def test_missing_artifact(art):
    (art / "players.json").unlink()
    r = validate(art)
    assert not r.ok
    assert any(v.artifact == "players.json" and "exist" in v.expected for v in r.violations)


def test_malformed_json(art):
    (art / "recommendation.json").write_text("{not json", encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("JSON" in v.expected for v in r.violations)


def test_too_few_players(art):
    write_valid_set(art, n_players=399)
    r = validate(art)
    assert not r.ok
    v = next(v for v in r.violations if v.artifact == "players.json")
    assert v.value == 399 and "400" in v.expected


def test_exactly_400_players_passes(art):
    write_valid_set(art, n_players=400)
    assert validate(art).ok


def test_stale_timestamp(art):
    old = (NOW - timedelta(hours=2)).isoformat(timespec="seconds")
    write_valid_set(art, generated_at=old)
    r = validate(art)
    assert not r.ok
    v = next(v for v in r.violations if v.field == "generated_at")
    assert "did not regenerate" in v.expected


def test_the_real_11_day_outage_is_caught(art):
    """The shipped 2026-07-26 artifacts against 2026-08-06."""
    write_valid_set(art, generated_at="2026-07-26T15:53:50+00:00")
    assert not validate(art).ok


def test_future_timestamp_beyond_skew(art):
    ahead = (NOW + timedelta(hours=3)).isoformat(timespec="seconds")
    write_valid_set(art, generated_at=ahead)
    r = validate(art)
    assert not r.ok
    assert any("future" in v.expected for v in r.violations)


def test_small_clock_skew_tolerated(art):
    ahead = (NOW + timedelta(minutes=2)).isoformat(timespec="seconds")
    write_valid_set(art, generated_at=ahead)
    assert validate(art).ok


def test_naive_timestamp_rejected(art):
    write_valid_set(art, generated_at="2026-08-06T12:00:00")
    r = validate(art)
    assert not r.ok
    assert any("UTC offset" in v.expected for v in r.violations)


def test_timestamps_must_agree_across_artifacts(art):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["generated_at"] = (NOW - timedelta(minutes=1)).isoformat(timespec="seconds")
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any(
        v.artifact == "recommendation.json" and "same run timestamp" in v.expected
        for v in r.violations
    )


@pytest.mark.parametrize("n", [10, 12])
def test_wrong_xi_size(art, n):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["starting"] = [_card(i) for i in range(1, n + 1)]
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    v = next(v for v in r.violations if v.field == "starting")
    assert "exactly 11" in v.expected


def test_duplicate_player_in_xi(art):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["starting"] = [_card(1)] + [_card(i) for i in range(1, 11)]
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("duplicate" in str(v.value) for v in r.violations)


def test_xi_and_bench_overlap(art):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["bench"] = [_card(1), _card(13), _card(14), _card(15)]
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("disjoint" in v.expected for v in r.violations)


def test_wrong_bench_size(art):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["bench"] = [_card(12), _card(13)]
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "bench" for v in r.violations)


def test_unknown_player_reference(art):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["starting"][0] = _card(999_999)
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("unknown ids" in str(v.value) for v in r.violations)


def test_captain_must_start(art):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["captain"] = _card(13)  # on the bench
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert "captain.id" in fields(r)


def test_captain_and_vice_must_differ(art):
    rec = json.loads((art / "recommendation.json").read_text(encoding="utf-8"))
    rec["vice"] = _card(1)
    (art / "recommendation.json").write_text(json.dumps(rec), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert "vice.id" in fields(r)


def test_status_loaded_with_null_my_team_is_a_violation(art):
    """'loaded' asserts a squad is stored; a null squad contradicts it."""
    write_valid_set(art, my_team=False, squad_status=gameweek.STATUS_LOADED)
    r = validate(art)
    assert not r.ok
    assert any(v.artifact == "my_team.json" for v in r.violations)


def test_null_my_team_allowed_only_before_the_first_deadline(art):
    """Pre-GW1 no entry has a readable squad — legitimate, and named as such.

    Without this the gate would block every publish before 2026-08-21.
    """
    write_valid_set(art, my_team=False,
                    squad_status=gameweek.STATUS_NO_PUBLIC_SQUAD_YET,
                    squad_source_event=None,
                    squad_status_reason="no deadline has passed yet")
    assert validate(art).ok, validate(art).render()


@pytest.mark.parametrize("status", [
    gameweek.STATUS_NOT_FOUND, gameweek.STATUS_FETCH_FAILED,
    gameweek.STATUS_MALFORMED, gameweek.STATUS_NO_ENTRY_ID,
])
def test_real_failures_are_not_hidden_behind_a_benign_status(art, status):
    """A 404 after a deadline has passed is a defect, not 'pre-season'."""
    write_valid_set(art, my_team=False, squad_status=status,
                    squad_source_event=None, squad_status_reason="something broke")
    r = validate(art)
    assert not r.ok
    assert any(v.artifact == "my_team.json" for v in r.violations)


def test_stale_squad_must_declare_its_source_event(art):
    """A retained squad is allowed, but only when labelled and attributed."""
    write_valid_set(art, squad_status=gameweek.STATUS_STALE, squad_source_event=1,
                    squad_status_reason="HTTP 503; showing the squad stored from GW1")
    assert validate(art).ok, validate(art).render()

    write_valid_set(art, squad_status=gameweek.STATUS_STALE, squad_source_event=None,
                    squad_status_reason="HTTP 503")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "squad_source_event" for v in r.violations)


def test_no_squad_status_must_not_claim_a_source_event(art):
    """The invariant that stops stale rows masquerading as current holdings."""
    write_valid_set(art, my_team=False,
                    squad_status=gameweek.STATUS_NO_PUBLIC_SQUAD_YET,
                    squad_source_event=7, squad_status_reason="pre-season")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "squad_source_event" for v in r.violations)


def test_squad_present_while_status_says_none_is_a_violation(art):
    write_valid_set(art, my_team=True,
                    squad_status=gameweek.STATUS_NO_PUBLIC_SQUAD_YET,
                    squad_source_event=None, squad_status_reason="pre-season")
    r = validate(art)
    assert not r.ok
    assert any(v.artifact == "my_team.json" for v in r.violations)


def test_unknown_squad_status_rejected(art):
    write_valid_set(art, squad_status="unavailable (404)")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "squad_status" for v in r.violations)


def test_squad_status_requires_a_reason(art):
    write_valid_set(art, squad_status_reason="")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "squad_status_reason" for v in r.violations)


def test_generic_run_may_have_null_my_team(art):
    write_valid_set(art, my_team=False, build_mode="generic", entry_id=None,
                    squad_status=gameweek.STATUS_NO_ENTRY_ID,
                    squad_source_event=None,
                    squad_status_reason="no entry id configured")
    r = validate(art, expected_entry_id=None, require_personalised=False)
    assert r.ok, r.render()


def test_personalised_run_must_name_the_configured_entry(art):
    write_valid_set(art, entry_id=999)
    r = validate(art)
    assert not r.ok
    v = next(v for v in r.violations if v.field == "entry_id")
    assert "1066421" in v.expected


def test_generic_build_mode_rejected_when_entry_configured(art):
    write_valid_set(art, build_mode="generic")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "build_mode" for v in r.violations)


def test_missing_build_mode_rejected(art):
    meta = json.loads((art / "meta.json").read_text(encoding="utf-8"))
    del meta["build_mode"]
    (art / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert "build_mode" in fields(r)


def test_league_ids_must_be_a_list(art):
    """Multi-league support: never assume a single scalar league."""
    meta = json.loads((art / "meta.json").read_text(encoding="utf-8"))
    meta["league_ids"] = 271619
    (art / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("list of league ids" in v.expected for v in r.violations)


def test_many_league_ids_pass(art):
    meta = json.loads((art / "meta.json").read_text(encoding="utf-8"))
    meta["league_ids"] = [271619, 314, 1]
    (art / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert validate(art).ok


def test_error_messages_are_actionable(art):
    write_valid_set(art, n_players=10)
    r = validate(art)
    text = r.render()
    assert "players.json" in text            # which artifact
    assert "<length>" in text                 # which field
    assert "10" in text                       # the invalid value
    assert "at least 400" in text             # the expected contract
    assert "FAILED" in text


def test_report_serialises(art):
    write_valid_set(art, n_players=10)
    d = validate(art).as_dict()
    assert d["ok"] is False
    assert d["violations"][0]["artifact"] == "players.json"


# --------------------------------------------------------------------------
# CLI exit status — this is what the workflow gates on.
# --------------------------------------------------------------------------

def test_cli_exit_zero_on_valid_set(art, monkeypatch, capsys):
    monkeypatch.setenv("GAFFER_ENTRY_ID", "1066421")
    config.reload_paths()
    # A fresh stamp so the 1-hour age rule passes against the real clock.
    write_valid_set(art, generated_at=datetime.now(UTC).isoformat(timespec="seconds"))
    assert contract.main(["--data-dir", str(art)]) == 0
    assert "OK" in capsys.readouterr().out


def test_cli_exit_one_on_invalid_set(art, capsys):
    write_valid_set(art, n_players=5,
                    generated_at=datetime.now(UTC).isoformat(timespec="seconds"))
    assert contract.main(["--data-dir", str(art), "--allow-generic"]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_cli_json_output(art, capsys):
    write_valid_set(art, generated_at=datetime.now(UTC).isoformat(timespec="seconds"))
    contract.main(["--data-dir", str(art), "--json", "--allow-generic"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"ok", "data_dir", "checked", "violations"}


def test_parse_iso_utc_accepts_z_suffix():
    assert contract.parse_iso_utc("2026-08-06T12:00:00Z") == NOW
    assert contract.parse_iso_utc("2026-08-06T12:00:00+00:00") == NOW
    assert contract.parse_iso_utc("2026-08-06T12:00:00") is None  # naive
    assert contract.parse_iso_utc("nonsense") is None
    assert contract.parse_iso_utc(None) is None


# --------------------------------------------------------------------------
# Backtest artifact (Batch 3)
# --------------------------------------------------------------------------

def _valid_backtest(**over):
    from gaffer import backtest as bt_mod
    from gaffer.model import projection as proj_mod
    d = {
        "schema_version": bt_mod.SCHEMA_VERSION,
        # Read from the module for the same reason `schema_version` is: the
        # published artifact must describe the model that is actually running,
        # and a frozen string here would let that drift pass the fixture.
        "model_version": proj_mod.MODEL_VERSION, "season": "2024-25",
        "per_horizon": {"1": {"n": 10, "mae": {}, "rank_corr": {}}},
        "coverage": {"rows_evaluated": 10},
        "leakage_check": {"enforced": True, "post_match_fields_in_features": []},
        "limitations": ["season-end ratings"],
        "withdrawn_baselines": {
            "fpl_xp": {"withdrawn_in_schema": 4,
                       "previously_reported": {"rank_corr_h1": 0.76},
                       "reason": "computed from the archive's xP column"},
        },
        "model_candidates": {
            "candidates": [
                {"candidate": "gbm", "decision": "rejected",
                 "worse_at_every_horizon": True,
                 "per_horizon": {"1": {"diff": -4.65}}},
                {"candidate": "ridge", "decision": "inconclusive",
                 "worse_at_every_horizon": False,
                 "per_horizon": {"1": {"diff": 2.70}}},
            ],
        },
        "generated_at": STAMP,
    }
    d.update(over)
    return d


def test_backtest_without_the_withdrawal_record_is_rejected(art):
    """A retracted baseline must stay visible, not silently vanish."""
    bad = _valid_backtest()
    del bad["withdrawn_baselines"]
    (art / "backtest.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "withdrawn_baselines" for v in r.violations)


def test_a_withdrawn_baseline_reported_as_measured_is_rejected(art):
    """T-26: fpl_xp/ensemble may never come back as a per-horizon metric."""
    bad = _valid_backtest(per_horizon={
        "1": {"n": 10, "mae": {"gaffer": 1.5, "fpl_xp": 0.9},
              "rank_corr": {"gaffer": 0.44, "fpl_xp": 0.76}}})
    (art / "backtest.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    v = next(v for v in r.violations if "fpl_xp" in str(v.value))
    assert "retracted for leakage" in v.expected


def test_an_unrecognised_artifact_is_rejected(art):
    """Nothing gets published that no checker claimed."""
    (art / "surprise.json").write_text('{"hello": 1}', encoding="utf-8")
    r = validate(art)
    assert not r.ok
    v = next(v for v in r.violations if v.artifact == "surprise.json")
    assert "validated by nothing" in v.expected


def test_valid_backtest_passes(art):
    (art / "backtest.json").write_text(json.dumps(_valid_backtest()), encoding="utf-8")
    assert validate(art).ok, validate(art).render()


def test_legacy_backtest_is_rejected(art):
    """The shipped artifact: no schema_version, ml-vs-heuristic numbers."""
    legacy = {"season": "2024-25", "mae": {"gaffer": 1.889, "ml": 1.858},
              "rank_corr": {"gaffer": 0.3, "ml": 0.379}, "generated_at": STAMP}
    (art / "backtest.json").write_text(json.dumps(legacy), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    # Find the violation by field rather than by position: a legacy artifact
    # now trips several checks at once, and which comes first is not the point.
    v = next(v for v in r.violations
             if v.artifact == "backtest.json" and v.field == "schema_version")
    assert "never shipped" in v.expected


def test_backtest_with_leakage_is_rejected(art):
    bad = _valid_backtest(leakage_check={
        "enforced": True, "post_match_fields_in_features": ["minutes"]})
    (art / "backtest.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("post-match" in v.expected for v in r.violations)


def test_backtest_without_leakage_check_is_rejected(art):
    bad = _valid_backtest(leakage_check={"enforced": False,
                                         "post_match_fields_in_features": []})
    (art / "backtest.json").write_text(json.dumps(bad), encoding="utf-8")
    assert not validate(art).ok


def test_backtest_without_limitations_is_rejected(art):
    (art / "backtest.json").write_text(
        json.dumps(_valid_backtest(limitations=[])), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("caveats" in v.expected for v in r.violations)


def test_backtest_with_empty_horizons_is_rejected(art):
    (art / "backtest.json").write_text(
        json.dumps(_valid_backtest(per_horizon={})), encoding="utf-8")
    assert not validate(art).ok


def test_missing_backtest_is_not_an_error(art):
    """It is a manual step; absence must not block a publish."""
    assert not (art / "backtest.json").exists()
    assert validate(art).ok


def test_a_candidate_claiming_it_lost_everywhere_cannot_record_a_win(art):
    """The artifact must not publish a claim its own numbers contradict."""
    bad = _valid_backtest(model_candidates={"candidates": [
        {"candidate": "gbm", "decision": "rejected",
         "worse_at_every_horizon": True,
         "per_horizon": {"1": {"diff": -4.0}, "2": {"diff": 1.5}}},
        {"candidate": "ridge", "decision": "inconclusive"},
    ]})
    (art / "backtest.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("worse_at_every_horizon" in v.field for v in r.violations)


def test_a_single_collapsed_verdict_is_rejected(art):
    """Reporting one candidate is how "ridge was inconclusive" became
    "trained models lose every decision metric"."""
    bad = _valid_backtest(model_candidates={"candidates": [
        {"candidate": "gbm", "decision": "rejected"}]})
    (art / "backtest.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("every evaluated candidate" in v.expected for v in r.violations)


def test_a_candidate_without_its_own_decision_is_rejected(art):
    bad = _valid_backtest(model_candidates={"candidates": [
        {"candidate": "gbm", "decision": "bad"},
        {"candidate": "ridge", "decision": "inconclusive"}]})
    (art / "backtest.json").write_text(json.dumps(bad), encoding="utf-8")
    assert not validate(art).ok


# --- the AI envelope ---------------------------------------------------------

def _valid_news(**over):
    d = {
        "digest_md": "- something happened",
        "items": [{"id": "src-a", "source": "BBC", "link": "https://x/1",
                   "title": "Arsenal sign someone"}],
        "claims": [{"text": "Arsenal signed someone.",
                    "source_item_ids": ["src-a"]}],
        "source": "ai", "fallback_reason": None, "model": "claude-haiku-4-5",
        "generated_at": STAMP, "season": "2026-27",
    }
    d.update(over)
    return d


def test_a_valid_news_artifact_passes(art):
    (art / "news.json").write_text(json.dumps(_valid_news()), encoding="utf-8")
    assert validate(art).ok, validate(art).render()


def test_the_old_leaky_source_string_is_rejected(art):
    """The exact value the pipeline used to publish."""
    bad = _valid_news(source="template (ai failed: APIStatusError)",
                      fallback_reason=None, model=None)
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "source" for v in r.violations)


def test_a_template_fallback_must_say_why(art):
    bad = _valid_news(source="template", fallback_reason=None, model=None)
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    assert not validate(art).ok


def test_a_template_fallback_must_not_name_a_model(art):
    bad = _valid_news(source="template", fallback_reason="no_credentials",
                      model="claude-haiku-4-5")
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "model" for v in r.violations)


def test_an_unknown_fallback_reason_is_rejected(art):
    bad = _valid_news(source="template", fallback_reason="vibes", model=None)
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    assert not validate(art).ok


def test_a_dangling_claim_citation_is_rejected(art):
    bad = _valid_news(claims=[{"text": "x", "source_item_ids": ["src-nope"]}])
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any("source_item_ids" in v.field for v in r.violations)


def test_an_uncited_claim_is_rejected(art):
    bad = _valid_news(claims=[{"text": "x", "source_item_ids": []}])
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    assert not validate(art).ok


def test_a_url_inside_generated_text_is_rejected(art):
    bad = _valid_news(claims=[{"text": "see https://evil.example",
                               "source_item_ids": ["src-a"]}])
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    assert not validate(art).ok


def test_exception_text_in_a_published_artifact_is_rejected(art):
    bad = _valid_news(digest_md="Traceback (most recent call last): ...")
    (art / "news.json").write_text(json.dumps(bad), encoding="utf-8")
    r = validate(art)
    assert not r.ok
    assert any(v.field == "<content>" for v in r.violations)



def _a4_decision(*, action="roll", direct=False, candidate=True):
    from gaffer import decision as decision_mod
    from gaffer import weekly as weekly_mod
    future = ({
        "status": "evidence_only", "basis": "future_horizon",
        "label": "Future plan — not this week's action",
        "reason": "positive over six weeks, negative now",
        "transfers_in": [_card(3)], "transfers_out": [_card(2)],
        "captain": _card(3), "vice": _card(1),
        "executability": {"paid_transfers": 4},
    } if candidate else None)
    return {
        "weekly_version": weekly_mod.WEEKLY_VERSION,
        "decision_version": decision_mod.DECISION_VERSION,
        "gameweek": 3,
        "decision": {
            "action": action, "headline": "Roll your transfer",
            "reason": "negative now", "biggest_risk": "future edge",
            "transfers_in": [_card(3)] if direct else [],
            "transfers_out": [_card(2)] if direct else [],
            "executability": {"paid_transfers": 4} if direct else None,
            "candidate_move": future,
            "comparison": {
                "move_expected": 43.76, "hold_expected": 48.34,
                "delta": -4.58, "delta_ci95": [-4.99, -4.17],
                "p_move_beats_hold": 0.2865, "simulations": 2000,
                "short_term_delta": -4.58, "horizon_delta": 16.67,
                "hit_cost": 16,
            },
        },
        "versions": {
            "model_version": "m", "objective_version": "o",
            "sim_version": "s", "n_sims": 2000, "seed": 1,
        },
        "freshness": {"generated_at": STAMP},
    }


def test_a4_contract_accepts_a_roll_with_separate_future_evidence():
    report = contract.Report("test")
    contract._check_decision(_a4_decision(), report)
    assert report.ok, report.render()


def test_a4_contract_rejects_transfers_attached_to_a_non_action():
    report = contract.Report("test")
    contract._check_decision(_a4_decision(action="too_close", direct=True), report)
    assert "decision.transfers" in fields(report)
    assert "decision.executability" in fields(report)
    assert any(v.field == "decision.action" and "negative result" in v.expected
               for v in report.violations)


def test_a4_contract_requires_the_conflicting_horizon_plan_to_be_labelled():
    report = contract.Report("test")
    contract._check_decision(_a4_decision(candidate=False), report)
    assert "decision.candidate_move" in fields(report)


def test_the_percentile_basis_check_reads_the_key_the_artifact_publishes():
    """The gate must read `outcome_percentile_basis`, not the attribute name.

    Reading `percentile_basis` matched nothing, so the check fired on every
    review that carried a percentile -- including correct ones -- and blocked a
    publish while proving nothing. A gate that cannot pass is not a gate.
    """
    from gaffer import review as R

    good = R.assess(expected=None, realised=50, percentile=0.206,
                    hold_expected=None, has_snapshot=True,
                    missing_fields=["decision.comparison.move_expected"]).as_dict()
    assert "outcome_percentile_basis" in good, (
        "the artifact key changed; this contract check reads it by name")

    rep = contract.Report(data_dir=Path("data"))
    contract._check_review({"review_version": R.REVIEW_VERSION, "event": 1,
                     "entry_id": 1, "generated_at": "2026-08-31T00:00:00+00:00",
                     "comparison": {}, "attribution": {}, "quality": good,
                     "has_snapshot": True,
                     "snapshot_as_of": "2026-08-31T00:00:00+00:00"}, rep)
    basis = [v for v in rep.violations if "percentile_basis" in v.field]
    assert basis == [], f"a correctly-formed review was rejected: {basis}"

    stripped = {k: v for k, v in good.items() if k != "outcome_percentile_basis"}
    rep2 = contract.Report(data_dir=Path("data"))
    contract._check_review({"review_version": R.REVIEW_VERSION, "event": 1,
                     "entry_id": 1, "generated_at": "2026-08-31T00:00:00+00:00",
                     "comparison": {}, "attribution": {}, "quality": stripped,
                     "has_snapshot": True,
                     "snapshot_as_of": "2026-08-31T00:00:00+00:00"}, rep2)
    assert [v for v in rep2.violations if "percentile_basis" in v.field], (
        "a published percentile with no stated reference class must be rejected")
