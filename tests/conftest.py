"""Shared pytest fixtures: an in-memory-ish DB seeded with a minimal, legal
dataset (enough clubs and players to form a valid 15-man squad)."""

from __future__ import annotations

import pytest

from gaffer.store import db


def _players():
    """Build a small pool: 6 clubs, enough per position for the squad rules."""
    pool = []
    pid = 1
    # (position, count, base_price, base_xgi, base_defcon)
    plan = [
        ("GKP", 6, 45, 0.0, 0.0),
        ("DEF", 12, 50, 0.10, 11.0),
        ("MID", 12, 60, 0.35, 8.0),
        ("FWD", 8, 70, 0.55, 0.0),
    ]
    for pos, n, price, xgi, dc in plan:
        for i in range(n):
            pool.append(
                {
                    "id": pid,
                    "web_name": f"{pos}{i}",
                    "first_name": "T",
                    "second_name": f"{pos}{i}",
                    "team_id": (pid % 6) + 1,  # spread across 6 clubs
                    "position": pos,
                    "price": price + (i % 3) * 5,
                    "status": "a",
                    "chance_playing": None,
                    "selected_by_pct": 5.0,
                    "minutes": 2500,
                    "starts": 30,
                    "form": 4.0,
                    "points_per_game": 4.0,
                    "ep_next": 4.0,
                    "xg_per_90": xgi * 0.6,
                    "xa_per_90": xgi * 0.4,
                    "xgi_per_90": xgi,
                    "xgc_per_90": 1.2,
                    "defcon_per_90": dc,
                    "news": "",
                    "set_piece_notes": "",
                }
            )
            pid += 1
    return pool


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    # 6 teams
    db.upsert(
        c, "teams",
        [{"id": t, "name": f"Club{t}", "short": f"C{t}",
          "strength_att_home": 1100, "strength_att_away": 1050,
          "strength_def_home": 1100, "strength_def_away": 1050,
          "strength_overall": 1075} for t in range(1, 7)],
        ["id"],
    )
    db.upsert(c, "players", _players(), ["id"])
    # one fixture per team for GW1 (round-robin-ish pairings)
    def fx(fid, h, a):
        return {"id": fid, "gw": 1, "team_h": h, "team_a": a, "kickoff": None,
                "fdr_h": 3, "fdr_a": 3, "finished": 0}

    db.upsert(c, "fixtures", [fx(1, 1, 2), fx(2, 3, 4), fx(3, 5, 6)], ["id"])
    db.set_meta(c, "current_gw", 1)
    yield c
    c.close()
