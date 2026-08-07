"""T-05 — squad-state ingestion: atomicity, failure modes, no stale masquerade.

The audited code returned before its own DELETE on a 404, leaving a previously
loaded squad in place while metadata said the squad was unavailable.
"""

from __future__ import annotations

import httpx
import pytest

from gaffer import gameweek as G
from gaffer import ingest
from gaffer.store import db

ENTRY = 1066421


def picks_payload(elements, captain=None, bank=5, value=1000, chip=None):
    return {
        "picks": [
            {"element": e, "position": i + 1,
             "is_captain": e == captain, "is_vice_captain": False, "multiplier": 1}
            for i, e in enumerate(elements)
        ],
        "entry_history": {"bank": bank, "value": value},
        "active_chip": chip,
    }


class Client:
    """Scripted picks client: a payload, or an exception, per call."""

    def __init__(self, *responses, transfers=None, chips=None):
        self.responses = list(responses)
        self.calls = []
        self._transfers = transfers if transfers is not None else []
        self._chips = chips or []

    def entry_picks(self, entry_id, gw):
        self.calls.append((entry_id, gw))
        r = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    def entry_transfers(self, entry_id):
        if isinstance(self._transfers, Exception):
            raise self._transfers
        return self._transfers

    def entry_history(self, entry_id):
        return {"chips": self._chips, "current": []}


def http_error(code):
    req = httpx.Request("GET", "https://example.test")
    return httpx.HTTPStatusError(
        f"{code}", request=req, response=httpx.Response(code, request=req)
    )


def _norm(v):
    """Mirror artifacts.build_meta: the '' sentinel exports as null."""
    return None if v in (None, "", "None") else v


def state(conn):
    return {
        "status": _norm(db.get_meta(conn, "squad_status")),
        "reason": _norm(db.get_meta(conn, "squad_status_reason")),
        "source": _norm(db.get_meta(conn, "squad_source_event")),
    }


def squad_rows(conn):
    return [(r["gw"], r["player_id"])
            for r in conn.execute("SELECT gw, player_id FROM my_squad ORDER BY gw, player_id")]


# --------------------------------------------------------------------------

def test_preseason_stores_nothing_and_never_calls_the_api(conn):
    c = Client()
    n = ingest.ingest_my_squad(conn, c, ENTRY, None, projection_gw=1)
    assert n == 0
    assert c.calls == [], "must not request picks when none are readable"
    assert squad_rows(conn) == []
    s = state(conn)
    assert s["status"] == G.STATUS_NO_PUBLIC_SQUAD_YET
    assert s["source"] is None
    assert "deadline" in s["reason"]


def test_successful_load_stores_the_squad_and_its_provenance(conn):
    c = Client(picks_payload([1, 2, 3], captain=1))
    n = ingest.ingest_my_squad(conn, c, ENTRY, 1, projection_gw=2)
    assert n == 3
    assert c.calls == [(ENTRY, 1)], "must request the READABLE event, not the projected one"
    assert squad_rows(conn) == [(1, 1), (1, 2), (1, 3)]
    s = state(conn)
    assert s["status"] == G.STATUS_LOADED
    assert s["source"] == "1"
    assert db.get_meta(conn, "squad_retrieved_at")
    assert db.get_meta(conn, "bank") == "5"


def test_replacement_is_total_not_additive(conn):
    ingest.ingest_my_squad(conn, Client(picks_payload([1, 2, 3])), ENTRY, 1)
    ingest.ingest_my_squad(conn, Client(picks_payload([4, 5])), ENTRY, 2)
    # Exactly one squad is ever stored — GW1's rows must not linger.
    assert squad_rows(conn) == [(2, 4), (2, 5)]
    assert state(conn)["source"] == "2"


def test_repeated_ingestion_is_idempotent(conn):
    c = Client(picks_payload([1, 2, 3]))
    for _ in range(3):
        ingest.ingest_my_squad(conn, Client(picks_payload([1, 2, 3])), ENTRY, 1)
    assert squad_rows(conn) == [(1, 1), (1, 2), (1, 3)]
    assert state(conn)["status"] == G.STATUS_LOADED
    assert c is not None


@pytest.mark.parametrize("code,expected", [
    (404, G.STATUS_NOT_FOUND),
    (500, G.STATUS_FETCH_FAILED),
    (503, G.STATUS_FETCH_FAILED),
])
def test_failure_modes_are_distinct_with_no_prior_squad(conn, code, expected):
    n = ingest.ingest_my_squad(conn, Client(http_error(code)), ENTRY, 1)
    assert n == 0
    assert squad_rows(conn) == []
    s = state(conn)
    assert s["status"] == expected
    assert s["source"] is None, "no squad stored, so no source event may be claimed"


