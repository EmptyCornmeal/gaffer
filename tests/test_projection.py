"""Projection-model behaviour tests."""

from gaffer.model import projection


def test_project_writes_rows(conn):
    n = projection.project(conn, from_gw=1, horizon=1)
    assert n == conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]


def test_unavailable_player_scores_near_zero(conn):
    # mark one player injured
    conn.execute("UPDATE players SET status='i' WHERE id=7")
    conn.commit()
    projection.project(conn, from_gw=1, horizon=1)
    row = conn.execute(
        "SELECT exp_points, p_start FROM projections WHERE player_id=7 AND gw=1"
    ).fetchone()
    assert row["p_start"] == 0.0
    assert row["exp_points"] < 0.2


def test_defcon_contributes_for_ballwinner(conn):
    projection.project(conn, from_gw=1, horizon=1)
    # DEF players in the fixture have defcon_per_90=11 (>10 threshold) -> a DEFCON term
    row = conn.execute(
        "SELECT exp_defcon_pts FROM projections pr JOIN players pl ON pl.id=pr.player_id "
        "WHERE pl.position='DEF' AND pl.defcon_per_90>10 AND pr.gw=1 LIMIT 1"
    ).fetchone()
    assert row["exp_defcon_pts"] > 0.5


def test_clean_sheet_only_for_defensive_positions(conn):
    projection.project(conn, from_gw=1, horizon=1)
    fwd = conn.execute(
        "SELECT exp_cs_pts FROM projections pr JOIN players pl ON pl.id=pr.player_id "
        "WHERE pl.position='FWD' AND pr.gw=1 LIMIT 1"
    ).fetchone()
    assert fwd["exp_cs_pts"] == 0.0
