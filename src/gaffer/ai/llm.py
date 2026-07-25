"""Shared thin wrapper over the Anthropic API for Gaffer's AI features.

Uses the official SDK. Default model ``claude-opus-4-8`` (best writing); the
caller can override. Callers should guard with ``has_credentials()`` and fall
back to a deterministic template on any failure.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = os.environ.get("GAFFER_AI_MODEL", "claude-opus-4-8")


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def complete(system: str, prompt: str, model: str | None = None, max_tokens: int = 1000) -> str:
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
