"""Frozen pre-deadline predictions, so that a gameweek can settle an argument.

A gameweek's pre-deadline state exists for a few hours and is then gone for good.
FPL overwrites it, the pipeline database does not survive the runner, and nobody
can reconstruct afterwards what a model *would* have said — the temptation to
reconstruct it favourably is exactly why it has to be written down first.

So this writes the candidates down BEFORE the deadline and refuses to change them
afterwards. That refusal is the entire feature; everything else here is
bookkeeping.

Each candidate is a full legal 15 chosen by one method, under the real budget,
quota and club limit, so every row is a team you could actually have fielded.
They are scored later from the same live data the site uses.

    python -m gaffer.ledger --freeze          # before the deadline
    python -m gaffer.ledger --score --gw 1    # after the gameweek

What the methods are, and why each is worth a row:

``gaffer``            what ships. The thing being judged.
``gaffer_horizon``    the same optimiser on 5-gameweek value, because the XI it
                      picks is currently a horizon XI (see the roadmap) and the
                      difference should be visible rather than argued about.
``naive_ppg``         last season's points per game. The backtest says this
                      beats the model on rank correlation, 0.666 to 0.440. If it
                      also beats it on real points, that is worth knowing early.
``template``          the most-owned legal squad. The benchmark that matters:
                      FPL is a rank game, so a model that cannot beat the crowd
                      is not earning its keep.
``random``            a legal squad drawn at random. The floor. Without it there
                      is no scale on which to read any of the others.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from gaffer import config
from gaffer.io import write_json_atomic

LEDGER_VERSION = 1

#: A naive baseline should be naive, not silly. `points_per_game` over a
#: three-game cameo is noise, and a human running this strategy would notice, so
#: the same prior-sample rule used everywhere else applies here too.
MIN_SAMPLE_MINUTES = config.BASE_SAMPLE_MINUTES

SQUAD_SIZE = 15
BUDGET = 1000
CLUB_LIMIT = 3
QUOTA = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}


@dataclass
class Entry:
    method: str
    label: str
    objective: str
    squad: list[int]
    xi: list[int]
    bench: list[int]
    captain: int | None
    vice: int | None
    #: What this method believed it would score. Recorded so a wrong forecast
    #: cannot later be described as a right one.
    projected_xi_points: float
    squad_value: float
    names: dict[str, str] = field(default_factory=dict)


def _pool(conn: sqlite3.Connection, gw: int) -> pd.DataFrame:
    """Every selectable player, with one column per candidate method."""
    rows = conn.execute(
        """
        SELECT pl.id, pl.web_name, pl.position AS pos, pl.price AS value,
               pl.team_id, pl.selected_by_pct, pl.points_per_game,
               pl.base_minutes, pl.status,
               COALESCE(pr.exp_points, 0)  AS next_gw_xp
        FROM players pl
        LEFT JOIN projections pr ON pr.player_id = pl.id AND pr.gw = ?
        """,
        (gw,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows]).set_index("id")
    horizon = conn.execute(
        "SELECT player_id, SUM(exp_points) s FROM projections "
        "WHERE gw >= ? GROUP BY player_id", (gw,),
    ).fetchall()
    df["horizon_xp"] = pd.Series(
        {r["player_id"]: r["s"] for r in horizon}).reindex(df.index).fillna(0.0)

    # Naive: last season's points per game, but only where there is a season
    # behind it. Unavailable players are excluded from every method — a squad
    # containing a player nobody could have picked is not a squad.
    df["naive_ppg"] = np.where(
        df["base_minutes"] >= MIN_SAMPLE_MINUTES, df["points_per_game"], 0.0)
    df["template"] = df["selected_by_pct"].fillna(0.0)
    # Reproducible without a clock or a global RNG: the same gameweek always
    # draws the same "random" squad, so the row can be re-derived by anyone.
    df["random"] = np.random.default_rng(20260806 + gw).random(len(df))
    return df[df["status"] == "a"]


def _select_squad(pool: pd.DataFrame, col: str) -> list[int] | None:
    """A legal 15 maximising ``col`` under budget, quota and the club limit."""
    import pulp

    ids = list(pool.index)
    if not ids:
        return None
    prob = pulp.LpProblem("ledger_squad", pulp.LpMaximize)
    x = {i: pulp.LpVariable(f"x{i}", cat="Binary") for i in ids}
    proj, price = pool[col].to_dict(), pool["value"].to_dict()
    pos, club = pool["pos"].to_dict(), pool["team_id"].to_dict()

    prob += pulp.lpSum(x[i] * float(proj[i]) for i in ids)
    prob += pulp.lpSum(x[i] for i in ids) == SQUAD_SIZE
    for p, n in QUOTA.items():
        prob += pulp.lpSum(x[i] for i in ids if pos[i] == p) == n
    prob += pulp.lpSum(x[i] * float(price[i]) for i in ids) <= BUDGET
    for c in {club[i] for i in ids}:
        prob += pulp.lpSum(x[i] for i in ids if club[i] == c) <= CLUB_LIMIT
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None
    return [i for i in ids if x[i].value() and x[i].value() > 0.5]


def _best_xi(pool: pd.DataFrame, squad: list[int], col: str) -> list[int]:
    """The highest-``col`` legal XI from a 15."""
    sub = pool.loc[squad].sort_values(col, ascending=False)
    chosen: list[int] = []
    counts = {p: 0 for p in XI_MIN}
    for p in XI_MIN:
        for idx in sub.index[sub["pos"] == p][: XI_MIN[p]]:
            chosen.append(idx)
            counts[p] += 1
    for idx, row in sub.iterrows():
        if len(chosen) >= 11:
            break
        if idx in chosen:
            continue
        if counts[row["pos"]] < XI_MAX[row["pos"]]:
            chosen.append(idx)
            counts[row["pos"]] += 1
    return chosen


#: (method, human label, the column it maximises, the column its XI is picked on)
METHODS: tuple[tuple[str, str, str, str], ...] = (
    ("gaffer", "Gaffer, next gameweek", "next_gw_xp", "next_gw_xp"),
    ("gaffer_horizon", "Gaffer, 5-gameweek value", "horizon_xp", "horizon_xp"),
    ("naive_ppg", "Last season's points per game", "naive_ppg", "naive_ppg"),
    ("template", "The most-owned legal squad", "template", "template"),
    ("random", "A legal squad at random", "random", "random"),
)


def build_slate(conn: sqlite3.Connection, gw: int, *, deadline: str | None,
                model_version: str, generated_at: str | None = None) -> dict[str, Any]:
    pool = _pool(conn, gw)
    entries: list[Entry] = []
    for method, label, squad_col, xi_col in METHODS:
        squad = _select_squad(pool, squad_col)
        if squad is None:
            continue
        xi = _best_xi(pool, squad, xi_col)
        bench = [i for i in squad if i not in xi]
        # Captaincy is pure expected points on every surface in this project,
        # including here — even for a method that did not select on it.
        order = pool.loc[xi].sort_values("next_gw_xp", ascending=False).index.tolist()
        cap = order[0] if order else None
        vice = order[1] if len(order) > 1 else None
        entries.append(Entry(
            method=method, label=label, objective=squad_col,
            squad=[int(i) for i in squad], xi=[int(i) for i in xi],
            bench=[int(i) for i in bench],
            captain=int(cap) if cap is not None else None,
            vice=int(vice) if vice is not None else None,
            projected_xi_points=round(
                float(pool.loc[xi, "next_gw_xp"].sum()
                      + (pool.at[cap, "next_gw_xp"] if cap is not None else 0.0)), 2),
            squad_value=round(float(pool.loc[squad, "value"].sum()) / 10.0, 1),
            names={str(int(i)): pool.at[i, "web_name"] for i in squad},
        ))
    return {
        "ledger_version": LEDGER_VERSION,
        "gameweek": gw,
        "season": config.SEASON,
        "deadline": deadline,
        "model_version": model_version,
        "frozen_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "entries": [asdict(e) for e in entries],
        "scored": None,
    }


def ledger_path(gw: int, data_dir: Path | str | None = None) -> Path:
    d = Path(data_dir) if data_dir is not None else config.DATA_DIR
    return d / "ledger" / f"gw{gw:02d}.json"


class AlreadyFrozen(RuntimeError):
    """A frozen slate exists. It is evidence, and evidence is not rewritten."""


def _parse(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def freeze(slate: dict[str, Any], path: Path, *,
           now: datetime | None = None, force: bool = False) -> Path:
    """Write a slate. Updatable until the deadline, immutable for ever after.

    This is the whole point of the module, and the boundary is the deadline
    rather than the first write. Before it, a candidate squad is still a
    prediction and a later run simply has better team news, so refreshing it is
    honest. After it, the football has started and every incentive points at
    editing — so nothing may be rewritten, and a slate that has already been
    scored is untouchable regardless.

    ``force`` covers a slate frozen against genuinely wrong inputs. It cannot
    resurrect a scored one.
    """
    now = now or datetime.now(UTC)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("scored"):
            raise AlreadyFrozen(
                f"{path} has been scored. A prediction cannot be revised after "
                f"its result is known, and --force will not do it either.")
        deadline = _parse(slate.get("deadline") or existing.get("deadline"))
        if deadline is not None and now >= deadline and not force:
            raise AlreadyFrozen(
                f"{path} was frozen for a deadline that has passed "
                f"({deadline.isoformat()}). It is evidence now, not a forecast.")
        if deadline is None and not force:
            raise AlreadyFrozen(
                f"{path} exists and carries no deadline, so there is no way to "
                f"tell a refresh from a rewrite. Refusing.")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, slate)
    return path


def score(slate: dict[str, Any], points: dict[int, int],
          minutes: dict[int, int] | None = None) -> dict[str, Any]:
    """Attach actual points to a frozen slate. Appends; never edits a prediction.

    Deliberately simple: XI plus the captain again. No autosubs, because the
    candidates are hypothetical teams with no real bench order, and inventing one
    would put a modelling choice inside the measurement.
    """
    minutes = minutes or {}
    out = dict(slate)
    results = []
    for e in slate["entries"]:
        xi = [int(i) for i in e["xi"]]
        cap = e.get("captain")
        played = sum(1 for i in xi if minutes.get(i, 0) > 0)
        total = sum(points.get(i, 0) for i in xi)
        if cap is not None:
            total += points.get(int(cap), 0)
        results.append({
            "method": e["method"],
            "label": e["label"],
            "actual_xi_points": total,
            "projected_xi_points": e["projected_xi_points"],
            "error": round(total - e["projected_xi_points"], 2),
            "xi_players_who_played": played,
        })
    results.sort(key=lambda r: -r["actual_xi_points"])
    out["scored"] = {
        "scored_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "results": results,
        # One gameweek cannot rank forecasting methods, and the ordering above is
        # the single most misreadable thing this file will ever produce.
        "caveat": "One gameweek is one sample. This ordering is not evidence "
                  "that any method is better than any other; it is one "
                  "observation, and it is only worth anything accumulated over "
                  "a season.",
    }
    return out


def _freeze_cli(args) -> int:
    from gaffer.model import projection
    from gaffer.store import db

    conn = db.connect(Path(args.db) if args.db else None)
    gw = args.gw
    if gw is None:
        row = conn.execute("SELECT value FROM meta WHERE key='projection_event'").fetchone()
        gw = int(row["value"]) if row else 1
    deadline = conn.execute(
        "SELECT value FROM meta WHERE key='deadline'").fetchone()
    slate = build_slate(
        conn, gw, deadline=deadline["value"] if deadline else None,
        model_version=projection.MODEL_VERSION)
    path = ledger_path(gw, args.data_dir)
    try:
        freeze(slate, path, force=args.force)
    except AlreadyFrozen as exc:
        print(f"[ledger] {exc}")
        return 0        # not an error: the slate is already safe
    except Exception as exc:
        # The ledger must never be the reason a refresh fails to publish. A
        # missing week of evidence is a bad day; a pipeline that stops running
        # is a bad season.
        print(f"[ledger] FAILED to freeze GW{gw}: {type(exc).__name__}: {exc}")
        return 0
    print(f"[ledger] froze {len(slate['entries'])} candidates for GW{gw} -> {path}")
    for e in slate["entries"]:
        print(f"  {e['method']:<16} £{e['squad_value']:<6} "
              f"projected {e['projected_xi_points']:>6} "
              f"C={e['names'].get(str(e['captain']), '?')}")
    return 0


def _score_cli(args) -> int:
    path = ledger_path(args.gw, args.data_dir)
    if not path.exists():
        print(f"[ledger] nothing frozen for GW{args.gw} at {path}")
        return 1
    slate = json.loads(path.read_text(encoding="utf-8"))
    live = json.loads(Path(args.live).read_text(encoding="utf-8"))
    pts = {int(e["id"]): int(e.get("total_points", 0)) for e in live.get("elements", [])}
    mins = {int(e["id"]): int(e.get("minutes", 0)) for e in live.get("elements", [])}
    write_json_atomic(path, score(slate, pts, mins))
    for r in json.loads(path.read_text(encoding="utf-8"))["scored"]["results"]:
        print(f"  {r['method']:<16} actual {r['actual_xi_points']:>4}  "
              f"projected {r['projected_xi_points']:>6}  ({r['error']:+.2f})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--gw", type=int, default=None)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--live", default=None, help="event/{gw}/live payload, for --score")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a slate frozen in error, before any kickoff")
    args = ap.parse_args(argv)
    if args.score:
        if args.gw is None or not args.live:
            ap.error("--score needs --gw and --live")
        return _score_cli(args)
    return _freeze_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
