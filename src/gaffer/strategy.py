"""Strategy layer: leagues, chips and the decisions they disagree about.

This is the seam between the pure modules and the pipeline. It owns the order of
operations and nothing else:

    scenarios (T-16)  ->  one shared simulated football
        league (T-17) ->  who your rivals actually own, and where you stand
    multileague (T-18)->  which decision each league wants, and where they clash
          chips (T-20)->  what a chip is worth in that same simulated football

Every probability, chip value and league comparison in the exported artifact is
computed from the SAME ``ScenarioSet``. That is the point: a goal that lifts your
captain is the same goal that lifts your rival's, and a Bench Boost is worth the
bench that those same fixtures produce.

Nothing here authenticates. Rival picks are public only after a deadline passes,
so "we do not know this rival's squad" is a first-class, reported state.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from gaffer import chips as CH
from gaffer import config
from gaffer import league as LG
from gaffer import multileague as ML
from gaffer.model import scenarios as SC
from gaffer.solver import optimize

STRATEGY_VERSION = "strategy-1.0"

#: Captain candidates compared per league. The armband is the highest-leverage
#: weekly decision and the one where league ownership genuinely changes the
#: answer, so it is the decision axis the multi-league layer resolves.
CAPTAIN_OPTIONS = 4

#: Leagues fetched per run. Each costs one standings call plus one picks call per
#: cohort member; an unbounded loop over every auto-joined global league is not a
#: strategy.
MAX_LEAGUES = 6

SEASON_EVENTS = 38


# ---------------------------------------------------------------------------
# Squad state
# ---------------------------------------------------------------------------

def stored_squad(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The user's last readable picks: XI, bench, armband. None when unknown."""
    rows = conn.execute(
        "SELECT gw, player_id, is_captain, is_vice, multiplier FROM my_squad "
        "WHERE gw = (SELECT MAX(gw) FROM my_squad)"
    ).fetchall()
    if not rows:
        return None
    starting = [r["player_id"] for r in rows if (r["multiplier"] or 0) > 0]
    bench = [r["player_id"] for r in rows if (r["multiplier"] or 0) == 0]
    captain = next((r["player_id"] for r in rows if r["is_captain"]), None)
    vice = next((r["player_id"] for r in rows if r["is_vice"]), None)
    return {
        "source_event": int(rows[0]["gw"]),
        "starting": starting, "bench": bench,
        "captain": captain, "vice": vice,
    }


def _wildcard_budget(conn: sqlite3.Connection) -> int | None:
    """Selling value of the held squad plus the bank — a real wildcard budget."""
    row = conn.execute(
        "SELECT SUM(COALESCE(selling_price, 0)) AS v FROM my_squad "
        "WHERE gw = (SELECT MAX(gw) FROM my_squad)"
    ).fetchone()
    if row is None or not row["v"]:
        return None
    bank = conn.execute("SELECT value FROM meta WHERE key='bank'").fetchone()
    try:
        bank_v = int(bank["value"]) if bank and str(bank["value"]).strip() else 0
    except (ValueError, TypeError):
        bank_v = 0
    return int(row["v"]) + bank_v


def free_squad(
    conn: sqlite3.Connection, from_gw: int, distributions: dict | None = None,
) -> optimize.Solution | None:
    """The best legal squad money could buy right now — the Free Hit / Wildcard
    comparison point.

    Solved with ``free_transfers`` set to the squad size so no hit is charged, and
    with an explicit budget so it is a genuine rebuild rather than a transfer from
    the current squad. It is a real budget-legal solve, not "the best XI in the
    game", which is what the old page implicitly compared against.
    """
    budget = _wildcard_budget(conn)
    if budget is None:
        return None
    try:
        return optimize.optimise(
            conn, from_gw, horizon=1, free_transfers=config.SQUAD_SIZE,
            budget=budget, distributions=distributions,
        )
    except Exception:  # noqa: BLE001 - a failed chip comparison must not kill the run
        return None


# ---------------------------------------------------------------------------
# Leagues
# ---------------------------------------------------------------------------

def default_target(state: LG.LeagueState) -> int:
    """What "doing well" means in this league, sized to the league.

    Positions are measured inside the fetched cohort, so the target must be too —
    a top-10% target in a truncated league means top 10% of the rivals we can
    actually see, and the caveats say so.
    """
    n = max(1, len(state.entries))
    if state.classification in (LG.TINY, LG.SMALL):
        return 1
    return max(1, round(n * 0.1))


