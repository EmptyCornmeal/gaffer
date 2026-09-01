"""A read-only MCP interface over Gaffer's validated data.

    python -m gaffer.mcp_server              # stdio, for a local MCP client
    python -m gaffer.mcp_server --self-test  # exercise every tool, then exit
    python -m gaffer.mcp_server --help

This is an *interface*, not a second analytics engine. Every answer comes from an
artifact the pipeline already wrote and `gaffer.contract` already validated, or —
where an artifact cannot hold it — from the database opened **read-only**. It
computes nothing the app does not compute, because two implementations of the
same number is how they start disagreeing.

**Authority boundaries, enforced rather than intended:**

- local stdio only; there is no HTTP transport in this module and no bind call;
- read-only: the SQLite URI carries `mode=ro`, and no tool writes a file;
- no FPL authentication, no transfer execution, no notification sending;
- no arbitrary SQL, filesystem path, URL or shell command is reachable from any
  tool argument — the only free-text input is a player-name fragment, and it is
  length-capped and matched with a parameterised `LIKE`;
- no LLM call inside the server;
- paths resolve from `gaffer.config`, never from the caller's working directory,
  so where the client happens to launch it cannot change what it reads.

`tests/test_mcp_server.py` asserts each of those, and
`tests/mcp_evals.json` pins the questions a client should be able to answer.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gaffer import calibration, config

#: Result envelope. Bump when the shape of a tool result changes.
MCP_SCHEMA_VERSION = "mcp-1.0"

SERVER_NAME = "gaffer"

#: An artifact older than this is reported stale rather than presented as
#: current. The pipeline runs three times a day.
STALE_AFTER = timedelta(hours=12)

#: Bounded by design. A tool that can return the whole player list is a tool that
#: can fill a context window with noise.
MAX_SEARCH_RESULTS = 25
MAX_QUERY_LENGTH = 60
MAX_COMPARE = 4

#: Every default tool response must serialise below this. `get_transfer_plan`
#: used to return 74 KB — five repetitions of fifteen full player cards — which
#: the MCP client refused outright, so the tool was unusable however correct its
#: contents were. The budget is asserted for every tool in
#: `tests/test_mcp_server.py`; it is a design constraint on what each tool
#: *returns*, never a blind truncation of what it built.
MAX_RESULT_BYTES = 20_000

#: How much of the budget a tool must leave spare. Being *just* under the cap is
#: how the cap gets breached: one more player, one more limitation string.
RESULT_HEADROOM_BYTES = 2_500

#: What a comparison gives up first when four full cards do not fit, in order.
#: Each is either spelled out by something that stays — `fixture_outlook` sums
#: the same weeks `fixtures` lists one by one — or prose, or an identifier the
#: `id` already carries. No number that exists nowhere else is ever dropped, and
#: what went is named on the response.
COMPARE_TRIM = ("tags", "rationale", "fixtures", "fixture_outlook",
                "full_name", "code")

#: Fixture-difficulty windows the tools offer, in gameweeks. Three is the span a
#: transfer is argued over; five is as far as `players.json` publishes fixtures.
FIXTURE_WINDOWS = (3, 5)

#: Ownership rows per list per league in `get_league_strategy`. The artifact
#: already caps at ten; this is what stops a third league making the tool
#: unanswerable, and it is reported rather than applied silently.
MAX_OWNERSHIP_ROWS = 10

#: Where the live numbers come from. Published with every scorecard so a reader
#: can go and re-derive the total from the same public endpoints Gaffer read,
#: rather than having to trust the aggregate. No request is ever made from this
#: module - these are provenance strings, and `tests/test_mcp_server.py` asserts
#: that no HTTP client is even importable here.
FPL_API_BASE = "https://fantasy.premierleague.com/api"

#: Rival rows in a scorecard before the list is thinned to fit the budget. The
#: manager's own fifteen rows are never thinned: they are the arithmetic.
MAX_RIVAL_ROWS = 12

#: Reported instead of a per-player breakdown for a rival. The live artifact
#: publishes per-player rows for the manager's own squad only, and no other
#: artifact - and no table in the read-only database - carries another entry's
#: picks. Producing one would mean fetching `entry/{id}/event/{gw}/picks/`, and
#: this server has no HTTP client by design. Saying so, with the join spelled
#: out, is worth more than a silently absent key.
RIVAL_ROWS_UNAVAILABLE = "no_rival_picks_in_any_artifact"

#: `rival_squads` is absent from the artifact entirely — it was written before
#: the block existed. Distinct from an EMPTY list, which means the run could not
#: read the league's season baseline. Collapsing the two would turn "this build
#: is old" into "you have no rivals", which is a different and wrong sentence.
RIVAL_ROWS_PREDATE = "live_artifact_predates_rival_squads"
RIVAL_ROWS_INCOMPLETE = "league_baseline_unreadable"

#: Detail levels for the transfer plan.
DETAIL_SUMMARY = "summary"
#: Detail level for `get_model_evidence`. Its full candidate block is evidence,
#: not bloat, so it is projected rather than truncated — and reachable in full.
DETAIL_FULL = "full"
DETAIL_GAMEWEEK = "gameweek"
ALL_DETAIL = frozenset({DETAIL_SUMMARY, DETAIL_GAMEWEEK})
EVIDENCE_DETAIL = frozenset({DETAIL_SUMMARY, DETAIL_FULL})


# ---------------------------------------------------------------------------
# Failure states
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_MISSING = "artifact_missing"
STATUS_STALE = "artifact_stale"
STATUS_UNSUPPORTED = "unsupported_schema"
STATUS_MALFORMED = "artifact_malformed"
STATUS_UNAVAILABLE = "data_unavailable"
STATUS_NOT_FOUND = "not_found"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_INVALID = "invalid_request"
ALL_STATUSES = frozenset({
    STATUS_OK, STATUS_MISSING, STATUS_STALE, STATUS_UNSUPPORTED,
    STATUS_MALFORMED, STATUS_UNAVAILABLE, STATUS_NOT_FOUND, STATUS_AMBIGUOUS,
    STATUS_INVALID,
})


class ToolError(Exception):
    """A stable, reportable failure. Never a stack trace."""

    def __init__(self, status: str, detail: str, **extra: Any):
        self.status = status
        self.detail = detail
        self.extra = extra
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Reading what the pipeline already validated
# ---------------------------------------------------------------------------

def data_dir() -> Path:
    """Always from config, never from the caller's cwd."""
    return Path(config.DATA_DIR)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def load_artifact(name: str, *, required: bool = True) -> Any:
    path = data_dir() / name
    if not path.exists():
        if required:
            raise ToolError(STATUS_MISSING,
                            f"{name} has not been generated. Run "
                            f"`python -m gaffer.pipeline`.", artifact=name)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ToolError(STATUS_MALFORMED,
                        f"{name} is not valid JSON ({type(exc).__name__}).",
                        artifact=name) from None


def freshness(meta: Any) -> dict[str, Any]:
    stamp = _parse_ts((meta or {}).get("generated_at"))
    if stamp is None:
        return {"generated_at": None, "age_seconds": None, "stale": True,
                "reason": "meta.json carries no parseable generated_at"}
    age = (_now() - stamp).total_seconds()
    return {"generated_at": stamp.isoformat(), "age_seconds": round(age),
            "stale": age > STALE_AFTER.total_seconds(),
            "reason": None if age <= STALE_AFTER.total_seconds()
            else f"last refresh was {round(age / 3600, 1)}h ago"}


VERSION_FIELDS = ("model_version", "objective_version", "sim_version")

#: Why a version is absent. `not_applicable` means this tool's answer does not
#: come from that machinery at all; `not_available` means it does, but the
#: artifact it read does not record it. Collapsing the two into a bare `null` is
#: what made every envelope look like an accidental omission.
NOT_APPLICABLE = "not_applicable"
NOT_AVAILABLE = "not_available"

#: Which versions each source artifact is *expected* to carry. Anything outside
#: a tool's set is `not_applicable` rather than missing.
APPLICABLE: dict[str, tuple[str, ...]] = {
    "meta.json": ("model_version",),
    "players.json": ("model_version",),
    "decision.json": VERSION_FIELDS,
    "decision_snapshots": VERSION_FIELDS,
    "strategy.json": ("model_version", "sim_version"),
    # The plan comes out of the shared objective and the scenario set, so both
    # genuinely apply — plan.json simply does not record them. That is
    # `not_available`, and saying so is the point.
    "plan.json": VERSION_FIELDS,
    "live.json": ("model_version",),
    "backtest.json": ("model_version",),
    "review.json": ("model_version",),
    # The in-season calibration read straight from the persisted record. Every
    # version field genuinely applies — the distributions came from the
    # simulator, the per-player numbers from the model, both selected by the
    # objective — so a missing one is `not_available`, never `not_applicable`.
    "data/state": VERSION_FIELDS,
}


def _versions(blob: Any, meta: Any, *, source: str) -> dict[str, Any]:
    """Versions taken from the artifact the tool actually read.

    Never copied from an unrelated artifact. The one cross-artifact fallback is
    `model_version` from `meta.json`, which describes the pipeline run that
    produced every file in the set — the same run, not a different one — and it
    is only used when the source itself is silent.

    This exists because the envelope used to read all three fields from
    `meta.json`, which carries only `model_version`. Every response therefore
    reported `sim_version: null` while `get_league_strategy` returned
    `simulation.sim_version: "scenarios-1.0"` in the same payload.
    """
    found: dict[str, str] = {}
    if isinstance(blob, dict):
        # `versions` (decision, snapshots), `simulation` (strategy), then the
        # artifact's own top level (backtest).
        for container in (blob.get("versions"), blob.get("simulation"), blob):
            if not isinstance(container, dict):
                continue
            for f in VERSION_FIELDS:
                if f not in found and isinstance(container.get(f), str):
                    found[f] = container[f]
    if "model_version" not in found and isinstance(meta, dict):
        if isinstance(meta.get("model_version"), str):
            found["model_version"] = meta["model_version"]

    applicable = APPLICABLE.get(source, VERSION_FIELDS)
    out: dict[str, Any] = {f: found.get(f) for f in VERSION_FIELDS}
    unavailable = {
        f: (NOT_AVAILABLE if f in applicable else NOT_APPLICABLE)
        for f in VERSION_FIELDS if f not in found
    }
    out["source"] = source
    out["unavailable"] = unavailable
    return out


def envelope(source_artifact: str, meta: Any, *, status: str = STATUS_OK,
             limitations: list[str] | None = None, blob: Any = None,
             **extra: Any) -> dict[str, Any]:
    """The fields every tool result carries, so no answer arrives undated.

    ``blob`` is the artifact the tool read; its version block is the provenance,
    rather than `meta.json`'s, which only ever knew the model version.
    """
    meta = meta if isinstance(meta, dict) else {}
    out: dict[str, Any] = {
        "mcp_schema_version": MCP_SCHEMA_VERSION,
        "status": status,
        "season": meta.get("season"),
        "as_of": meta.get("generated_at"),
        "source_artifact": source_artifact,
        "freshness": freshness(meta),
        "versions": _versions(blob, meta, source=source_artifact),
    }
    if limitations:
        out["limitations"] = limitations
    out.update(extra)
    return out


def serialized_bytes(result: Any) -> int:
    """UTF-8 size of a tool result **as the transport carries it**.

    Indented, not compact: the MCP SDK serialises tool results with `indent=2`,
    which is about 30% larger. Measuring the compact form would set a budget
    against a payload nobody ever sends — verified against a real client, whose
    reported size matches this exactly.
    """
    return len(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))


def _meta() -> Any:
    return load_artifact("meta.json")


def read_only_db() -> sqlite3.Connection:
    """The database, opened so a write is impossible rather than merely absent."""
    path = Path(config.DB_PATH)
    if not path.exists():
        raise ToolError(STATUS_UNAVAILABLE,
                        "no local database — run the pipeline first.")
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Tool implementations — plain functions, so tests do not need a transport
# ---------------------------------------------------------------------------

def projection_regime(meta: Any) -> dict[str, Any]:
    """Which h=1 number was published, and the evidence for refusing the blend.

    A property of the RUN rather than of a player, so it travels once per
    response instead of on every card. `component_only` means the published
    `next_gw_xp` is Gaffer's model alone: `ep_next` was measured to be a copy of
    `form` for most blend-eligible players, and a second opinion that is the
    same opinion is not one.
    """
    meta = meta if isinstance(meta, dict) else {}
    return {
        "regime": meta.get("projection_regime"),
        "reason": meta.get("projection_regime_reason"),
        "nominal_blend_weight": config.EP_NEXT_BLEND_WEIGHT,
        "applied_blend_weight_mean": meta.get("ep_next_blend_weight_applied_mean"),
        "ep_next_matched_form_pct": meta.get("ep_next_form_match"),
        "ep_next_form_sample": meta.get("ep_next_form_sample"),
        "blend_is_fitted": config.EP_NEXT_BLEND_IS_FITTED,
    }


def _tenths(raw: Any) -> float | None:
    """FPL money as millions. `meta.json` stores it in tenths, as strings."""
    try:
        return round(int(str(raw).strip()) / 10, 1)
    except (TypeError, ValueError):
        return None


def squad_value() -> dict[str, Any]:
    """What the stored squad was bought for and sells for, **as stored**.

    Summed from `my_squad`, which is where the purchase prices live: no
    published artifact carries them, because `players.json` knows only today's
    market price and what you paid is a fact about a past transfer.

    The event is returned with the totals. A squad stored for GW1 beside a
    projection for GW3 is a real and readable state, not something to smooth
    over.
    """
    try:
        conn = read_only_db()
    except ToolError:
        return {"available": False, "unavailable_reason": COMPONENTS_NO_DB}
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(selling_price) AS selling, "
            "SUM(purchase_price) AS purchase, gw FROM my_squad "
            "WHERE gw = (SELECT MAX(gw) FROM my_squad)").fetchone()
    except sqlite3.Error:
        return {"available": False, "unavailable_reason": COMPONENTS_NO_DB}
    finally:
        conn.close()
    if row is None or not row["n"]:
        return {"available": False, "unavailable_reason": SQUAD_NONE_STORED}
    return {
        "available": True,
        "players": row["n"],
        "squad_event": row["gw"],
        "selling_value": _tenths(row["selling"]),
        "purchase_value": _tenths(row["purchase"]),
    }


