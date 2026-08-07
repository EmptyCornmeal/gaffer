"""Notification engine (T-24) — provider-neutral, dry-run by default.

Gaffer could not reach the user at all. The deadline countdown lived in a topbar
you had to already be looking at, which is the opposite of a reminder. That is
the single biggest gap against the second half of the brief: a 2,069-point
manager who plays every gameweek beats a 2,200-point model that gets abandoned
in November.

This package builds and records alerts. It deliberately does **not** send them.

  * ``DRY_RUN`` is the default everywhere, and tests force it on at import.
  * The only sink wired by default is :class:`MemorySink`, which stores and
    discards.
  * Real provider adapters exist, take their credentials from the environment,
    and refuse to construct without them — so nothing can half-send.
  * No scheduler is installed or activated. The launchd template is a documented
    file the user runs themselves.

A provider failure is recorded against the alert and never propagates: the data
pipeline must publish whether or not a phone buzzed.
"""

from gaffer.notify.engine import (  # noqa: F401
    DRY_RUN_DEFAULT,
    Alert,
    Engine,
    EngineResult,
    Severity,
    quiet_hours,
)
from gaffer.notify.rules import ALL_KINDS, build_alerts  # noqa: F401
from gaffer.notify.sinks import (  # noqa: F401
    ConfigError,
    MemorySink,
    Sink,
    resolve_sink,
)

__all__ = [
    "ALL_KINDS", "Alert", "ConfigError", "DRY_RUN_DEFAULT", "Engine",
    "EngineResult", "MemorySink", "Severity", "Sink", "build_alerts",
    "quiet_hours", "resolve_sink",
]