def _league_block(
    scen: SC.ScenarioSet, state: LG.LeagueState, starting: list[int],
    captain: int | None, gws_remaining: int,
) -> ML.LeagueView:
    target = default_target(state)
    placing = LG.placing_probabilities(
        scen, state, starting, captain, target=target,
        gameweeks_remaining=gws_remaining,
    )
    return ML.build_view(
        state, list(starting) + [], captain, placing, gws_remaining, target
    )


def fetch_leagues(
    client: Any, league_ids: list[int], entry_id: int, squad_event: int | None,
) -> tuple[list[LG.LeagueState], list[dict[str, Any]]]:
    """Fetch each configured league. One failure never kills the others."""
    states: list[LG.LeagueState] = []
    errors: list[dict[str, Any]] = []
    for lid in league_ids[:MAX_LEAGUES]:
        try:
            states.append(
                LG.fetch_league(client, lid, entry_id, squad_event=squad_event)
            )
        except Exception as exc:  # noqa: BLE001 - transport/HTTP/shape
            errors.append({"league_id": lid, "error": f"{type(exc).__name__}: {exc}"})
    if len(league_ids) > MAX_LEAGUES:
        errors.append({
            "league_id": None,
            "error": f"{len(league_ids)} leagues configured; the first "
                     f"{MAX_LEAGUES} were analysed",
        })
    return states, errors


# ---------------------------------------------------------------------------
# The decision the leagues argue about
# ---------------------------------------------------------------------------

def captain_options(
    scen: SC.ScenarioSet, starting: list[int], names: dict[int, str],
    limit: int = CAPTAIN_OPTIONS,
) -> list[int]:
    """Armband candidates, best expected points first."""
    ranked = sorted(starting, key=lambda p: -float(scen.row(p).mean()))
    return ranked[:limit]


def build_options(
    scen: SC.ScenarioSet, states: list[LG.LeagueState], starting: list[int],
    candidates: list[int], names: dict[int, str], gws_remaining: int,
) -> list[ML.Option]:
    """One option per captain candidate, scored in every league at once."""
    options: list[ML.Option] = []
    for pid in candidates:
        ep = float(scen.squad_points(starting, captain=pid).mean())
        p_target: dict[str, float] = {}
        for st in states:
            res = LG.placing_probabilities(
                scen, st, starting, pid, target=default_target(st),
                gameweeks_remaining=gws_remaining,
            )
            # A league with no measurable probability contributes no axis. Writing
            # 0.0 would make every option look equally hopeless there and let an
            # unmeasured league vote in the resolution.
            if res.basis != LG.BASIS_UNAVAILABLE:
                p_target[str(st.league_id)] = res.p_target
        options.append(ML.Option(
            key=f"captain:{pid}",
            label=f"Captain {names.get(pid, pid)}",
            expected_points=ep, p_target=p_target,
        ))
    return options


# ---------------------------------------------------------------------------
# Chips
# ---------------------------------------------------------------------------