def gaffer_status() -> dict[str, Any]:
    """Season, gameweek, deadline, freshness, build mode, squad and budget."""
    meta = _meta()
    decision = load_artifact("decision.json", required=False) or {}
    squad = (decision.get("squad_state") or {})
    held = squad_value()
    return envelope(
        "meta.json", meta, blob=meta,
        gameweek={
            "projecting": meta.get("current_gw"),
            "name": meta.get("gw_name"),
            "deadline": meta.get("deadline"),
            "last_finished": meta.get("last_finished_gw"),
        },
        build_mode=meta.get("build_mode"),
        entry={"entry_id": meta.get("entry_id"),
               "entry_name": meta.get("entry_name"),
               "league_ids": meta.get("league_ids")},
        squad={"known": bool(squad.get("known")),
               "status": meta.get("squad_status"),
               "reason": meta.get("squad_status_reason"),
               "source_event": meta.get("squad_source_event")},
        # What a transfer has to be paid for out of. Every one of these was
        # already in `meta.json` or the stored squad and none of them reached a
        # caller, so "can I afford him" could not be answered from this server.
        budget={
            "bank": _tenths(meta.get("bank")),
            "bank_source": meta.get("bank_source"),
            "team_value": _tenths(meta.get("team_value")),
            "free_transfers": meta.get("free_transfers"),
            "free_transfers_source": meta.get("free_transfers_source"),
            "squad_selling_value": held.get("selling_value"),
            "squad_value_event": held.get("squad_event"),
            "squad_value_unavailable": held.get("unavailable_reason"),
            "selling_price_confidence": meta.get("selling_price_confidence"),
            "selling_prices_exact": meta.get("selling_prices_exact"),
            "selling_prices_total": meta.get("selling_prices_total"),
            "units": "millions",
        },
        projection_regime=projection_regime(meta),
        artifacts=sorted(p.name for p in data_dir().glob("*.json")),
        limitations=[
            "`bank`, `team_value` and `free_transfers` are FPL's own, as of the "
            "last refresh. `squad_selling_value` is summed from the stored "
            "squad, whose event is given beside it and is often an earlier "
            "gameweek than the one being projected.",
        ],
    )




def get_weekly_decision() -> dict[str, Any]:
    """This week's single action, with the hold comparison behind it."""
    meta = _meta()
    d = load_artifact("decision.json")
    if not isinstance(d, dict):
        raise ToolError(STATUS_MALFORMED, "decision.json is not an object")
    dec = d.get("decision") or {}
    if not dec.get("action"):
        raise ToolError(STATUS_UNAVAILABLE,
                        "no decision has been published for this gameweek")

    def card(p: Any) -> Any:
        if not isinstance(p, dict):
            return p
        return {k: p.get(k) for k in ("id", "name", "team", "pos", "price",
                                      "next_gw_xp")}

    return envelope(
        "decision.json", meta, blob=d,
        action=dec.get("action"),
        headline=dec.get("headline"),
        reason=dec.get("reason"),
        transfers={"out": [card(p) for p in dec.get("transfers_out") or []],
                   "in": [card(p) for p in dec.get("transfers_in") or []]},
        captain=card(dec.get("captain")),
        vice=card(dec.get("vice")),
        comparison=dec.get("comparison"),
        executability=dec.get("executability"),
        chip=d.get("chip"),
        confidence=dec.get("confidence"),
        biggest_risk=dec.get("biggest_risk"),
        assumptions=dec.get("assumptions"),
        threshold_status=dec.get("threshold_status"),
        squad_known=bool((d.get("squad_state") or {}).get("known")),
        limitations=[
            "The action bar (points and probability) is a policy choice, not a "
            "fitted parameter — see `threshold_status`.",
        ],
    )


#: Why a blend component is absent, as a stable code.
COMPONENTS_NO_DB = "no_local_database"
#: The local database and the published artifacts are refreshed by different
#: things — `data/*.json` by the scheduled refresh, `data/gaffer.db` only by a
#: pipeline run in this checkout — so a stored component can belong to an older
#: run than the number it is shown beside. Saying so is the whole point.
COMPONENTS_OTHER_RUN = "stored_by_a_different_run_than_the_published_projection"
#: Squad states, kept apart from each other for the same reason.
SQUAD_NOT_OWNED = "not in the stored squad"
SQUAD_NONE_STORED = "no squad has been stored yet"
COMPONENTS_NO_ROW = "no_projection_row_for_this_gameweek"
COMPONENTS_NOT_STORED = "not_stored_in_this_record"
#: Why a player carries no defensive-contribution block. A goalkeeper cannot
#: score it at all; everyone else simply has not recorded one yet, and the two
#: are not the same absence.
DEFCON_NO_THRESHOLD = "this position has no defensive-contribution threshold"
DEFCON_NONE_OBSERVED = "no defensive contribution observed yet this season"


def stored_components(player_ids: list[int], gw: Any) -> dict[int, dict[str, Any]]:
    """The blend's two components for one gameweek, **as stored**.

    `players.json` publishes only the blended `next_gw_xp`; the components live
    in the `projections` table as `exp_points_model` and `exp_points_ep_next`,
    written by the projection step before the blend is applied. They are read
    here, never recomputed and never derived backwards from the blend — the
    whole point of separating them is that the separation is a record, not an
    arithmetic identity somebody could have reconstructed anyway.

    Returns `{}` when the database is absent, so callers report unavailability
    rather than a number.
    """
    if not player_ids:
        return {}
    try:
        target = int(gw)
    except (TypeError, ValueError):
        return {}
    try:
        conn = read_only_db()
    except ToolError:
        return {}
    try:
        marks = ",".join("?" for _ in player_ids)
        rows = conn.execute(
            f"SELECT player_id, exp_points, exp_points_model, exp_points_ep_next, "
            f"exp_minutes, p_start, confidence FROM projections "
            f"WHERE gw = ? AND player_id IN ({marks})",
            [target, *player_ids]).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {int(r["player_id"]): dict(r) for r in rows}


def _component_block(row: dict[str, Any] | None, have_db: bool,
                     published_next_gw_xp: Any = None) -> dict[str, Any]:
    """`model_only` / `fpl_ep_next` plus an explicit availability statement.

    A stored `0.0` is a real value, not a missing one: the pipeline skips the
    blend when `ep_next` is zero, and forty-two players are in exactly that
    state right now.

    Given `published_next_gw_xp`, the stored blend is compared against the
    published one and the answer travels with the components. They come out of
    the database while `next_gw_xp` comes out of the artifact, and the two are
    refreshed by different things, so "these are the components of that number"
    is a claim that has to be checked rather than assumed.
    """
    if not have_db:
        return {"model_only": None, "fpl_ep_next": None,
                "components_available": False,
                "unavailable_reason": COMPONENTS_NO_DB}
    if row is None:
        return {"model_only": None, "fpl_ep_next": None,
                "components_available": False,
                "unavailable_reason": COMPONENTS_NO_ROW}
    model = row.get("exp_points_model")
    ep = row.get("exp_points_ep_next")
    out: dict[str, Any] = {"model_only": model, "fpl_ep_next": ep}
    missing = [n for n, v in (("model_only", model), ("fpl_ep_next", ep))
               if v is None]
    out["components_available"] = not missing
    out["unavailable_reason"] = COMPONENTS_NOT_STORED if missing else None
    if missing:
        out["missing_components"] = missing
    if published_next_gw_xp is not None:
        stored = row.get("exp_points")
        agrees = (isinstance(stored, (int, float))
                  and isinstance(published_next_gw_xp, (int, float))
                  and abs(round(stored, 2) - published_next_gw_xp) <= 0.011)
        out["same_run_as_published"] = bool(agrees)
        if not agrees:
            out["provenance"] = COMPONENTS_OTHER_RUN
            out["stored_blend"] = (round(stored, 2)
                                   if isinstance(stored, (int, float)) else None)
    return out


def squad_holdings(player_ids: list[int]) -> dict[str, Any]:
    """Purchase prices for players you hold, **as stored**, with their event.

    `my_squad` records what you paid. No published artifact carries it, because
    `players.json` knows only today's market price, and what you paid is a fact
    about a past transfer rather than a property of the player. It is read here
    and never inferred from the market price.
    """
    if not player_ids:
        return {"rows": {}, "event": None, "unavailable_reason": None}
    try:
        conn = read_only_db()
    except ToolError:
        return {"rows": {}, "event": None, "unavailable_reason": COMPONENTS_NO_DB}
    try:
        marks = ",".join("?" for _ in player_ids)
        rows = conn.execute(
            f"SELECT player_id, purchase_price, selling_price, price_source, "
            f"price_exact, gw FROM my_squad "
            f"WHERE gw = (SELECT MAX(gw) FROM my_squad) "
            f"AND player_id IN ({marks})", list(player_ids)).fetchall()
        top = conn.execute("SELECT MAX(gw) AS gw FROM my_squad").fetchone()
    except sqlite3.Error:
        return {"rows": {}, "event": None, "unavailable_reason": COMPONENTS_NO_DB}
    finally:
        conn.close()
    event = top["gw"] if top is not None else None
    return {"rows": {int(r["player_id"]): dict(r) for r in rows},
            "event": event,
            "unavailable_reason": None if event is not None else SQUAD_NONE_STORED}


def _holding_block(held: dict[str, Any] | None, market_price: Any,
                   squad: dict[str, Any]) -> dict[str, Any]:
    """What this player would sell for, and what that number rests on.

    FPL pays the purchase price back plus half of any RISE, rounded down. That
    rule lives in `config.fpl_selling_price` and is applied here through that
    same function rather than written out a second time — two implementations of
    a rounding rule is how they start disagreeing by 0.1m.

    Its two inputs come from two places and both are named: the purchase price
    from the stored squad, the market price from the published artifact.
    """
    if squad.get("unavailable_reason"):
        return {"owned": None,
                "unavailable_reason": squad["unavailable_reason"]}
    if held is None:
        return {"owned": False, "squad_event": squad.get("event"),
                "reason": SQUAD_NOT_OWNED}
    purchase = held.get("purchase_price")
    now = (round(float(market_price) * 10)
           if isinstance(market_price, (int, float)) else None)
    out: dict[str, Any] = {
        "owned": True,
        "squad_event": held.get("gw"),
        "purchase_price": None if purchase is None else round(purchase / 10, 1),
        "purchase_price_source": held.get("price_source"),
        "purchase_price_exact": bool(held.get("price_exact")),
        "selling_price": None,
        "selling_price_rule": "purchase + half of any rise, rounded down to 0.1m",
    }
    if purchase is None or now is None:
        out["unavailable_reason"] = COMPONENTS_NOT_STORED
        return out
    sell = config.fpl_selling_price(int(purchase), now)
    out["selling_price"] = round(sell / 10, 1)
    out["locked_in"] = round((now - sell) / 10, 1)
    stored = held.get("selling_price")
    if stored is not None and int(stored) != sell:
        out["stored_selling_price"] = round(int(stored) / 10, 1)
        out["stored_differs_because"] = (
            "the stored figure was computed at the market price of the run that "
            "wrote it; this one uses the published price beside it")
    return out




def _set_piece_order(note: Any) -> dict[str, Any]:
    """The set-piece note as an order per type, rather than a label to read.

    `players.json` publishes a string — `"pens #1, corners #2"` — which the UI
    renders as a tag and which a caller cannot sort, filter or compare on. The
    orders behind it are integers, so they come back as integers.

    That string is the whole record: ingest keeps only first and second choice,
    so `null` here means "not first or second choice", which is NOT the same
    statement as "does not take them".
    """
    text = str(note or "").strip()
    out: dict[str, Any] = {"penalties": None, "free_kicks": None,
                           "corners": None, "on_any": False, "note": text,
                           "recorded": "first and second choice only"}
    for part in text.split(","):
        label, marker, num = part.strip().partition("#")
        if not marker:
            continue
        key = {"pens": "penalties", "fk": "free_kicks",
               "corners": "corners"}.get(label.strip().lower())
        try:
            order = int(num.strip())
        except ValueError:
            continue
        if key:
            out[key] = order
    out["on_any"] = any(out[k] is not None
                        for k in ("penalties", "free_kicks", "corners"))
    return out


def _window(fixtures: Any, gw_xp: Any, gameweek: Any, span: int) -> dict[str, Any]:
    """Fixture difficulty and projected points summed over the next `span` GWs.

    `fixtures[]` already carries a per-gameweek difficulty — Gaffer's own
    xGC-based rating, 1 easiest to 5 hardest — and `gw_xp[]` a per-gameweek
    projection. Neither is ever added up, and the sum over the next two or three
    weeks is what decides which transfer goes first. This adds published
    numbers. It is not a second difficulty model.

    The window is defined over GAMEWEEKS rather than over the next N fixtures,
    so a blank contributes nothing and a double contributes twice — which is
    most of the reason the sum is worth having at all.
    """
    try:
        first = int(gameweek)
    except (TypeError, ValueError):
        return {"available": False,
                "unavailable_reason": "the published run names no gameweek"}
    gws = list(range(first, first + span))
    fx = [f for f in (fixtures or [])
          if isinstance(f, dict) and f.get("gw") in gws]
    diffs = [f["difficulty"] for f in fx
             if isinstance(f.get("difficulty"), (int, float))]
    xps = [x["xp"] for x in (gw_xp or [])
           if isinstance(x, dict) and x.get("gw") in gws
           and isinstance(x.get("xp"), (int, float))]
    covered = [f.get("gw") for f in fx]
    return {
        "available": True,
        "gameweeks": gws,
        "fixtures": len(fx),
        "blanks": [g for g in gws if g not in covered],
        "doubles": sorted({g for g in covered if covered.count(g) > 1}),
        "difficulty_sum": sum(diffs) if diffs else None,
        "difficulty_mean": round(sum(diffs) / len(diffs), 2) if diffs else None,
        "home_fixtures": sum(1 for f in fx if f.get("home")),
        "xp_sum": round(sum(xps), 2) if xps else None,
    }


def _signals(row: dict[str, Any], gameweek: Any) -> dict[str, Any]:
    """Every published number a search may rank or filter on, in one flat row.

    Flat rather than grouped on purpose: this shape is repeated up to twenty-five
    times in one response, and a caller comparing two of them should be able to
    read down a column.
    """
    pp = row.get("price_pred") if isinstance(row.get("price_pred"), dict) else {}
    dc = row.get("defcon") if isinstance(row.get("defcon"), dict) else {}
    w = _window(row.get("fixtures"), row.get("gw_xp"), gameweek, 3)
    return {
        "next_gw_xp": row.get("next_gw_xp"),
        "horizon_xp": row.get("horizon_xp"),
        "xp_window": row.get("xp_window"),
        "xp_next3": w.get("xp_sum"),
        "fdr3": w.get("difficulty_sum"),
        "defcon90": row.get("defcon90"),
        "defcon_p_hit": dc.get("p_hit"),
        "defcon_threshold": dc.get("threshold"),
        "form": row.get("form"),
        "ict": row.get("ict"),
        "xgi90": row.get("xgi90"),
        "price": row.get("price"),
        "owned_by": row.get("owned_by"),
        "net_transfers": row.get("net_transfers"),
        "cost_change_event": row.get("cost_change_event"),
        "price_direction": pp.get("dir"),
        "price_progress": pp.get("progress"),
        "set_pieces": row.get("set_pieces"),
    }


#: What `find_players` will sort on. Every one is a field `players.json`
#: publishes or a sum of them; none is a new quantity, and none is an opinion.
SORTABLE = ("next_gw_xp", "horizon_xp", "xp_window", "xp_next3", "fdr3",
            "defcon90", "defcon_p_hit", "form", "ict", "xgi90", "price",
            "owned_by", "net_transfers", "cost_change_event", "price_progress")
