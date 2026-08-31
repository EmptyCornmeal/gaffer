"""End-to-end pipeline: ingest -> project -> optimise -> export JSON.

Run with::

    python -m gaffer.pipeline            # full run
    python -m gaffer.pipeline --fast     # skip per-player DEFCON enrichment

Runs on a schedule in GitHub Actions (``.github/workflows/refresh.yml``), which
commits ``data/*.json`` and dispatches the Pages deploy.

The scheduling model changed and this docstring described the old one. It is no
longer a small number of fixed slots waiting to be punctual: the workflow fires
``*/15`` and ``gaffer.schedule --should-refresh`` decides whether each tick does
any work, with 02:00, 11:00 and 17:00 UTC kept only as belt and braces. That is
the answer to GitHub's drift, which is real and measured — over 61 runs of this
repo the 17:00 slot started a **median of 53 minutes late**, and once landed 16
minutes *after* where a GW1 deadline would have been.

Firing often does not make any single tick punctual, so before a hard deadline a
manual ``gh workflow run refresh.yml`` is still the only way to be certain,
rather than the only way to be current.
"""

from __future__ import annotations

import argparse
import json
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
from gaffer.store import db, persist


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

    # G1. This runner has never seen a previous run: Actions machines are
    # ephemeral and `data/*.db` is gitignored, so without this the database is
    # empty every time and the pre-deadline record of what Gaffer advised is
    # lost the moment the deadline passes. Restored here — after ingest has
    # created and migrated the schema, before anything reads a snapshot table.
    #
    # Never fatal. A damaged archive costs the archive; refusing to publish a
    # gameweek over it would cost the season.
    try:
        log["state_restored"] = persist.restore(conn)
    except Exception as exc:                                  # noqa: BLE001
        log["state_restored"] = f"FAILED {type(exc).__name__}: {exc}"

    from_gw = int(db.get_meta(conn, "current_gw") or 1)
    log["from_gw"] = from_gw

    log["projection_rows"] = projection.project(conn, from_gw, horizon)
    # Which h=1 number was published, and why. `component_only` means FPL's
    # `ep_next` was measured as uninformative and left out entirely.
    log["projection_regime"] = (
        f"{db.get_meta(conn, 'projection_regime')} "
        f"(ep_next weight {db.get_meta(conn, 'ep_next_blend_weight')}; "
        f"{db.get_meta(conn, 'projection_regime_reason')})"
    )

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

    # The prediction ledger: rival candidate squads, frozen before the deadline
    # so the gameweek can settle which method was right. `weekly.persist` above
    # snapshots what GAFFER said; this snapshots what the alternatives said, and
    # without it there is nothing to compare against but memory.
    #
    # Refreshes on every pre-deadline run and locks the moment the deadline
    # passes. Wrapped, because a missing week of evidence is a bad day and a
    # pipeline that stops publishing is a bad season.
    try:
        from gaffer import ledger

        slate = ledger.build_slate(
            conn, from_gw, deadline=db.get_meta(conn, "deadline"),
            model_version=projection.MODEL_VERSION, generated_at=generated_at)
        path = ledger.freeze(slate, ledger.ledger_path(from_gw), now=now)
        log["ledger"] = f"{len(slate['entries'])} candidates -> {path.name}"
    except ledger.AlreadyFrozen as exc:
        log["ledger"] = f"locked ({exc})"
    except Exception as exc:                                  # noqa: BLE001
        log["ledger"] = f"FAILED {type(exc).__name__}: {exc}"

    # T-22: live gameweek. This follows the football, not the decision: once a
    # deadline passes, from_gw is already the NEXT event while the current one
    # is still being played. Pre-season nothing is in flight, so fall back to
    # the projection event and let the artifact say "not started" out loud.
    live_state = None
    if not skip_strategy:
        with FplClient() as client:
            live_gw = client.live_event(now) or from_gw
            live_state = _build_live(conn, client, settings, live_gw, now,
                                     generated_at)
    log["live"] = (
        "skipped" if live_state is None
        else f"gw{live_state.get('gameweek')} "
             f"available={live_state.get('available')} "
             f"({live_state.get('unavailable_reason') or 'scored'})")

    # T-22b: settle the prediction ledger. `freeze` has always run; `score` never
    # did — it existed only as a manual CLI nobody invoked, so every gameweek
    # recorded what the candidates predicted and never what happened. A ledger
    # that only ever holds forecasts cannot settle anything.
    #
    # Runs after the live stage because it uses the same payload, and only for a
    # gameweek the API calls finished: scoring mid-match would freeze provisional
    # bonus as though it were the result.
    if not skip_strategy:
        try:
            with FplClient() as client:
                log["ledger_scored"] = _score_finished_ledgers(client, now)
        except Exception as exc:                              # noqa: BLE001
            # A missing settlement is a gap in the evidence. A pipeline that
            # stops publishing is a gap in the product.
            log["ledger_scored"] = f"FAILED {type(exc).__name__}: {exc}"

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

    # G1, the other half. Written last so it captures this run's own snapshot,
    # and skipped on a dry run for the same reason the artifacts are: a dry run
    # must not leave anything behind for the next one to inherit.
    if dry_run:
        log["state_saved"] = "skipped (dry run)"
    else:
        try:
            log["state_saved"] = persist.dump(conn)
        except Exception as exc:                              # noqa: BLE001
            log["state_saved"] = f"FAILED {type(exc).__name__}: {exc}"

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



