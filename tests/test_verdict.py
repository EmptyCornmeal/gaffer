"""T-06 — the AI verdict must be grounded in the squad it captions.

The shipped verdict praised Enzo, Gabriel, Watkins, Mbeumo and Hume as part of
the team; none were in the 15. Root cause: build_context() passed only
``players[:8]`` and ``differentials``, never the selected squad.
"""

from __future__ import annotations

import json

import pytest

from gaffer.ai import verdict as V

XI = ["Calafiori", "Rogers", "Verbruggen", "João Pedro", "Lacroix", "Virgil",
      "Szoboszlai", "Guéhi", "Haaland", "Shaw", "B.Fernandes"]
BENCH = ["Lecomte", "Furo", "Oriola", "Howell"]
# In the catalogue, NOT in the squad — the five the live verdict hallucinated.
OUTSIDE = ["Enzo", "Gabriel", "Watkins", "Mbeumo", "Hume"]


def _card(i, name):
    return {"id": i, "name": name, "pos": "MID", "team": "C1", "price": 6.0,
            "next_gw_xp": 4.0, "rationale": "because", "xmins_badge": "NAILED"}


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    names = XI + BENCH + OUTSIDE
    players = [_card(i + 1, n) for i, n in enumerate(names)]
    (d / "players.json").write_text(json.dumps(players), encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps({"gw_name": "Gameweek 1", "current_gw": "1",
                    "deadline": "2026-08-21T17:30:00Z"}), encoding="utf-8")
    (d / "recommendation.json").write_text(json.dumps({
        "mode": "build", "formation": "5-3-2", "squad_value": 100.0,
        "xi_expected": 60.93, "hits": 0, "summary": "Optimal 5-3-2.",
        "captain": _card(9, "Haaland"),
        "vice": _card(11, "B.Fernandes"),
        "starting": [_card(i + 1, n) for i, n in enumerate(XI)],
        "bench": [_card(len(XI) + i + 1, n) for i, n in enumerate(BENCH)],
        "transfers_in": [], "transfers_out": [],
    }), encoding="utf-8")
    return d


# --------------------------------------------------------------------------
# The context handed to the model
# --------------------------------------------------------------------------

def test_context_contains_the_full_squad(data_dir):
    ctx = V.build_context(data_dir)
    sq = ctx["selected_squad"]
    assert [p["name"] for p in sq["starting_xi"]] == XI
    assert [p["name"] for p in sq["bench"]] == BENCH
    assert len(sq["starting_xi"]) == 11
    assert len(sq["bench"]) == 4


def test_context_carries_ids_for_exact_validation(data_dir):
    ctx = V.build_context(data_dir)
    sq = ctx["selected_squad"]
    assert all(isinstance(p["id"], int) for p in sq["starting_xi"] + sq["bench"])


def test_context_represents_captain_and_vice(data_dir):
    sq = V.build_context(data_dir)["selected_squad"]
    assert sq["captain"]["name"] == "Haaland"
    assert sq["vice_captain"]["name"] == "B.Fernandes"
    assert sq["formation"] == "5-3-2"


def test_context_marks_league_wide_lists_as_not_the_squad(data_dir):
    ctx = V.build_context(data_dir)
    assert "NOT in the squad" in ctx["note_on_context"]


def test_system_prompt_states_the_grounding_rule():
    assert "selected_squad" in V.SYSTEM
    assert "ONLY" in V.SYSTEM


def test_squad_names_covers_xi_bench_and_armband(data_dir):
    ctx = V.build_context(data_dir)
    names = V.squad_names(ctx)
    assert set(XI) | set(BENCH) <= names
    assert not (set(OUTSIDE) & names)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def test_grounded_briefing_passes(data_dir):
    ctx = V.build_context(data_dir)
    good = (
        "**Captain Haaland and trust the spine.**\n"
        "- Virgil (6.33) anchors the defence with Calafiori and Shaw.\n"
        "- B.Fernandes is the engine; João Pedro leads the line.\n"
        "**Bottom line: back Haaland.**"
    )
    assert V.find_unselected_mentions(good, V.squad_names(ctx),
                                      V.catalogue_names(data_dir)) == []


def test_the_real_hallucination_is_rejected(data_dir):
    """Verbatim from the shipped verdict.json."""
    ctx = V.build_context(data_dir)
    bad = (
        "B.Fernandes is the standout engine, backed by Enzo (6.49) and a "
        "rock-solid defence in Virgil (6.33) and Gabriel (5.83). "
        "Watkins (5.3) and João Pedro (5.25) give you real forward depth."
    )
    hits = V.find_unselected_mentions(bad, V.squad_names(ctx),
                                      V.catalogue_names(data_dir))
    assert set(hits) == {"Enzo", "Gabriel", "Watkins"}


