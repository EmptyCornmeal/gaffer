"""Dependency reproducibility: does the lock match the declaration, and the
environment match the lock?

Three failures this exists to catch, all of which the repository actually had:

1. **An undeclared import.** `numpy` was imported by four modules and declared by
   none — invisible, because pandas always dragged it in. It works until the day
   pandas drops it.
2. **A lock that drifts from `pyproject.toml`.** A cap tightened in the project
   file and not re-locked means CI installs something the author never ran.
3. **An environment that drifts from the lock.** A `pip install -U` in a hurry,
   and the machine that produces the artifacts stops matching the recipe.

    python -m gaffer.deps            # check all three, exit 1 on drift
    python -m gaffer.deps --json
    python -m gaffer.deps --regenerate-hint
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tomllib
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from gaffer import config

LOCK_PATH = config.REPO_ROOT / "requirements.lock.txt"
PYPROJECT_PATH = config.REPO_ROOT / "pyproject.toml"

#: Import name -> distribution name, where they differ.
IMPORT_TO_DIST = {"yaml": "pyyaml", "dateutil": "python-dateutil"}

#: Imported by tests/tooling rather than the package. Declared in [dev].
DEV_ONLY_IMPORTS = {"pytest", "yaml", "ruff", "packaging"}


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


@dataclass
class DepReport:
    ok: bool = True
    undeclared_imports: list[str] = field(default_factory=list)
    missing_from_lock: list[str] = field(default_factory=list)
    lock_violates_declaration: list[str] = field(default_factory=list)
    environment_drift: list[str] = field(default_factory=list)
    lock_entries: int = 0
    python: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items()}
        d["ok"] = self.ok
        return d

    def render(self) -> str:
        if self.ok:
            return (f"dependencies OK — {self.lock_entries} locked packages, "
                    f"environment matches, on Python {self.python}")
        lines = ["dependency check FAILED:"]
        for label, items, fix in (
            ("imported but not declared in pyproject.toml", self.undeclared_imports,
             "add it to [project] dependencies (or [dev] if it is tooling)"),
            ("declared but absent from requirements.lock.txt", self.missing_from_lock,
             "regenerate the lock — see --regenerate-hint"),
            ("locked at a version the declaration forbids", self.lock_violates_declaration,
             "regenerate the lock — see --regenerate-hint"),
            ("installed version differs from the lock", self.environment_drift,
             "pip install -r requirements.lock.txt"),
        ):
            if items:
                lines.append(f"  {label}:")
                lines += [f"    - {i}" for i in items]
                lines.append(f"    fix: {fix}")
        return "\n".join(lines)


def declared() -> dict[str, list[str]]:
    """Every requirement string in pyproject, by group."""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    out = {"main": list(data["project"].get("dependencies", []))}
    for group, reqs in data["project"].get("optional-dependencies", {}).items():
        out[group] = list(reqs)
    return out


def parse_lock(text: str | None = None) -> dict[str, str]:
    """`name -> exact version` from the lockfile. Markers are kept out of the key."""
    return {name: version for name, version, _ in _lock_entries(text)}


def _lock_entries(text: str | None = None) -> list[tuple[str, str, str | None]]:
    """`(name, version, marker)` per line, marker None when unconditional."""
    raw = text if text is not None else LOCK_PATH.read_text(encoding="utf-8")
    out: list[tuple[str, str, str | None]] = []
    for line in raw.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        spec, _, marker = line.partition(";")
        spec = spec.strip()
        if "==" not in spec:
            continue
        name, _, version = spec.partition("==")
        out.append((_norm(name), version.strip(), marker.strip() or None))
    return out


def applies_here(marker: str | None) -> bool:
    """Whether a lock entry's environment marker matches this interpreter.

    `pywin32` is a Windows-only transitive of the MCP SDK and has no Linux
    distribution at all, so a lockfile that pins it unconditionally cannot be
    installed in CI. A lock is only reproducible if it says *where* each pin
    applies — and only if the checker agrees, or every Linux run reports drift
    for a package that is correctly absent.
    """
    if not marker:
        return True
    from packaging.markers import InvalidMarker, Marker
    try:
        return Marker(marker).evaluate()
    except InvalidMarker:  # pragma: no cover - defensive
        return True


def third_party_imports(src: Path | None = None) -> set[str]:
    """Distribution names the package imports and does not vendor."""
    src = src or Path(config.REPO_ROOT) / "src"
    std = set(sys.stdlib_module_names)
    found: set[str] = set()
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {IMPORT_TO_DIST.get(m, m) for m in found
            if m not in std and m != "gaffer"}


def check(*, check_environment: bool = True) -> DepReport:
    from packaging.requirements import Requirement

    rep = DepReport(python=f"{sys.version_info.major}.{sys.version_info.minor}."
                           f"{sys.version_info.micro}")
    groups = declared()
    declared_names = {_norm(Requirement(r).name)
                      for reqs in groups.values() for r in reqs}

    for dist in sorted(third_party_imports()):
        if _norm(dist) not in declared_names:
            rep.undeclared_imports.append(dist)

    lock = parse_lock()
    rep.lock_entries = len(lock)
    for group, reqs in groups.items():
        for raw in reqs:
            req = Requirement(raw)
            key = _norm(req.name)
            if key not in lock:
                rep.missing_from_lock.append(f"{req.name} ({group})")
            elif not req.specifier.contains(lock[key], prereleases=True):
                rep.lock_violates_declaration.append(
                    f"{req.name}=={lock[key]} violates '{req.specifier}' ({group})")

    if check_environment:
        for name, want, marker in sorted(_lock_entries()):
            if not applies_here(marker):
                continue  # correctly absent on this platform
            try:
                have = metadata.version(name)
            except metadata.PackageNotFoundError:
                rep.environment_drift.append(f"{name}: locked {want}, not installed")
                continue
            if _norm(have) != _norm(want):
                rep.environment_drift.append(f"{name}: locked {want}, installed {have}")

    rep.ok = not (rep.undeclared_imports or rep.missing_from_lock
                  or rep.lock_violates_declaration or rep.environment_drift)
    return rep


REGENERATE_HINT = """\
Regenerate requirements.lock.txt from a clean environment — never from the
working venv, which accumulates whatever was ever installed in it:

    python -m venv /tmp/lockenv
    /tmp/lockenv/bin/python -m pip install --upgrade pip
    /tmp/lockenv/bin/python -m pip install ".[ai,dev]"
    /tmp/lockenv/bin/python -m pip freeze --exclude-editable \\
        | grep -v '^gaffer' | sort > requirements.lock.txt

Then re-add the header and the environment markers — `pip freeze` does not emit
them, and a platform-specific pin without one cannot be installed anywhere else:

    colorama==0.4.6 ; sys_platform == "win32"
    pywin32==312    ; sys_platform == "win32"

Run `python -m gaffer.deps` to confirm. On Windows use `.venv/Scripts/python.exe`.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check dependency reproducibility")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-environment", action="store_true",
                    help="check the lock against pyproject only, not the "
                         "installed environment")
    ap.add_argument("--regenerate-hint", action="store_true",
                    help="print how to rebuild the lockfile, and exit")
    args = ap.parse_args(argv)
    if args.regenerate_hint:
        print(REGENERATE_HINT)
        return 0
    rep = check(check_environment=not args.no_environment)
    print(json.dumps(rep.as_dict(), indent=2) if args.json else rep.render())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