def chip_block(
    client: Any, scen: SC.ScenarioSet, entry_id: int | None, gw: int,
    starting: list[int], bench: list[int], captain: int | None,
    free_sol: optimize.Solution | None, weeks_retained: int,
    squad_known: bool = True,
) -> dict[str, Any]:
    """Value every available chip in the same scenarios as the squad."""
    try:
        bootstrap = client.bootstrap()
    except Exception:  # noqa: BLE001
        bootstrap = {}
    windows = CH.parse_windows(bootstrap)
    used: list[str] = []
    if entry_id:
        try:
            used = CH.chips_used_from_history(client.entry_history(entry_id))
        except Exception:  # noqa: BLE001
            used = []

    evaluations: list[CH.ChipEvaluation] = []
    if starting:
        if bench:
            evaluations.append(
                CH.evaluate_bench_boost(scen, starting, bench, captain, gw))
        if captain is not None:
            evaluations.append(CH.evaluate_triple_captain(scen, starting, captain, gw))
        if free_sol is not None and free_sol.starting:
            evaluations.append(CH.evaluate_free_hit(
                scen, starting, captain, free_sol.starting, free_sol.captain, gw))
            evaluations.append(CH.evaluate_wildcard(
                scen, starting, captain, free_sol.starting, free_sol.captain, gw,
                weeks_retained=weeks_retained))
    plan = CH.plan_chips(evaluations, windows, used, gw, squad_known=squad_known)
    return plan.as_dict()


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build(
    conn: sqlite3.Connection, client: Any, settings: config.Settings, *,
    from_gw: int, squad_event: int | None, sol: optimize.Solution,
    distributions: dict | None = None, n_sims: int = SC.DEFAULT_SIMS,
    seed: int = SC.DEFAULT_SEED, generated_at: str | None = None,
    scen: SC.ScenarioSet | None = None,
) -> dict[str, Any]:
    """Produce ``strategy.json``. Never raises: a failure is reported, not thrown.

    The pipeline's core recommendation must survive a league API outage, so every
    outward call here is contained and its failure recorded in the artifact.
    """
    names = {
        r["id"]: r["web_name"] for r in conn.execute("SELECT id, web_name FROM players")
    }
    gws_remaining = max(1, SEASON_EVENTS - from_gw + 1)

    # One ScenarioSet for the whole run. The caller may pass one in so that the
    # weekly decision, the league probabilities and the chip values are all
    # measured in the SAME simulated football rather than three separate draws.
    if scen is None:
        scen = SC.simulate(conn, from_gw, n_sims=n_sims, seed=seed)

    held = stored_squad(conn)
    if held and held["starting"]:
        starting, bench = held["starting"], held["bench"]
        captain = held["captain"]
        basis = "your stored squad"
    else:
        starting, bench = list(sol.starting), list(sol.bench)
        captain = sol.captain or None
        basis = "the recommended squad (no readable squad of your own yet)"

    free_sol = free_squad(conn, from_gw, distributions) if held else None
    chips = chip_block(
        client, scen, settings.entry_id, from_gw, starting, bench, captain,
        free_sol, weeks_retained=min(4, gws_remaining),
        squad_known=bool(held and held["starting"]),
    )

    states: list[LG.LeagueState] = []
    errors: list[dict[str, Any]] = []
    if settings.entry_id and settings.league_ids:
        states, errors = fetch_leagues(
            client, settings.league_ids, settings.entry_id, squad_event)

    views = [_league_block(scen, st, starting, captain, gws_remaining) for st in states]
    ML.assert_isolated(views)

    candidates = captain_options(scen, starting, names) if starting else []
    # Only leagues with a measurable placing probability get a vote.
    measurable = [
        st for st, v in zip(states, views, strict=True)
        if v.placing.get("available")
    ]
    options = (
        build_options(scen, measurable, starting, candidates, names, gws_remaining)
        if measurable and candidates else []
    )
    keys = [str(st.league_id) for st in measurable]
    resolution = ML.resolve(options, None, keys) if options else {
        "default": None,
        "reason": (
            "no league has a measurable placing probability yet, so there is "
            "nothing for the leagues to disagree about — the neutral "
            "recommendation stands"
            if states else
            "no league data, so there is nothing for the leagues to disagree "
            "about — the neutral recommendation stands"),
        "shortlist": [], "conflicts": [],
    }

    return {
        "strategy_version": STRATEGY_VERSION,
        "league_version": LG.LEAGUE_VERSION,
        "multileague_version": ML.MULTILEAGUE_VERSION,
        "chips_version": CH.CHIPS_VERSION,
        "generated_at": generated_at,
        "gameweek": from_gw,
        "gameweeks_remaining": gws_remaining,
        "simulation": scen.as_meta(),
        "basis": basis,
        "squad": {
            "starting": starting, "bench": bench, "captain": captain,
            "source_event": (held or {}).get("source_event"),
        },
        "leagues": [v.as_dict() for v in views],
        "league_errors": errors,
        "options": [o.as_dict() for o in options],
        "resolution": resolution,
        "chips": chips,
        "limitations": _limitations(scen, views, held is not None, gws_remaining),
    }


def _limitations(
    scen: SC.ScenarioSet, views: list[ML.LeagueView], have_squad: bool,
    gws_remaining: int,
) -> list[str]:
    out = [
        f"All probabilities come from {scen.n_sims} shared fixture scenarios "
        f"(seed {scen.seed}); they carry simulation error of roughly "
        f"{100 * 0.5 / max(scen.n_sims, 1) ** 0.5:.1f} percentage points.",
        "Probabilities describe the next gameweek's football, not the rest of the "
        "season: rivals are assumed to keep their current squads.",
    ]
    if not have_squad:
        out.append(
            "No squad of your own is readable yet, so league comparisons use the "
            "recommended squad as a stand-in.")
    thin = [v for v in views if v.data_quality.get("coverage_pct", 0) < 100]
    if thin:
        out.append(
            "Some rival squads are unknown and were modelled as a distribution "
            "rather than a team; see each league's coverage.")
    if gws_remaining > 1:
        out.append(
            "Gaffer's multi-week mean projections are materially weaker than its "
            "one-week ones, so anything beyond the next gameweek is directional.")
    return out
