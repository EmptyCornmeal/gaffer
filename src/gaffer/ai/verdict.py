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

VERDICT_MODEL = os.environ.get("GAFFER_VERDICT_MODEL", "claude-opus-4-8")

SYSTEM = (
    "You are 'The Gaffer', a sharp, confident FPL analyst writing a short weekly "
    "briefing for a Fantasy Premier League dashboard used by many managers. "
    "Write in plain English with a bit of touchline swagger, but stay strictly "
    "grounded in the numbers you are given — never invent players, prices, "
    "fixtures, injuries, or stats not present in the data. Be decisive.\n\n"
    "Output GitHub-flavoured markdown, under ~180 words, in this shape:\n"
    "- A bold one-line headline (the week's single biggest call).\n"
    "- 2-3 short paragraphs or bullets covering: the recommended squad/transfer "
    "and captain (with the why), one differential worth a look, and one risk to "
    "watch (rotation/injury/tough fixtures).\n"
    "- A final bold 'Bottom line:' sentence.\n"
    "Do not include a preamble or restate these instructions."
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
    flagged = [
        {"name": p["name"], "news": p["news"]}
        for p in players[:40] if p.get("news")
    ][:5]

    return {
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
    lines: list[str] = []
    lines.append(f"**{ctx['gameweek']}: {rec.get('summary', 'Set your team.')}**")
    lines.append("")
    if rec.get("mode") == "build":
        lines.append(
            f"The model's optimal {rec.get('formation')} squad comes in at "
            f"£{rec.get('squad_value')}m for a projected {rec.get('xi_expected')} pts."
        )
    elif rec.get("transfers_in"):
        lines.append(
            f"Move: out {', '.join(rec['transfers_out'])} → in "
            f"{', '.join(rec['transfers_in'])}."
        )
    else:
        lines.append("No transfer clears its hit — roll it.")
    if cap.get("name"):
        lines.append(f"**Captain {cap['name']}** — {cap.get('why', '')}")
    if ctx["differentials"]:
        d = ctx["differentials"][0]
        lines.append(f"Differential: **{d['name']}** ({d['team']}, £{d['price']}m, {d['xp']} xP).")
    if ctx["flagged_news"]:
        n = ctx["flagged_news"][0]
        lines.append(f"Watch: {n['name']} — {n['news']}")
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
    (data_dir / "verdict.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out