ORDERS = ("desc", "asc")
PRICE_DIRECTIONS = ("up", "down", "stable")


def _players() -> list[dict[str, Any]]:
    blob = load_artifact("players.json")
    if not isinstance(blob, list):
        raise ToolError(STATUS_MALFORMED, "players.json is not a list")
    return blob


def _number(value: Any, name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ToolError(STATUS_INVALID, f"{name} must be a number") from None


def find_players(query: str = "", team: str = "", position: str = "",
                 limit: int = 10, sort: str = "next_gw_xp", order: str = "desc",
                 min_price: Any = None, max_price: Any = None,
                 min_defcon90: Any = None, min_form: Any = None,
                 min_xgi90: Any = None, max_fdr3: Any = None,
                 price_direction: str = "", available_only: bool = False,
                 ) -> dict[str, Any]:
    """Bounded search over the published player list, ranked on any published signal.

    Sortable: next_gw_xp, horizon_xp, xp_window, xp_next3, fdr3, defcon90,
    defcon_p_hit, form, ict, xgi90, price, owned_by, net_transfers,
    cost_change_event, price_progress. Filterable: price, defcon90, form, xgi90,
    fdr3, price direction, availability, team, position, name.

    `fdr3` is the three-gameweek fixture-difficulty SUM and `xp_next3` the
    projection over the same window; on `fdr3` lower is better, so ask for
    `order="asc"`. Every field is one `players.json` publishes or a sum of them.
    A ranking is not a recommendation: this tool orders players, it does not
    argue for one.
    """
    sort = (sort or "next_gw_xp").strip().lower()
    order = (order or "desc").strip().lower()
    if sort not in SORTABLE:
        raise ToolError(STATUS_INVALID,
                        f"sort must be one of {list(SORTABLE)}")
    if order not in ORDERS:
        raise ToolError(STATUS_INVALID, f"order must be one of {list(ORDERS)}")
    direction = (price_direction or "").strip().lower()
    if direction and direction not in PRICE_DIRECTIONS:
        raise ToolError(STATUS_INVALID,
                        f"price_direction must be one of {list(PRICE_DIRECTIONS)}")
    if len(query) > MAX_QUERY_LENGTH:
        raise ToolError(STATUS_INVALID,
                        f"query is capped at {MAX_QUERY_LENGTH} characters")
    bounds = {
        "min_price": _number(min_price, "min_price"),
        "max_price": _number(max_price, "max_price"),
        "min_defcon90": _number(min_defcon90, "min_defcon90"),
        "min_form": _number(min_form, "min_form"),
        "min_xgi90": _number(min_xgi90, "min_xgi90"),
        "max_fdr3": _number(max_fdr3, "max_fdr3"),
    }
    q, t = query.strip().lower(), team.strip().upper()
    p = position.strip().upper()
    if p and p not in ("GKP", "DEF", "MID", "FWD"):
        raise ToolError(STATUS_INVALID, "position must be GKP, DEF, MID or FWD")
    criteria = any((q, t, p, direction, available_only,
                    sort != "next_gw_xp", order != "desc",
                    *(v is not None for v in bounds.values())))
    if not criteria:
        raise ToolError(
            STATUS_INVALID,
            "give at least one of query, team, position, a filter, or a sort "
            "other than the default next_gw_xp descending")
    limit = max(1, min(int(limit or 10), MAX_SEARCH_RESULTS))
    gameweek = (_meta() or {}).get("current_gw")

    def under(value: Any, cap: float | None) -> bool:
        return cap is None or (isinstance(value, (int, float)) and value <= cap)

    def over(value: Any, floor: float | None) -> bool:
        return floor is None or (isinstance(value, (int, float)) and value >= floor)

    hits = []
    for row in _players():
        if q and q not in str(row.get("name", "")).lower():
            continue
        if t and str(row.get("team", "")).upper() != t:
            continue
        if p and str(row.get("pos", "")).upper() != p:
            continue
        if available_only and str(row.get("status", "")) != "a":
            continue
        sig = _signals(row, gameweek)
        if direction and sig["price_direction"] != direction:
            continue
        if not over(sig["price"], bounds["min_price"]):
            continue
        if not under(sig["price"], bounds["max_price"]):
            continue
        if not over(sig["defcon90"], bounds["min_defcon90"]):
            continue
        if not over(sig["form"], bounds["min_form"]):
            continue
        if not over(sig["xgi90"], bounds["min_xgi90"]):
            continue
        if not under(sig["fdr3"], bounds["max_fdr3"]):
            continue
        hits.append({"id": row.get("id"), "name": row.get("name"),
                     "team": row.get("team"), "pos": row.get("pos"),
                     "status": row.get("status"), **sig})

    # A row with nothing stored for the sort field goes last in BOTH directions.
    # Ordering nulls as if they were zero puts "we do not know" above a measured
    # low value in one direction and below it in the other.
    def rank(r: dict[str, Any]) -> tuple[int, float]:
        v = r.get(sort)
        if not isinstance(v, (int, float)):
            return (1, 0.0)
        return (0, -float(v) if order == "desc" else float(v))

    hits.sort(key=rank)
    applied = {k: v for k, v in bounds.items() if v is not None}
    if direction:
        applied["price_direction"] = direction
    if available_only:
        applied["available_only"] = True
    out = envelope(
        "players.json", _meta(),
        status=STATUS_OK if hits else STATUS_NOT_FOUND,
        matched=len(hits), truncated=len(hits) > limit,
        players=hits[:limit],
        query={"query": query, "team": team, "position": position,
               "limit": limit, "sort": sort, "order": order, "filters": applied},
        sortable=list(SORTABLE),
        limitations=[
            "`fdr3` sums the difficulties Gaffer published for the next three "
            "gameweeks, so LOWER is better and a blank gameweek contributes "
            "nothing rather than a hard fixture.",
            "`defcon90` is the model's BELIEVED defensive-contribution rate, "
            "shrunk toward a positional prior, not a raw count. Compare it "
            "against `defcon_threshold` (10 for defenders, 12 otherwise); "
            "`defcon_threshold` is null for goalkeepers, who cannot score it.",
            "`price_progress` is a share of an ESTIMATED threshold — FPL does "
            "not publish the real ones.",
        ],
    )
    if not hits:
        out["detail"] = ("no player matched "
                         + ", ".join(f"{k}={v!r}" for k, v in
                                     (("query", query), ("team", team),
                                      ("position", position),
                                      ("filters", applied)) if v))
    return out




def _resolve(name_or_id: Any) -> dict[str, Any]:
    """One player, or a stable failure. Never a guess between two people."""
    rows = _players()
    if isinstance(name_or_id, int) or (isinstance(name_or_id, str)
                                       and name_or_id.isdigit()):
        pid = int(name_or_id)
        for r in rows:
            if r.get("id") == pid:
                return r
        raise ToolError(STATUS_NOT_FOUND, f"no player with id {pid}")
    needle = str(name_or_id).strip().lower()
    if not needle:
        raise ToolError(STATUS_INVALID, "empty player reference")
    exact = [r for r in rows if str(r.get("name", "")).lower() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [r for r in rows if needle in str(r.get("name", "")).lower()]
    if not partial:
        raise ToolError(STATUS_NOT_FOUND, f"no player matching {name_or_id!r}")
    if len(partial) > 1:
        raise ToolError(
            STATUS_AMBIGUOUS, f"{name_or_id!r} matches {len(partial)} players",
            candidates=[{"id": r.get("id"), "name": r.get("name"),
                         "team": r.get("team"), "pos": r.get("pos")}
                        for r in partial[:MAX_SEARCH_RESULTS]])
    return partial[0]


def _outlook(row: dict[str, Any], components: dict[str, Any] | None = None,
             *, gameweek: Any = None, holding: dict[str, Any] | None = None,
             ) -> dict[str, Any]:
    """The published projection, with the model's own number and FPL's apart.

    Every nullable field here is either a real stored value or accompanied by a
    reason. Nothing promises a separation it does not deliver.

    `players.json` carries nineteen fields this used to leave behind, and the
    ones that decide a transfer are here now: the defensive-contribution rate
    against its positional threshold (a new scoring mechanic, and a floor-versus-
    spike test that was being done by hand); the two blend components as the
    ARTIFACT recorded them, beside the ones the database recorded; the
    underlying rates that were previously reachable only as prose inside
    `rationale`, where they could not be sorted or compared; the set-piece order
    as an order; and the price-change signal. They are grouped rather than
    flattened, because `compare_players` repeats this shape up to four times.
    """
    dist = row.get("dist") if isinstance(row.get("dist"), dict) else {}
    defcon = row.get("defcon") if isinstance(row.get("defcon"), dict) else {}
    pp = row.get("price_pred") if isinstance(row.get("price_pred"), dict) else {}
    pos = str(row.get("pos") or "")
    proj: dict[str, Any] = {
        "next_gw_xp": row.get("next_gw_xp"),
        "horizon_xp": row.get("horizon_xp"),
        "xp_window": row.get("xp_window"),
        # The same two quantities as `model_only`/`fpl_ep_next` below, but as
        # the ARTIFACT published them — which is the run `next_gw_xp` belongs
        # to. The pair below comes out of the local database, which is
        # refreshed by something else.
        "model_xp": row.get("model_xp"),
        "ep_next_xp": row.get("ep_next_xp"),
        # Sums to `model_xp`, never to the blend.
        "breakdown": row.get("breakdown"),
        "blend_weight_nominal": config.EP_NEXT_BLEND_WEIGHT,
        "blend_is_fitted": config.EP_NEXT_BLEND_IS_FITTED,
    }
    block = dict(components) if components is not None else {
        "model_only": None, "fpl_ep_next": None,
        "components_available": False,
        "unavailable_reason": COMPONENTS_NO_DB}
    # Minutes belong under `minutes`, not inside the blend components.
    exp_minutes = block.pop("exp_minutes", None)
    proj.update(block)
    badge = row.get("xmins_badge")
    threshold = config.DEFCON_THRESHOLD.get(pos)
    return {
        "id": row.get("id"), "name": row.get("name"),
        "full_name": row.get("full_name"),
        "team": row.get("team"), "team_id": row.get("team_id"),
        "team_code": row.get("team_code"), "code": row.get("code"),
        "pos": row.get("pos"), "price": row.get("price"),
        "projection": proj,
        "minutes": {
            "p_start": row.get("p_start"),
            "exp_minutes": exp_minutes,
            "exp_minutes_source": ("projections" if exp_minutes is not None
                                   else COMPONENTS_NO_ROW),
            "badge": badge,
        },
        "availability": {"status": row.get("status"),
                         "chance_playing": row.get("chance_playing"),
                         "news": row.get("news")},
        # The published distribution, under the names it is stored with. These
        # were previously read from top-level keys that do not exist, so every
        # one of them came back null beside a claim that they were provided.
        "uncertainty": {"confidence": row.get("confidence"),
                        "floor": dist.get("floor"), "ceiling": dist.get("ceiling"),
                        "boom_pct": dist.get("boom"), "mean": dist.get("mean"),
                        "std": dist.get("std"),
                        "distribution_available": bool(dist)},
        # Defensive contribution scores this season, so the rate against the
        # positional threshold is a transfer test rather than trivia. `per90` is
        # the BELIEVED rate; `p_hit` is a probability, and a player can clear
        # the threshold routinely on a modest one.
        "defensive_contribution": {
            "per90": row.get("defcon90"),
            "threshold": None if threshold is None or threshold > 90 else threshold,
            "p_hit": defcon.get("p_hit"),
            "near_hit": defcon.get("near_hit"),
            "scored_by_position": bool(threshold is not None and threshold <= 90),
            "unavailable_reason": None if defcon else (
                DEFCON_NO_THRESHOLD if threshold is None or threshold > 90
                else DEFCON_NONE_OBSERVED),
        },
        # Previously reachable only as prose inside `rationale`.
        "underlying": {"xgi90": row.get("xgi90"), "form": row.get("form"),
                       "ict": row.get("ict"),
                       "last_season": row.get("last_season")},
        "set_pieces": _set_piece_order(row.get("set_pieces")),
        "price_signal": {
            "now": row.get("price"),
            "change_this_gw": row.get("cost_change_event"),
            "net_transfers_this_gw": row.get("net_transfers"),
            "direction": pp.get("dir"),
            "progress_to_change": pp.get("progress"),
            "threshold_estimate": pp.get("threshold"),
            "basis": "estimated; FPL does not publish its real thresholds",
        },
        "ownership": {"global_pct": row.get("owned_by")},
        "holding": holding if holding is not None else {
            "owned": None, "unavailable_reason": COMPONENTS_NO_DB},
        "rationale": row.get("rationale"),
        "tags": row.get("tags"),
        "fixtures": row.get("fixtures"),
        "fixture_outlook": {
            f"next{n}": _window(row.get("fixtures"), row.get("gw_xp"),
                                gameweek, n)
            for n in FIXTURE_WINDOWS
        },
    }




def _component_limitations(blocks: list[dict[str, Any]]) -> list[str]:
    """Say what the components are, or why they are not there. Never both."""
    have = [b for b in blocks if b.get("components_available")]
    if have and len(have) == len(blocks):
        out = [
            "`next_gw_xp` is a blend of Gaffer's own model (`model_only`) and "
            "FPL's `ep_next` (`fpl_ep_next`). Both are read from the stored "
            "projection, not reconstructed from the blend.",
            f"The blend weight is a NOMINAL {config.EP_NEXT_BLEND_WEIGHT} and "
            "UNFITTED — no backtest chose it. What is APPLIED is scaled by "
            "Gaffer's availability read AND by start probability, is not "
            "applied at all when `fpl_ep_next` is zero, and is zero across the "
            "whole population when FPL's `ep_next` fails the degeneracy test. "
            "See `projection_regime` on this response and get_model_evidence.",
            "When the regime is `component_only` the published `next_gw_xp` IS "
            "`model_xp` and no part of it is FPL's — `fpl_ep_next` is then what "
            "was REJECTED, not an ingredient.",
            "Beyond the next gameweek the projection is Gaffer's model alone, "
            "and it is materially weaker there.",
        ]
    else:
        reasons = sorted({b.get("unavailable_reason") or "" for b in blocks
                          if not b.get("components_available")} - {""})
        out = [
            "`model_only` and `fpl_ep_next` are NOT available in this record "
            f"({', '.join(reasons) or 'unknown'}), so `next_gw_xp` is reported "
            "as a single blended number. They are never derived backwards from it.",
            f"The blend weight is a NOMINAL {config.EP_NEXT_BLEND_WEIGHT} and is "
            "UNFITTED. What is APPLIED is scaled by availability and start "
            "probability, and is zero for everyone when FPL's `ep_next` fails "
            "the degeneracy test — see `projection_regime` and "
            "get_model_evidence.",
            "Beyond the next gameweek the projection is Gaffer's model alone, "
            "and it is materially weaker there.",
        ]
    if any(b.get("same_run_as_published") is False for b in blocks):
        out.append(
            "`model_only`/`fpl_ep_next` come from the local database, and the "
            "blend stored there does NOT match the published `next_gw_xp`: they "
            "were written by an EARLIER pipeline run. The artifacts are "
            "refreshed on a schedule, the database only by a run in this "
            "checkout. Use `projection.model_xp` and `projection.ep_next_xp` — "
            "the same two quantities, published by the same run as "
            "`next_gw_xp`.")
    return out


def _outlook_limitations() -> list[str]:
    """What the new blocks are, and what they are not. One list, both tools."""
    return [
        "`defensive_contribution.per90` is the model's BELIEVED rate, shrunk "
        "toward a positional prior — not a count of what has happened. The "
        "floor-versus-spike test is that rate against `threshold` (10 for "
        "defenders, 12 for midfielders and forwards).",
        "`set_pieces` records first and second choice only, so a null is 'not "
        "first or second choice', never 'does not take them'.",
        "`price_signal` is an ESTIMATE: FPL does not publish its price-change "
        "thresholds and `progress_to_change` is a share of an approximated one.",
        "`fixture_outlook` adds up difficulties Gaffer already published; it is "
        "not a projection. Two 2s and a 5 sum to the same 9 as three 3s.",
        "`holding.purchase_price` comes from the stored squad in the local "
        "database and `price` from the published artifact. `squad_event` says "
        "which gameweek the squad was read for.",
    ]


def get_player_outlook(player: str) -> dict[str, Any]:
    """One player's projection, returns, set pieces, price, fixtures and holding."""
    row = _resolve(player)
    meta = _meta()
    pid = int(row["id"])
    gameweek = (meta or {}).get("current_gw")
    stored = stored_components([pid], gameweek)
    block = _component_block(stored.get(pid), have_db=bool(stored),
                             published_next_gw_xp=row.get("next_gw_xp"))
    if pid in stored:
        block["exp_minutes"] = stored[pid].get("exp_minutes")
    squad = squad_holdings([pid])
    out = _outlook(row, block, gameweek=gameweek,
                   holding=_holding_block(squad["rows"].get(pid),
                                          row.get("price"), squad))
    return envelope(
        "players.json", meta, player=out,
        projection_regime=projection_regime(meta),
        limitations=_component_limitations([block]) + _outlook_limitations())


def compare_players(players: list[str]) -> dict[str, Any]:
    """Two to four players side by side. Differences only — no recommendation."""
    if not isinstance(players, list) or not 2 <= len(players) <= MAX_COMPARE:
        raise ToolError(STATUS_INVALID,
                        f"give between 2 and {MAX_COMPARE} players")
    rows = [_resolve(p) for p in players]
    meta = _meta()
    gameweek = (meta or {}).get("current_gw")
    ids = [int(r["id"]) for r in rows]
    stored = stored_components(ids, gameweek)
    squad = squad_holdings(ids)
    blocks = []
    out = []
    for r in rows:
        pid = int(r["id"])
        block = _component_block(stored.get(pid), have_db=bool(stored),
                                 published_next_gw_xp=r.get("next_gw_xp"))
        if pid in stored:
            block["exp_minutes"] = stored[pid].get("exp_minutes")
        blocks.append(block)
        out.append(_outlook(r, block, gameweek=gameweek,
                            holding=_holding_block(squad["rows"].get(pid),
                                                   r.get("price"), squad)))
    deltas = {}
    for field in ("next_gw_xp", "horizon_xp", "price"):
        vals = [(o["name"], o["projection"].get(field) if field != "price"
                 else o.get("price")) for o in out]
        known = [(n, v) for n, v in vals if isinstance(v, (int, float))]
        if len(known) >= 2:
            hi = max(known, key=lambda t: t[1])
            lo = min(known, key=lambda t: t[1])
            deltas[field] = {"highest": hi[0], "lowest": lo[0],
                             "spread": round(hi[1] - lo[1], 3)}
    # The three-gameweek fixture sum is the quantity that usually decides which
    # of two comparable players is bought FIRST, so it is differenced like the
    # projections rather than left for the caller to add up per player.
    fdr = [(o["name"], (o["fixture_outlook"].get("next3") or {}).get("difficulty_sum"))
           for o in out]
    known_fdr = [(n, v) for n, v in fdr if isinstance(v, (int, float))]
    if len(known_fdr) >= 2:
        deltas["fdr3"] = {
            "easiest": min(known_fdr, key=lambda t: t[1])[0],
            "hardest": max(known_fdr, key=lambda t: t[1])[0],
            "spread": max(v for _, v in known_fdr) - min(v for _, v in known_fdr),
            "note": "sum of the next three gameweeks' difficulty; lower is easier",
        }
    result = envelope(
        "players.json", meta, players=out, differences=deltas,
        projection_regime=projection_regime(meta),
        limitations=[
            *_component_limitations(blocks),
            *_outlook_limitations(),
            "Deterministic differences only. Which player to own depends on "
            "your squad, budget and league, which this tool does not read — "
            "use get_weekly_decision for advice.",
        ])
    # Four full cards do not fit, and a response the client refuses is worse
    # than one that says what it left out. `out` is the same list object the
    # envelope holds, so trimming a card trims the result.
    gone: list[str] = []
    for key in COMPARE_TRIM:
        if serialized_bytes(result) <= MAX_RESULT_BYTES - RESULT_HEADROOM_BYTES:
            break
        for card in out:
            _trim_card(card, key)
        gone.append(key)
        # Written inside the loop, not after it: saying what was dropped costs
        # bytes too, and a trim that stops 23 bytes short because it forgot to
        # count its own explanation is the bug this budget exists to prevent.
        result["projected"] = {
            "dropped": gone,
            "reason": f"{len(out)} full cards exceed the "
                      f"{MAX_RESULT_BYTES:,}-byte response budget",
            "still_available": "get_player_outlook returns any one of them whole",
        }
    return result




def _trim_card(card: dict[str, Any], key: str) -> None:
    """Take one thing off a comparison card.

    `fixture_outlook` is THINNED rather than dropped: the five-gameweek window
    is context and the three-gameweek one is what the transfer turns on, so the
    sum that matters survives every level of trimming.
    """
    if key == "fixture_outlook":
        window = card.get(key)
        if isinstance(window, dict) and "next3" in window:
            card[key] = {"next3": window["next3"]}
        return
    card.pop(key, None)


def _mini(p: Any) -> Any:
    """A player reduced to what a decision needs: who, where, how much — and,
    since the decision is now argued in conversation rather than settled by the
    tool, the evidence the argument turns on.

    `next_gw_xp`, `horizon_xp` and `owned_by` are already on the card
    `plan.json` carries. The heavy parts of that card — rationale, tags,
    fixtures, minutes badge — stay out: repeating them fifteen times per
    gameweek is what made this tool return 74 KB and be refused outright.
    """
    if not isinstance(p, dict):
        return p
    return {k: p.get(k) for k in ("id", "name", "team", "pos", "price",
                                  "next_gw_xp", "horizon_xp", "owned_by")
            if p.get(k) is not None}




def get_transfer_plan(detail: str = DETAIL_SUMMARY,
                      gameweek: int | None = None) -> dict[str, Any]:
    """The multi-gameweek plan: transfers, hits, captain and bench, per week.

    Defaults to a decision-shaped summary. `plan.json` repeats a full fifteen-
    player card set for every gameweek — 74 KB, which the MCP client refuses
    outright — and none of that detail is needed to answer "what is the plan".
    Ask for `detail="gameweek"` with a `gameweek` number to see one week's squad
    in full.
    """
    detail = (detail or DETAIL_SUMMARY).strip().lower()
    if detail not in ALL_DETAIL:
        raise ToolError(STATUS_INVALID,
                        f"detail must be one of {sorted(ALL_DETAIL)}")
    meta = _meta()
    plan = load_artifact("plan.json")
    if not isinstance(plan, dict):
        raise ToolError(STATUS_MALFORMED, "plan.json is not an object")
    steps = plan.get("steps")
    if not isinstance(steps, list):
        raise ToolError(STATUS_MALFORMED, "plan.json carries no steps")

    gws = [s.get("gw") for s in steps if isinstance(s, dict)]
    if detail == DETAIL_GAMEWEEK:
        if gameweek is None:
            raise ToolError(STATUS_INVALID,
                            f"detail='gameweek' needs a gameweek; this plan "
                            f"covers {gws}")
        try:
            want = int(gameweek)
        except (TypeError, ValueError):
            raise ToolError(STATUS_INVALID, "gameweek must be an integer") from None
        chosen = next((s for s in steps if isinstance(s, dict)
                       and s.get("gw") == want), None)
        if chosen is None:
            raise ToolError(STATUS_NOT_FOUND,
                            f"gameweek {want} is not in this plan; it covers {gws}",
                            gameweeks=gws)
        return envelope(
            "plan.json", meta, blob=plan, detail=detail, gameweek=want,
            plan={k: plan.get(k) for k in
                  ("status", "mode", "horizon", "total_expected", "generated_at")},
            step={
                "gw": chosen.get("gw"),
                "transfers_in": [_mini(p) for p in chosen.get("transfers_in") or []],
                "transfers_out": [_mini(p) for p in chosen.get("transfers_out") or []],
                "hits": chosen.get("hits"),
                "free_transfers": chosen.get("free_transfers"),
                "bank": chosen.get("bank"),
                "xi_expected": chosen.get("xi_expected"),
                "captain": _mini(chosen.get("captain")),
                "vice": _mini(chosen.get("vice")),
                "starting": [_mini(p) for p in chosen.get("starting") or []],
                "bench": [_mini(p) for p in chosen.get("bench") or []],
            },
            limitations=[
                "Prices are static across the planning horizon; team-value "
                "growth is not modelled.",
            ])

    def ident(p: Any) -> Any:
        return p.get("id") if isinstance(p, dict) else p

    summary = []
    first_move = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        ins = [_mini(p) for p in step.get("transfers_in") or []]
        outs = [_mini(p) for p in step.get("transfers_out") or []]
        summary.append({
            "gw": step.get("gw"),
            "transfers_in": ins, "transfers_out": outs,
            "hits": step.get("hits"),
            "free_transfers": step.get("free_transfers"),
            "bank": step.get("bank"),
            "xi_expected": step.get("xi_expected"),
            "captain": _mini(step.get("captain")),
            "vice": _mini(step.get("vice")),
            # IDs only: the squad is the same fifteen most weeks, and the cards
            # are what made this artifact unusable.
            "starting_ids": [ident(p) for p in step.get("starting") or []],
            "bench_ids_in_order": [ident(p) for p in step.get("bench") or []],
        })
        if first_move is None and (ins or outs):
            first_move = {"gw": step.get("gw"), "transfers_in": ins,
                          "transfers_out": outs, "hits": step.get("hits")}

    opening = summary[0] if summary else {}
    return envelope(
        "plan.json", meta, blob=plan, detail=detail,
        plan={k: plan.get(k) for k in
              ("status", "mode", "horizon", "total_expected", "generated_at")},
        initial_state={
            "gameweek": opening.get("gw"),
            "free_transfers": opening.get("free_transfers"),
            "bank": opening.get("bank"),
            "squad_known": bool(((load_artifact("decision.json", required=False)
                                  or {}).get("squad_state") or {}).get("known")),
        },
        first_move=first_move,
        no_move_reason=None if first_move else
        "the plan makes no transfer inside its horizon",
        steps=summary,
        detail_available=f"call again with detail='gameweek' and one of {gws} "
                         f"for that week's full squad",
        limitations=[
            "Prices are static across the planning horizon; team-value growth "
            "is not modelled.",
            "Squad membership is given as ids; use detail='gameweek' or "
            "get_player_outlook to resolve them.",
        ])


def _ownership_row(row: Any) -> dict[str, Any]:
    """One league-ownership row: who he is, and how much of the league has him.

    The artifact carries a full player card per row. This keeps the name, the
    price and the projection — enough to act on — and drops the shirt codes,
    because there are up to thirty of these rows in one response.
    """
    if not isinstance(row, dict):
        return {}
    p = row.get("player") or {}
    return {"player_id": row.get("player_id"), "name": p.get("name"),
            "team": p.get("team"), "pos": p.get("pos"), "price": p.get("price"),
            "next_gw_xp": p.get("next_gw_xp"),
            "owners": row.get("owners"), "of_rivals": row.get("n_rivals"),
            "ownership_pct": row.get("ownership_pct"),
            "effective_ownership_pct": row.get("effective_ownership_pct"),
            "captain_eo_pct": row.get("captain_eo_pct")}


def _thin_chips(chips: Any, *, drop_assumptions: bool = False) -> Any:
    """Project the chips block without losing a decision or a reason.

    `candidate` is published as a FULL COPY of whichever entry in `alternatives`
    the chip module picked -- assumptions and all. On 2026-09-01 that copy was
    1,137 bytes of the 18,607 this tool reached, which is more than the 1,107 it
    was over by. Two objects answering "which chip is the candidate" is a
    CARDINALITY problem before it is a size one, so the copy becomes a reference
    and the alternative stays the single source.

    `drop_assumptions` is the second, coarser lever: assumption prose is 843 of
    the 1,040 bytes of one alternative. It is dropped only from alternatives the
    module did NOT pick, never from the candidate, and the response says so.
    Nothing is lost -- `get_weekly_decision` publishes every alternative whole.
    """
    if not isinstance(chips, dict):
        return chips
    out = dict(chips)
    alts, cand = out.get("alternatives"), out.get("candidate")
    if isinstance(alts, list) and isinstance(cand, dict):
        for i, a in enumerate(alts):
            if (isinstance(a, dict)
                    and a.get("chip") == cand.get("chip")
                    and a.get("gameweek") == cand.get("gameweek")):
                out["candidate"] = {
                    "chip": cand.get("chip"),
                    "gameweek": cand.get("gameweek"),
                    "expected_gain": cand.get("expected_gain"),
                    "why_not_recommended": cand.get("why_not_recommended"),
                    "same_as": f"alternatives[{i}]",
                }
                out["candidate_projected"] = (
                    "`candidate` duplicated an entry in `alternatives` verbatim; "
                    "it is published here as a reference. Read "
                    f"alternatives[{i}] for its ci95, baseline and assumptions.")
                break
    if drop_assumptions and isinstance(out.get("alternatives"), list):
        picked = out.get("candidate")
        cand_chip = picked.get("chip") if isinstance(picked, dict) else None
        thinned, dropped = [], 0
        for a in out["alternatives"]:
            if (isinstance(a, dict) and a.get("chip") != cand_chip
                    and a.get("assumptions")):
                a = {k: v for k, v in a.items() if k != "assumptions"}
                dropped += 1
            thinned.append(a)
        if dropped:
            out["alternatives"] = thinned
            out["alternatives_projected"] = (
                f"assumptions dropped from {dropped} alternative(s) the chip "
                "module did not pick, to fit the response budget; the "
                "candidate's are kept, and get_weekly_decision publishes all "
                "of them in full.")
    return out


def get_league_strategy() -> dict[str, Any]:
    """League-scoped ownership — shields, differentials and threats — with placing.

    All four of the league layer's answers, not the one that used to fit:
    `shields` (what protects your position), `differentials` (what can move it),
    `threats` (what your rivals own and you do not — the only one that names a
    move you have not made) and `my_captain_eo_pct` (how much of the league
    captained who you captained, which is what decides whether a differential
    captain pays for its variance).
    """
    meta = _meta()
    strat = load_artifact("strategy.json", required=False)
    if strat is None:
        raise ToolError(STATUS_UNAVAILABLE,
                        "no strategy artifact — this run had no leagues "
                        "configured, or --skip-strategy was used")

    def assemble(rows: int, lean_chips: bool = False) -> dict[str, Any]:
        leagues = []
        for lg in strat.get("leagues") or []:
            block: dict[str, Any] = {
                "league_id": lg.get("league_id"), "name": lg.get("name"),
                "size": lg.get("size"),
                "target_position": lg.get("target_position"),
                "placing": lg.get("placing"),
                "data_quality": lg.get("data_quality"),
                "posture": lg.get("posture"),
                "shields": [_ownership_row(r)
                            for r in (lg.get("shields") or [])[:rows]],
                "differentials": [_ownership_row(r)
                                  for r in (lg.get("differentials") or [])[:rows]],
                "differentials_ranked_by": lg.get("differentials_ranked_by"),
                "differs_from_neutral": lg.get("differs_from_neutral"),
                "difference_reason": lg.get("difference_reason"),
            }
            # Absence stays absence. An empty `threats` list would read as "your
            # rivals own nothing you do not", which is a different claim from
            # "this build did not publish them".
            if "threats" in lg:
                block["threats"] = [_ownership_row(r)
                                    for r in (lg.get("threats") or [])[:rows]]
            else:
                block["threats_unavailable"] = (
                    "this strategy artifact predates the threats field")
            if "my_captain_eo_pct" in lg:
                block["my_captain_eo_pct"] = lg.get("my_captain_eo_pct")
            else:
                block["my_captain_eo_pct_unavailable"] = (
                    "this strategy artifact predates the field")
            leagues.append(block)
        return envelope(
            "strategy.json", meta, blob=strat, leagues=leagues,
            ownership_rows_per_list=rows,
            simulation=strat.get("simulation"),
            chips=_thin_chips(strat.get("chips"),
                              drop_assumptions=lean_chips),
            resolution=strat.get("resolution"), errors=strat.get("league_errors"),
            limitations=[
                *(strat.get("limitations") or []),
                "Ownership here is league-scoped (how many of YOUR rivals own a "
                "player), never the global selected-by percentage.",
                "`threats` are players your rivals own and you do not. They are "
                "an exposure, not a shortlist: a threat you cannot afford and a "
                "threat you should buy look identical here.",
                "`effective_ownership_pct` counts a captain twice, so it can "
                "exceed 100%. `my_captain_eo_pct` is the share of rivals who "
                "captained YOUR captain — at 100% the armband cannot move you.",
                "Differentials all have an effective ownership of exactly zero, "
                "so ownership cannot rank them; they are ordered by projected "
                "points, which `differentials_ranked_by` names.",
            ])

    rows = MAX_OWNERSHIP_ROWS
    out = assemble(rows, False)
    # One league fits comfortably; several do not. Thin the ownership lists
    # rather than return a payload the client refuses, and say it was done.
    #
    # Thinned against the budget MINUS the headroom every other tool keeps, not
    # against the raw cap. Targeting the cap meant this tool stopped shrinking at
    # 19,673 of 20,000 bytes — technically inside it, 327 bytes from unusable,
    # and one more league from breaching it. The cap is where the client refuses;
    # the headroom is where a response is already too big to be safe.
    budget = MAX_RESULT_BYTES - RESULT_HEADROOM_BYTES
    def note_rows(d: dict[str, Any]) -> dict[str, Any]:
        if rows < MAX_OWNERSHIP_ROWS:
            d["ownership_rows_thinned"] = (
                f"the ownership lists were cut to {rows} rows each to fit the "
                f"{budget:,}-byte working budget ({MAX_RESULT_BYTES:,} cap less "
                f"{RESULT_HEADROOM_BYTES:,} bytes of headroom)")
        return d
    while serialized_bytes(out) > budget and rows > 2:
        rows = max(2, rows - 2)
        out = note_rows(assemble(rows, False))
    # Ownership rows are ONE lever and they run out at two. On 2026-09-01 they
    # did: the response stopped shrinking at 18,607 bytes with 1,393 spare, the
    # headroom test failed, and because publishing was gated on the whole suite
    # the WEBSITE stopped updating for 26 hours over an MCP response size. A
    # second lever exists precisely so an exhausted first one cannot do that
    # again -- and it removes a duplicated object rather than real content.
    if serialized_bytes(out) > budget:
        out = note_rows(assemble(rows, True))
    return out




def get_live_gameweek() -> dict[str, Any]:
    """Live scoring, with confirmed, provisional and predicted kept apart."""
    meta = _meta()
    live = load_artifact("live.json", required=False)
    if live is None:
        raise ToolError(STATUS_UNAVAILABLE, "no live artifact has been published")
    if not live.get("available"):
        return envelope("live.json", meta, blob=live, status=STATUS_UNAVAILABLE,
                        available=False,
                        unavailable_reason=live.get("unavailable_reason"),
                        gameweek=live.get("gameweek"))
    return envelope(
        "live.json", meta, blob=live, available=True, gameweek=live.get("gameweek"),
        totals=live.get("totals"), me=live.get("me"), rivals=live.get("rivals"),
        fixtures=live.get("fixture_summary"),
        autosubs=(live.get("me") or {}).get("substitutions"),
        largest_swing=live.get("largest_swing"),
        players_yet_to_play=(live.get("me") or {}).get("yet_to_play"),
        # Everything above is an aggregate, which is precisely the figure a
        # reader is told not to quote while a fixture is in play. Nothing here
        # showed how it was arrived at, so every live conversation left Gaffer
        # and rebuilt the join by hand against the public API. Naming the tool
        # that does show it is what stops the aggregate travelling alone.
        recompute={
            "tool": "get_live_scorecard",
            "why": "the totals here are aggregates. `get_live_scorecard` "
                   "returns the per-player rows behind yours - minutes, FPL's "
                   "own total_points, the multiplier and the product - plus "
                   "hits, so `sum(total_points x multiplier) - hits` can be "
                   "re-added by hand and checked.",
        },
        limitations=[
            "Bonus is PROVISIONAL until every relevant fixture is finished. "
            "Confirmed points, provisional bonus and predicted remaining are "
            "three separate numbers and must not be added into one 'total' "
            "that reads as final.",
            "These are aggregates with no arithmetic attached. Call "
            "`get_live_scorecard` before quoting one: it carries the per-player "
            "rows the total is made of, and the endpoints they came from.",
        ])



def _scorecard_row(p: dict[str, Any], mult: int, captain: Any,
                   subs_in: set[int], subs_out: set[int],
                   scoring: set[int], in_xi: set[int]) -> dict[str, Any]:
    """One line of the recomputation, readable as arithmetic.

    ``total_points`` is FPL's own live figure for the player, untouched: it is
    `event/{gw}/live/` `elements[].stats.total_points` as `live.player_live`
    read it, and it already contains whatever bonus FPL has published. The
    multiplied product sits beside it so a column of them can be re-added by
    hand and checked against the published total.

    The multiplier is Gaffer's, and it is autosub-aware: 0 for a bench player
    who is not scoring, ``autosubs.multiplier`` for whoever actually holds the
    armband, 1 otherwise. Until every relevant fixture is over that is a
    projection of what FPL will do, which ``autosubs.provisional`` states.
    """
    pid = int(p.get("id") or 0)
    points = int(p.get("confirmed") or 0)
    wears_armband = captain is not None and pid == captain and pid in scoring
    if pid not in scoring:
        m = 0
    elif wears_armband:
        m = int(mult)
    else:
        m = 1
    if wears_armband:
        role = "captain"
    elif pid in subs_in:
        role = "autosub_in"
    elif pid in subs_out:
        role = "autosub_out"
    elif pid in in_xi:
        role = "xi"
    elif pid in scoring:
        role = "bench_boost"
    else:
        role = "bench"
    states = p.get("fixture_states") or []
    return {
        "element": pid,
        "name": p.get("name"),
        "pos": p.get("pos"),
        "role": role,
        "minutes": p.get("minutes"),
        "total_points": points,
        "multiplier": m,
        "points": points * m,
        "provisional_bonus": int(p.get("provisional") or 0),
        "predicted_remaining": p.get("predicted"),
        "yet_to_play": bool(p.get("yet_to_play")),
        "fixture_state": "+".join(str(s) for s in states) or None,
    }


def _live_provenance(live: dict[str, Any], meta: Any) -> dict[str, Any]:
    """Which public endpoints these numbers came from, and when they were read.

    ``read_at`` is the live artifact's own `as_of` - the moment the pipeline
    read FPL - never the moment this tool was called. The two are different
    numbers, and conflating them is how a forty-minute-old score gets quoted as
    the current one during a match.
    """
    gw = live.get("gameweek")
    entry = ((live.get("squad") or {}).get("entry_id")
             or (meta or {}).get("entry_id"))
    read_at = live.get("as_of")
    stamp = _parse_ts(read_at)
    return {
        "read_at": read_at,
        "read_seconds_ago": (None if stamp is None
                             else round((_now() - stamp).total_seconds())),
        "endpoints": [
            {"url": f"{FPL_API_BASE}/event/{gw}/live/",
             "supplies": "total_points, minutes and any bonus FPL has awarded, "
                         "per element"},
            {"url": f"{FPL_API_BASE}/entry/{entry}/event/{gw}/picks/",
             "supplies": "the fifteen picks, FPL's own multipliers, the active "
                         "chip, and entry_history.event_transfers_cost (hits)"},
            {"url": f"{FPL_API_BASE}/fixtures/?event={gw}",
             "supplies": "match state, and live BPS for bonus FPL has not "
                         "awarded yet"},
            {"url": f"{FPL_API_BASE}/entry/{entry}/history/",
             "supplies": "the season total carried into this gameweek"},
        ],
        "rival_picks_url_template":
            f"{FPL_API_BASE}/entry/{{entry_id}}/event/{gw}/picks/",
        "how_to_recompute":
            "join picks[].element to event/{gw}/live/ "
            "elements[].stats.total_points, multiply by the multiplier, sum, "
            "subtract entry_history.event_transfers_cost",
    }


def _rival_squads(live: Any) -> list[dict[str, Any]] | None:
    """`rival_squads` as published, or None when the key is absent.

    Absent and empty are different states and this function keeps them apart.
    Absent: the artifact predates the block. Empty: the league's season baseline
    could not be read on this run, which empties `rivals` too — so an empty list
    never means "there are no other managers", and `incomplete` is the field that
    says which it is.
    """
    squads = live.get("rival_squads") if isinstance(live, dict) else None
    if not isinstance(squads, list):
        return None
    return [s for s in squads if isinstance(s, dict)]


def _rival_scorecard(live: dict[str, Any], meta: Any, entry_id: int
                     ) -> dict[str, Any]:
    """One rival's fifteen rows, with the arithmetic that closes on his total.

    Served one manager at a time rather than as a table: all six rivals' rows
    together serialise to about 29 kB against a 20,000-byte budget, and the
    alternative to picking one is truncating a scorecard until it no longer adds
    up — which is the one thing a scorecard may not do.
    """
    squads = _rival_squads(live)
    if squads is None:
        raise ToolError(
            STATUS_UNAVAILABLE,
            "this live artifact carries no `rival_squads`, so no rival's "
            "per-player rows exist in it. Call without `entry_id` for the "
            "aggregates it does publish.",
            unavailable_reason=RIVAL_ROWS_PREDATE)
    if not squads:
        incomplete = live.get("incomplete")
        raise ToolError(
            STATUS_UNAVAILABLE,
            ("`rival_squads` is empty because this run could not read the "
             "league's season baseline — `rivals` is empty for the same reason. "
             "That is a gap in the data, NOT a league with no other managers."
             if incomplete else
             "`rival_squads` is present and empty: no rival squad was scored on "
             "this run."),
            unavailable_reason=(RIVAL_ROWS_INCOMPLETE if incomplete
                                else RIVAL_ROWS_UNAVAILABLE),
            incomplete=incomplete)

    found = next((s for s in squads if s.get("entry_id") == entry_id), None)
    if found is None:
        raise ToolError(
            STATUS_NOT_FOUND,
            f"entry {entry_id} was not scored in this gameweek's league",
            entries_scored=[s.get("entry_id") for s in squads])

    players = [r for r in (found.get("players") or []) if isinstance(r, dict)]
    hits = int(found.get("hits") or 0)
    products = round(sum(float(r.get("product") or 0.0) for r in players), 2)
    published = found.get("gw_points")
    reconciles = (published is None
                  or abs(products - hits - float(published)) < 0.005)

    fx = live.get("fixture_summary") or {}
    subs = found.get("autosubs") or {}
    blockers = []
    if not fx.get("all_finished"):
        blockers.append("not every fixture in this gameweek has finished")
    if not fx.get("bonus_final"):
        blockers.append("bonus is not final in every played fixture")
    if subs.get("provisional"):
        blockers.append("his substitutions are projected rather than applied")

    # A differential id with no row is a real state, not a dropped player: it is
    # a player NEITHER side has live state for, kept in the list because he still
    # separates the two squads. Silently dropping him would understate the gap.
    mine_ids = {r.get("id") for r in (live.get("players") or [])
                if isinstance(r, dict)}
    his_ids = {r.get("element") for r in players}
    diff = [int(d) for d in (found.get("differential") or [])
            if isinstance(d, int)]
    unrowed = [d for d in diff if d not in mine_ids and d not in his_ids]

    me = live.get("me") or {}
    return envelope(
        "live.json", meta, blob=live, available=True,
        gameweek=live.get("gameweek"), active_chip=live.get("active_chip"),
        entry_id=entry_id,
        whose_scorecard="a rival, not you — call without `entry_id` for yours",
        provenance=_live_provenance(live, meta),
        rival={
            "entry_id": found.get("entry_id"),
            "name": found.get("name"),
            "provisional_position": found.get("provisional_position"),
            "gameweek_points": published,
            "hits": hits,
            "players_yet_to_play": found.get("yet_to_play"),
            "autosubs": subs,
        },
        players=players,
        arithmetic={
            "formula": "sum(product) - hits",
            "sum_of_products": products,
            "hits": hits,
            "gameweek_points": published,
            "reconciles_with_published_total": reconciles,
            "discrepancy": (None if published is None
                            else round(products - hits - float(published), 2)),
            "is_final": not blockers,
            "not_final_because": blockers,
        },
        you={
            "entry_id": me.get("entry_id") or (live.get("squad") or {}).get("entry_id"),
            "gameweek_points": me.get("gw_points"),
            "provisional_position": me.get("provisional_position"),
            "players_yet_to_play": me.get("yet_to_play"),
        },
        differential={
            "element_ids": diff,
            "without_a_row": unrowed,
            "note": ("players whose multiplier differs between the two squads — "
                     "the ones who can actually move the gap. An id in "
                     "`without_a_row` is one neither side has live state for; he "
                     "is listed because he still separates you, not because a "
                     "row went missing."),
        },
        rivals_per_player={
            "available": True,
            "detail": "this response IS a rival's per-player breakdown",
            "entries_scored": [s.get("entry_id") for s in squads],
        },
        limitations=[
            "`product` is `(confirmed + provisional) * multiplier` — his "
            "contribution to the CURRENT score. `predicted` is deliberately NOT "
            "in it: `predicted` is the player's OWN share and must be multiplied "
            "by `multiplier` to reach his contribution to the projected total. "
            "The scoreline and the projection are two different sums.",
            "`confirmed` is FPL's own live figure and already contains any bonus "
            "FPL has published; `provisional` is Gaffer's BPS-derived award for a "
            "fixture whose bonus FPL has not published. Adding a bonus column of "
            "your own on top would double-count.",
            "All fifteen rows are here, not the scoring eleven: a benched player "
            "carries multiplier 0 and contributes nothing, but a bench player yet "
            "to play is exactly how an autosub arrives.",
            "A `differential` id may legitimately have no row, when neither side "
            "has live state for that player. Check `differential.without_a_row` "
            "before concluding a player is missing.",
            "When the league's season baseline cannot be read, `rival_squads` is "
            "`[]` alongside `rivals`. Check `incomplete`; an empty list is not "
            "the claim that there are no rivals.",
            "Nothing here is final while `arithmetic.is_final` is false.",
        ])


def get_live_scorecard(entry_id: int | None = None) -> dict[str, Any]:
    """A live score with the rows it is made of: points x multiplier, per player.

    Yours by default. Pass a rival's `entry_id` — the ids are listed in
    `rivals[].entry_id` and in `rivals_per_player.entries` — for HIS fifteen
    rows, scored by the same pass over the same live payload, so the two sides
    of a mini-league can never disagree about a goal that has just gone in. One
    manager at a time: every rival's rows together are about 29 kB against a
    20,000-byte budget.

    An aggregate is the one thing a careful reader is told not to trust while a
    fixture is in play, so `get_live_gameweek` on its own always ended in a
    hand-written join against the public API. This returns the same total *with
    its inputs*: element id, name, minutes, FPL's own `total_points`, the
    multiplier Gaffer applied and the product - plus hits, so
    `sum(total_points x multiplier) - hits` can be re-added straight off the
    response and checked against what is published.

    Nothing here re-scores anything. Every number is lifted from the live
    artifact `gaffer.live` already produced; the one derived column is the
    multiplier, and the sum of the products is reconciled against the published
    total so a disagreement is reported rather than hidden.
    """
    if entry_id is not None:
        if isinstance(entry_id, bool) or not isinstance(entry_id, int):
            raise ToolError(STATUS_INVALID,
                            "entry_id must be an integer FPL entry id")
        if entry_id <= 0:
            raise ToolError(STATUS_INVALID, "entry_id must be a positive integer")

    meta = _meta()
    live = load_artifact("live.json", required=False)
    if live is None:
        raise ToolError(STATUS_UNAVAILABLE, "no live artifact has been published")
    if not live.get("available"):
        return envelope("live.json", meta, blob=live, status=STATUS_UNAVAILABLE,
                        available=False,
                        unavailable_reason=live.get("unavailable_reason"),
                        detail=live.get("note") or "no live data to score",
                        gameweek=live.get("gameweek"))

    if entry_id is not None:
        return _rival_scorecard(live, meta, entry_id)

    rows_in = live.get("players") or []
    squad = live.get("squad") or {}
    subs = squad.get("autosubs") or {}
    if not rows_in or not subs.get("xi"):
        return envelope(
            "live.json", meta, blob=live, status=STATUS_UNAVAILABLE,
            available=False, gameweek=live.get("gameweek"),
            unavailable_reason="no_per_player_rows",
            detail="this live artifact carries no per-player rows, or no "
                   "post-substitution XI, so the arithmetic cannot be shown")

    chip = live.get("active_chip")
    xi = [int(p) for p in subs.get("xi") or []]
    bench = [int(p) for p in subs.get("bench") or []]
    # Bench Boost is the one week the bench scores, so the scoring set is not
    # the XI. `live.score_squad` makes the same distinction; getting it wrong
    # here would silently zero four players in exactly the week they matter.
    scoring = set(xi) | (set(bench) if chip == "bboost" else set())
    captain = subs.get("captain")
    mult = int(subs.get("multiplier") or 1)
    rows = [_scorecard_row(p, mult, captain, set(subs.get("subs_in") or []),
                           set(subs.get("subs_out") or []), scoring, set(xi))
            for p in rows_in if isinstance(p, dict)]

    hits = int(squad.get("hits") or 0)
    products = sum(r["points"] for r in rows)
    published = squad.get("confirmed")
    reconciles = published is None or products == published
    prov = int(squad.get("provisional_bonus") or 0)
    fx = live.get("fixture_summary") or {}
    blockers = []
    if not fx.get("all_finished"):
        blockers.append("not every fixture in this gameweek has finished")
    if not fx.get("bonus_final"):
        blockers.append("bonus is not final in every played fixture")
    if subs.get("provisional"):
        blockers.append("substitutions are projected rather than applied - a "
                        "player still to finish could change who counts")

    arithmetic = {
        "formula": "sum(total_points x multiplier) - hits",
        "sum_of_products": products,
        "hits": hits,
        "gameweek_points_confirmed": products - hits,
        "plus_provisional_bonus": prov,
        "gameweek_points_including_provisional": squad.get("current"),
        "plus_predicted_remaining": squad.get("predicted_remaining"),
        "projected_if_nothing_changes": squad.get("projected"),
        "season_total_before": squad.get("season_total_before"),
        "season_total_including_provisional": squad.get("season_total_projected"),
        # The self-check. `sum_of_products` is re-derived here from the rows the
        # caller can see; `published_confirmed` is what `live.score_squad`
        # computed from the same payload. They are two routes to one number, and
        # a scorecard whose rows do not add up to its own total is worse than no
        # scorecard, so the disagreement is published rather than smoothed over.
        "reconciles_with_published_total": reconciles,
        "published_confirmed": published,
        "discrepancy": (0 if published is None else products - int(published)),
        "is_final": not blockers,
        "not_final_because": blockers,
    }

    my_entry = squad.get("entry_id")
    rival_rows = []
    for r in live.get("rivals") or []:
        if not isinstance(r, dict):
            continue
        rival_rows.append({
            "entry_id": r.get("entry_id"),
            "name": r.get("name"),
            "is_you": bool(r.get("you")) or r.get("entry_id") == my_entry,
            "gameweek_points": r.get("gw_points"),
            "season_total": r.get("current"),
            "projected": r.get("projected"),
            "players_yet_to_play": r.get("yet_to_play"),
            "provisional_position": r.get("provisional_position"),
        })
    seen = [r["entry_id"] for r in rival_rows]
    warnings = []
    dupes = sorted({i for i in seen if seen.count(i) > 1})
    if dupes:
        warnings.append(
            f"the live artifact lists entry {dupes} more than once, so this "
            "league table double-counts a manager and every position below him "
            "is one too low. Read the rows, not the placing.")
    if sum(1 for r in rival_rows if r["is_you"]) > 1:
        warnings.append(
            "more than one row resolves to your own entry - a manager is a "
            "member of his own mini-league and this artifact did not drop him.")

    limitations = [
        "`total_points` is FPL's own live figure and ALREADY contains any bonus "
        "FPL has published. `provisional_bonus` is Gaffer's BPS-derived award "
        "for a fixture whose bonus FPL has NOT published, and is zero wherever "
        "FPL has published one, so the two are never two copies of the same "
        "points - but adding a bonus column of your own on top would "
        "double-count.",
        "`multiplier` is Gaffer's, after autosubs. While "
        "`autosubs.provisional` is true it is a projection of what FPL will do; "
        "FPL's own `picks[].multiplier` still carries the pre-match value and "
        "the two can differ until every relevant fixture is over.",
        "Nothing here is final while `arithmetic.is_final` is false. "
        "`gameweek_points_confirmed`, `plus_provisional_bonus` and "
        "`plus_predicted_remaining` are three separate numbers and must not be "
        "added into one total that reads as settled.",
        ("Rival totals in `rivals` are aggregates. Their arithmetic is one "
         "call away: pass `entry_id` for that manager's fifteen rows."
         if _rival_squads(live) else
         "Per-player rows exist for your squad only. Rival totals are "
         "aggregates Gaffer scored from the same live payload at the same "
         "instant; their arithmetic is not shown because this artifact carries "
         "no rival's picks, and a rival total without `players_yet_to_play` "
         "beside it is not comparable to yours."),
    ]

    # What used to be an unconditional "this cannot be done". It can now, for any
    # artifact carrying `rival_squads`, and a stale impossibility claim is worse
    # than a missing key: it teaches a caller to stop asking.
    squads = _rival_squads(live)
    if squads:
        per_player: dict[str, Any] = {
            "available": True,
            "detail": "call this tool again with `entry_id` set to one of "
                      "`entries` for that manager's fifteen rows, scored by the "
                      "same pass over the same live payload as yours.",
            "entries": [s.get("entry_id") for s in squads],
            "one_at_a_time": "every rival's rows in one response is about 29 kB "
                             f"against the {MAX_RESULT_BYTES:,}-byte budget, so "
                             "they are served per manager rather than truncated "
                             "into a table that no longer adds up.",
        }
    else:
        per_player = {
            "available": False,
            "unavailable_reason": (
                RIVAL_ROWS_PREDATE if squads is None else
                RIVAL_ROWS_INCOMPLETE if live.get("incomplete")
                else RIVAL_ROWS_UNAVAILABLE),
            "detail": ("this live artifact carries no `rival_squads` block"
                       if squads is None else
                       "`rival_squads` is empty because the league's season "
                       "baseline could not be read on this run — `rivals` is "
                       "empty for the same reason, and neither means you have "
                       "no rivals"
                       if live.get("incomplete") else
                       "`rival_squads` is present and empty on this run"),
            "how_to_get_it": "GET provenance.rival_picks_url_template with "
                             "the entry id, then join picks[].element to "
                             "event/{gw}/live/ "
                             "elements[].stats.total_points and multiply by "
                             "picks[].multiplier",
        }

    def build(n_rivals: int) -> dict[str, Any]:
        return envelope(
            "live.json", meta, blob=live, available=True,
            gameweek=live.get("gameweek"), active_chip=chip,
            entry_id=my_entry,
            provenance=_live_provenance(live, meta),
            players=rows,
            arithmetic=arithmetic,
            bench_points=squad.get("bench_points"),
            players_played=squad.get("players_played"),
            players_yet_to_play=squad.get("players_yet_to_play"),
            autosubs={
                "captain": captain,
                "captain_source": subs.get("captain_source"),
                "multiplier": mult,
                "subs_in": subs.get("subs_in"),
                "subs_out": subs.get("subs_out"),
                "provisional": subs.get("provisional"),
                "notes": subs.get("notes"),
                "xi_and_bench_omitted": "spelled out by players[].role",
            },
            rivals=rival_rows[:n_rivals],
            rivals_per_player=per_player,
            data_warnings=warnings,
            limitations=limitations)

    n = MAX_RIVAL_ROWS
    out = build(n)
    # The fifteen player rows are the answer and are never cut. The rival table
    # is a summary of numbers `get_live_gameweek` publishes in full, so it is
    # what gives way first - and it says it gave way rather than going quiet.
    while serialized_bytes(out) > MAX_RESULT_BYTES - RESULT_HEADROOM_BYTES \
            and n > 3:
        n = max(3, n - 3)
        out = build(n)
        out["rivals_thinned"] = (
            f"the rival table was cut to {n} rows to fit the "
            f"{MAX_RESULT_BYTES:,}-byte response budget; `get_live_gameweek` "
            "publishes the full table")
    return out


def _journal_block(event: Any) -> dict[str, Any]:
    """The manager's own record for this gameweek, when a vault is readable.

    Never fatal and never noisy: a machine with no vault (every CI run) gets a
    stated absence, not an error and not an empty object that reads as "he
    wrote nothing".
    """
    try:
        from gaffer import journal
    except ImportError:
        return {"available": False, "reason": "journal module unavailable"}
    try:
        st = journal.status()
        if not st.get("available"):
            return st
        entries = journal.read(event if isinstance(event, int) else None)
        st["entry"] = entries[0] if entries else None
        if not entries:
            st["reason"] = (
                f"no `{journal.FENCE}` block found for GW{event}. Add one to "
                "the gameweek note to record what you actually did and why.")
        return st
    except OSError as exc:
        return {"available": False, "reason": f"journal unreadable: {exc}"}


def get_decision_review() -> dict[str, Any]:
    """Last gameweek judged: decision quality separated from outcome luck."""
    meta = _meta()
    rev = load_artifact("review.json", required=False)
    if rev is None:
        raise ToolError(
            STATUS_UNAVAILABLE,
            "no completed gameweek to review yet — a review is written only "
            "after a gameweek finishes")
    return envelope(
        "review.json", meta, blob=rev, event=rev.get("event"),
        snapshot_as_of=rev.get("snapshot_as_of"),
        comparison=rev.get("comparison"), quality=rev.get("quality"),
        attribution=rev.get("attribution"), lesson=rev.get("lesson"),
        league=rev.get("league"),
        # 1.12 -- the human half of the record. Gaffer knows what it advised and
        # whether it was followed; it does not know what was done INSTEAD or
        # why, and those are the informative rows. Captured in the vault
        # gameweek note, which is written every week anyway and already holds
        # the reasoning, and joined HERE rather than in the pipeline: Actions
        # cannot see the vault, and Gaffer stays read-only.
        journal=_journal_block(rev.get("event")),
        limitations=[
            *(rev.get("limitations") or []),
            "Everything judgemental comes from the immutable pre-deadline "
            "snapshot. Perfect hindsight is shown but never affects the verdict.",
            "`journal` is the manager's own note, joined locally. It is "
            "testimony, not measurement, and Gaffer never scores a decision "
            "on it — but a recorded reason is what separates a good call from "
            "a lucky one when the record is read back.",
        ])


def _summarise_candidates(mc: Any) -> Any:
    """Project `model_candidates` to what a decision needs, losslessly reachable.

    `get_model_evidence` serialised to 19,452 of a 20,000-byte budget — 548 bytes
    of headroom, so one more candidate or limitation string would have stopped the
    tool answering at all. Two things are dropped here and **nothing else**:

    * horizons 2-6 of each candidate's paired comparison, replaced by a summary
      that still carries the two claims the `reason` prose makes — whether the
      candidate loses at every horizon, and how many intervals exclude zero;
    * `current_split_reference`'s `rank_corr` and `mae`, which are **the same
      numbers** this envelope already ships in `honest_metrics`.

    Everything remains available with `detail="full"`. Truncating evidence to fit
    a budget would be the wrong trade; projecting a duplicate is not.
    """
    if not isinstance(mc, dict):
        return mc
    out = dict(mc)

    cands = mc.get("candidates")
    if isinstance(cands, list):
        slim = []
        for c in cands:
            if not isinstance(c, dict):
                slim.append(c)
                continue
            c2 = dict(c)
            ph = c.get("per_horizon")
            if isinstance(ph, dict) and ph:
                diffs, excl = [], 0
                for row in ph.values():
                    if not isinstance(row, dict):
                        continue
                    d = row.get("diff")
                    if isinstance(d, (int, float)):
                        diffs.append(d)
                    ci = row.get("ci95")
                    if (isinstance(ci, (list, tuple)) and len(ci) == 2
                            and all(isinstance(x, (int, float)) for x in ci)
                            and not (ci[0] <= 0 <= ci[1])):
                        excl += 1
                c2["per_horizon"] = {"1": ph.get("1")} if "1" in ph else {}
                c2["per_horizon_summary"] = {
                    "horizons_measured": len(ph),
                    "worst_diff": round(min(diffs), 3) if diffs else None,
                    "best_diff": round(max(diffs), 3) if diffs else None,
                    "intervals_excluding_zero": excl,
                    "detail": "horizons 2-6 omitted; detail='full' returns them",
                }
            slim.append(c2)
        out["candidates"] = slim

    ref = mc.get("current_split_reference")
    if isinstance(ref, dict):
        r2 = {k: v for k, v in ref.items() if k not in ("rank_corr", "mae")}
        if "rank_corr" in ref or "mae" in ref:
            r2["rank_corr_and_mae"] = "see honest_metrics on this envelope"
        out["current_split_reference"] = r2

    return out


def _summarise_minutes(mm: Any) -> Any:
    """The minutes model reduced to what a decision needs, keeping both bands.

    Both band populations survive deliberately: the pool-wide table alone is
    precisely how the CAMEO? band looked calibrated (claims 0.256, realises
    0.269) while being wrong on everyone anybody owns (0.339 against 0.574).
    """
    if not isinstance(mm, dict) or not mm.get("measured"):
        return mm
    ph = mm.get("per_horizon") or {}
    h1 = ph.get("1") or ph.get(1)
    bands = mm.get("bands") or {}
    return {
        "measured": True,
        "verdict": mm.get("verdict"),
        "next_gameweek": h1,
        "bands": {k: bands.get(k) for k in ("overall", "considered")
                  if k in bands},
        "baselines": mm.get("baselines"),
        "branches": mm.get("branches"),
        "candidate_fix": mm.get("candidate_fix"),
        # `limitations` is published at the envelope level, where this module
        # puts every other tool's. Carrying it here as well duplicated 2.3 KB of
        # prose and cost more budget than the whole projection saved.
        "projected": ("decile calibration curves, the remaining band "
                      "populations and horizons 2-6 are omitted here; call "
                      "again with detail='full'"),
    }


def _thin_candidates(mc: Any) -> Any:
    """Keep every candidate's decision and reason; drop its metric block.

    `model_candidates` is FROZEN at 2024-25, measured against a code path that
    was deleted in the same batch that recorded it, and the file says so
    plainly: "Nothing here describes what ships today." Its per-candidate
    numbers are history; its decisions are the part that still governs. Full
    detail remains one `detail="full"` away.
    """
    if not isinstance(mc, dict):
        return mc
    cands = mc.get("candidates")
    if not isinstance(cands, list):
        return mc
    # `candidate` is the identity key the eval harness reads; dropping it made
    # every candidate anonymous and the decisions unattributable.
    keep = ("candidate", "name", "decision", "reason", "outcome", "verdict",
            "status")
    thin = [{k: c.get(k) for k in keep if k in c} if isinstance(c, dict) else c
            for c in cands]
    return {**mc, "candidates": thin,
            "candidates_projected": ("per-candidate metrics omitted; this block "
                                     "is frozen at 2024-25 and describes nothing "
                                     "that ships today. detail='full' returns it")}


def get_model_evidence(detail: str = DETAIL_SUMMARY) -> dict[str, Any]:
    """What the model is actually measured to do, and what was withdrawn.

    Defaults to a projected candidate block that keeps every decision and its
    stated reason. Ask for `detail="full"` for the complete paired comparison at
    all six horizons.
    """
    detail = (detail or DETAIL_SUMMARY).strip().lower()
    if detail not in EVIDENCE_DETAIL:
        raise ToolError(STATUS_INVALID,
                        f"detail must be one of {sorted(EVIDENCE_DETAIL)}")
    meta = _meta()
    bt = load_artifact("backtest.json", required=False)
    if bt is None:
        raise ToolError(STATUS_UNAVAILABLE, "no backtest artifact published")
    from gaffer import backtest as BT
    if bt.get("schema_version") != BT.SCHEMA_VERSION:
        raise ToolError(
            STATUS_UNSUPPORTED,
            f"backtest.json is schema {bt.get('schema_version')}, this build "
            f"reads {BT.SCHEMA_VERSION}", artifact="backtest.json")
    mm = bt.get("minutes_model")
    return envelope(
        "backtest.json", meta, blob=bt,
        # The minutes model has its own tool: named here it costs 3.4 KB over
        # budget, and truncating either model's evidence to fit would be the
        # wrong trade. `blob` is read by `envelope` for versions and never
        # rendered, so a caller needs the pointer.
        minutes_model=("measured separately — call get_minutes_evidence"
                       if isinstance(mm, dict) and mm.get("measured")
                       else "not measured in this artifact"),
        schema_version=bt.get("schema_version"),
        season_tested=bt.get("season"),
        honest_metrics={h: {"mae": b.get("mae"), "rank_corr": b.get("rank_corr")}
                        for h, b in (bt.get("per_horizon") or {}).items()},
        decisions=(bt.get("per_horizon") or {}).get("1", {}).get("decisions"),
        withdrawn_baselines=bt.get("withdrawn_baselines"),
        model_candidates=(bt.get("model_candidates") if detail == DETAIL_FULL
                          else _thin_candidates(
                              _summarise_candidates(bt.get("model_candidates")))),
        detail=detail,
        detail_available=("already the full candidate block"
                          if detail == DETAIL_FULL else
                          "call again with detail='full' for all six horizons "
                          "of each candidate comparison"),
        shipped_projection=bt.get("shipped_projection"),
        ep_next_blend={"weight": config.EP_NEXT_BLEND_WEIGHT,
                       "fitted": config.EP_NEXT_BLEND_IS_FITTED},
        limitations=bt.get("limitations"))


def _decision_snapshots(event, limit: int = 2) -> list[dict[str, Any]]:
    """Pre-deadline decision snapshots for one gameweek, newest first.

    Reads `data/state/decisions.ndjson` -- the committed, immutable store --
    rather than the local sqlite table, which only a pipeline run in this
    checkout ever populates. See the note in `what_changed`.

    A malformed line is skipped rather than fatal: the store is append-only and
    one bad row must not hide the rest.
    """
    path = data_dir() / "state" / "decisions.ndjson"
    if not path.exists():
        path = config.REPO_ROOT / "data" / "state" / "decisions.ndjson"
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if event is not None and str(row.get("target_event")) != str(event):
            continue
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if not isinstance(payload, dict):
            continue
        out.append({"as_of": row.get("as_of"), "payload": payload})
    out.sort(key=lambda r: r.get("as_of") or "", reverse=True)
    return out[:limit]



def what_changed() -> dict[str, Any]:
    """What moved since the previous immutable decision snapshot."""
    meta = _meta()
    current = load_artifact("decision.json", required=False) or {}
    cur_dec = current.get("decision") or {}
    event = current.get("gameweek")

    # 1.11 -- read the COMMITTED snapshot store, not the local database.
    #
    # This queried `decision_snapshots` in `data/gaffer.db`. The pipeline runs
    # in GitHub Actions, and only a run in THIS checkout writes that table, so
    # on the Mac mini it held 15 rows for GW1 and 6 for GW2 and NOTHING for the
    # gameweek being projected. `what_changed` therefore answered
    # "snapshots_found: 0 -- this is the normal state on a first run" for every
    # gameweek after the first, on every machine that had not run the pipeline
    # locally. Measured 2026-09-01: 0 rows found against 26 in the NDJSON.
    #
    # `data/state/decisions.ndjson` is the immutable, version-controlled store
    # the calibration ledger already reads. It travels with the repository, so
    # it is correct everywhere, and ONE store now answers this question.
    rows = _decision_snapshots(event, limit=2)
    if len(rows) < 2:
        return envelope(
            "decision_snapshots", meta, blob=current, status=STATUS_UNAVAILABLE,
            compared=False, snapshots_found=len(rows),
            detail="no prior snapshot for this gameweek, so there is nothing "
                   "to compare against. This is the normal state on a first run.")

    prev_payload = rows[1]["payload"]
    prev = prev_payload.get("decision") or {}

    # The snapshot stores player IDs; the published artifact stores resolved
    # cards. Comparing one against the other reported the captain as changed on
    # every single run — `426` is never equal to `"B.Fernandes"`. Both sides are
    # reduced to an id before comparison, and the id is resolved to a name only
    # for display.
    def ident(v: Any) -> Any:
        return v.get("id") if isinstance(v, dict) else v

    def label(v: Any) -> Any:
        if isinstance(v, dict):
            return v.get("name") or v.get("id")
        return v

    names = {p.get("id"): p.get("name") for p in _players()
             if isinstance(p, dict)} if data_dir().joinpath("players.json").exists() else {}

    def shown(v: Any) -> Any:
        i = ident(v)
        return names.get(i, label(v)) if isinstance(i, int) else label(v)

    changed = []
    for field in ("action", "headline", "captain", "vice", "confidence",
                  "biggest_risk"):
        a, b = prev.get(field), cur_dec.get(field)
        if ident(a) != ident(b):
            changed.append({"field": field, "was": shown(a), "now": shown(b)})
    return envelope(
        "decision_snapshots", meta, blob=prev_payload, compared=True,
        previous_as_of=rows[1]["as_of"], current_as_of=rows[0]["as_of"],
        changed_fields=changed, unchanged=not changed,
        limitations=["Compares the published recommendation, not every input "
                     "that produced it."])


def get_minutes_evidence(detail: str = DETAIL_SUMMARY) -> dict[str, Any]:
    """How well `p_start` predicts who starts, against naive baselines.

    Separate from `get_model_evidence` because they answer different questions
    and together they do not fit one response. Defaults to a projection that
    keeps the verdict, the next gameweek, both band populations and every
    decision; `detail="full"` adds the decile calibration curves and horizons
    2-6.
    """
    detail = (detail or DETAIL_SUMMARY).strip().lower()
    if detail not in EVIDENCE_DETAIL:
        raise ToolError(STATUS_INVALID,
                        f"detail must be one of {sorted(EVIDENCE_DETAIL)}")
    meta = _meta()
    bt = load_artifact("backtest.json", required=False)
    if bt is None:
        raise ToolError(STATUS_UNAVAILABLE, "no backtest artifact published")
    mm = bt.get("minutes_model")
    if not isinstance(mm, dict) or not mm.get("measured"):
        raise ToolError(
            STATUS_UNAVAILABLE,
            "this backtest artifact carries no measured minutes model",
            artifact="backtest.json")
    return envelope(
        "backtest.json", meta, blob=bt,
        schema_version=bt.get("schema_version"),
        season_tested=bt.get("season"),
        minutes_model=(mm if detail == DETAIL_FULL else _summarise_minutes(mm)),
        detail=detail,
        detail_available=("already the full minutes block" if detail == DETAIL_FULL
                          else "call again with detail='full' for the decile "
                               "calibration curves and horizons 2-6"),
        limitations=mm.get("limitations"))


#: `season_calibration` rides on the review artifact; when a build predates it,
#: the distributional half is computed here from the same persisted record the
#: pipeline would have used. Named so a caller can tell the two apart.
CALIBRATION_FROM_ARTIFACT = "review.json"
CALIBRATION_FROM_STATE = "data/state"

#: Per-gameweek rows kept in a calibration response. The tables grow one row a
#: week for a whole season; the verdict, the counts and the caveats never give
#: way, and the oldest rows do.
CALIBRATION_ROWS = 12

#: A projection this large has never been shipped. Rejecting it is cheaper than
#: explaining which bin a 900-point forecast falls into.
MAX_PREDICTION = 100.0


def _thin_calibration(block: Any) -> Any:
    """Cap the two per-gameweek tables. Never a verdict, a count or a caveat.

    The whole point of this block is that no figure can be read without its `n`,
    so the projection here may only ever drop rows a caller can get elsewhere —
    the site renders the full tables from `review.json`.
    """
    if not isinstance(block, dict):
        return block
    out = dict(block)
    for key in ("distribution", "projection"):
        section = out.get(key)
        if not isinstance(section, dict):
            continue
        section = dict(section)
        rows = section.get("per_gameweek")
        if isinstance(rows, list) and len(rows) > CALIBRATION_ROWS:
            section["per_gameweek"] = rows[-CALIBRATION_ROWS:]
            section["per_gameweek_truncated"] = (
                f"showing the {CALIBRATION_ROWS} most recent of {len(rows)} "
                "gameweeks to fit the response budget; every aggregate above is "
                "computed on all of them")
        out[key] = section
    proj = out.get("projection")
    if isinstance(proj, dict) and isinstance(proj.get("appeared"), dict):
        appeared = {k: v for k, v in proj["appeared"].items() if k != "curve"}
        appeared["curve_omitted"] = (
            "the appeared-only curve is conditioned on a post-match fact and is "
            "not the one to read; the pooled `curve` above is")
        out["projection"] = {**proj, "appeared": appeared}
    return out


def get_calibration(prediction: float | None = None) -> dict[str, Any]:
    """How far Gaffer's own published numbers have held up this season, with n.

    Measured from the record the pipeline froze before each deadline — the stored
    outcome distributions and per-player projections — against what happened.
    Distinct from `get_model_evidence`, which grades the model on an archive of
    finished seasons that nothing was ever published against.

    Pass `prediction` to locate a specific projected score on the measured curve.
    Every figure carries its own `n`, and `distribution.reportable` is false until
    the sample can support a claim; that is a real answer, not a placeholder.
    """
    if prediction is not None:
        if isinstance(prediction, bool) or not isinstance(prediction, (int, float)):
            raise ToolError(STATUS_INVALID, "prediction must be a number")
        if not (0.0 <= float(prediction) <= MAX_PREDICTION):
            raise ToolError(
                STATUS_INVALID,
                f"prediction must be between 0 and {MAX_PREDICTION:g} points")

    meta = _meta()
    rev = load_artifact("review.json", required=False)
    stored = rev.get("season_calibration") if isinstance(rev, dict) else None
    if isinstance(stored, dict):
        source, blob, block = CALIBRATION_FROM_ARTIFACT, rev, stored
        if block.get("schema_version") != calibration.SCHEMA_VERSION:
            raise ToolError(
                STATUS_UNSUPPORTED,
                f"review.json carries calibration schema "
                f"{block.get('schema_version')}, this build reads "
                f"{calibration.SCHEMA_VERSION}", artifact="review.json")
    else:
        # A build that predates the block, or a season with no review yet. The
        # persisted record is the same evidence the pipeline would have read, so
        # the distributional half is still answerable; the per-player half needs
        # realised results this server has no way to fetch, by design.
        source, blob = CALIBRATION_FROM_STATE, None
        block = calibration.build_from_state(data_dir() / "state")

    dist = block.get("distribution") or {}
    if int((dist.get("followed_the_advice") or {}).get("n") or 0) == 0 \
            and int((dist.get("every_gameweek") or {}).get("n") or 0) == 0 \
            and (block.get("projection") or {}).get("status") != calibration.STATUS_MEASURED:
        raise ToolError(
            STATUS_UNAVAILABLE,
            "no gameweek has both a frozen pre-deadline distribution and a "
            "finished result yet, so nothing Gaffer published this season can be "
            "scored. This measures the live record only; "
            "`get_model_evidence` reports the historical backtest.")

    curve = ((block.get("projection") or {}).get("curve")) or []
    for_prediction: Any = None
    if prediction is not None:
        found = calibration.lookup_bin(float(prediction), curve)
        for_prediction = found or {
            "prediction": round(float(prediction), 2),
            "nearest_bin": None,
            "within_the_measured_range": False,
            "caveat": ("no per-player calibration curve has been measured this "
                       "season, so there is nothing to place this number on"),
        }

    return envelope(
        source, meta, blob=blob,
        computed_from=("the published review artifact" if blob is not None
                       else "the persisted pre-deadline record, read at call time"),
        headline=block.get("headline"),
        calibration=_thin_calibration(block),
        for_prediction=for_prediction,
        limitations=[
            *(block.get("limitations") or []),
            "This is the live season, not the backtest. `get_model_evidence` "
            "and `get_minutes_evidence` report the archive; they answer a "
            "different question and their sample is far larger.",
            "A figure here is only a finding when its block says `reportable`. "
            "Quote the `n` beside anything you repeat.",
        ])


def internal_clashes(squad: list[dict], fixtures: Any, gw: Any) -> list[dict]:
    """Fixtures in ``gw`` where the squad holds players on both sides.

    Returned per fixture, not per player, because the fixture is the thing that
    correlates them: one goal changes both sides of the row at once.
    """
    if not squad or not isinstance(fixtures, dict) or gw is None:
        return []
    try:
        gw = int(gw)
    except (TypeError, ValueError):
        return []
    mine: dict[str, list[str]] = {}
    for pl in squad:
        if isinstance(pl, dict) and pl.get("team"):
            mine.setdefault(str(pl["team"]), []).append(
                str(pl.get("name") or pl.get("id")))
    out, seen = [], set()
    for team in mine:
        for fx in ((fixtures.get(team) or {}).get("fixtures") or []):
            if not isinstance(fx, dict) or fx.get("gw") != gw:
                continue
            opp = str(fx.get("opp") or "")
            if opp not in mine:
                continue
            key = tuple(sorted((team, opp)))
            if key in seen:
                continue
            seen.add(key)
            home, away = (team, opp) if fx.get("home") else (opp, team)
            out.append({
                "fixture": f"{home} v {away}",
                "yours": {home: sorted(mine[home]), away: sorted(mine[away])},
                "players": len(mine[home]) + len(mine[away]),
            })
    return sorted(out, key=lambda c: -c["players"])


def get_gameweek_brief() -> dict[str, Any]:
    """Where you stand against your league right now, and what is deciding it.

    One call for the question actually asked first -- how am I doing, and how are
    they doing -- rather than six. Deliberately small: it orients, then names the
    tool to call for depth. It does not recommend a move.
    """
    meta = _meta()
    live = load_artifact("live.json", required=False) or {}
    strat = load_artifact("strategy.json", required=False) or {}
    rivals = [r for r in (live.get("rivals") or []) if isinstance(r, dict)]
    me = next((r for r in rivals if r.get("you")), None)
    others = [r for r in rivals if not r.get("you")]

    closest = None
    if me and others:
        near = min(others, key=lambda r: abs((r.get("current") or 0)
                                             - (me.get("current") or 0)))
        gap = round((me.get("current") or 0) - (near.get("current") or 0), 2)
        closest = {
            "name": near.get("name"), "entry_id": near.get("entry_id"),
            "their_total": near.get("current"), "gap": gap,
            "you_are": "ahead" if gap > 0 else "behind" if gap < 0 else "level",
            "their_players_yet_to_play": near.get("yet_to_play"),
        }

    lg = (strat.get("leagues") or [{}])[0] if strat.get("leagues") else {}
    top = lambda k, n=3: [  # noqa: E731
        {"name": (r.get("player") or {}).get("name", r.get("player_id")),
         "owners": r.get("owners"), "of_rivals": r.get("n_rivals")}
        for r in (lg.get(k) or [])[:n]]

    return envelope(
        "live.json", meta, blob=live,
        gameweek={"number": live.get("gameweek"),
                  "deadline": meta.get("deadline"),
                  "projecting": meta.get("current_gw"),
                  # `fixtures` is the per-fixture LIST; the counts live in
                  # `fixture_summary`.
                  "fixtures": (live.get("fixture_summary") or {}).get("by_state"),
                  "bonus_final": (live.get("fixture_summary") or {}).get(
                      "bonus_final")},
        you=({"position": me.get("provisional_position"),
              "gameweek_points": me.get("gw_points"),
              "season_total": me.get("current"),
              "players_yet_to_play": me.get("yet_to_play")} if me else None),
        closest_rival=closest,
        deciding_player=live.get("largest_swing"),
        table=[{"position": r.get("provisional_position"), "name": r.get("name"),
                "gw": r.get("gw_points"), "total": r.get("current"),
                "left": r.get("yet_to_play"), "you": bool(r.get("you"))}
               for r in rivals],
        league={"name": lg.get("name"), "size": lg.get("size"),
                "placing": lg.get("placing"),
                "posture": lg.get("posture")} if lg else None,
        # E5/8. Every projection treats the fifteen as independent. They are
        # not when they are playing each other.
        internal_clashes=internal_clashes(
            (load_artifact("my_team.json", required=False) or {}).get("players")
            or [],
            load_artifact("fixtures.json", required=False),
            meta.get("projection_event") or meta.get("current_gw")),
        threats=top("threats"),
        differentials=top("differentials"),
        where_to_look={
            "the arithmetic behind any total": "get_live_scorecard",
            "a rival's fifteen": "get_live_scorecard(entry_id=...)",
            "this week's single action": "get_weekly_decision",
            "ownership, shields and placing": "get_league_strategy",
            "what moved since the last snapshot": "what_changed",
            "how much to trust a projection": "get_calibration",
        },
        limitations=[
            "Positions are provisional while any fixture is unfinished, and this "
            "is an orientation, not a recommendation.",
            "`gap` is against the CLOSEST rival by season total, which is not "
            "necessarily the one above you.",
            "`internal_clashes` lists fixtures where you hold players on both "
            "sides. Their outcomes are anti-correlated, so a projected total is "
            "more certain than the week will be. It is context, not a signal to "
            "act on.",
            "Recompute any total you intend to act on: get_live_scorecard "
            "publishes the per-player products this is summarised from.",
        ])


#: name -> (callable, one-line description)
TOOLS: dict[str, Any] = {
    "gaffer_status": gaffer_status,
    "get_gameweek_brief": get_gameweek_brief,
    "get_weekly_decision": get_weekly_decision,
    "find_players": find_players,
    "get_player_outlook": get_player_outlook,
    "compare_players": compare_players,
    "get_transfer_plan": get_transfer_plan,
    "get_league_strategy": get_league_strategy,
    "get_live_gameweek": get_live_gameweek,
    "get_live_scorecard": get_live_scorecard,
    "get_decision_review": get_decision_review,
    "get_model_evidence": get_model_evidence,
    "get_minutes_evidence": get_minutes_evidence,
    "get_calibration": get_calibration,
    "what_changed": what_changed,
}


def call(name: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke a tool and turn any failure into a stable structured result.

    Nothing raises out of here: an MCP client showing a stack trace is worse
    than one showing "no gameweek has finished yet".
    """
    fn = TOOLS.get(name)
    if fn is None:
        return {"mcp_schema_version": MCP_SCHEMA_VERSION,
                "status": STATUS_INVALID, "detail": f"no tool named {name!r}",
                "tools": sorted(TOOLS)}
    try:
        return fn(**kwargs)
    except ToolError as exc:
        meta = None
        try:
            meta = _meta()
        except ToolError:
            pass
        out = envelope(name, meta, status=exc.status, detail=exc.detail)
        out.update(exc.extra)
        return out
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
        return {"mcp_schema_version": MCP_SCHEMA_VERSION,
                "status": STATUS_MALFORMED,
                "detail": f"{name} failed: {type(exc).__name__}"}


# ---------------------------------------------------------------------------
# The MCP server
# ---------------------------------------------------------------------------

def build_server() -> Any:
    """Register every tool on an MCPServer. Imported lazily so the module is
    testable, and `--self-test` runnable, without the SDK installed."""
    from mcp.server import MCPServer

    server = MCPServer(SERVER_NAME, instructions=(
        "Read-only access to Gaffer, a Fantasy Premier League decision engine. "
        "Every result comes from an artifact Gaffer's own pipeline generated and "
        "validated. Answer from these results only; do not compute projections, "
        "probabilities or league positions yourself, and do not present a "
        "provisional live score as final. If a tool reports status "
        "'data_unavailable', say so — it usually means the season has not "
        "started or the gameweek has not finished, which is a real answer."
    ))

    @server.tool(description=gaffer_status.__doc__)
    def status() -> dict[str, Any]:
        return call("gaffer_status")

    @server.tool(name="get_weekly_decision",
                 description=get_weekly_decision.__doc__)
    def weekly_decision() -> dict[str, Any]:
        return call("get_weekly_decision")

    @server.tool(name="find_players", description=find_players.__doc__)
    def players_search(query: str = "", team: str = "", position: str = "",
                       limit: int = 10, sort: str = "next_gw_xp",
                       order: str = "desc", min_price: float | None = None,
                       max_price: float | None = None,
                       min_defcon90: float | None = None,
                       min_form: float | None = None,
                       min_xgi90: float | None = None,
                       max_fdr3: float | None = None,
                       price_direction: str = "",
                       available_only: bool = False) -> dict[str, Any]:
        return call("find_players", query=query, team=team, position=position,
                    limit=limit, sort=sort, order=order, min_price=min_price,
                    max_price=max_price, min_defcon90=min_defcon90,
                    min_form=min_form, min_xgi90=min_xgi90, max_fdr3=max_fdr3,
                    price_direction=price_direction,
                    available_only=available_only)

    @server.tool(name="get_player_outlook",
                 description=get_player_outlook.__doc__)
    def player_outlook(player: str) -> dict[str, Any]:
        return call("get_player_outlook", player=player)

    @server.tool(name="compare_players", description=compare_players.__doc__)
    def players_compare(players: list[str]) -> dict[str, Any]:
        return call("compare_players", players=players)

    @server.tool(name="get_transfer_plan", description=get_transfer_plan.__doc__)
    def transfer_plan(detail: str = DETAIL_SUMMARY,
                      gameweek: int | None = None) -> dict[str, Any]:
        return call("get_transfer_plan", detail=detail, gameweek=gameweek)

    @server.tool(name="get_league_strategy",
                 description=get_league_strategy.__doc__)
    def league_strategy() -> dict[str, Any]:
        return call("get_league_strategy")

    @server.tool(name="get_live_gameweek", description=get_live_gameweek.__doc__)
    def live_gameweek() -> dict[str, Any]:
        return call("get_live_gameweek")

    @server.tool(name="get_live_scorecard",
                 description=get_live_scorecard.__doc__)
    def live_scorecard(entry_id: int | None = None) -> dict[str, Any]:
        return call("get_live_scorecard", entry_id=entry_id)

    @server.tool(name="get_decision_review",
                 description=get_decision_review.__doc__)
    def decision_review() -> dict[str, Any]:
        return call("get_decision_review")

    @server.tool(name="get_model_evidence",
                 description=get_model_evidence.__doc__)
    def model_evidence(detail: str = DETAIL_SUMMARY) -> dict[str, Any]:
        return call("get_model_evidence", detail=detail)

    @server.tool(name="get_minutes_evidence",
                 description=get_minutes_evidence.__doc__)
    def minutes_evidence(detail: str = DETAIL_SUMMARY) -> dict[str, Any]:
        return call("get_minutes_evidence", detail=detail)

    @server.tool(name="get_calibration", description=get_calibration.__doc__)
    def in_season_calibration(prediction: float | None = None) -> dict[str, Any]:
        return call("get_calibration", prediction=prediction)

    @server.tool(name="get_gameweek_brief",
                 description=get_gameweek_brief.__doc__)
    def gameweek_brief() -> dict[str, Any]:
        return call("get_gameweek_brief")

    @server.tool(name="what_changed", description=what_changed.__doc__)
    def changed() -> dict[str, Any]:
        return call("what_changed")

    return server


def self_test() -> int:
    """Call every tool once and report. Never reads stdin, always terminates."""
    print(f"gaffer MCP self-test — {MCP_SCHEMA_VERSION}")
    print(f"  data dir : {data_dir()}")
    print(f"  database : {config.DB_PATH}")
    try:
        build_server()
        print("  sdk      : mcp SDK present, server builds")
    except ImportError:
        print("  sdk      : MISSING — pip install -r requirements.lock.txt",
              file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"  sdk      : FAILED to build server: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    args: dict[str, dict[str, Any]] = {
        "find_players": {"query": "a", "limit": 3},
        "get_player_outlook": {"player": "1"},
        "compare_players": {"players": ["1", "2"]},
    }
    bad = 0
    for name in sorted(TOOLS):
        result = call(name, **args.get(name, {}))
        status = result.get("status")
        ok = status in ALL_STATUSES
        if not ok:
            bad += 1
        note = result.get("detail") or ""
        print(f"  {'ok ' if ok else 'BAD'} {name:<22} {status:<18} {note[:60]}")
    print(f"\n{len(TOOLS)} tools, {bad} with an unrecognised status")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m gaffer.mcp_server",
        description="Read-only MCP interface over Gaffer's validated data "
                    "(local stdio only).")
    ap.add_argument("--self-test", action="store_true",
                    help="call every tool once and exit; reads no stdin")
    ap.add_argument("--list-tools", action="store_true",
                    help="print the tool names and exit")
    args = ap.parse_args(argv)

    if args.list_tools:
        for name, fn in sorted(TOOLS.items()):
            print(f"{name:<22} {(fn.__doc__ or '').splitlines()[0]}")
        return 0
    if args.self_test:
        return self_test()

    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
