"""``python -m gaffer.notify`` — evaluate alerts. Dry-run unless forced.

Sending requires BOTH ``--send`` and a configured provider. There is no config
file switch and no environment variable that turns sending on by itself, because
"it started sending because a stale env var was set" is not a failure mode worth
having.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from gaffer import config
from gaffer.notify import rules
from gaffer.notify.engine import Engine
from gaffer.notify.sinks import ConfigError, describe, resolve_sink
from gaffer.store import db


def _artifact(name: str, data_dir=None) -> dict | list | None:
    path = (data_dir or config.DATA_DIR) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def evaluate(
    *, data_dir=None, now: datetime | None = None, sink_name: str | None = None,
    send: bool = False, quiet: bool = True, db_path=None,
) -> dict:
    """Read the published artifacts and resolve what would be sent."""
    now = now or datetime.now(UTC)
    meta = _artifact("meta.json", data_dir) or {}
    my_team = _artifact("my_team.json", data_dir) or {}
    strategy = _artifact("strategy.json", data_dir) or {}
    decision = _artifact("decision.json", data_dir) or {}
    live = _artifact("live.json", data_dir) or {}

    owned = [
        {"id": p.get("id"), "name": p.get("name"), "status": p.get("status"),
         "chance_playing": p.get("chance_playing"), "news": p.get("news")}
        for p in (my_team.get("players") or []) if isinstance(p, dict)
    ]

    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        previous = _previous_decision(conn)
        current = (decision.get("decision") or None)
        alerts = rules.build_alerts(
            meta=meta, now=now, owned=owned,
            current_decision=current, previous_decision=previous,
            chips=strategy.get("chips"), swing=live.get("largest_swing"))

        sink = resolve_sink(sink_name)
        engine = Engine(conn, sink, dry_run=not send, quiet=quiet)
        result = engine.run(alerts, now=now)
        if current:
            _remember_decision(conn, current)
        return {
            "result": result.as_dict(),
            "config": describe(sink_name),
            "summary": engine.summary(),
        }
    finally:
        conn.close()


_PREV_KEY = "notify_last_decision"


def _previous_decision(conn) -> dict | None:
    raw = db.get_meta(conn, _PREV_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _remember_decision(conn, decision: dict) -> None:
    keep = {k: decision.get(k) for k in
            ("action", "headline", "captain", "transfers_in", "transfers_out")}
    db.set_meta(conn, _PREV_KEY, json.dumps(keep, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Evaluate Gaffer notifications (dry-run by default)")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--sink", default=None,
                    help="memory (default) | console | webhook")
    ap.add_argument("--send", action="store_true",
                    help="ACTUALLY DELIVER. Requires a configured provider; "
                         "without this nothing leaves the machine.")
    ap.add_argument("--no-quiet-hours", action="store_true",
                    help="ignore the 22:30-07:30 Europe/London window")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from pathlib import Path
    data_dir = Path(args.data_dir) if args.data_dir else None

    if args.send:
        cfg = describe(args.sink)
        if not cfg["configured"]:
            print("refusing to send: the selected sink is not configured "
                  f"(missing {cfg['missing_env'] or 'provider'})", file=sys.stderr)
            return 2
        print(f"SENDING for real via {cfg['sink']}")
    try:
        out = evaluate(data_dir=data_dir, sink_name=args.sink, send=args.send,
                       quiet=not args.no_quiet_hours)
    except ConfigError as exc:
        print(f"notification config error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        r = out["result"]
        mode = "DRY RUN — nothing was sent" if r["dry_run"] else "LIVE"
        print(f"=== Gaffer notifications ({mode}) ===")
        print(f"  considered {r['considered']}, new {r['new']}, "
              f"duplicates {r['duplicates']}, suppressed {r['suppressed']}, "
              f"delivered {r['delivered']}, failed {r['failed']}")
        for a in r["alerts"]:
            print(f"  [{a['severity']:>9}] {a['title']}")
            print(f"              {a['body']}")
            print(f"              {a['deep_link']}  ({a['state']})")
        for e in r["errors"]:
            print(f"  ! {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
