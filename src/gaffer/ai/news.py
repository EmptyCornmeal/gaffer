"""Real-world transfer news, with an FPL-angle digest.

Fetches free RSS transfer stories and (if an API key is set) asks Claude for a
short 'what matters for FPL' digest. Falls back to a headline list otherwise.
Writes ``news.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gaffer import config
from gaffer.ai import llm
from gaffer.sources.news import fetch_transfer_news

SYSTEM = (
    "You are an FPL analyst. From these real-world football transfer headlines, "
    "write a short GitHub-flavoured markdown digest of the moves that matter for "
    "Fantasy Premier League 2026/27 — new Premier League signings to watch, who "
    "gains or loses from a move, and any role/price implications. Use only the "
    "headlines provided; do not invent transfers. Bullet points, under 140 words, "
    "no preamble."
)


def _template_digest(items: list[dict[str, Any]]) -> str:
    if not items:
        return "_No transfer stories fetched right now._"
    lines = ["**Latest transfer talk:**", ""]
    for it in items[:8]:
        lines.append(f"- {it['title']} _({it['source']})_")
    return "\n".join(lines)


def generate(data_dir: Path | None = None, model: str | None = None) -> dict[str, Any]:
    data_dir = data_dir or config.DATA_DIR
    items = fetch_transfer_news()

    source = "template"
    digest = _template_digest(items)
    if items and llm.has_credentials():
        try:
            headlines = "\n".join(f"- [{it['source']}] {it['title']}" for it in items)
            digest = llm.complete(SYSTEM, headlines, model=model, max_tokens=900)
            source = "ai"
        except Exception as exc:  # graceful fallback
            digest = _template_digest(items)
            source = f"template (ai failed: {type(exc).__name__})"

    out = {
        "items": items,
        "digest_md": digest,
        "source": source,
        "count": len(items),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (data_dir / "news.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out
