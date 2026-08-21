"""T-22b — the prediction ledger settles itself.

`freeze` always ran and `score` never did: it existed only as a manual CLI, so
gw01.json held five predictions and "scored": null, permanently. These pin the
loop closed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from gaffer import ledger, pipeline

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _slate(gw: int = 1) -> dict:
    return {
        "ledger_version": 1,
        "season": "2026-27",
        "gameweek": gw,
        "deadline": "2026-08-21T17:30:00Z",
        "frozen_at": "2026-08-21T17:00:00+00:00",
        "model_version": "test",
        "scored": None,
        "entries": [
            {"method": "model", "label": "Model", "xi": [1, 2], "captain": 1,
             "vice": 2, "bench": [], "squad": [1, 2], "squad_value": 100.0,
             "objective": "x", "names": {}, "projected_xi_points": 9.0},
            {"method": "template", "label": "Template", "xi": [2, 3],
             "captain": 2, "vice": 3, "bench": [], "squad": [2, 3],
             "squad_value": 100.0, "objective": "x", "names": {},
             "projected_xi_points": 4.0},
        ],
    }


class FakeClient:
    """Only what the scoring helper touches."""

    def __init__(self, finished: list[int], elements: list[dict] | None = None):
        self._finished = finished
        self._elements = elements if elements is not None else [
            {"id": 1, "stats": {"total_points": 12, "minutes": 90}},
            {"id": 2, "stats": {"total_points": 2, "minutes": 90}},
            {"id": 3, "stats": {"total_points": 0, "minutes": 0}},
        ]
        self.live_calls: list[int] = []

    def events(self):
        return [{"id": gw, "finished": gw in self._finished,
                 "data_checked": gw in self._finished} for gw in (1, 2, 3)]

    def event_live(self, gw: int):
        self.live_calls.append(gw)
        return {"elements": self._elements}


def _write(tmp_path, gw=1):
    p = tmp_path / f"gw{gw:02d}.json"
    p.write_text(json.dumps(_slate(gw)), encoding="utf-8")
    return p


def test_a_finished_gameweek_gets_scored(tmp_path, monkeypatch):
    p = _write(tmp_path)
    monkeypatch.setattr(ledger, "ledger_path", lambda gw, d=None: tmp_path / f"gw{gw:02d}.json")
    out = pipeline._score_finished_ledgers(FakeClient([1]), NOW)

    assert "gw01" in out
    scored = json.loads(p.read_text(encoding="utf-8"))["scored"]
    assert scored is not None
    by = {r["method"]: r for r in scored["results"]}
    # xi [1,2] = 12 + 2, captain 1 counted again = 26, against a 9.0 forecast.
    assert by["model"]["actual_xi_points"] == 26
    assert by["model"]["error"] == 17.0
    # A prediction is never edited by scoring it.
    assert by["model"]["projected_xi_points"] == 9.0


def test_an_unfinished_gameweek_is_left_alone(tmp_path, monkeypatch):
    """Scoring mid-match would freeze provisional bonus as though it were final."""
    p = _write(tmp_path)
    monkeypatch.setattr(ledger, "ledger_path", lambda gw, d=None: tmp_path / f"gw{gw:02d}.json")
    client = FakeClient([])
    out = pipeline._score_finished_ledgers(client, NOW)

    assert "no finished gameweek" in out
    assert client.live_calls == []
    assert json.loads(p.read_text(encoding="utf-8"))["scored"] is None


def test_scoring_twice_does_not_rewrite_a_result(tmp_path, monkeypatch):
    p = _write(tmp_path)
    monkeypatch.setattr(ledger, "ledger_path", lambda gw, d=None: tmp_path / f"gw{gw:02d}.json")
    pipeline._score_finished_ledgers(FakeClient([1]), NOW)
    first = p.read_text(encoding="utf-8")

    client = FakeClient([1], elements=[{"id": 1, "stats": {"total_points": 999, "minutes": 90}}])
    out = pipeline._score_finished_ledgers(client, NOW)

    assert "nothing new" in out
    assert client.live_calls == [], "a scored slate must not be re-fetched"
    assert p.read_text(encoding="utf-8") == first


def test_an_empty_live_payload_scores_nothing_rather_than_zeroes(tmp_path, monkeypatch):
    """A silent zero would read as 'every candidate blanked', which is a lie."""
    p = _write(tmp_path)
    monkeypatch.setattr(ledger, "ledger_path", lambda gw, d=None: tmp_path / f"gw{gw:02d}.json")
    out = pipeline._score_finished_ledgers(FakeClient([1], elements=[]), NOW)

    assert json.loads(p.read_text(encoding="utf-8"))["scored"] is None
    assert "nothing new" in out
