"""Ingest the FPL API into SQLite.

Single free source of truth. The FPL bootstrap already carries Opta expected
stats (xG/xA/xGI and xG-conceded per 90) plus defensive-contribution per 90, so
no scraping is required. Pre-season, the per-90 rate fields mirror last season —
exactly the baseline the GW1 projection needs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

from gaffer import config, gameweek, rules, teamstate
from gaffer import season as season_mod
from gaffer.fpl.client import FplClient
from gaffer.store import db

_POSITION = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _set_piece_notes(e: dict[str, Any]) -> str:
    bits = []
    if (e.get("penalties_order") or 99) <= 2:
        bits.append(f"pens #{e['penalties_order']}")
    if (e.get("direct_freekicks_order") or 99) <= 2:
        bits.append(f"FK #{e['direct_freekicks_order']}")
    if (e.get("corners_and_indirect_freekicks_order") or 99) <= 2:
        bits.append(f"corners #{e['corners_and_indirect_freekicks_order']}")
    return ", ".join(bits)


def ingest_teams(conn: sqlite3.Connection, bootstrap: dict[str, Any]) -> int:
    teams = bootstrap["teams"]
    # Pre-season the FPL API ships the fine-grained attack/defence ratings as all
    # zeros and only populates the coarse 1-5 strength_overall_home/away. Without
    # a fallback every team looks identical (TeamContext turns 0 -> a flat 1000),
    # so fixtures can't move any projection. When the fine-grained set is absent
    # league-wide, use strength_overall_{home,away} as the per-venue rating for
    # both attack and defence — the projection math is ratio-based, so the 1-5
    # scale still tiers fixtures correctly. Once FPL fills the detailed ratings
    # in-season, those take over automatically.
    has_fine = any((t.get("strength_attack_home") or 0) > 0 for t in teams)
    rows = []
    for t in teams:
        oh = t.get("strength_overall_home") or 3
        oa = t.get("strength_overall_away") or 3
        if has_fine:
            att_h = t.get("strength_attack_home")
            att_a = t.get("strength_attack_away")
            def_h = t.get("strength_defence_home")
            def_a = t.get("strength_defence_away")
        else:  # pre-season: coarse overall rating stands in for attack + defence
            att_h = def_h = oh
            att_a = def_a = oa
        rows.append(
            {
                "id": t["id"],
                "code": t.get("code"),
                "name": t["name"],
                "short": t["short_name"],
                "strength_att_home": att_h,
                "strength_att_away": att_a,
                "strength_def_home": def_h,
                "strength_def_away": def_a,
                "strength_overall": (oh + oa) // 2,
            }
        )
    return db.upsert(conn, "teams", rows, ["id"])


def _scoring_rates(e: dict[str, Any]) -> dict[str, float]:
    """Per-90 rates for the T-13 scoring components, from season totals.

    A player with no minutes has no evidence, so every rate is 0.0 — which the
    model reads as "no contribution", not as "definitely never books".
    """
    mins = _f(e.get("minutes"))
    if mins <= 0:
        return {k: 0.0 for k in (
            "saves_per_90", "yellow_per_90", "red_per_90", "og_per_90",
            "pen_save_per_90", "pen_miss_per_90", "bonus_per_90")}
    per90 = 90.0 / mins
    return {
        "saves_per_90": round(_f(e.get("saves")) * per90, 4),
        "yellow_per_90": round(_f(e.get("yellow_cards")) * per90, 4),
        "red_per_90": round(_f(e.get("red_cards")) * per90, 4),
        "og_per_90": round(_f(e.get("own_goals")) * per90, 4),
        "pen_save_per_90": round(_f(e.get("penalties_saved")) * per90, 4),
        "pen_miss_per_90": round(_f(e.get("penalties_missed")) * per90, 4),
        "bonus_per_90": round(_f(e.get("bonus")) * per90, 4),
    }


def ingest_players(conn: sqlite3.Connection, bootstrap: dict[str, Any]) -> int:
    # Preserve enriched DEFCON: pre-season the bootstrap field is 0, so keep any
    # value we computed from history; let a non-zero (in-season) value override.
    existing_defcon = {
        r["id"]: r["defcon_per_90"]
        for r in conn.execute("SELECT id, defcon_per_90 FROM players")
    }
    rows = []
    for e in bootstrap["elements"]:
        if e["element_type"] not in _POSITION:
            continue  # skip non-squad assets (e.g. element_type 5 = managers)
        rows.append(
            {
                "id": e["id"],
                "code": e.get("code"),
                "web_name": e["web_name"],
                "first_name": e.get("first_name"),
                "second_name": e.get("second_name"),
                "team_id": e["team"],
                "position": _POSITION[e["element_type"]],
                "price": e["now_cost"],
                "status": e.get("status"),
                "chance_playing": e.get("chance_of_playing_next_round"),
                "selected_by_pct": _f(e.get("selected_by_percent")),
                "transfers_in_event": e.get("transfers_in_event", 0),
                "transfers_out_event": e.get("transfers_out_event", 0),
                "cost_change_event": e.get("cost_change_event", 0),
                "cost_change_start": e.get("cost_change_start", 0),
                "minutes": e.get("minutes", 0),
                "starts": e.get("starts", 0),
                "form": _f(e.get("form")),
                "points_per_game": _f(e.get("points_per_game")),
                "ep_next": _f(e.get("ep_next")),
                "ict_index": _f(e.get("ict_index")),
                "xg_per_90": _f(e.get("expected_goals_per_90")),
                "xa_per_90": _f(e.get("expected_assists_per_90")),
                "xgi_per_90": _f(e.get("expected_goal_involvements_per_90")),
                "xgc_per_90": _f(e.get("expected_goals_conceded_per_90")),
                "defcon_per_90": (
                    _f(e.get("defensive_contribution_per_90"))
                    or existing_defcon.get(e["id"], 0.0)
                ),
                # T-13 scoring rates. Season totals / minutes; pre-season these
                # mirror last season, exactly like the xG/xA rates above.
                **_scoring_rates(e),
                "news": e.get("news") or "",
                "set_piece_notes": _set_piece_notes(e),
            }
        )
    return db.upsert(conn, "players", rows, ["id"])


def ingest_fixtures(conn: sqlite3.Connection, fixtures: list[dict[str, Any]]) -> int:
    rows = []
    for fx in fixtures:
        rows.append(
            {
                "id": fx["id"],
                "gw": fx.get("event"),
                "team_h": fx["team_h"],
                "team_a": fx["team_a"],
                "kickoff": fx.get("kickoff_time"),
                "fdr_h": fx.get("team_h_difficulty"),
                "fdr_a": fx.get("team_a_difficulty"),
                "finished": 1 if fx.get("finished") else 0,
            }
        )
    return db.upsert(conn, "fixtures", rows, ["id"])


def ingest_game_settings(conn: sqlite3.Connection, bootstrap: dict[str, Any]) -> None:
    """Persist the season's rules from the API so we stop hardcoding them.

    Stored in ``meta`` (rule_*) and read by the solver with config fallbacks, so
    Gaffer self-adjusts if FPL changes the budget, club limit, sell-on fee, etc.

    Squad and transfer rules are read through ``gaffer.rules.parse_rules``, which
    prefers the newer ``game_config.rules`` block and falls back to the older
    top-level ``game_settings`` — the two carry the same keys, and which one an
    API build ships is not this function's problem.
    """
    gs = rules.parse_rules(bootstrap)
    mapping = {
        "rule_squad_size": gs.get("squad_squadsize"),
        "rule_budget": gs.get("squad_total_spend"),
        "rule_club_limit": gs.get("squad_team_limit"),
        "rule_transfers_cap": gs.get("transfers_cap"),
        "rule_sell_on_fee": gs.get("transfers_sell_on_fee"),
        "rule_max_extra_ft": gs.get("max_extra_free_transfers"),
    }
    for key, val in mapping.items():
        if val is not None:
            db.set_meta(conn, key, val)
    # Total active managers — scales the price-change threshold (a rise/fall needs
    # net transfers proportional to the number of people who own the player).
    tp = bootstrap.get("total_players")
    if tp is not None:
        db.set_meta(conn, "total_players", tp)


def _history_rows(pid: int, history: list[dict[str, Any]], season: str,
                  now: str) -> list[dict[str, Any]]:
    """Map ``element_summary[...]['history']`` entries to player_gw rows."""
    rows = []
    for h in history:
        if not isinstance(h, dict):
            continue
        fixture = h.get("fixture")
        rnd = h.get("round")
        if fixture is None or rnd is None:
            continue  # cannot key it; skip rather than invent an id
        rows.append({
            "season": season,
            "player_id": pid,
            "gw": int(rnd),
            "fixture": int(fixture),
            "kickoff_time": h.get("kickoff_time"),
            "minutes": h.get("minutes"),
            "total_points": h.get("total_points"),
            "goals": h.get("goals_scored"),
            "assists": h.get("assists"),
            "clean_sheet": h.get("clean_sheets"),
            "goals_conceded": h.get("goals_conceded"),
            "own_goals": h.get("own_goals"),
            "penalties_saved": h.get("penalties_saved"),
            "penalties_missed": h.get("penalties_missed"),
            "yellow_cards": h.get("yellow_cards"),
            "red_cards": h.get("red_cards"),
            "saves": h.get("saves"),
            "bonus": h.get("bonus"),
            "bps": h.get("bps"),
            "starts": h.get("starts"),
            "defcon": h.get("defensive_contribution"),
            "xg": _f(h.get("expected_goals")),
            "xa": _f(h.get("expected_assists")),
            "xgi": _f(h.get("expected_goal_involvements")),
            "xgc": _f(h.get("expected_goals_conceded")),
            "value": h.get("value"),
            "selected": h.get("selected"),
            "was_home": 1 if h.get("was_home") else 0,
            "opponent_team": h.get("opponent_team"),
            "ingested_at": now,
        })
    return rows


def ingest_player_history(
    conn: sqlite3.Connection, client: FplClient, season: str | None = None,
    player_ids: list[int] | None = None,
) -> int:
    """Persist per-player per-fixture results already fetched during enrichment.

    ``enrich_history`` calls ``element_summary`` for every relevant player and
    throws the ``history`` array away. Retaining it costs no extra HTTP and is
    the foundation for calibration and post-gameweek review.

    Idempotent: re-running upserts the same (season, player_id, fixture) rows, so
    an upstream correction (FPL revising bonus or xG after review) overwrites the
    earlier value rather than duplicating it.
    """
    season = season or season_mod.current(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if player_ids is None:
        player_ids = [r["id"] for r in conn.execute("SELECT id FROM players ORDER BY id")]

    written = 0
    for pid in player_ids:
        try:
            summ = client.element_summary(pid)
        except (httpx.HTTPStatusError, httpx.TransportError):
            continue  # one player's history must not abort the run
        history = summ.get("history") if isinstance(summ, dict) else None
        if not isinstance(history, list) or not history:
            continue
        rows = _history_rows(pid, history, season, now)
        if rows:
            written += db.upsert(conn, "player_gw", rows, ["season", "player_id", "fixture"])
    return written


def _stored_squad_event(conn: sqlite3.Connection) -> int | None:
    """The event the currently stored squad actually came from, from the rows."""
    row = conn.execute("SELECT MAX(gw) AS gw FROM my_squad").fetchone()
    return int(row["gw"]) if row and row["gw"] is not None else None


def _record_squad_state(
    conn: sqlite3.Connection, status: str, reason: str,
    source_event: int | None, retrieved_at: str | None = None,
) -> None:
    """Write the machine-readable squad state. Always these five keys together."""
    db.set_meta(conn, "squad_status", status)
    db.set_meta(conn, "squad_status_reason", reason)
    db.set_meta(conn, "squad_source_event", "" if source_event is None else source_event)
    db.set_meta(conn, "squad_retrieved_at", retrieved_at or "")


def _clear_squad(conn: sqlite3.Connection) -> None:
    """Remove every stored squad row. Used when no squad may legitimately exist."""
    conn.execute("DELETE FROM my_squad")
    conn.commit()


def _valid_picks(payload: Any) -> list[dict[str, Any]] | None:
    """Validate the picks payload. Returns the picks list, or None if unusable."""
    if not isinstance(payload, dict):
        return None
    picks = payload.get("picks")
    if not isinstance(picks, list) or not picks:
        return None
    for p in picks:
        if not isinstance(p, dict) or not isinstance(p.get("element"), int):
            return None
    return picks


def ingest_my_squad(
    conn: sqlite3.Connection, client: FplClient, entry_id: int,
    squad_gw: int | None, projection_gw: int | None = None,
) -> int:
    """Load the entry's picks for the *readable* event, atomically.

    ``squad_gw`` is the latest event whose picks FPL will serve (see
    ``gameweek.readable_squad_event``) — never the projection event, whose picks
    are private until its deadline passes.

    Failure modes are recorded distinctly (``not_found`` / ``fetch_failed`` /
    ``malformed``), and a failed fetch never leaves a half-written squad: the
    replacement runs in one transaction, and any previously stored squad is
    either retained *and labelled stale* or cleared — never retained while the
    metadata claims the squad is current.
    """
    if squad_gw is None:
        # Pre-season: no event's picks are readable yet. Nothing may be stored,
        # or a stale prior-season squad would masquerade as current holdings.
        _clear_squad(conn)
        _record_squad_state(
            conn, gameweek.STATUS_NO_PUBLIC_SQUAD_YET,
            "no gameweek deadline has passed yet, so FPL exposes no picks", None,
        )
        return 0

    def _degrade(status: str, reason: str) -> int:
        """Keep a usable prior squad if one exists, but label it truthfully."""
        stored = _stored_squad_event(conn)
        if stored is None:
            _record_squad_state(conn, status, reason, None)
            return 0
        _record_squad_state(
            conn, gameweek.STATUS_STALE,
            f"{reason}; showing the squad stored from GW{stored}", stored,
        )
        return 0

    try:
        payload = client.entry_picks(entry_id, squad_gw)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 404:
            # A readable event that 404s is a real problem, not "pre-season".
            return _degrade(
                gameweek.STATUS_NOT_FOUND,
                f"FPL returned 404 for entry {entry_id} GW{squad_gw} even though "
                f"that deadline has passed",
            )
        return _degrade(gameweek.STATUS_FETCH_FAILED, f"HTTP {code} fetching picks")
    except (httpx.TransportError, httpx.InvalidURL) as exc:
        return _degrade(gameweek.STATUS_FETCH_FAILED, f"{type(exc).__name__} fetching picks")
    except (ValueError, json.JSONDecodeError) as exc:
        return _degrade(gameweek.STATUS_MALFORMED, f"unparseable picks response: {exc}")

    picks = _valid_picks(payload)
    if picks is None:
        return _degrade(
            gameweek.STATUS_MALFORMED,
            "picks response present but had no usable 'picks' list",
        )

    squad_ids = [p["element"] for p in picks]

    # T-11: reconstruct real purchase/selling prices from public data. Valuing a
    # held player at market price hands the solver money FPL will not pay.
    try:
        transfers = client.entry_transfers(entry_id)
        if not isinstance(transfers, list):
            transfers = None
    except (httpx.HTTPStatusError, httpx.TransportError, ValueError):
        transfers = None
    try:
        chips = (client.entry_history(entry_id) or {}).get("chips")
    except (httpx.HTTPStatusError, httpx.TransportError, ValueError):
        chips = None

    market = {r["id"]: r["price"] for r in conn.execute("SELECT id, price FROM players")}
    starts = {
        r["id"]: r["cost_change_start"] or 0
        for r in conn.execute("SELECT id, cost_change_start FROM players")
    }
    settings = config.Settings.load()
    priced = teamstate.reconstruct(
        squad_ids, market, starts, transfers, chips,
        overrides=settings.purchase_prices,
    )

    rows = [
        {
            "gw": squad_gw,
            "player_id": p["element"],
            "is_captain": 1 if p.get("is_captain") else 0,
            "is_vice": 1 if p.get("is_vice_captain") else 0,
            "multiplier": p.get("multiplier", 1),
            "purchase_price": priced.prices[p["element"]].purchase,
            "selling_price": priced.prices[p["element"]].selling,
            "price_source": priced.prices[p["element"]].source,
            "price_exact": 1 if priced.prices[p["element"]].exact else 0,
        }
        for p in picks
    ]

    # Atomic replace: exactly one squad is ever stored, and a mid-write failure
    # rolls back rather than leaving a mixture of old and new rows.
    cols = list(rows[0].keys())
    sql = (
        f"INSERT INTO my_squad ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)})"
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM my_squad")
        conn.executemany(sql, rows)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    eh = payload.get("entry_history") or {}
    if eh.get("value") is not None:
        db.set_meta(conn, "team_value", eh["value"])
    db.set_meta(conn, "active_chip", payload.get("active_chip") or "")

    # T-11: bank and free transfers, with their provenance. An unknown bank is
    # recorded as unknown — never silently as £0.0m.
    bank = teamstate.resolve_bank(settings.bank, from_picks=eh.get("bank"))
    summary = teamstate.summarise(
        priced, bank, settings.free_transfers, settings.sources.get("free_transfers", "default")
    )
    db.set_meta(conn, "bank", "" if bank.value is None else bank.value)
    for k, v in summary.as_meta().items():
        if k != "bank":
            db.set_meta(conn, k, "" if v is None else v)
    _record_squad_state(
        conn, gameweek.STATUS_LOADED,
        f"picks read for GW{squad_gw}"
        + (f" while projecting GW{projection_gw}" if projection_gw else ""),
        squad_gw, datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return len(rows)


def enrich_history(conn: sqlite3.Connection, client: FplClient) -> int:
    """Persist a last-season baseline from each relevant player's ``history_past``.

    Captures DEFCON *and* last-season xG/xA/minutes/starts (``base_*``). This is
    the projection's fallback so it keeps using real underlying numbers once FPL
    resets the bootstrap stats to zero for the new season. Gated on price/ownership
    (NOT current minutes, which reset to 0) so the enrichment still selects players
    after the reset. One cached call per player (~350).
    """
    # The `base_defcon90 IS NULL` arm is a backfill, and it is why that column is
    # nullable. Any database written before DEFCON had its own baseline column
    # already carries base_minutes > 0 for every enriched player, so the
    # `base_minutes=0` gate alone would never revisit them and `base_defcon90`
    # would stay empty forever — which the projection reads as "no prior-season
    # DEFCON evidence" and answers with a positional average, for exactly the
    # ball-winners the column exists to protect. NULL is "never read" and 0.0 is
    # "read, and none", so each player is selected once and then stops matching.
    # G28 — the `price>=45 OR selected_by_pct>=0.5` gate is deliberate, and its
    # consequence is asymmetric in a way worth stating rather than rediscovering.
    # It exists because `element_summary` is one HTTP call *per player* and the
    # cheap-and-unowned tail is most of the league. Those players therefore never
    # receive a `base_defcon90` and fall through to the positional prior.
    #
    # That is the right answer for them — a 4.0 defender nobody owns has no
    # prior-season signal worth a round trip, and the prior is what he would
    # regress to anyway. The asymmetry is only a problem if someone reads a
    # missing `base_defcon90` as "measured and found to be zero". It is not: it
    # means "never looked". `base_defcon90 IS NULL` and `0.0` are distinct
    # states for exactly this reason, per the note above.
    targets = conn.execute(
        "SELECT id FROM players WHERE (price>=45 OR selected_by_pct>=0.5) "
        "AND (base_minutes=0 OR base_defcon90 IS NULL)"
    ).fetchall()
    updated = 0
    for row in targets:
        pid = row["id"]
        try:
            summ = client.element_summary(pid)
        except httpx.HTTPStatusError:
            continue
        past = summ.get("history_past") or []
        if not past:
            continue
        # The most recent season FPL HAS for this player — which is only last
        # season if he played in the Premier League last season. For anyone who
        # spent the intervening years abroad or in the Championship this is an
        # older record, so the season is captured with the numbers rather than
        # assumed from the calendar downstream.
        last = past[-1]
        mins = last.get("minutes") or 0
        if mins < config.BASE_SAMPLE_MINUTES:
            continue
        per90 = 90.0 / mins  # FPL returns expected_* as strings -> cast with _f
        season_name = str(last.get("season_name") or "").strip()
        vals = {
            "base_minutes": mins,
            "base_starts": last.get("starts") or 0,
            "base_xg90": round(_f(last.get("expected_goals")) * per90, 3),
            "base_xa90": round(_f(last.get("expected_assists")) * per90, 3),
        }
        dc = _f(last.get("defensive_contribution"))
        # `base_defcon90` is written on EVERY pass, including when the answer is
        # 0.0: that is what turns NULL ("never read") into a recorded value and
        # takes the player out of the backfill arm of the query above.
        # `defcon_per_90` keeps its existing behaviour and is only overwritten by
        # a non-zero figure, because a zero written there would erase a rate the
        # bootstrap may legitimately still be carrying.
        cols = ("base_minutes=?, base_starts=?, base_xg90=?, base_xa90=?, "
                "base_defcon90=?, base_season=?")
        params = [vals["base_minutes"], vals["base_starts"], vals["base_xg90"],
                  vals["base_xa90"], round(dc * per90, 3), season_name]
        if dc:
            cols += ", defcon_per_90=?"
            params.append(round(dc * per90, 3))
        params.append(pid)
        conn.execute(f"UPDATE players SET {cols} WHERE id=?", params)
        updated += 1
    conn.commit()
    return updated


def ingest_entry_meta(conn: sqlite3.Connection, client: FplClient, entry_id: int) -> None:
    try:
        info = client.entry(entry_id)
    except httpx.HTTPStatusError:
        return
    db.set_meta(conn, "entry_name", info.get("name", ""))
    db.set_meta(conn, "manager_name", f"{info.get('player_first_name','')} "
                f"{info.get('player_last_name','')}".strip())
    db.set_meta(conn, "overall_rank", info.get("summary_overall_rank") or "")
    # The live view needs a season-points baseline to sit its in-gameweek score
    # on top of. This was never written, so `pipeline` read a missing key and
    # fell back to 0 — every live rank and total was wrong from the first
    # kickoff. Note it is the *whole-season* total and therefore already
    # includes an in-progress gameweek once scoring starts; `live` prefers the
    # entry history for that reason and treats this as the fallback.
    db.set_meta(conn, "overall_points", info.get("summary_overall_points") or "")
    # bank/value from last deadline as a fallback when picks aren't available yet.
    if db.get_meta(conn, "bank") is None:
        db.set_meta(conn, "bank", info.get("last_deadline_bank", 0))
        db.set_meta(conn, "team_value", info.get("last_deadline_value", 1000))


class SeasonMismatch(RuntimeError):
    """Raised when the API's season and the database's disagree.

    Deliberately fatal. The alternative is a run that succeeds, publishes, and
    leaves a database holding two seasons of players under one set of ids.
    """

    def __init__(self, identity):
        self.identity = identity
        super().__init__(
            f"refusing to ingest: {identity.state}.\n{identity.render()}\n"
            "Nothing was written. Resolve it with `python -m gaffer.season` "
            "(and `--rollover --confirm` if this really is a new season)."
        )


def run(
    db_path=None, skip_enrich: bool = False, now: datetime | None = None
) -> dict[str, int]:
    """Full ingest. Returns a small summary of row counts.

    Set ``skip_enrich`` (or env ``GAFFER_SKIP_ENRICH=1``) to skip the per-player
    DEFCON history calls during fast dev iterations.

    ``now`` overrides the clock used to resolve the projection and readable-squad
    events, so gameweek-boundary behaviour is testable without waiting for one.
    """
    import os

    config.ensure_dirs()
    conn = db.connect(db_path)
    db.init_schema(conn)
    settings = config.Settings.load()
    skip_enrich = skip_enrich or os.environ.get("GAFFER_SKIP_ENRICH") == "1"
    summary: dict[str, int] = {}
    with FplClient() as client:
        bootstrap = client.bootstrap()

        # T-29: identify the season BEFORE writing a single row. FPL reuses
        # element ids every summer, and most working tables are keyed on that id
        # alone — so ingesting a new season over an old one does not fail, it
        # silently rewrites last season's players as this season's. Refusing here
        # is the only place that costs nothing.
        api_season, why = season_mod.derive_from_bootstrap(bootstrap)
        ident = season_mod.identify(
            api=api_season, database=season_mod.stored(conn),
            empty_database=season_mod.database_is_empty(conn), api_detail=why)
        if not ident.safe_to_run:
            raise SeasonMismatch(ident)
        db.set_meta(conn, "season", ident.api)
        summary["season"] = ident.api

        # T-30: check the live scoring table BEFORE writing a single row. A
        # season projected under the previous season's rules is a green run that
        # is wrong everywhere, and the only cheap place to catch it is here.
        scoring = rules.verify(bootstrap)
        db.set_meta(conn, "rule_scoring_source", scoring["source"])
        db.set_meta(conn, "rule_scoring_status", scoring["status"])
        db.set_meta(conn, "rule_scoring_drift", "; ".join(scoring["drift"]))
        # G21 — `verify` already returns which rules the payload never covered,
        # and its own docstring promises the record carries them. Only this line
        # was missing, so we published *how many* rules went unverified and never
        # *which*. "Unverified" is only actionable if you can see what it covers.
        db.set_meta(conn, "rule_scoring_unchecked",
                    "; ".join(scoring.get("unchecked") or []))
        summary["scoring_rules"] = scoring["status"]

        summary["teams"] = ingest_teams(conn, bootstrap)
        summary["players"] = ingest_players(conn, bootstrap)
        summary["fixtures"] = ingest_fixtures(conn, client.fixtures())
        ingest_game_settings(conn, bootstrap)
        # Two distinct events. `projection_gw` is what we plan for; `squad_gw` is
        # the latest event whose picks FPL will actually serve. Conflating them
        # is what made every pre-deadline run 404 and silently keep stale rows.
        events = bootstrap["events"]
        projection_gw = gameweek.projection_event(events, now)
        squad_gw = gameweek.readable_squad_event(events, now)
        db.set_meta(conn, "current_gw", projection_gw)
        db.set_meta(conn, "projection_event", projection_gw)
        db.set_meta(conn, "last_finished_gw", gameweek.last_finished_event(events) or "")
        ev = next((e for e in events if e["id"] == projection_gw), None)
        if ev:
            db.set_meta(conn, "deadline", ev.get("deadline_time") or "")
            db.set_meta(conn, "gw_name", ev.get("name") or f"Gameweek {projection_gw}")
        if not skip_enrich:
            summary["enriched"] = enrich_history(conn, client)
            summary["player_gw"] = ingest_player_history(conn, client)
        # Free transfers: not in the public API, so trust the user-set value.
        db.set_meta(conn, "free_transfers", settings.free_transfers)
        if settings.entry_id:
            ingest_entry_meta(conn, client, settings.entry_id)
            summary["my_squad"] = ingest_my_squad(
                conn, client, settings.entry_id, squad_gw, projection_gw
            )
        else:
            _clear_squad(conn)
            _record_squad_state(
                conn, gameweek.STATUS_NO_ENTRY_ID,
                "no entry id configured; this is a generic build", None,
            )
    conn.close()
    return summary


if __name__ == "__main__":
    print(run())
