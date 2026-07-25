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
from gaffer.model import projection
from gaffer.solver import optimize
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

    ft = _free_transfers(conn)
    sol = optimize.optimise(conn, from_gw, horizon, free_transfers=ft)
    log["solver"] = {
        "mode": sol.meta.get("mode"), "status": sol.status,
        "formation": sol.formation, "xi_expected": sol.xi_expected,
        "transfers_in": len(sol.transfers_in), "hits": sol.hits,
    }

    written = artifacts.write_all(
        conn, sol, from_gw, horizon, projection.MODEL_VERSION
    )
    log["artifacts"] = written

    # AI "Gaffer's Verdict" — reads the artifacts just written; template fallback
    # when no API key is configured, so this never breaks the pipeline.
    from gaffer.ai import news as news_mod
    from gaffer.ai import verdict as verdict_mod

    v = verdict_mod.generate()
    log["verdict"] = v["source"]
    n = news_mod.generate()
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
