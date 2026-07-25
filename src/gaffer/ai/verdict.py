"""The Gaffer's Verdict — an AI-written weekly briefing.

Reads the exported JSON artifacts (recommendation + players + meta), asks Claude
to write the week's plan in plain English grounded ONLY in the model's numbers,
and writes ``verdict.json``. Degrades to a deterministic templated briefing when
no API credentials are present, so the pipeline never fails.

Model defaults to ``claude-opus-4-8`` (best writing); set ``GAFFER_VERDICT_MODEL``
to ``claude-haiku-4-5`` to keep it lean/cheap. No key set -> template fallback.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gaffer import config
from gaffer.io import write_json_atomic

VERDICT_MODEL = os.environ.get("GAFFER_VERDICT_MODEL", "claude-opus-4-8")

SYSTEM = (
    "You are 'The Gaffer', a sharp, confident FPL analyst writing a short weekly "
    "briefing for a Fantasy Premier League dashboard. Write in plain English with "
    "a bit of touchline swagger, but stay strictly grounded in the numbers given "
    "— never invent players, prices, fixtures, injuries, or stats. Be decisive.\n\n"
    "If a 'your_team' is provided, the briefing is about THAT squad. Otherwise it "
    "is about the model's recommended squad.\n\n"
    "Output GitHub-flavoured markdown, under ~190 words, in exactly this shape:\n"
    "- A bold one-line headline (the single biggest call this week).\n"
    "- **✅ What's strong** — the best things about the squad right now (key "
    "assets, captain, defensive/DEFCON value, fixtures). Explicitly flag any "
    "injury/fitness doubts on important players (use the 'flagged_news' data).\n"
    "- **🔄 What I'd change** — the transfer(s)/tweaks you'd make and why "
    "(who out → who in), or say 'roll it' if nothing beats a -4. If building from "
    "scratch, make this the key picks to prioritise.\n"
    "- A final bold 'Bottom line:' sentence.\n"
    "No preamble; do not restate these instructions."
)


def _load(data_dir: Path, name: str) -> Any:
    p = data_dir / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def build_context(data_dir: Path) -> dict[str, Any]:
    meta = _load(data_dir, "meta.json") or {}
    rec = _load(data_dir, "recommendation.json") or {}
    players = _load(data_dir, "players.json") or []

    def slim(p: dict) -> dict:
        return {
            "name": p["name"], "pos": p["pos"], "team": p["team"],
            "price": p["price"], "xp": p["next_gw_xp"], "why": p.get("rationale"),
        }

    top = [slim(p) for p in players[:8]]
    differentials = [
        slim(p) for p in players
        if 0 < (p.get("owned_by") or 0) < 8 and p["next_gw_xp"] >= 3.5
    ][:4]

    # your actual team (in-season, once picks are available)
    my = _load(data_dir, "my_team.json")
    your_team = None
    flag_pool = players[:40]
    if my and my.get("players"):
        your_team = [slim(p) for p in my["players"]]
        flag_pool = my["players"]  # prioritise injuries in YOUR squad

    flagged = [
        {"name": p["name"], "news": p.get("news"), "status": p.get("status")}
        for p in flag_pool
        if p.get("news") or (p.get("status") and p.get("status") != "a")
    ][:6]

    return {
        "your_team": your_team,
        "gameweek": meta.get("gw_name") or f"GW{meta.get('current_gw')}",
        "deadline": meta.get("deadline"),
        "recommendation": {
            "mode": rec.get("mode"),
            "formation": rec.get("formation"),
            "summary": rec.get("summary"),
            "squad_value": rec.get("squad_value"),
            "xi_expected": rec.get("xi_expected"),
            "captain": {
                "name": rec.get("captain", {}).get("name"),
                "why": rec.get("captain", {}).get("rationale"),
            },
            "transfers_in": [t.get("name") for t in rec.get("transfers_in", [])],
            "transfers_out": [t.get("name") for t in rec.get("transfers_out", [])],
        },
        "top_players": top,
        "differentials": differentials,
        "flagged_news": flagged,
    }


def _has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _ai_briefing(ctx: dict[str, Any], model: str) -> str:
    from anthropic import Anthropic  # lazy: only needed on the AI path

    client = Anthropic()
    prompt = (
        "Write this week's Gaffer's Verdict from the following model output. "
        "Use only these numbers.\n\n```json\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n```"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=1200,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _template_briefing(ctx: dict[str, Any]) -> str:
    rec = ctx["recommendation"]
    cap = rec.get("captain", {})
    lines: list[str] = [f"**{ctx['gameweek']}: {rec.get('summary', 'Set your team.')}**", ""]

    lines.append("**✅ What's strong**")
    if cap.get("name"):
        lines.append(f"- Captain **{cap['name']}** — {cap.get('why', '')}")
    if ctx.get("differentials"):
        d = ctx["differentials"][0]
        lines.append(
            f"- Value/differential: **{d['name']}** ({d['team']}, £{d['price']}m, {d['xp']} xP)."
        )
    if ctx.get("flagged_news"):
        n = ctx["flagged_news"][0]
        note = n.get("news") or "fitness doubt"
        lines.append(f"- ⚠️ Watch: **{n['name']}** — {note}")
    lines.append("")

    lines.append("**🔄 What I'd change**")
    if rec.get("mode") == "build":
        lines.append(f"- Build the {rec.get('formation')} for £{rec.get('squad_value')}m "
                     f"(projected {rec.get('xi_expected')} pts); prioritise the value defenders.")
    elif rec.get("transfers_in"):
        outs = ", ".join(rec["transfers_out"])
        ins = ", ".join(rec["transfers_in"])
        lines.append(f"- Out {outs} → in {ins}.")
    else:
        lines.append("- Nothing beats a -4 — roll the transfer.")
    lines.append("")
    cap_name = cap.get("name", "your best pick")
    lines.append(f"**Bottom line: captain {cap_name} and trust the process.**")
    return "\n".join(lines)


def generate(data_dir: Path | None = None, model: str | None = None) -> dict[str, Any]:
    data_dir = data_dir or config.DATA_DIR
    model = model or VERDICT_MODEL
    ctx = build_context(data_dir)

    source = "template"
    briefing = ""
    if _has_credentials():
        try:
            briefing = _ai_briefing(ctx, model)
            source = "ai"
        except Exception as exc:  # any API/SDK failure -> graceful fallback
            briefing = _template_briefing(ctx)
            source = f"template (ai failed: {type(exc).__name__})"
    else:
        briefing = _template_briefing(ctx)

    out = {
        "briefing_md": briefing,
        "model": model if source == "ai" else None,
        "source": source,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_json_atomic(data_dir / "verdict.json", out)
    return out
