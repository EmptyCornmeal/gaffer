"""Atomic-write reliability tests."""

import json

import pytest

from gaffer.io import write_json_atomic


def test_writes_valid_json_and_no_temp_leftover(tmp_path):
    path = tmp_path / "players.json"
    write_json_atomic(path, {"a": 1, "b": [1, 2, 3]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2, 3]}
    # the temp file must have been renamed away, not left behind
    assert list(tmp_path.iterdir()) == [path]


def test_overwrites_atomically(tmp_path):
    path = tmp_path / "meta.json"
    write_json_atomic(path, {"v": 1})
    write_json_atomic(path, {"v": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 2}
    assert list(tmp_path.iterdir()) == [path]


def test_failed_serialisation_leaves_no_partial_file(tmp_path):
    path = tmp_path / "out.json"
    write_json_atomic(path, {"ok": True})
    # a non-serialisable payload must not clobber the good file or leave a temp
    with pytest.raises(TypeError):
        write_json_atomic(path, {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert list(tmp_path.iterdir()) == [path]