def test_404_on_a_readable_event_is_not_disguised_as_preseason(conn):
    """A deadline has passed, so a 404 is a real defect, not 'no squad yet'."""
    ingest.ingest_my_squad(conn, Client(http_error(404)), ENTRY, 3)
    s = state(conn)
    assert s["status"] == G.STATUS_NOT_FOUND
    assert s["status"] != G.STATUS_NO_PUBLIC_SQUAD_YET
    assert "404" in s["reason"]


def test_transient_failure_retains_the_prior_squad_but_labels_it_stale(conn):
    ingest.ingest_my_squad(conn, Client(picks_payload([1, 2, 3])), ENTRY, 1)
    ingest.ingest_my_squad(conn, Client(http_error(503)), ENTRY, 2)
    # The holdings are still genuinely useful, but nothing may claim they are current.
    assert squad_rows(conn) == [(1, 1), (1, 2), (1, 3)]
    s = state(conn)
    assert s["status"] == G.STATUS_STALE
    assert s["source"] == "1", "stale squad must attribute the event it came from"
    assert "503" in s["reason"] and "GW1" in s["reason"]


def test_stale_status_never_claims_the_requested_event(conn):
    """The regression: rows from GW1 must not be reported as GW2's squad."""
    ingest.ingest_my_squad(conn, Client(picks_payload([1, 2, 3])), ENTRY, 1)
    ingest.ingest_my_squad(conn, Client(http_error(500)), ENTRY, 2)
    assert state(conn)["source"] != "2"


@pytest.mark.parametrize("payload", [
    {}, {"picks": []}, {"picks": None}, {"picks": [{"no_element": 1}]},
    {"picks": [{"element": "not-an-int"}]}, [], "nonsense",
])
def test_malformed_payloads_are_rejected(conn, payload):
    ingest.ingest_my_squad(conn, Client(payload), ENTRY, 1)
    assert squad_rows(conn) == []
    assert state(conn)["status"] == G.STATUS_MALFORMED


def test_malformed_payload_does_not_destroy_a_good_squad(conn):
    ingest.ingest_my_squad(conn, Client(picks_payload([1, 2, 3])), ENTRY, 1)
    ingest.ingest_my_squad(conn, Client({"picks": []}), ENTRY, 2)
    assert squad_rows(conn) == [(1, 1), (1, 2), (1, 3)]
    assert state(conn)["status"] == G.STATUS_STALE


def test_transport_error_is_a_fetch_failure_not_a_crash(conn):
    err = httpx.ConnectTimeout("timed out", request=httpx.Request("GET", "https://x.test"))
    ingest.ingest_my_squad(conn, Client(err), ENTRY, 1)
    assert state(conn)["status"] == G.STATUS_FETCH_FAILED


def test_replacement_is_atomic_on_insert_failure(conn):
    """A mid-write failure must roll back, never leave a mixture.

    Duplicate elements violate my_squad's (gw, player_id) primary key partway
    through the executemany, which is a genuine integrity failure rather than a
    mocked one.
    """
    import sqlite3

    ingest.ingest_my_squad(conn, Client(picks_payload([1, 2, 3])), ENTRY, 1)
    before = squad_rows(conn)
    assert before, "precondition: a squad is stored"

    bad = picks_payload([9, 8, 9])  # 9 twice -> PK collision on the third insert
    with pytest.raises(sqlite3.IntegrityError):
        ingest.ingest_my_squad(conn, Client(bad), ENTRY, 2)

    # The DELETE and the partial INSERTs both rolled back: the old squad is
    # intact, and no GW2 row leaked through.
    assert squad_rows(conn) == before
    assert not [r for r in squad_rows(conn) if r[0] == 2]


def test_no_entry_id_path_clears_and_labels(conn):
    ingest.ingest_my_squad(conn, Client(picks_payload([1, 2, 3])), ENTRY, 1)
    ingest._clear_squad(conn)
    ingest._record_squad_state(conn, G.STATUS_NO_ENTRY_ID, "no entry id configured", None)
    assert squad_rows(conn) == []
    assert state(conn)["status"] == G.STATUS_NO_ENTRY_ID
    assert state(conn)["source"] is None
