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
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gaffer import config
from gaffer.ai import grounding as G
from gaffer.ai import llm
from gaffer.io import write_json_atomic

VERDICT_MODEL = os.environ.get("GAFFER_VERDICT_MODEL", "claude-opus-4-8")

SYSTEM = (
    "You are 'The Gaffer', a sharp, confident FPL analyst writing a short weekly "
    "briefing for a Fantasy Premier League dashboard. Write in plain English with "
    "a bit of touchline swagger, but stay strictly grounded in the numbers given "
    "— never invent players, prices, fixtures, injuries, or stats. Be decisive.\n\n"
    "GROUNDING RULES — these override style, and breaking them invalidates the "
    "briefing:\n"
    "1. The context contains 'selected_squad' — the exact 15 the model chose, "
    "split into starting_xi and bench, plus captain, vice_captain and formation. "
    "That squad IS the subject of the briefing.\n"
    "2. When describing 'your squad', 'the XI', 'the bench', 'the defence', 'the "
    "forwards' or 'the recommended team', you may name ONLY players listed in "
    "selected_squad.starting_xi or selected_squad.bench.\n"
    "3. Never state or imply that a player outside selected_squad is in the team. "
    "'top_players' and 'differentials' are league-wide context, NOT your squad — "
    "most of them are not selected.\n"
    "4. If you mention a non-selected player at all, you must label it explicitly "
    "as an alternative or a watch-list name (e.g. 'not in the squad, but ...'). "
    "Prefer not to mention them.\n"
    "5. Do not invent transfers. Only 'transfers_in'/'transfers_out' are real "
    "moves; if both are empty this is either a fresh build or a roll.\n"
    "6. The structured numbers are authoritative. If the context looks incomplete, "
    "say so plainly rather than filling the gap.\n\n"
    "If a 'your_team' is provided, the briefing is about THAT squad. Otherwise it "
    "is about selected_squad.\n\n"
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

# Catalogue names that are also ordinary English words. Matching is
# case-sensitive and word-bounded, but these still produce false positives in
# normal prose ("Long-term", "Best of the bunch"), so they never trigger a
# violation on their own.
AMBIGUOUS_NAMES = frozenset({
    "Best", "Bright", "Cash", "Long", "King", "Young", "Wood", "Ward", "Bell",
    "Reed", "Rice", "Sun", "Fry", "May", "Moore", "Power", "Stone", "Wells",
})


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

    def squad_card(p: dict) -> dict:
        """Structured, id-bearing entry so validation is exact, not fuzzy."""
        return {
            "id": p.get("id"), "name": p.get("name"), "pos": p.get("pos"),
            "team": p.get("team"), "price": p.get("price"),
            "xp": p.get("next_gw_xp"), "xmins": p.get("xmins_badge"),
            "why": p.get("rationale"),
        }

    starting = [squad_card(p) for p in rec.get("starting") or []]
    bench = [squad_card(p) for p in rec.get("bench") or []]
    cap_card = rec.get("captain") or {}
    vice_card = rec.get("vice") or {}

    return {
        "your_team": your_team,
        "gameweek": meta.get("gw_name") or f"GW{meta.get('current_gw')}",
        "deadline": meta.get("deadline"),
        # The authoritative subject of the briefing. Everything else is context.
        "selected_squad": {
            "formation": rec.get("formation"),
            "mode": rec.get("mode"),
            "squad_value": rec.get("squad_value"),
            "xi_expected": rec.get("xi_expected"),
            "starting_xi": starting,
            "bench": bench,
            "captain": {
                "id": cap_card.get("id"), "name": cap_card.get("name"),
                "why": cap_card.get("rationale"),
            },
            "vice_captain": {
                "id": vice_card.get("id"), "name": vice_card.get("name"),
            },
            "transfers_in": [
                {"id": t.get("id"), "name": t.get("name")}
                for t in rec.get("transfers_in") or []
            ],
            "transfers_out": [
                {"id": t.get("id"), "name": t.get("name")}
                for t in rec.get("transfers_out") or []
            ],
            "hits": rec.get("hits"),
        },
        "recommendation": {
            "mode": rec.get("mode"),
            "formation": rec.get("formation"),
            "summary": rec.get("summary"),
            "squad_value": rec.get("squad_value"),
            "xi_expected": rec.get("xi_expected"),
            "captain": {
                "name": cap_card.get("name"),
                "why": cap_card.get("rationale"),
            },
            "transfers_in": [t.get("name") for t in rec.get("transfers_in", [])],
            "transfers_out": [t.get("name") for t in rec.get("transfers_out", [])],
        },
        "note_on_context": (
            "top_players and differentials are league-wide context and are NOT in "
            "the squad unless they also appear in selected_squad."
        ),
        "top_players": top,
        "differentials": differentials,
        "flagged_news": flagged,
    }


def squad_names(ctx: dict[str, Any]) -> set[str]:
    """Names the briefing is allowed to present as selected."""
    sq = ctx.get("selected_squad") or {}
    names = {
        p.get("name") for p in (sq.get("starting_xi") or []) + (sq.get("bench") or [])
        if p.get("name")
    }
    for key in ("captain", "vice_captain"):
        card = sq.get(key) or {}
        if card.get("name"):
            names.add(card["name"])
    for key in ("transfers_in", "transfers_out"):
        for t in sq.get(key) or []:
            if t.get("name"):
                names.add(t["name"])
    # A user's real squad is equally legitimate subject matter.
    for p in ctx.get("your_team") or []:
        if p.get("name"):
            names.add(p["name"])
    return names


def catalogue_names(data_dir: Path) -> set[str]:
    """Every player name FPL currently knows about."""
    players = _load(data_dir, "players.json") or []
    return {p["name"] for p in players if isinstance(p, dict) and p.get("name")}


# Phrases that explicitly mark a player as NOT part of the selected squad.
# A mention inside such a sentence is a legitimate alternative, not a claim.
_ALTERNATIVE_MARKERS = (
    "not in the squad", "not in your squad", "not selected", "not owned",
    "outside the squad", "alternative", "alternatives", "watch-list",
    "watchlist", "watch list", "if you own", "keep an eye", "worth watching",
    "on the radar", "elsewhere", "consider later", "for next week",
)


def _sentences(text: str) -> list[str]:
    """Split on sentence and markdown-bullet boundaries."""
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def find_unselected_mentions(
    text: str, selected: set[str], catalogue: set[str]
) -> list[str]:
    """Catalogue players the briefing presents as being in the squad.

    Case-sensitive and word-bounded against the real catalogue rather than naive
    substring matching, so 'Rice' does not match 'price' and an arbitrary English
    word cannot be mistaken for a player. Hyphenated compounds ('Long-term') and
    a small ambiguous-name list are excluded to avoid false positives.

    A mention in a sentence that explicitly labels it as an alternative or a
    watch-list name is permitted — the rule is "never imply selected", not
    "never mention".
    """
    if not text:
        return []
    candidates = sorted(n for n in (catalogue - selected) if n not in AMBIGUOUS_NAMES)
    if not candidates:
        return []

    hits: list[str] = []
    sentences = _sentences(text)
    for name in candidates:
        # Names carry dots and accents ("B.Fernandes", "João Pedro"), so \b is
        # unreliable — bound on the character classes that make up a name.
        pattern = (
            r"(?<![0-9A-Za-zÀ-ɏ.])"
            + re.escape(name)
            + r"(?![0-9A-Za-zÀ-ɏ]|-\w)"
        )
        for sentence in sentences:
            if not re.search(pattern, sentence):
                continue
            low = sentence.lower()
            if any(marker in low for marker in _ALTERNATIVE_MARKERS):
                continue  # explicitly labelled as not-selected
            hits.append(name)
            break
    return hits


def _has_credentials() -> bool:
    return llm.has_credentials()


def _ai_briefing(ctx: dict[str, Any], model: str, correction: str | None = None) -> str:
    from anthropic import Anthropic  # lazy: only needed on the AI path

    client = Anthropic()
    prompt = (
        "Write this week's Gaffer's Verdict from the following model output. "
        "Use only these numbers.\n\n```json\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n```"
    )
    if correction:
        prompt += (
            "\n\nYour previous attempt was REJECTED. It named these players as if "
            f"they were in the squad, but they are not selected: {correction}.\n"
            "Rewrite it naming only players from selected_squad.starting_xi and "
            "selected_squad.bench."
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


def _season(data_dir) -> str:
    """The season the rest of this artifact set declares.

    Read from `meta.json` rather than `config.SEASON`: the constant is edited by
    hand once a year and is a full season stale the moment a rollover happens,
    and this file must agree with the set it is published alongside.
    """
    import json as _json

    path = data_dir / "meta.json"
    if path.exists():
        try:
            got = _json.loads(path.read_text(encoding="utf-8")).get("season")
            if isinstance(got, str) and got:
                return got
        except (ValueError, OSError):
            pass
    return config.SEASON


def context_numbers(ctx: dict[str, Any]) -> set[str]:
    """Every number the briefing is allowed to state.

    Walks the whole supplied context and collects its numeric values, at the
    precisions a writer might use. Omitting a number is fine; inventing one is
    not, and "£12.0m" for a player priced 12.0 must not be treated as invented.
    """
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            out.add(f"{node:g}")
            out.add(f"{float(node):.1f}")
            out.add(f"{float(node):.2f}")
            out.add(str(int(round(float(node)))))
        elif isinstance(node, str):
            out.update(G.numbers_in(node))

    walk(ctx)
    # Gameweek numbers and the small integers of FPL scoring are structural.
    out |= {str(i) for i in range(0, 39)}
    return out


def find_ungrounded_numbers(text: str, ctx: dict[str, Any]) -> list[str]:
    """Numeric claims in the briefing that are not in the supplied context."""
    return sorted(G.ungrounded_numbers(text, "", context_numbers(ctx)))


def generate(
    data_dir: Path | None = None, model: str | None = None, max_attempts: int = 2
) -> dict[str, Any]:
    data_dir = data_dir or config.DATA_DIR
    model = model or VERDICT_MODEL
    ctx = build_context(data_dir)
    selected = squad_names(ctx)
    catalogue = catalogue_names(data_dir)

    source, reason = G.SOURCE_TEMPLATE, None
    briefing = ""
    violations: list[str] = []
    numeric_violations: list[str] = []

    # Paid narration is opt-in. Without it the deterministic briefing ships —
    # same shape, same numbers, no metered call. The numbers are the pipeline's
    # either way; the model never computes one.
    if not llm.narration_enabled():
        briefing = _template_briefing(ctx)
        reason = (G.REASON_NARRATION_DISABLED if _has_credentials()
                  else G.REASON_NO_CREDENTIALS)
    else:
        correction: str | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                candidate = _ai_briefing(ctx, model, correction=correction)
            except Exception as exc:  # noqa: BLE001 - reported as a stable code
                briefing = _template_briefing(ctx)
                reason = G.error_reason(exc)
                violations = []
                break
            if not candidate.strip():
                briefing = _template_briefing(ctx)
                reason = G.REASON_EMPTY_OUTPUT
                break
            bad = find_unselected_mentions(candidate, selected, catalogue)
            # A number nobody supplied is an invention, and prices and expected
            # points are exactly what a reader would act on.
            bad_numbers = find_ungrounded_numbers(candidate, ctx)
            if not bad and not bad_numbers:
                briefing, source, reason, violations = candidate, G.SOURCE_AI, None, []
                numeric_violations = []
                break
            violations, numeric_violations = bad, bad_numbers
            correction = "; ".join(filter(None, [
                ", ".join(bad),
                ("do not state these numbers: " + ", ".join(bad_numbers))
                if bad_numbers else "",
            ]))
            if attempt == max_attempts:
                # Never publish prose that contradicts the squad on screen, or
                # that states a figure nothing supplied.
                briefing = _template_briefing(ctx)
                reason = G.REASON_GROUNDING_REJECTED

    # The template path is generated from the squad itself, so it is grounded by
    # construction — but assert it, so a future template edit can't regress.
    if source == G.SOURCE_TEMPLATE:
        violations = find_unselected_mentions(briefing, selected, catalogue)
        numeric_violations = find_ungrounded_numbers(briefing, ctx)

    out = {
        "briefing_md": briefing,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        # Structured grounding record: what the prose was allowed to name, and
        # whether it stayed inside that set.
        "squad_player_ids": sorted(
            p["id"] for p in (
                (ctx.get("selected_squad") or {}).get("starting_xi") or []
            ) + ((ctx.get("selected_squad") or {}).get("bench") or [])
            if p.get("id") is not None
        ),
        "validation": {
            "ok": not violations and not numeric_violations,
            "unselected_mentions": violations,
            "ungrounded_numbers": numeric_violations,
        },
        **G.envelope(source, reason=reason, model=model),
    }
    out["season"] = _season(data_dir)
    write_json_atomic(data_dir / "verdict.json", out)
    return out
