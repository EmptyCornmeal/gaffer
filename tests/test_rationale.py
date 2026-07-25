"""Tests for the plain-English rationale generator."""

from gaffer.model import rationale


def _base(**over):
    p = {
        "position": "DEF", "price": 6.0, "p_start": 0.95, "exp_minutes": 82,
        "xgi90": 0.1, "defcon90": 11.5, "form": 4.0, "owned_by": 12.0,
        "set_pieces": "", "news": "", "status": "a", "cs_pts": 1.3,
        "goal_pts": 0.2, "assist_pts": 0.2, "defcon_pts": 1.1, "xp_next": 4.8,
        "fixtures": [{"difficulty": 2}, {"difficulty": 2}],
    }
    p.update(over)
    return p


def test_nailed_defcon_defender():
    p = _base()
    why = rationale.player_rationale(p)
    assert why.startswith("Nailed defender")
    assert "clean-sheet" in why and "defensive volume" in why
    labels = {t["label"] for t in rationale.player_tags(p)}
    assert "nailed" in labels and "elite DEFCON" in labels and "soft fixtures" in labels


def test_badge_uses_start_probability_not_capped_minutes():
    # a 90%-start premium (minutes cap ~82') must still read NAILED
    b = rationale.xmins_badge(76, p_start=0.9)
    assert b["label"] == "NAILED"
    assert rationale.xmins_badge(30, p_start=0.4)["label"] == "CAMEO?"


def test_injury_flag_surfaces():
    p = _base(status="i", news="Knee injury - 50% chance")
    labels = {t["label"] for t in rationale.player_tags(p)}
    assert "fitness doubt" in labels
    assert "note:" in rationale.player_rationale(p)


def test_no_ungrammatical_defcon_clause_for_mid():
    p = _base(position="MID", cs_pts=0.1, xgi90=0.1, defcon90=13.0)
    why = rationale.player_rationale(p)
    assert "with hits the" not in why
    assert "reliable DEFCON points" in why