def test_explicitly_labelled_alternative_is_allowed(data_dir):
    ctx = V.build_context(data_dir)
    ok = (
        "Haaland keeps the armband. Mbeumo is not in the squad, but he is "
        "worth watching for next week."
    )
    assert V.find_unselected_mentions(ok, V.squad_names(ctx),
                                      V.catalogue_names(data_dir)) == []


def test_alternative_marker_does_not_whitewash_a_separate_claim(data_dir):
    ctx = V.build_context(data_dir)
    mixed = (
        "Hume is not in the squad, but keep an eye on him.\n"
        "Gabriel anchors your back line alongside Virgil."
    )
    hits = V.find_unselected_mentions(mixed, V.squad_names(ctx),
                                      V.catalogue_names(data_dir))
    assert hits == ["Gabriel"]


def test_substring_collisions_do_not_false_positive(data_dir):
    """'Enzo' must not match inside another word; validation is word-bounded."""
    ctx = V.build_context(data_dir)
    text = "The price and the frenzied Enzomania of pre-season mean little."
    assert V.find_unselected_mentions(text, V.squad_names(ctx),
                                      V.catalogue_names(data_dir)) == []


def test_ambiguous_english_words_are_not_flagged(data_dir):
    ctx = V.build_context(data_dir)
    catalogue = V.catalogue_names(data_dir) | {"Long", "Rice"}
    text = "Long-term, the Rice of this squad is its defence."
    assert V.find_unselected_mentions(text, V.squad_names(ctx), catalogue) == []


# --------------------------------------------------------------------------
# generate() end-to-end (no credentials, no spend)
# --------------------------------------------------------------------------

def test_template_path_is_grounded_and_recorded(data_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    out = V.generate(data_dir=data_dir)
    assert out["source"] == "template"
    assert out["validation"]["ok"] is True
    assert out["validation"]["unselected_mentions"] == []
    assert len(out["squad_player_ids"]) == 15
    assert json.loads((data_dir / "verdict.json").read_text(encoding="utf-8"))


def test_ai_output_naming_non_squad_players_is_retried_then_rejected(
    data_dir, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    calls = []

    def fake(ctx, model, correction=None):
        calls.append(correction)
        return "Gabriel and Watkins anchor your XI alongside Virgil."

    monkeypatch.setattr(V, "_ai_briefing", fake)
    out = V.generate(data_dir=data_dir)

    assert len(calls) == 2, "should retry once with a correction"
    assert calls[0] is None
    assert "Gabriel" in calls[1]  # the retry names the offenders
    # Never publish prose that contradicts the squad: falls back to the template.
    # The reason is a stable code beside `source`, not smuggled inside it — the
    # old value was "template (ai named non-squad players: Gabriel)", which the
    # artifact contract rejected while the pipeline kept publishing it.
    assert out["source"] == "template"
    assert out["fallback_reason"] == "grounding_rejected"
    assert out["model"] is None
    assert out["validation"]["ok"] is True
    assert "Gabriel" not in out["briefing_md"]


def test_ai_output_is_accepted_when_grounded(data_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr(
        V, "_ai_briefing",
        lambda ctx, model, correction=None:
            "**Haaland keeps the armband.** Virgil and Shaw hold the back line.",
    )
    out = V.generate(data_dir=data_dir)
    assert out["source"] == "ai"
    assert out["validation"]["ok"] is True


def test_retry_succeeds_on_second_attempt(data_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    seq = iter([
        "Gabriel is superb at the back.",
        "**Virgil is superb at the back.** Haaland captains.",
    ])
    monkeypatch.setattr(
        V, "_ai_briefing", lambda ctx, model, correction=None: next(seq)
    )
    out = V.generate(data_dir=data_dir)
    assert out["source"] == "ai"
    assert out["validation"]["ok"] is True
    assert "Gabriel" not in out["briefing_md"]


def test_api_failure_falls_back_safely(data_dir, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")

    def boom(ctx, model, correction=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(V, "_ai_briefing", boom)
    out = V.generate(data_dir=data_dir)
    # An unrecognised exception class collapses to the bare code: the class name
    # of an arbitrary failure can itself be informative, and this file is public.
    assert out["source"] == "template"
    assert out["fallback_reason"] == "provider_error"
    assert out["model"] is None
    assert "connection reset" not in json.dumps(out)
    assert out["validation"]["ok"] is True
    assert out["briefing_md"]


def test_missing_recommendation_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    d = tmp_path / "empty"
    d.mkdir()
    out = V.generate(data_dir=d)
    assert out["squad_player_ids"] == []
    assert out["briefing_md"]
