"""End-to-end pipeline: ingest -> project -> optimise -> export JSON.

Run with::

    python -m gaffer.pipeline            # full run
    python -m gaffer.pipeline --fast     # skip per-player DEFCON enrichment

Intended to run on a schedule (Mac Mini launchd), committing ``data/*.json`` for
the GitHub Pages front-end.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime

from gaffer import config, ingest, strategy, weekly
from gaffer import live as live_mod
from gaffer import review as review_mod
from gaffer.export import artifacts
from gaffer.fpl.client import FplClient
from gaffer.model import projection, scenarios, simulate
from gaffer.solver import multiperiod, optimize
from gaffer.store import db


def run(
    fast: bool = False, horizon: int | None = None, now: datetime | None = None,
    skip_strategy: bool = False, dry_run: bool = False,
) -> dict[str, object]:
    """``now`` overrides the clock for gameweek resolution (tests only)."""
    horizon = horizon or config.PROJECTION_HORIZON
    t0 = time.time()
    log: dict[str, object] = {}

    # Fail before doing any work if artifacts would land outside the checkout.
    # A run that writes into site-packages looks successful and publishes nothing.
    config.verify_publish_paths()
    settings = config.Settings.load()
    generated_at = artifacts.run_timestamp()
    log["paths"] = config.describe_paths()
    log["build_mode"] = settings.build_mode
    log["generated_at"] = generated_at

    log["ingest"] = ingest.run(skip_enrich=fast, now=now)

    conn = db.connect()
    from_gw = int(db.get_meta(conn, "current_gw") or 1)
    log["from_gw"] = from_gw

    log["projection_rows"] = projection.project(conn, from_gw, horizon)

    # Monte-Carlo next-GW distribution (floor/ceiling/boom%) over the same rates.
    distributions = simulate.simulate_next_gw(conn, from_gw)
    log["simulated"] = len(distributions)

    ft = _free_transfers(conn)
    # T-14: the headline solve is now pure expected points. The ownership dial is
    # neutralised (see optimize.RISK_WEIGHTS) until league-specific objectives
    # exist, so the base recommendation cannot be distorted by global popularity.
    risk_weights = optimize.RISK_WEIGHTS
    sol = optimize.optimise(
        conn, from_gw, horizon, free_transfers=ft,
        template_weight=risk_weights["balanced"], distributions=distributions,
    )
    log["solver"] = {
        "mode": sol.meta.get("mode"), "status": sol.status,
        "formation": sol.formation, "xi_expected": sol.xi_expected,
        "transfers_in": len(sol.transfers_in), "hits": sol.hits,
    }

    # Grid of optimal squads: 3 planning windows × 3 risk stances. Each is a full
    # re-solve; the Planner toggles both (window rewards durable picks, stance
    # trades rank-safety off against differential value).
    horizon_solutions = {
        h: {
            r: optimize.optimise(
                conn, from_gw, h, free_transfers=ft,
                template_weight=w, distributions=distributions,
            )
            for r, w in risk_weights.items()
        }
        for h in (1, 3, 5)
    }

    # Multi-GW transfer path: the optimal *sequence* of moves (when to transfer,
    # roll a free transfer, or take a -4) across a 5-GW window — the planner half
    # of the engine, distinct from the single-window optimal squad above.
    plan_horizon = min(5, horizon)
    plan = multiperiod.optimise_path(conn, from_gw, horizon=plan_horizon, free_transfers=ft)
    log["plan"] = {
        "status": plan.status, "mode": plan.meta.get("mode"),
        "total_expected": plan.total_expected,
        "moves": sum(len(s.transfers_in) for s in plan.steps),
    }

    # T-16/17/18/20/21: one ScenarioSet for the entire run. The weekly decision, the league placing
    # probabilities and the chip values are all measured in this same simulated
    # football — three separate draws would let sampling noise look like an edge.
    scen = scenarios.simulate(conn, from_gw)
    log["scenarios"] = f"{scen.n_sims} sims, seed {scen.seed}"

    strat = None
    if not skip_strategy:
        with FplClient() as client:
            squad_event = client.readable_squad_event(now)
            try:
                strat = strategy.build(
                    conn, client, settings, from_gw=from_gw,
                    squad_event=squad_event, sol=sol, distributions=distributions,
                    generated_at=generated_at, scen=scen,
                )
            except Exception as exc:  # noqa: BLE001 - never lose the run to this
                strat = {
                    "strategy_version": strategy.STRATEGY_VERSION,
                    "generated_at": generated_at, "gameweek": from_gw,
                    "error": f"{type(exc).__name__}: {exc}",
                    "leagues": [], "options": [], "league_errors": [],
                    "resolution": {"default": None, "reason": "strategy build failed",
                                   "shortlist": [], "conflicts": []},
                }
    log["strategy"] = (
        "skipped" if strat is None
        else f"{len(strat.get('leagues', []))} league(s), "
             f"chip: {(strat.get('chips') or {}).get('recommendation', 'n/a')}"
    )

    # T-21: the one weekly decision, plus its immutable pre-deadline snapshot.
    dec = weekly.build(conn, sol=sol, from_gw=from_gw, horizon=horizon,
                       scen=scen, settings=settings, strategy=strat)
    payload = weekly.snapshot_payload(
        conn, dec, from_gw=from_gw, horizon=horizon, scen=scen,
        settings=settings, strategy=strat, generated_at=generated_at)
    # The distribution the review will score the outcome against. It must be
    # stored BEFORE the deadline or decision quality cannot be measured later.
    payload["outcome_distribution"] = _outcome_distribution(scen, dec)
    snap = weekly.persist(
        conn, payload, entry_id=settings.entry_id, target_event=from_gw,
        deadline=db.get_meta(conn, "deadline"), now=now)
    log["decision"] = f"{dec.action} ({dec.confidence} confidence)"
    log["snapshot"] = snap["outcome"]

    # T-22: live gameweek. Between deadlines this is an honest "not started".
    live_state = None
    if not skip_strategy:
        with FplClient() as client:
            live_state = _build_live(conn, client, settings, from_gw, now,
                                     generated_at)
    log["live"] = (
        "skipped" if live_state is None
        else f"available={live_state.get('available')} "
             f"({live_state.get('unavailable_reason') or 'scored'})")

    # T-23: review of the last finished gameweek, if one exists and a snapshot
    # was recorded for it. Never fabricated.
    review_state = None
    if not skip_strategy:
        with FplClient() as client:
            review_state = _build_review(conn, client, settings, now)
    log["review"] = "none" if review_state is None else f"GW{review_state['event']}"

    # T-24: notifications. Dry-run — this resolves and records, and sends nothing.
    notif = _evaluate_notifications(conn, settings, dec, strat, live_state, now,
                                    generated_at)
    log["notifications"] = (
        f"{notif['result']['new']} new, {notif['result']['suppressed']} quiet, "
        f"dry_run={notif['result']['dry_run']}")

    written = artifacts.write_all(
        conn, sol, from_gw, horizon, projection.MODEL_VERSION,
        horizon_solutions=horizon_solutions, distributions=distributions, plan=plan,
        generated_at=generated_at, settings=settings, strategy=strat,
        decision=payload, live=live_state, review=review_state,
        notifications=notif, dry_run=dry_run,
    )
    log["artifacts"] = written

    # AI "Gaffer's Verdict" — reads the artifacts just written; template fallback
    # when no API key is configured, so this never breaks the pipeline.
    #
    # Skipped entirely on a dry run. These two write their own files rather than
    # going through write_all, so without this a "dry" run still modified two
    # tracked artifacts — which is precisely the silent overwrite the flag exists
    # to prevent. (It also avoids two metered LLM calls for a run that publishes
    # nothing.)
    if dry_run:
        log["verdict"] = "skipped (dry run)"
        log["news"] = "skipped (dry run)"
    else:
        from gaffer.ai import news as news_mod
        from gaffer.ai import verdict as verdict_mod

        v = verdict_mod.generate()
        log["verdict"] = v["source"]
        clubs = [(r["name"], r["short"])
                 for r in conn.execute("SELECT name, short FROM teams")]
        n = news_mod.generate(clubs=clubs)
        log["news"] = f"{n['count']} items ({n['source']})"
    log["entry_id"] = settings.entry_id
    log["elapsed_s"] = round(time.time() - t0, 1)
    conn.close()
    return log


def _outcome_distribution(scen, dec, cap: int = 500) -> list[float] | None:
    """The XI's simulated point distribution, stored with the snapshot.

    T-23 needs this to say where the realised score landed — which is the only
    way to separate a bad decision from bad luck. It must be captured before the
    deadline; recomputing it afterwards would use a model that has since seen
    the team news.
    """
    if scen is None or not dec.starting:
        return None
    try:
        arr = scen.squad_points(dec.starting, captain=dec.captain)
    except Exception:  # noqa: BLE001 - a distribution is optional, never fatal
        return None
    # Store a bounded sample: the percentile is what matters, not 2000 floats.
    step = max(1, len(arr) // cap)
    return [round(float(x), 2) for x in arr[::step]]


def _team_and_positions(conn) -> tuple[dict[int, int], dict[int, str], dict[int, str]]:
    team_of: dict[int, int] = {}
    positions: dict[int, str] = {}
    names: dict[int, str] = {}
    for r in conn.execute("SELECT id, team_id, position, web_name FROM players"):
        team_of[r["id"]] = r["team_id"]
        positions[r["id"]] = r["position"]
        names[r["id"]] = r["web_name"]
    return team_of, positions, names


def _build_live(conn, client, settings, from_gw, now, generated_at):
    """Assemble the live view. Contained: a live-endpoint outage is a state."""
    try:
        fixtures_payload = client.fixtures()
    except Exception as exc:  # noqa: BLE001
        return {"live_version": live_mod.LIVE_VERSION, "gameweek": from_gw,
                "as_of": generated_at, "available": False,
                "unavailable_reason": live_mod.UNAVAILABLE_NO_LIVE_DATA,
                "note": f"could not read fixtures: {type(exc).__name__}",
                "fixtures": [], "fixture_summary": {"total": 0, "by_state": {}}}
    try:
        live_payload = client.event_live(from_gw)
    except Exception:  # noqa: BLE001 - pre-season this 404s or returns nothing
        live_payload = {}

    team_of, positions, names = _team_and_positions(conn)
    squad = weekly.held_squad(conn)
    preds = {
        r["player_id"]: float(r["exp_points"] or 0.0)
        for r in conn.execute(
            "SELECT player_id, exp_points FROM projections WHERE gw=?", (from_gw,))
    }
    rivals = _live_rivals(client, settings, from_gw)
    return live_mod.assemble(
        gw=from_gw, live_payload=live_payload, fixtures_payload=fixtures_payload,
        squad=squad, positions=positions, team_of=team_of, now=now or
        datetime.now(UTC), predictions=preds, rivals=rivals, names=names,
        entry_id=settings.entry_id,
        baseline=int(db.get_meta(conn, "overall_points") or 0),
        hits=0, active_chip=db.get_meta(conn, "active_chip") or None,
        as_of=generated_at)


def _live_rivals(client, settings, gw) -> list[dict]:
    """Rival squads for the live view, from the first configured league only.

    Bounded on purpose: a live view refreshes often, and walking every league's
    cohort would turn a scoreboard into a rate-limit problem.
    """
    if not (settings.entry_id and settings.league_ids):
        return []
    from gaffer import league as LG
    try:
        state = LG.fetch_league(client, settings.league_ids[0], settings.entry_id,
                                squad_event=gw)
    except Exception:  # noqa: BLE001
        return []
    return [
        {"entry_id": e.entry_id, "name": e.manager or e.entry_name,
         "starting": e.starting, "bench": e.bench, "captain": e.captain,
         "vice": e.vice, "total": e.total, "hits": e.hits,
         "active_chip": (e.chips_used or [None])[0]}
        for e in state.entries if e.has_picks
    ]


def _build_review(conn, client, settings, now):
    """Review the last finished gameweek, if there is anything real to review."""
    if not settings.entry_id:
        return None
    last = db.get_meta(conn, "last_finished_gw")
    if not (last and str(last).isdigit()):
        return None
    event = int(last)
    try:
        picks = client.entry_picks(settings.entry_id, event)
        live_payload = client.event_live(event)
        history = client.entry_history(settings.entry_id)
    except Exception:  # noqa: BLE001 - no review is better than a fabricated one
        return None
    if not (picks or {}).get("picks") or not (live_payload or {}).get("elements"):
        return None

    points = {
        e["id"]: int((e.get("stats") or {}).get("total_points") or 0)
        for e in live_payload["elements"] if isinstance(e.get("id"), int)
    }
    starting = [p["element"] for p in picks["picks"] if (p.get("position") or 99) <= 11]
    bench = [p["element"] for p in picks["picks"] if (p.get("position") or 99) > 11]
    cur = next((h for h in (history or {}).get("current") or []
                if h.get("event") == event), {})
    actual = {
        "total_points": cur.get("points"),
        "starting": starting, "bench": bench,
        "captain": next((p["element"] for p in picks["picks"] if p.get("is_captain")),
                        None),
        "multiplier": 3 if picks.get("active_chip") == "3xc" else 2,
        "hits": int(cur.get("event_transfers_cost") or 0),
        "chip": picks.get("active_chip"),
        "transfers_in": [], "transfers_out": [],
    }
    rev = review_mod.build(conn, entry_id=settings.entry_id, event=event,
                           actual=actual, points=points, now=now)
    review_mod.save(conn, rev)
    return rev.as_dict()


def _evaluate_notifications(conn, settings, dec, strat, live_state, now,
                            generated_at):
    """Resolve alerts and record them. Dry-run: nothing leaves the machine."""
    from gaffer.notify import rules as notify_rules
    from gaffer.notify.engine import Engine
    from gaffer.notify.sinks import MemorySink, describe

    meta = {
        "current_gw": db.get_meta(conn, "current_gw"),
        "deadline": db.get_meta(conn, "deadline"),
        "generated_at": generated_at,
        "squad_status": db.get_meta(conn, "squad_status"),
        "squad_status_reason": db.get_meta(conn, "squad_status_reason"),
    }
    owned = [
        {"id": r["id"], "name": r["web_name"], "status": r["status"],
         "chance_playing": r["chance_playing"], "news": r["news"]}
        for r in conn.execute(
            "SELECT p.id, p.web_name, p.status, p.chance_playing, p.news "
            "FROM players p JOIN my_squad s ON s.player_id = p.id "
            "WHERE s.gw = (SELECT MAX(gw) FROM my_squad)")
    ]
    from gaffer.notify.cli import _previous_decision, _remember_decision
    current = dec.as_dict()
    alerts = notify_rules.build_alerts(
        meta=meta, now=now or datetime.now(UTC), owned=owned,
        current_decision=current, previous_decision=_previous_decision(conn),
        chips=(strat or {}).get("chips"),
        swing=(live_state or {}).get("largest_swing"))
    engine = Engine(conn, MemorySink())        # dry-run by construction
    result = engine.run(alerts, now=now or datetime.now(UTC))
    _remember_decision(conn, current)
    return {
        "notify_version": "notify-1.0",
        "generated_at": generated_at,
        "result": result.as_dict(),
        "config": describe(),
        "summary": engine.summary(),
    }


def _free_transfers(conn) -> int:
    val = db.get_meta(conn, "free_transfers")
    try:
        return int(val) if val not in (None, "") else 1
    except (ValueError, TypeError):
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gaffer pipeline")
    ap.add_argument("--fast", action="store_true", help="skip DEFCON history enrichment")
    ap.add_argument("--horizon", type=int, default=None, help="gameweeks to project")
    ap.add_argument("--skip-strategy", action="store_true",
                    help="skip league/chip analysis (no rival picks are fetched)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute everything and PRINT the target files, but "
                         "write nothing")
    args = ap.parse_args(argv)

    log = run(fast=args.fast, horizon=args.horizon,
              skip_strategy=args.skip_strategy, dry_run=args.dry_run)
    print("=== Gaffer pipeline complete ===")
    for k, v in log.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