def _score_finished_ledgers(client, now) -> str:
    """Attach results to every frozen, unscored slate whose gameweek is done.

    Idempotent by construction: a slate carrying `scored` is skipped, and
    `ledger.score` appends rather than editing a prediction.
    """
    from gaffer import io, ledger

    events = client.events()
    finished = [
        int(e["id"]) for e in events
        if e.get("id") is not None and e.get("finished") and e.get("data_checked")
    ]
    if not finished:
        return "no finished gameweek yet"

    done: list[str] = []
    for gw in sorted(finished):
        path = ledger.ledger_path(gw)
        if not path.exists():
            continue
        slate = json.loads(path.read_text(encoding="utf-8"))
        if slate.get("scored"):
            continue
        payload = client.event_live(gw) or {}
        elements = payload.get("elements") or []
        if not elements:
            continue
        pts = {int(e["id"]): int((e.get("stats") or {}).get("total_points", 0))
               for e in elements}
        mins = {int(e["id"]): int((e.get("stats") or {}).get("minutes", 0))
                for e in elements}
        io.write_json_atomic(path, ledger.score(slate, pts, mins))
        done.append(f"gw{gw:02d}")
    return ", ".join(done) if done else "nothing new to settle"

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
    baseline, hits, baseline_source = _live_baseline(client, settings, from_gw)
    state = live_mod.assemble(
        gw=from_gw, live_payload=live_payload, fixtures_payload=fixtures_payload,
        squad=squad, positions=positions, team_of=team_of, now=now or
        datetime.now(UTC), predictions=preds, rivals=rivals, names=names,
        entry_id=settings.entry_id,
        # An unknown baseline reaches the scorer as 0 and every figure built on
        # it is withheld below. Passing None through would put it into
        # arithmetic `gaffer.live` shares byte-for-byte with the browser port.
        baseline=baseline or 0, hits=hits,
        active_chip=db.get_meta(conn, "active_chip") or None,
        as_of=generated_at)
    state["baseline_source"] = baseline_source
    _mark_live_gaps(state, squad, live_payload, baseline)
    return state


def _baseline_from_row(row) -> tuple[int, int] | None:
    """Season points carried in, and the hit paid, from ONE history row.

    That row is what the picks endpoint returns under ``entry_history``. Its
    ``total_points`` is cumulative, net of every hit taken so far, and INCLUDES
    the gameweek the row belongs to; ``points`` is that gameweek's gross score.
    So what was carried in is ``total_points - points + this week's hit``, the
    same figure ``entry_baseline_and_hits`` reaches by walking the whole
    history.

    Returns None rather than a guess when the row is not a history row, so an
    unreadable baseline stays unreadable. Mirrors ``baselineFromRow`` in
    web/src/lib/live/source.ts.
    """
    def num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if not isinstance(row, dict):
        return None
    total, points = row.get("total_points"), row.get("points")
    if not num(total) or not num(points):
        return None
    hits = int(row.get("event_transfers_cost") or 0)
    return int(total) - int(points) + hits, hits


def _live_baseline(client, settings, gw) -> tuple[int | None, int, str]:
    """Season points carried into ``gw`` and the transfer cost paid for it.

    Both were wrong before this existed: `overall_points` was never written at
    ingest so the baseline read 0, and hits were hardcoded to 0 so a -8 week
    looked four points better than it was.

    The entry history is preferred because its cumulative `total_points` at the
    previous event is exactly "before this gameweek".

    C9: the fallback used to be the ingested season total, which the paragraph
    above this one already said contains the in-progress gameweek. So whenever
    the history read failed, the live score was added to a figure that already
    held it and the projected season total came out a full gameweek too high —
    while the browser failed the same moment the opposite way, caching a
    baseline of 0 for the session. The fallback is now the picks endpoint's own
    ``entry_history`` row, which carries the same arithmetic in one line and is
    a read this path can make anyway; and when neither answers the baseline is
    None, reported as unavailable rather than substituted. The source is stated
    either way, so no screen can present any of them as exact.
    """
    if not settings.entry_id:
        return None, 0, "unavailable"
    try:
        history = client.entry_history(settings.entry_id)
    except Exception:  # noqa: BLE001 - a baseline is never worth losing the run
        history = None
    # A payload without a `current` list is not a history, however cheerfully it
    # arrived. The browser applies the same test, in the same order.
    if isinstance(history, dict) and isinstance(history.get("current"), list):
        baseline, hits = live_mod.entry_baseline_and_hits(history, gw)
        return baseline, hits, "entry_history"
    try:
        picks = client.entry_picks(settings.entry_id, gw) or {}
    except Exception:  # noqa: BLE001 - the fallback is allowed to fail too
        picks = {}
    row = _baseline_from_row(picks.get("entry_history"))
    if row is None:
        return None, 0, "unavailable"
    return row[0], row[1], "picks_entry_history"


