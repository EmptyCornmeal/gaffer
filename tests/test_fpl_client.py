"""FplClient cache-path handling.

The live smoke test found that `leagues-classic/{id}/standings/?page_standings=1`
produced the cache filename `leagues-classic_271619_standings_?page_standings=1`,
which is illegal on Windows: every league fetch died with `OSError: [Errno 22]`
before a single byte was written. The league layer's containment turned that into
"0 leagues found" rather than a crash, which is exactly how it went unnoticed —
so the invariant is asserted here rather than left to a running FPL season.
"""

from __future__ import annotations

import pytest

from gaffer.fpl.client import FplClient

# Characters Windows rejects outright in a filename.
WINDOWS_ILLEGAL = set('<>:"/\\|?*')


@pytest.fixture
def client(tmp_path):
    c = FplClient(cache_dir=tmp_path)
    yield c
    c.close()


ENDPOINTS = [
    "bootstrap-static/",
    "fixtures/",
    "element-summary/123/",
    "event/7/live/",
    "entry/1066421/",
    "entry/1066421/event/7/picks/",
    "entry/1066421/history/",
    "entry/1066421/transfers/",
    "leagues-classic/271619/standings/?page_standings=1",
    "leagues-classic/314/standings/?page_standings=17",
]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_no_cache_filename_contains_a_character_windows_rejects(client, path):
    name = client._cache_file(path).name
    assert not (WINDOWS_ILLEGAL & set(name)), f"{path} -> {name}"


@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_cache_path_is_actually_writable(client, path):
    """The real assertion: the OS accepts it."""
    p = client._cache_file(path)
    p.write_text("{}", encoding="utf-8")
    assert p.read_text(encoding="utf-8") == "{}"


def test_league_pages_do_not_collide(client):
    a = client._cache_file("leagues-classic/271619/standings/?page_standings=1")
    b = client._cache_file("leagues-classic/271619/standings/?page_standings=2")
    assert a != b


def test_different_leagues_do_not_collide(client):
    a = client._cache_file("leagues-classic/1/standings/?page_standings=1")
    b = client._cache_file("leagues-classic/2/standings/?page_standings=1")
    assert a != b


def test_existing_endpoint_filenames_are_unchanged(client):
    """The fix must not silently invalidate the whole cache."""
    assert client._cache_file("bootstrap-static/").name == "bootstrap-static.json"
    assert client._cache_file("fixtures/").name == "fixtures.json"
    assert client._cache_file("element-summary/123/").name == "element-summary_123.json"
    assert (client._cache_file("entry/1/event/7/picks/").name
            == "entry_1_event_7_picks.json")


def test_an_empty_path_still_produces_a_filename(client):
    assert client._cache_file("/").name == "root.json"
