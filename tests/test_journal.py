"""1.12 -- the human half of the decision record.

Gaffer records what it advised and whether it was followed. It does not record
what was done INSTEAD or why, and those are the informative rows: this season
already has two overrides where the human was right for a stated reason.

Captured in the vault gameweek note, which is written every week anyway, and
joined at the MCP layer -- Actions cannot see the vault and Gaffer stays
read-only.
"""
from __future__ import annotations

import textwrap

from gaffer import journal


def _note(**kv) -> str:
    body = "\n".join(f"{k.replace('_', ' ') if False else k}: {v}"
                     for k, v in kv.items())
    return textwrap.dedent("""\
        # Gameweek note

        ```gaffer-decision
        {body}
        ```
        """).replace("{body}", body)


def test_a_block_is_parsed(tmp_path, monkeypatch):
    (tmp_path / "gw3.md").write_text(
        _note(gameweek=3, followed="no",
              i_did="Le Fee -> Sangare, captain Haaland",
              because="Konsa has 0 starts and 11 minutes"),
        encoding="utf-8")
    monkeypatch.setenv("GAFFER_JOURNAL_DIR", str(tmp_path))
    rows = journal.read()
    assert len(rows) == 1
    assert rows[0]["gameweek"] == 3
    assert rows[0]["followed"] is False
    assert "Sangare" in rows[0]["i_did"]
    assert rows[0]["source"] == "gw3.md"


def test_partly_is_not_forced_into_a_boolean(tmp_path, monkeypatch):
    """Most real weeks are partial: the captain taken, the transfer refused.
    Coercing that to True or False would destroy the only interesting signal."""
    (tmp_path / "gw3.md").write_text(_note(gameweek=3, followed="partly"),
                                     encoding="utf-8")
    monkeypatch.setenv("GAFFER_JOURNAL_DIR", str(tmp_path))
    assert journal.read()[0]["followed"] is None


def test_unknown_keys_are_kept_verbatim(tmp_path, monkeypatch):
    """A note that says more than the parser knew about must not be truncated
    to the schema of the day it was written."""
    (tmp_path / "gw3.md").write_text(_note(gameweek=3, mood="furious"),
                                     encoding="utf-8")
    monkeypatch.setenv("GAFFER_JOURNAL_DIR", str(tmp_path))
    assert journal.read()[0]["extra"]["mood"] == "furious"


def test_a_note_without_a_block_contributes_nothing(tmp_path, monkeypatch):
    (tmp_path / "gw1.md").write_text("# GW1\nno block here\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_JOURNAL_DIR", str(tmp_path))
    assert journal.read() == []


def test_a_half_written_line_does_not_hide_the_rest(tmp_path, monkeypatch):
    (tmp_path / "gw3.md").write_text(
        "```gaffer-decision\ngameweek: 3\nthis line has no colon\n"
        "because: still readable\n```\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_JOURNAL_DIR", str(tmp_path))
    row = journal.read()[0]
    assert row["gameweek"] == 3
    assert row["because"] == "still readable"


def test_filtering_by_gameweek(tmp_path, monkeypatch):
    for gw in (2, 3):
        (tmp_path / f"gw{gw}.md").write_text(_note(gameweek=gw),
                                             encoding="utf-8")
    monkeypatch.setenv("GAFFER_JOURNAL_DIR", str(tmp_path))
    assert [r["gameweek"] for r in journal.read()] == [3, 2]
    assert [r["gameweek"] for r in journal.read(2)] == [2]


def test_no_vault_is_a_stated_absence_not_an_error(tmp_path, monkeypatch):
    """Every CI run is this case. It must not fail, and must not read as
    'he wrote nothing'."""
    monkeypatch.setenv("GAFFER_JOURNAL_DIR", str(tmp_path / "nope"))
    st = journal.status()
    assert st["available"] is False
    assert "LOCAL join" in st["reason"]
    assert journal.read() == []


def test_gaffer_gains_no_write_path():
    """The journal is READ. If this module ever learns to write, the read-only
    guarantee has quietly moved, and that must be a deliberate decision rather
    than a commit nobody noticed."""
    import inspect
    src = inspect.getsource(journal)
    for forbidden in ("write_text(", ".unlink(", "mkdir("):
        assert forbidden not in src, (
            f"journal.py must not {forbidden} -- Gaffer reads the vault, "
            "it does not write to it")
