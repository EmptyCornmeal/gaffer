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

from gaffer import config

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
#: contents were. The budget is asserted for all eleven tools in
#: `tests/test_mcp_server.py`; it is a design constraint on what each tool
#: *returns*, never a blind truncation of what it built.
MAX_RESULT_BYTES = 20_000

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

def gaffer_status() -> dict[str, Any]:
    """Season, gameweek, deadline, freshness, build mode and squad state."""
    meta = _meta()
    decision = load_artifact("decision.json", required=False) or {}
    squad = (decision.get("squad_state") or {})
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
        artifacts=sorted(p.name for p in data_dir().glob("*.json")),
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
COMPONENTS_NO_ROW = "no_projection_row_for_this_gameweek"
COMPONENTS_NOT_STORED = "not_stored_in_this_record"


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


def _component_block(row: dict[str, Any] | None, have_db: bool) -> dict[str, Any]:
    """`model_only` / `fpl_ep_next` plus an explicit availability statement.

    A stored `0.0` is a real value, not a missing one: the pipeline skips the
    blend when `ep_next` is zero, and forty-two players are in exactly that
    state right now.
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
    return out


def _players() -> list[dict[str, Any]]:
    blob = load_artifact("players.json")
    if not isinstance(blob, list):
        raise ToolError(STATUS_MALFORMED, "players.json is not a list")
    return blob


def find_players(query: str = "", team: str = "", position: str = "",
                 limit: int = 10) -> dict[str, Any]:
    """Bounded name/team/position search over the published player list."""
    if not any((query, team, position)):
        raise ToolError(STATUS_INVALID,
                        "give at least one of query, team or position")
    if len(query) > MAX_QUERY_LENGTH:
        raise ToolError(STATUS_INVALID,
                        f"query is capped at {MAX_QUERY_LENGTH} characters")
    limit = max(1, min(int(limit or 10), MAX_SEARCH_RESULTS))
    q, t = query.strip().lower(), team.strip().upper()
    p = position.strip().upper()
    if p and p not in ("GKP", "DEF", "MID", "FWD"):
        raise ToolError(STATUS_INVALID, "position must be GKP, DEF, MID or FWD")

    hits = []
    for row in _players():
        if q and q not in str(row.get("name", "")).lower():
            continue
        if t and str(row.get("team", "")).upper() != t:
            continue
        if p and str(row.get("pos", "")).upper() != p:
            continue
        hits.append({k: row.get(k) for k in
                     ("id", "name", "team", "pos", "price", "next_gw_xp",
                      "horizon_xp", "status", "owned_by")})
    hits.sort(key=lambda r: -(r.get("next_gw_xp") or 0))
    out = envelope(
        "players.json", _meta(),
        status=STATUS_OK if hits else STATUS_NOT_FOUND,
        matched=len(hits), truncated=len(hits) > limit,
        players=hits[:limit],
        query={"query": query, "team": team, "position": position, "limit": limit},
    )
    if not hits:
        out["detail"] = ("no player matched "
                         + ", ".join(f"{k}={v!r}" for k, v in
                                     (("query", query), ("team", team),
                                      ("position", position)) if v))
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
             ) -> dict[str, Any]:
    """The published projection, with the model's own number and FPL's apart.

    Every nullable field here is either a real stored value or accompanied by a
    reason. Nothing promises a separation it does not deliver.
    """
    dist = row.get("dist") if isinstance(row.get("dist"), dict) else {}
    proj: dict[str, Any] = {
        "next_gw_xp": row.get("next_gw_xp"),
        "horizon_xp": row.get("horizon_xp"),
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
    return {
        "id": row.get("id"), "name": row.get("name"), "team": row.get("team"),
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
        "ownership": {"global_pct": row.get("owned_by")},
        "rationale": row.get("rationale"),
        "tags": row.get("tags"),
        "fixtures": row.get("fixtures"),
    }


def _component_limitations(blocks: list[dict[str, Any]]) -> list[str]:
    """Say what the components are, or why they are not there. Never both."""
    have = [b for b in blocks if b.get("components_available")]
    if have and len(have) == len(blocks):
        return [
            "`next_gw_xp` is a blend of Gaffer's own model (`model_only`) and "
            "FPL's `ep_next` (`fpl_ep_next`). Both are read from the stored "
            "projection, not reconstructed from the blend.",
            f"The blend weight is a NOMINAL {config.EP_NEXT_BLEND_WEIGHT}, "
            "scaled down by Gaffer's own availability read, and not applied at "
            "all when `fpl_ep_next` is zero. It is UNFITTED — see "
            "get_model_evidence.",
            "Beyond the next gameweek the projection is Gaffer's model alone, "
            "and it is materially weaker there.",
        ]
    reasons = sorted({b.get("unavailable_reason") for b in blocks
                      if not b.get("components_available")} - {None})
    return [
        "`model_only` and `fpl_ep_next` are NOT available in this record "
        f"({', '.join(reasons) or 'unknown'}), so `next_gw_xp` is reported "
        "as a single blended number. They are never derived backwards from it.",
        f"The blend weight is a NOMINAL {config.EP_NEXT_BLEND_WEIGHT} and is "
        "UNFITTED — see get_model_evidence.",
        "Beyond the next gameweek the projection is Gaffer's model alone, "
        "and it is materially weaker there.",
    ]


def get_player_outlook(player: str) -> dict[str, Any]:
    """One player's current structured projection, fixtures and availability."""
    row = _resolve(player)
    meta = _meta()
    stored = stored_components([int(row["id"])], (meta or {}).get("current_gw"))
    block = _component_block(stored.get(int(row["id"])), have_db=bool(stored))
    if int(row["id"]) in stored:
        block["exp_minutes"] = stored[int(row["id"])].get("exp_minutes")
    out = _outlook(row, block)
    return envelope(
        "players.json", meta, player=out,
        limitations=_component_limitations([block]))


