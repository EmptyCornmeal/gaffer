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


def envelope(source_artifact: str, meta: Any, *, status: str = STATUS_OK,
             limitations: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """The fields every tool result carries, so no answer arrives undated."""
    meta = meta if isinstance(meta, dict) else {}
    out: dict[str, Any] = {
        "mcp_schema_version": MCP_SCHEMA_VERSION,
        "status": status,
        "season": meta.get("season"),
        "as_of": meta.get("generated_at"),
        "source_artifact": source_artifact,
        "freshness": freshness(meta),
        "versions": {
            "model_version": meta.get("model_version"),
            "objective_version": meta.get("objective_version"),
            "sim_version": meta.get("sim_version"),
        },
    }
    if limitations:
        out["limitations"] = limitations
    out.update(extra)
    return out


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
        "meta.json", meta,
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
        "decision.json", meta,
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


def _outlook(row: dict[str, Any]) -> dict[str, Any]:
    """The published projection, with the model's own number and FPL's apart."""
    return {
        "id": row.get("id"), "name": row.get("name"), "team": row.get("team"),
        "pos": row.get("pos"), "price": row.get("price"),
        "projection": {
            "next_gw_xp": row.get("next_gw_xp"),
            "horizon_xp": row.get("horizon_xp"),
            "model_only": row.get("next_gw_xp_model"),
            "fpl_ep_next": row.get("ep_next"),
            "blend_weight": config.EP_NEXT_BLEND_WEIGHT,
            "blend_is_fitted": config.EP_NEXT_BLEND_IS_FITTED,
        },
        "minutes": {"p_start": row.get("p_start"),
                    "exp_minutes": row.get("exp_minutes"),
                    "badge": row.get("xmins_badge")},
        "availability": {"status": row.get("status"),
                         "chance_playing": row.get("chance_playing"),
                         "news": row.get("news")},
        "uncertainty": {"confidence": row.get("confidence"),
                        "floor": row.get("floor"), "ceiling": row.get("ceiling"),
                        "boom_pct": row.get("boom_pct")},
        "ownership": {"global_pct": row.get("owned_by")},
        "rationale": row.get("rationale"),
        "tags": row.get("tags"),
        "fixtures": row.get("fixtures"),
    }


def get_player_outlook(player: str) -> dict[str, Any]:
    """One player's current structured projection, fixtures and availability."""
    row = _resolve(player)
    return envelope(
        "players.json", _meta(), player=_outlook(row),
        limitations=[
            "`next_gw_xp` is a blend of Gaffer's own model and FPL's `ep_next` "
            "at an UNFITTED weight; `model_only` and `fpl_ep_next` are the two "
            "components, kept separate.",
            "Beyond the next gameweek the projection is Gaffer's model alone, "
            "and it is materially weaker there.",
        ])


def compare_players(players: list[str]) -> dict[str, Any]:
    """Two to four players side by side. Differences only — no recommendation."""
    if not isinstance(players, list) or not 2 <= len(players) <= MAX_COMPARE:
        raise ToolError(STATUS_INVALID,
                        f"give between 2 and {MAX_COMPARE} players")
    rows = [_resolve(p) for p in players]
    out = [_outlook(r) for r in rows]
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
        "players.json", _meta(), players=out, differences=deltas,
        limitations=[
            "Deterministic differences only. Which player to own depends on "
            "your squad, budget and league, which this tool does not read — "
            "use get_weekly_decision for advice.",
        ])


def get_transfer_plan() -> dict[str, Any]:
    """The multi-gameweek plan, with its costs and assumptions."""
    meta = _meta()
    plan = load_artifact("plan.json")
    if not isinstance(plan, dict):
        raise ToolError(STATUS_MALFORMED, "plan.json is not an object")
    return envelope("plan.json", meta, plan=plan,
                    limitations=["Prices are static across the planning "
                                 "horizon; team-value growth is not modelled."])


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
        "strategy.json", meta, leagues=leagues,
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
        return envelope("live.json", meta, status=STATUS_UNAVAILABLE,
                        available=False,
                        unavailable_reason=live.get("unavailable_reason"),
                        gameweek=live.get("gameweek"))
    return envelope(
        "live.json", meta, available=True, gameweek=live.get("gameweek"),
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
        "review.json", meta, event=rev.get("event"),
        snapshot_as_of=rev.get("snapshot_as_of"),
        comparison=rev.get("comparison"), quality=rev.get("quality"),
        attribution=rev.get("attribution"), lesson=rev.get("lesson"),
        league=rev.get("league"),
        limitations=[
            *(rev.get("limitations") or []),
            "Everything judgemental comes from the immutable pre-deadline "
            "snapshot. Perfect hindsight is shown but never affects the verdict.",
        ])


def get_model_evidence() -> dict[str, Any]:
    """What the model is actually measured to do, and what was withdrawn."""
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
        "backtest.json", meta,
        schema_version=bt.get("schema_version"),
        season_tested=bt.get("season"),
        honest_metrics={h: {"mae": b.get("mae"), "rank_corr": b.get("rank_corr")}
                        for h, b in (bt.get("per_horizon") or {}).items()},
        decisions=(bt.get("per_horizon") or {}).get("1", {}).get("decisions"),
        withdrawn_baselines=bt.get("withdrawn_baselines"),
        model_candidates=bt.get("model_candidates"),
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
        return envelope("decision.json", meta, status=STATUS_UNAVAILABLE,
                        compared=False,
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
            "decision_snapshots", meta, status=STATUS_UNAVAILABLE,
            compared=False, snapshots_found=len(rows),
            detail="no prior snapshot for this gameweek, so there is nothing "
                   "to compare against. This is the normal state on a first run.")

    try:
        prev = json.loads(rows[1]["payload"]).get("decision") or {}
    except ValueError:
        raise ToolError(STATUS_MALFORMED, "the prior snapshot is unreadable") from None

    changed = []
    for field in ("action", "headline", "captain", "vice", "confidence",
                  "biggest_risk"):
        a, b = prev.get(field), cur_dec.get(field)
        a_v = a.get("name") if isinstance(a, dict) else a
        b_v = b.get("name") if isinstance(b, dict) else b
        if a_v != b_v:
            changed.append({"field": field, "was": a_v, "now": b_v})
    return envelope(
        "decision_snapshots", meta, compared=True,
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
    def transfer_plan() -> dict[str, Any]:
        return call("get_transfer_plan")

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
    def model_evidence() -> dict[str, Any]:
        return call("get_model_evidence")

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
