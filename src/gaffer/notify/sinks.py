"""Delivery adapters. The default one delivers nowhere.

Every real provider reads its credentials from the environment and refuses to
construct without them. That is deliberate: a half-configured provider that
silently no-ops is worse than one that will not start, because you find out it
never worked on the week you needed it.

No credential is ever written to an artifact, a log line, or an exception
message — :func:`describe` is the only thing that gets published, and it reports
whether a variable is *set*, never its value.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol


class ConfigError(RuntimeError):
    """A provider was selected but its configuration is incomplete."""


class Sink(Protocol):
    def send(self, alert: Any) -> None: ...


class MemorySink:
    """Stores alerts and delivers nothing. The default, and the test sink."""

    name = "memory"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, alert: Any) -> None:
        self.sent.append(alert)

    def __len__(self) -> int:
        return len(self.sent)


class ConsoleSink:
    """Prints. Useful for a manual dry run on the Mac Mini."""

    name = "console"

    def send(self, alert: Any) -> None:
        print(f"[{alert.severity}] {alert.title}\n    {alert.body}\n    "
              f"{alert.deep_link}")


class WebhookSink:
    """POSTs JSON to a URL from ``GAFFER_NOTIFY_WEBHOOK``.

    Provider-neutral on purpose: Discord, Slack, ntfy, Telegram bots and Home
    Assistant all accept a JSON POST, so one adapter covers the realistic set
    without committing Gaffer to anyone's SDK.
    """

    name = "webhook"
    ENV = "GAFFER_NOTIFY_WEBHOOK"

    def __init__(self, url: str | None = None, timeout: float = 10.0) -> None:
        url = url or os.environ.get(self.ENV, "").strip()
        if not url:
            raise ConfigError(
                f"the webhook sink needs {self.ENV} to be set to a full https URL")
        if not url.startswith("https://"):
            raise ConfigError(f"{self.ENV} must be an https URL")
        self.url = url
        self.timeout = timeout

    def send(self, alert: Any) -> None:
        payload = json.dumps({
            "title": alert.title, "body": alert.body,
            "severity": alert.severity, "kind": alert.kind,
            "link": alert.deep_link,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "gaffer-notify/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status >= 400:
                    raise ConfigError(f"webhook returned HTTP {r.status}")
        except urllib.error.URLError as exc:
            # Re-raised as a plain message: the URL may embed a token, so it must
            # not reach a log line or an artifact.
            raise ConfigError(f"webhook delivery failed: {exc.reason}") from None


SINKS = {"memory": MemorySink, "console": ConsoleSink, "webhook": WebhookSink}


def resolve_sink(name: str | None = None) -> Sink:
    """Build the configured sink. Defaults to the one that sends nothing."""
    name = (name or os.environ.get("GAFFER_NOTIFY_SINK", "memory")).strip().lower()
    cls = SINKS.get(name)
    if cls is None:
        raise ConfigError(
            f"unknown notification sink {name!r}; choose one of {sorted(SINKS)}")
    return cls()


def describe(name: str | None = None) -> dict[str, Any]:
    """Publishable configuration status. Reports presence, never values."""
    name = (name or os.environ.get("GAFFER_NOTIFY_SINK", "memory")).strip().lower()
    known = name in SINKS
    required = {"webhook": [WebhookSink.ENV]}.get(name, [])
    missing = [v for v in required if not os.environ.get(v, "").strip()]
    return {
        "sink": name,
        "known": known,
        "configured": known and not missing,
        "required_env": required,
        "missing_env": missing,
        "note": ("no delivery is attempted in dry-run mode regardless of "
                 "configuration"),
    }