def _live_missing_players(squad, live_payload) -> list[int]:
    """Squad members the live endpoint carried no row for.

    C13. ``player_live`` invents a row for anyone it has a fixture for but no
    live data on, and the invented row holds that player's full PRE-MATCH
    projection against zero confirmed points. Before kick-off that is right and
    is what lets a squad render at all; once his match is running it is a guess
    wearing a live score's clothes, and a payload truncated at 70 minutes then
    reports "yet to kick off" for a man who may already have scored twice.

    The invention is left alone — it is the browser's behaviour too, and
    tests/test_live_parity.py holds both sides to it. What this adds is the
    means to say so. Mirrors ``missingFromLive`` in
    web/src/lib/live/assemble.ts.

    An empty payload names nobody: that is ``no_live_data``, a state the view
    already reports on its own, not fifteen individually missing players.
    """
    have = {e.get("id") for e in ((live_payload or {}).get("elements") or [])
            if isinstance(e, dict) and isinstance(e.get("id"), int)}
    if not squad or not have:
        return []
    ours = list(squad.get("starting") or []) + list(squad.get("bench") or [])
    return sorted(set(ours) - have)


def _mark_live_gaps(state, squad, live_payload, baseline) -> None:
    """Record on the state what could not be read, and withhold whatever cannot
    honestly be shown without it.

    Mirror of ``markLiveGaps`` in web/src/lib/live/source.ts. The Live page
    renders whichever of the two answered — the browser while matches are on,
    this artifact when the proxy is down — so both must blank the same fields
    and name the same gaps, or how honest the page is depends on which half of
    the system it got.

    Deliberately outside ``gaffer.live.assemble``: that is the scoring rulebook,
    pinned byte-for-byte against the browser port by tests/test_live_parity.py,
    and it never sees the I/O that failed. This is the layer that knows.
    """
    gaps: list[str] = []
    if baseline is None:
        gaps.append("your season total so far")
        sq = state.get("squad")
        if isinstance(sq, dict):
            # A zero baseline is not a smaller answer than the real one, it is a
            # different and wrong one: the season total renders as this
            # gameweek's score. Nothing beats that.
            sq["season_total_before"] = None
            sq["season_total_projected"] = None
        if state.get("rivals"):
            # The table is ordered on season totals. Without yours you sort as
            # though the season began this morning, which moves every rival up a
            # place and hands the swing the wrong "closest" manager.
            state["rivals"] = []
            state["largest_swing"] = None
            gaps.append("the league table, which needs it")
    absent = _live_missing_players(squad, live_payload)
    if absent:
        state["missing_players"] = absent
        gaps.append(f"{len(absent)} of your players missing from the live feed")
    state["incomplete"] = ", ".join(gaps) or None


def _live_rivals(client, settings, gw) -> list[dict]:
    """Rival squads for the live view, from the first configured league only.

    Bounded on purpose: a live view refreshes often, and walking every league's
    cohort would turn a scoreboard into a rate-limit problem.

    A5. The manager is a member of his own mini-league, so his own entry comes
    back in the standings and is dropped here. ``live.assemble`` prepends a "You"
    row, so leaving him in listed him twice in the published table — and handed
    ``largest_swing`` a rival at distance zero from himself, which is exactly the
    thing it measures against. ``gatherRivals`` in web/src/lib/live/source.ts
    drops him the same way; ``assemble`` now does it again, defensively.
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
         "vice": e.vice,
         # C10. `total` is a season total that MOVES during the gameweek, so
         # handing it to the scorer as a baseline adds this week's live points
         # to a figure that already contains them — and that one number decides
         # the league table, the closest-rival choice and the swing together.
         # `event_total` is FPL's own account of what this gameweek contributed
         # to `total` (the table is built as the sum of them), so the difference
         # is exactly what was carried in, and it holds still while the matches
         # run. `gatherRivals` in web/src/lib/live/source.ts subtracts the same
         # pair; the two must not drift.
         "total": e.total - e.event_total, "hits": e.hits,
         "active_chip": (e.chips_used or [None])[0]}
        for e in state.entries
        if e.has_picks and e.entry_id != settings.entry_id
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