def compare_players(players: list[str]) -> dict[str, Any]:
    """Two to four players side by side. Differences only — no recommendation."""
    if not isinstance(players, list) or not 2 <= len(players) <= MAX_COMPARE:
        raise ToolError(STATUS_INVALID,
                        f"give between 2 and {MAX_COMPARE} players")
    rows = [_resolve(p) for p in players]
    meta = _meta()
    ids = [int(r["id"]) for r in rows]
    stored = stored_components(ids, (meta or {}).get("current_gw"))
    blocks = []
    out = []
    for r in rows:
        pid = int(r["id"])
        block = _component_block(stored.get(pid), have_db=bool(stored))
        if pid in stored:
            block["exp_minutes"] = stored[pid].get("exp_minutes")
        blocks.append(block)
        out.append(_outlook(r, block))

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
    return envelope(
        "players.json", meta, players=out, differences=deltas,
        limitations=[
            *_component_limitations(blocks),
            "Deterministic differences only. Which player to own depends on "
            "your squad, budget and league, which this tool does not read — "
            "use get_weekly_decision for advice.",
        ])


def _mini(p: Any) -> Any:
    """A player reduced to what a decision needs: who, where, how much."""
    if not isinstance(p, dict):
        return p
    return {k: p.get(k) for k in ("id", "name", "team", "pos", "price")
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


def get_league_strategy() -> dict[str, Any]:
    """League-scoped ownership, placing probabilities and their data quality."""
    meta = _meta()
    strat = load_artifact("strategy.json", required=False)
    if strat is None:
        raise ToolError(STATUS_UNAVAILABLE,
                        "no strategy artifact — this run had no leagues "
                        "configured, or --skip-strategy was used")
    leagues = []
    for lg in strat.get("leagues") or []:
        leagues.append({
            "league_id": lg.get("league_id"), "name": lg.get("name"),
            "size": lg.get("size"), "target_position": lg.get("target_position"),
            "placing": lg.get("placing"),
            "data_quality": lg.get("data_quality"),
            "posture": lg.get("posture"),
            "differentials": lg.get("differentials"),
            "differs_from_neutral": lg.get("differs_from_neutral"),
            "difference_reason": lg.get("difference_reason"),
        })
    return envelope(
        "strategy.json", meta, blob=strat, leagues=leagues,
        simulation=strat.get("simulation"), chips=strat.get("chips"),
        resolution=strat.get("resolution"), errors=strat.get("league_errors"),
        limitations=[
            *(strat.get("limitations") or []),
            "Ownership here is league-scoped (how many of YOUR rivals own a "
            "player), never the global selected-by percentage.",
        ])


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
        limitations=[
            "Bonus is PROVISIONAL until every relevant fixture is finished. "
            "Confirmed points, provisional bonus and predicted remaining are "
            "three separate numbers and must not be added into one 'total' "
            "that reads as final.",
        ])


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
        limitations=[
            *(rev.get("limitations") or []),
            "Everything judgemental comes from the immutable pre-deadline "
            "snapshot. Perfect hindsight is shown but never affects the verdict.",
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
    return envelope(
        "backtest.json", meta, blob=bt,
        schema_version=bt.get("schema_version"),
        season_tested=bt.get("season"),
        honest_metrics={h: {"mae": b.get("mae"), "rank_corr": b.get("rank_corr")}
                        for h, b in (bt.get("per_horizon") or {}).items()},
        decisions=(bt.get("per_horizon") or {}).get("1", {}).get("decisions"),
        withdrawn_baselines=bt.get("withdrawn_baselines"),
        model_candidates=(bt.get("model_candidates") if detail == DETAIL_FULL
                          else _summarise_candidates(bt.get("model_candidates"))),
        detail=detail,
        detail_available=("already the full candidate block"
                          if detail == DETAIL_FULL else
                          "call again with detail='full' for all six horizons "
                          "of each candidate comparison"),
        shipped_projection=bt.get("shipped_projection"),
        ep_next_blend={"weight": config.EP_NEXT_BLEND_WEIGHT,
                       "fitted": config.EP_NEXT_BLEND_IS_FITTED},
        limitations=bt.get("limitations"))


def what_changed() -> dict[str, Any]:
    """What moved since the previous immutable decision snapshot."""
    meta = _meta()
    current = load_artifact("decision.json", required=False) or {}
    cur_dec = current.get("decision") or {}
    event = current.get("gameweek")

    try:
        conn = read_only_db()
    except ToolError:
        return envelope("decision.json", meta, blob=current,
                        status=STATUS_UNAVAILABLE, compared=False,
                        detail="no local database, so no prior snapshot exists")
    try:
        rows = conn.execute(
            "SELECT as_of, payload FROM decision_snapshots "
            "WHERE target_event = ? ORDER BY as_of DESC LIMIT 2",
            (event,)).fetchall()
    except sqlite3.Error as exc:
        raise ToolError(STATUS_UNAVAILABLE,
                        f"snapshot table unreadable ({type(exc).__name__})") from None
    finally:
        conn.close()

    if len(rows) < 2:
        return envelope(
            "decision_snapshots", meta, blob=current, status=STATUS_UNAVAILABLE,
            compared=False, snapshots_found=len(rows),
            detail="no prior snapshot for this gameweek, so there is nothing "
                   "to compare against. This is the normal state on a first run.")

    try:
        prev_payload = json.loads(rows[1]["payload"])
        prev = prev_payload.get("decision") or {}
    except ValueError:
        raise ToolError(STATUS_MALFORMED, "the prior snapshot is unreadable") from None

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


#: name -> (callable, one-line description)
TOOLS: dict[str, Any] = {
    "gaffer_status": gaffer_status,
    "get_weekly_decision": get_weekly_decision,
    "find_players": find_players,
    "get_player_outlook": get_player_outlook,
    "compare_players": compare_players,
    "get_transfer_plan": get_transfer_plan,
    "get_league_strategy": get_league_strategy,
    "get_live_gameweek": get_live_gameweek,
    "get_decision_review": get_decision_review,
    "get_model_evidence": get_model_evidence,
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
                       limit: int = 10) -> dict[str, Any]:
        return call("find_players", query=query, team=team, position=position,
                    limit=limit)

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

    @server.tool(name="get_decision_review",
                 description=get_decision_review.__doc__)
    def decision_review() -> dict[str, Any]:
        return call("get_decision_review")

    @server.tool(name="get_model_evidence",
                 description=get_model_evidence.__doc__)
    def model_evidence(detail: str = DETAIL_SUMMARY) -> dict[str, Any]:
        return call("get_model_evidence", detail=detail)

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
