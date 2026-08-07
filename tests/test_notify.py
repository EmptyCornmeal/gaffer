"""T-24 — the notification engine. Dry-run, deduplicated, timezone-safe.

Three properties are non-negotiable and are asserted here rather than trusted:

  * **Nothing is sent.** The default is dry-run, the env guard forces it on in
    tests, and no test in this file can reach a network socket (conftest blocks
    it) — so a regression that starts delivering fails loudly.
  * **A re-run is not a new alert.** Dedupe keys are built from the fact, not the
    clock, so a pipeline running three times an hour buzzes once.
  * **Quiet hours are real hours.** Europe/London, verified across the BST/GMT
    boundary, because a hard-coded UTC offset is wrong for five months a year.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from gaffer.notify import rules
from gaffer.notify.engine import (
    CRITICAL,
    IMPORTANT,
    INFO,
    STATE_DRY_RUN,
    STATE_FAILED,
    STATE_SENT,
    STATE_SUPPRESSED,
    Alert,
    Engine,
    quiet_hours,
)
from gaffer.notify.sinks import ConfigError, MemorySink, describe, resolve_sink
from gaffer.store import db

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)          # midday, BST
DEADLINE = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)
DL_ISO = DEADLINE.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _force_dry_run(monkeypatch):
    """Belt and braces: even a test that asks to send cannot."""
    monkeypatch.setenv("GAFFER_NOTIFY_FORCE_DRY_RUN", "1")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "n.db")
    db.init_schema(c)
    yield c
    c.close()


def alert(kind="deadline", severity=IMPORTANT, parts=(1,), **kw):
    return Alert(kind=kind, title="t", body="b", severity=severity,
                 dedupe_parts=parts, **kw)


# ==========================================================================
# Dry run is the default and cannot be lost by accident
# ==========================================================================

def test_dry_run_is_the_default(conn):
    e = Engine(conn, MemorySink())
    assert e.dry_run is True


def test_the_env_guard_overrides_an_explicit_send(conn):
    """A test asking for send=True must still not send."""
    e = Engine(conn, MemorySink(), dry_run=False)
    assert e.dry_run is True, "GAFFER_NOTIFY_FORCE_DRY_RUN must win"


def test_a_dry_run_records_dry_run_state_not_sent(conn):
    sink = MemorySink()
    res = Engine(conn, sink).run([alert()], now=NOW)
    assert res.dry_run is True and res.delivered == 1
    row = conn.execute("SELECT state, dry_run FROM notifications").fetchone()
    assert row["state"] == STATE_DRY_RUN and row["dry_run"] == 1
    assert len(sink) == 1, "the sink is exercised; it just stores and drops"


def test_the_default_sink_delivers_nowhere():
    assert isinstance(resolve_sink(), MemorySink)
    assert resolve_sink("memory").send(alert()) is None


def test_an_unconfigured_webhook_refuses_to_construct(monkeypatch):
    monkeypatch.delenv("GAFFER_NOTIFY_WEBHOOK", raising=False)
    with pytest.raises(ConfigError, match="GAFFER_NOTIFY_WEBHOOK"):
        resolve_sink("webhook")


def test_a_non_https_webhook_is_refused(monkeypatch):
    monkeypatch.setenv("GAFFER_NOTIFY_WEBHOOK", "http://insecure.example")
    with pytest.raises(ConfigError, match="https"):
        resolve_sink("webhook")


def test_an_unknown_sink_is_an_error_not_a_silent_noop():
    with pytest.raises(ConfigError, match="unknown notification sink"):
        resolve_sink("carrier-pigeon")


def test_describe_reports_presence_never_the_secret(monkeypatch):
    monkeypatch.setenv("GAFFER_NOTIFY_WEBHOOK", "https://hooks.example/SECRET-TOKEN")
    d = describe("webhook")
    assert d["configured"] is True
    assert "SECRET-TOKEN" not in repr(d)
    assert d["required_env"] == ["GAFFER_NOTIFY_WEBHOOK"]


def test_describe_reports_a_missing_variable(monkeypatch):
    monkeypatch.delenv("GAFFER_NOTIFY_WEBHOOK", raising=False)
    d = describe("webhook")
    assert d["configured"] is False and d["missing_env"]


# ==========================================================================
# Deduplication
# ==========================================================================

def test_the_same_fact_twice_alerts_once(conn):
    e = Engine(conn, MemorySink())
    first = e.run([alert()], now=NOW)
    second = e.run([alert()], now=NOW + timedelta(hours=1))
    assert first.delivered == 1
    assert second.delivered == 0 and second.duplicates == 1


def test_a_changed_fact_alerts_again(conn):
    e = Engine(conn, MemorySink())
    e.run([alert(parts=("50%",))], now=NOW)
    res = e.run([alert(parts=("25%",))], now=NOW + timedelta(hours=1))
    assert res.delivered == 1, "the doubt got worse; that is news"


def test_the_dedupe_key_ignores_the_clock():
    a = Alert(kind="x", title="t", body="b", dedupe_parts=(1,),
              created_at="2026-08-20T10:00:00+00:00")
    b = Alert(kind="x", title="t", body="b", dedupe_parts=(1,),
              created_at="2026-08-20T23:59:00+00:00")
    assert a.dedupe_key == b.dedupe_key


def test_different_kinds_never_collide():
    a = Alert(kind="deadline", title="t", body="b", dedupe_parts=(1,))
    b = Alert(kind="captain_changed", title="t", body="b", dedupe_parts=(1,))
    assert a.dedupe_key != b.dedupe_key


def test_notification_state_is_season_aware(conn):
    Engine(conn, MemorySink(), season="2025-26").run([alert()], now=NOW)
    res = Engine(conn, MemorySink(), season="2026-27").run([alert()], now=NOW)
    assert res.delivered == 1, "a new season starts a clean slate"


# ==========================================================================
# Quiet hours — Europe/London, across the BST/GMT boundary
# ==========================================================================

@pytest.mark.parametrize("utc_hour,expected", [
    (12, False),   # 13:00 BST — awake
    (22, True),    # 23:00 BST — quiet
    (2, True),     # 03:00 BST — quiet
    (7, False),    # 08:00 BST — awake
])
def test_quiet_hours_in_british_summer_time(utc_hour, expected):
    t = datetime(2026, 8, 20, utc_hour, 0, tzinfo=UTC)
    assert quiet_hours(t) is expected


@pytest.mark.parametrize("utc_hour,expected", [
    (12, False),   # 12:00 GMT — awake
    (23, True),    # 23:00 GMT — quiet
    (6, True),     # 06:00 GMT — quiet
    (8, False),    # 08:00 GMT — awake
])
def test_quiet_hours_in_greenwich_mean_time(utc_hour, expected):
    t = datetime(2026, 12, 20, utc_hour, 0, tzinfo=UTC)
    assert quiet_hours(t) is expected


def test_the_same_utc_instant_differs_across_the_dst_boundary():
    """22:00 UTC is 23:00 in August (quiet) and 22:00 in December (awake)."""
    assert quiet_hours(datetime(2026, 8, 20, 22, 0, tzinfo=UTC)) is True
    assert quiet_hours(datetime(2026, 12, 20, 22, 0, tzinfo=UTC)) is False


def test_a_naive_timestamp_is_treated_as_utc():
    assert quiet_hours(datetime(2026, 8, 20, 2, 0)) is True


def test_quiet_hours_suppress_an_important_alert(conn):
    night = datetime(2026, 8, 20, 23, 0, tzinfo=UTC)
    res = Engine(conn, MemorySink()).run([alert(severity=IMPORTANT)], now=night)
    assert res.suppressed == 1 and res.delivered == 0
    assert res.alerts[0]["state"] == STATE_SUPPRESSED
    assert "quiet hours" in res.alerts[0]["reason"]


def test_a_critical_alert_ignores_quiet_hours(conn):
    night = datetime(2026, 8, 20, 23, 0, tzinfo=UTC)
    res = Engine(conn, MemorySink()).run([alert(severity=CRITICAL)], now=night)
    assert res.delivered == 1 and res.suppressed == 0


def test_quiet_hours_can_be_disabled_explicitly(conn):
    night = datetime(2026, 8, 20, 23, 0, tzinfo=UTC)
    res = Engine(conn, MemorySink(), quiet=False).run([alert()], now=night)
    assert res.delivered == 1


def test_alerts_are_processed_most_severe_first(conn):
    res = Engine(conn, MemorySink()).run(
        [alert(kind="a", severity=INFO, parts=(1,)),
         alert(kind="b", severity=CRITICAL, parts=(2,)),
         alert(kind="c", severity=IMPORTANT, parts=(3,))], now=NOW)
    assert [a["severity"] for a in res.alerts] == [CRITICAL, IMPORTANT, INFO]


# ==========================================================================
# Failure handling
# ==========================================================================

class BrokenSink:
    def send(self, alert):
        raise RuntimeError("provider down")


def test_a_provider_failure_never_propagates(conn, monkeypatch):
    monkeypatch.delenv("GAFFER_NOTIFY_FORCE_DRY_RUN", raising=False)
    res = Engine(conn, BrokenSink(), dry_run=False).run([alert()], now=NOW)
    assert res.failed == 1 and res.delivered == 0
    assert "provider down" in res.errors[0]


def test_a_failure_is_recorded_and_retried(conn, monkeypatch):
    monkeypatch.delenv("GAFFER_NOTIFY_FORCE_DRY_RUN", raising=False)
    e = Engine(conn, BrokenSink(), dry_run=False)
    for i in range(2):
        e.run([alert()], now=NOW + timedelta(hours=i))
    row = conn.execute("SELECT state, attempts FROM notifications").fetchone()
    assert row["state"] == STATE_FAILED and row["attempts"] == 2


def test_retries_stop_after_the_cap(conn, monkeypatch):
    monkeypatch.delenv("GAFFER_NOTIFY_FORCE_DRY_RUN", raising=False)
    e = Engine(conn, BrokenSink(), dry_run=False)
    for i in range(5):
        res = e.run([alert()], now=NOW + timedelta(hours=i))
    assert "giving up" in res.errors[0]


def test_a_recovered_provider_delivers(conn, monkeypatch):
    monkeypatch.delenv("GAFFER_NOTIFY_FORCE_DRY_RUN", raising=False)
    Engine(conn, BrokenSink(), dry_run=False).run([alert()], now=NOW)
    res = Engine(conn, MemorySink(), dry_run=False).run(
        [alert()], now=NOW + timedelta(hours=1))
    assert res.delivered == 1
    assert conn.execute(
        "SELECT state FROM notifications").fetchone()["state"] == STATE_SENT


# ==========================================================================
# Rules
# ==========================================================================

def test_deadline_reminders_fire_per_bucket_not_per_run():
    a24 = rules.deadline_alerts(DL_ISO, 1, DEADLINE - timedelta(hours=20))
    b24 = rules.deadline_alerts(DL_ISO, 1, DEADLINE - timedelta(hours=19))
    assert a24 and a24[0].dedupe_key == b24[0].dedupe_key


def test_a_closer_deadline_is_a_different_and_more_urgent_alert():
    far = rules.deadline_alerts(DL_ISO, 1, DEADLINE - timedelta(hours=20))[0]
    near = rules.deadline_alerts(DL_ISO, 1, DEADLINE - timedelta(hours=2))[0]
    assert far.dedupe_key != near.dedupe_key
    assert near.severity == CRITICAL and far.severity == IMPORTANT


def test_no_deadline_reminder_once_it_has_passed():
    assert rules.deadline_alerts(DL_ISO, 1, DEADLINE + timedelta(minutes=1)) == []


def test_no_deadline_reminder_a_week_out():
    assert rules.deadline_alerts(DL_ISO, 1, DEADLINE - timedelta(days=7)) == []


def test_a_missing_deadline_produces_no_alert():
    assert rules.deadline_alerts(None, 1, NOW) == []
    assert rules.deadline_alerts("nonsense", 1, NOW) == []


def test_an_injured_owned_player_is_critical():
    a = rules.availability_alerts(
        [{"id": 1, "name": "Saka", "status": "i", "news": "Hamstring"}])
    assert len(a) == 1 and a[0].severity == CRITICAL
    assert "Saka" in a[0].title and "Hamstring" in a[0].body


def test_an_available_player_produces_nothing():
    assert rules.availability_alerts([{"id": 1, "name": "X", "status": "a"}]) == []


def test_a_worsening_doubt_is_a_new_alert():
    a = rules.availability_alerts([{"id": 1, "name": "X", "status": "d",
                                    "chance_playing": 75}])[0]
    b = rules.availability_alerts([{"id": 1, "name": "X", "status": "d",
                                    "chance_playing": 25}])[0]
    assert a.dedupe_key != b.dedupe_key


def test_the_recommendation_alert_fires_on_the_action_not_the_decimals():
    same = rules.recommendation_alerts(
        {"action": "roll", "captain": 1, "transfers_in": []},
        {"action": "roll", "captain": 1, "transfers_in": []})
    assert same == []
    changed = rules.recommendation_alerts(
        {"action": "transfer", "captain": 1, "transfers_in": [9],
         "headline": "A → B"},
        {"action": "roll", "captain": 1, "transfers_in": []})
    assert len(changed) == 1 and changed[0].kind == rules.KIND_RECOMMENDATION


def test_a_changed_captain_is_its_own_alert():
    out = rules.recommendation_alerts(
        {"action": "roll", "captain": 2, "captain_name": "Palmer",
         "transfers_in": []},
        {"action": "roll", "captain": 1, "captain_name": "Haaland",
         "transfers_in": []})
    assert [a.kind for a in out] == [rules.KIND_CAPTAIN]
    assert "Haaland" in out[0].body and "Palmer" in out[0].body


def test_no_previous_decision_means_no_change_alert():
    assert rules.recommendation_alerts({"action": "roll"}, None) == []


def test_a_broken_squad_state_alerts():
    a = rules.squad_state_alerts(
        {"squad_status": "fetch_failed", "squad_status_reason": "HTTP 503"})
    assert len(a) == 1 and "503" in a[0].body


def test_a_legitimate_preseason_squad_state_does_not_alert():
    assert rules.squad_state_alerts({"squad_status": "no_public_squad_yet"}) == []


def test_stale_data_alerts_once_per_day():
    old = (NOW - timedelta(days=2)).isoformat()
    a = rules.stale_alerts(old, NOW)
    assert len(a) == 1 and "stale" in a[0].title
    b = rules.stale_alerts((NOW - timedelta(days=2, hours=1)).isoformat(), NOW)
    assert a[0].dedupe_key == b[0].dedupe_key, "same day bucket"


def test_fresh_data_does_not_alert():
    assert rules.stale_alerts((NOW - timedelta(hours=2)).isoformat(), NOW) == []


def test_a_missing_timestamp_is_itself_an_alert():
    assert rules.stale_alerts(None, NOW)[0].kind == rules.KIND_STALE


def test_a_big_league_swing_alerts_and_a_small_one_does_not():
    big = rules.league_swing_alerts({"player_id": 1, "name": "Haaland",
                                     "swing": 18.0}, 1)
    small = rules.league_swing_alerts({"player_id": 1, "name": "X",
                                       "swing": 3.0}, 1)
    assert len(big) == 1 and small == []
    assert "winning you the week" in big[0].title


def test_a_negative_swing_is_worded_as_a_loss():
    a = rules.league_swing_alerts({"player_id": 1, "name": "X", "swing": -20.0}, 1)
    assert "hurting you" in a[0].title


def test_a_closing_chip_window_alerts():
    chips = {"used": [], "available": [{"name": "bboost", "stop_event": 19}]}
    assert rules.chip_window_alerts(chips, 17)[0].kind == rules.KIND_CHIP_WINDOW
    assert rules.chip_window_alerts(chips, 10) == []


def test_a_used_chip_does_not_warn():
    chips = {"used": ["bboost"], "available": [{"name": "bboost",
                                                "stop_event": 19}]}
    assert rules.chip_window_alerts(chips, 18) == []


def test_there_is_no_price_change_rule():
    """Transfer volume is not a validated price source, so no rule exists."""
    assert not any("price" in k for k in rules.ALL_KINDS)
    import inspect
    src = inspect.getsource(rules)
    assert "price" in src, "the absence must be documented, not merely omitted"
    assert "def price_alerts" not in src


def test_every_alert_carries_a_deep_link_into_the_app():
    alerts = rules.build_alerts(
        meta={"current_gw": 1, "deadline": DL_ISO,
              "generated_at": NOW.isoformat(), "squad_status": "fetch_failed"},
        now=DEADLINE - timedelta(hours=2),
        owned=[{"id": 1, "name": "X", "status": "i", "news": "out"}])
    assert alerts
    for a in alerts:
        assert a.deep_link.startswith("#/")


def test_build_alerts_stamps_every_alert_with_one_time():
    alerts = rules.build_alerts(
        meta={"current_gw": 1, "deadline": DL_ISO,
              "generated_at": NOW.isoformat()},
        now=DEADLINE - timedelta(hours=2))
    assert alerts and len({a.created_at for a in alerts}) == 1


def test_every_kind_produced_is_declared():
    alerts = rules.build_alerts(
        meta={"current_gw": 17, "deadline": DL_ISO,
              "generated_at": (NOW - timedelta(days=3)).isoformat(),
              "squad_status": "not_found"},
        now=DEADLINE - timedelta(hours=2),
        owned=[{"id": 1, "name": "X", "status": "i"}],
        current_decision={"action": "transfer", "captain": 2, "transfers_in": [3]},
        previous_decision={"action": "roll", "captain": 1, "transfers_in": []},
        chips={"used": [], "available": [{"name": "3xc", "stop_event": 19}]},
        swing={"player_id": 9, "name": "H", "swing": 22.0})
    kinds = {a.kind for a in alerts}
    assert kinds <= rules.ALL_KINDS
    assert len(kinds) >= 6, "the realistic full set is exercised"


# ==========================================================================
# Summary
# ==========================================================================

def test_the_summary_is_publishable_and_credential_free(conn, monkeypatch):
    monkeypatch.setenv("GAFFER_NOTIFY_WEBHOOK", "https://x/SECRET")
    e = Engine(conn, MemorySink())
    e.run([alert()], now=NOW)
    s = e.summary()
    assert s["dry_run"] is True and s["sink"] == "MemorySink"
    assert s["quiet_hours"]["timezone"] == "Europe/London"
    assert s["by_state"][STATE_DRY_RUN] == 1
    assert "SECRET" not in repr(s)


def test_the_env_guard_variable_is_respected_when_absent(conn, monkeypatch):
    monkeypatch.delenv("GAFFER_NOTIFY_FORCE_DRY_RUN", raising=False)
    assert Engine(conn, MemorySink(), dry_run=False).dry_run is False
    assert os.environ.get("GAFFER_NOTIFY_FORCE_DRY_RUN") is None


def test_the_tightest_deadline_bucket_wins_not_the_widest():
    """With 2h left, both the 3h and 24h windows contain 'now'.

    Reporting 'tomorrow' 90 minutes before a deadline — and then suppressing the
    urgent reminder because the bucket already fired — is the exact failure this
    ordering prevents.
    """
    for hours, expect_severity, expect_phrase in (
        (0.5, CRITICAL, "in under an hour"),
        (2.0, CRITICAL, "in under 3 hours"),
        (20.0, IMPORTANT, "tomorrow"),
    ):
        a = rules.deadline_alerts(
            DL_ISO, 1, DEADLINE - timedelta(hours=hours))[0]
        assert a.severity == expect_severity, hours
        assert expect_phrase in a.title, hours
