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

from gaffer import config, ingest
from gaffer.export import artifacts
from gaffer.model import projection, simulate
from gaffer.solver import multiperiod, optimize
from gaffer.store import db


def run(fast: bool = False, horizon: int | None = None) -> dict[str, object]:
    horizon = horizon or config.PROJECTION_HORIZON
    t0 = time.time()
    log: dict[str, object] = {}

    log["ingest"] = ingest.run(skip_enrich=fast)

    conn = db.connect()
    settings = config.Settings.load()
    from_gw = int(db.get_meta(conn, "current_gw") or 1)
    log["from_gw"] = from_gw

    log["projection_rows"] = projection.project(conn, from_gw, horizon)

    # Monte-Carlo next-GW distribution (floor/ceiling/boom%) over the same rates.
    distributions = simulate.simulate_next_gw(conn, from_gw)
    log["simulated"] = len(distributions)

    ft = _free_transfers(conn)
    # Risk stance = the effective-ownership dial. Balanced is the default so the
    # headline squad is template-aware (owns near-must-owns like Haaland) instead
    # of a pure points-per-£ team that punts the template.
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

    written = artifacts.write_all(
        conn, sol, from_gw, horizon, projection.MODEL_VERSION,
        horizon_solutions=horizon_solutions, distributions=distributions, plan=plan,
    )
    log["artifacts"] = written

    # AI "Gaffer's Verdict" — reads the artifacts just written; template fallback
    # when no API key is configured, so this never breaks the pipeline.
    from gaffer.ai import news as news_mod
    from gaffer.ai import verdict as verdict_mod

    v = verdict_mod.generate()
    log["verdict"] = v["source"]
    clubs = [(r["name"], r["short"]) for r in conn.execute("SELECT name, short FROM teams")]
    n = news_mod.generate(clubs=clubs)
    log["news"] = f"{n['count']} items ({n['source']})"
    log["entry_id"] = settings.entry_id
    log["elapsed_s"] = round(time.time() - t0, 1)
    conn.close()
    return log


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
    args = ap.parse_args(argv)

    log = run(fast=args.fast, horizon=args.horizon)
    print("=== Gaffer pipeline complete ===")
    for k, v in log.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
