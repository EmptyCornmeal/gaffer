"""Real-world transfer news, with an FPL-angle digest.

Fetches free RSS transfer stories and (if an API key is set) asks Claude for a
short 'what matters for FPL' digest. Falls back to a headline list otherwise.
Writes ``news.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gaffer import config
from gaffer.ai import llm
from gaffer.io import write_json_atomic
from gaffer.sources.news import fetch_transfer_news

# Match keywords per FPL short_name (only shorts present in the current 20 are used).
_CLUB_ALIASES: dict[str, list[str]] = {
    "ARS": ["arsenal"], "AVL": ["aston villa", "villa"], "BOU": ["bournemouth"],
    "BRE": ["brentford"], "BHA": ["brighton"], "BUR": ["burnley"], "CHE": ["chelsea"],
    "CRY": ["crystal palace", "palace"], "EVE": ["everton"], "FUL": ["fulham"],
    "IPS": ["ipswich"], "LEE": ["leeds"], "LEI": ["leicester"], "LIV": ["liverpool"],
    "MCI": ["man city", "manchester city"], "MUN": ["man utd", "man united", "manchester united"],
    "NEW": ["newcastle"], "NFO": ["nottingham forest", "nott'm forest", "forest"],
    "SHU": ["sheffield united"], "SOU": ["southampton"], "SUN": ["sunderland"],
    "TOT": ["tottenham", "spurs"], "WHU": ["west ham"], "WOL": ["wolves", "wolverhampton"],
    "LUT": ["luton"], "COV": ["coventry"], "HUL": ["hull"], "NOR": ["norwich"],
    "WBA": ["west brom", "west bromwich"], "MID": ["middlesbrough"], "BIR": ["birmingham"],
}

SYSTEM_TMPL = (
    "You are an FPL analyst writing a short, HIGH-TRUST transfer digest. "
    "The Premier League 2026/27 clubs are: {clubs}.\n\n"
    "STRICT RULES — accuracy over completeness:\n"
    "1. Use ONLY facts stated verbatim in the headlines provided. Do NOT add, "
    "estimate, or infer anything not written there.\n"
    "2. NEVER invent or guess transfer fees, values, wages, or numbers. If a fee "
    "isn't in the headline, don't mention one.\n"
    "3. NEVER invent player names, coaches, or backroom-staff moves. Ignore "
    "manager/coach/staff stories entirely — they aren't FPL-relevant.\n"
    "4. A headline that says 'linked', 'eyeing', 'chasing', 'talks', 'rumour' is "
    "NOT a done deal — write it as a rumour ('rumoured', 'linked') or skip it.\n"
    "5. Only include a bullet if the headline clearly involves one of the listed "
    "clubs. If you're unsure whether a club is in the list, SKIP it — do not "
    "speculate about who is or isn't in the Premier League.\n"
    "6. Men's football only — ignore women's/WSL entirely.\n"
    "7. Prefer FEWER, confident bullets. It is better to return 3 solid lines than "
    "8 shaky ones. If nothing is clearly relevant, say so in one line.\n\n"
    "For each real, relevant move give the club, the player, and the one-line FPL "
    "angle (new asset to watch, who gains minutes, set-piece/penalty implication). "
    "GitHub-flavoured markdown bullets, under 130 words, no preamble."
)


def _template_digest(items: list[dict[str, Any]]) -> str:
    if not items:
        return "_No transfer stories fetched right now._"
    lines = ["**Latest transfer talk:**", ""]
    for it in items[:8]:
        lines.append(f"- {it['title']} _({it['source']})_")
    return "\n".join(lines)


def _keywords_for(clubs: list[tuple[str, str]]) -> list[str]:
    """Build lowercase match keywords for the current PL clubs (name + aliases)."""
    kws: set[str] = set()
    for name, short in clubs:
        kws.update(_CLUB_ALIASES.get(short, []))
        n = (name or "").lower()
        if len(n) > 3:
            kws.add(n)
    return sorted(kws)


def generate(
    data_dir: Path | None = None,
    model: str | None = None,
    clubs: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    data_dir = data_dir or config.DATA_DIR
    keywords = _keywords_for(clubs) if clubs else None
    club_names = ", ".join(sorted(n for n, _ in clubs)) if clubs else "the 20 Premier League clubs"
    items = fetch_transfer_news(club_keywords=keywords)

    source = "template"
    digest = _template_digest(items)
    if items and llm.has_credentials():
        try:
            headlines = "\n".join(f"- [{it['source']}] {it['title']}" for it in items)
            system = SYSTEM_TMPL.format(clubs=club_names)
            digest = llm.complete(system, headlines, model=model, max_tokens=900)
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
    write_json_atomic(data_dir / "news.json", out)
    return out
