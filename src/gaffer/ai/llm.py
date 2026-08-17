"""Shared thin wrapper over the Anthropic API for Gaffer's AI features.

Uses the official SDK. Callers must guard with ``narration_enabled()`` and fall
back to a deterministic template on any failure.

**Paid narration is off by default.** A configured API key is not consent to
spend: the key lives in CI for the day somebody wants prose, and a pipeline that
runs three times a day would otherwise bill for it indefinitely without anyone
choosing that. Set ``GAFFER_AI_NARRATION=1`` to turn it on.

The AI layer is a **narrator**. It reads numbers the pipeline has already
computed and writes sentences about them; it never calculates, ranks or alters
anything. Turning it off changes the words on the page and nothing else, which is
why the deterministic template renders the same shape.
"""

from __future__ import annotations

import os

#: Claude Opus 5. Two properties of this model shape the calls below.
#:
#: Thinking is ON BY DEFAULT — omitting the parameter runs adaptive, where Opus
#: 4.8 ran without thinking at all. Both call sites already pass
#: ``thinking={"type": "adaptive"}`` explicitly, so the default never applies and
#: the upgrade did not silently change what we ask for. Keep it explicit.
#:
#: ``max_tokens`` caps thinking AND response text together, and this model writes
#: longer by default than 4.8 did. The budgets below therefore carry more
#: headroom than a 4.8-era number would: a truncated briefing ships as a sentence
#: that stops mid-word, which reads as a bug rather than as brevity.
DEFAULT_MODEL = os.environ.get("GAFFER_AI_MODEL", "claude-opus-5")

#: Explicit opt-in for metered narration. Anything else is off.
NARRATION_ENV = "GAFFER_AI_NARRATION"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def narration_enabled() -> bool:
    """True only when paid narration is explicitly switched on *and* usable."""
    if os.environ.get(NARRATION_ENV, "").strip().lower() not in _TRUTHY:
        return False
    return has_credentials()


def complete(system: str, prompt: str, model: str | None = None, max_tokens: int = 1500) -> str:
    from anthropic import Anthropic  # lazy import

    client = Anthropic()
    msg = client.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()
