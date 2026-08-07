"""T-28 — the lock, the declaration and the environment must agree.

`numpy` was imported by four modules and declared by none for the whole of
Batches 1-5. Nothing noticed, because pandas always installed it. These tests
are the thing that would have noticed.
"""

from __future__ import annotations

import tomllib

import pytest

from gaffer import config, deps


def test_the_lock_satisfies_every_declared_dependency():
    rep = deps.check(check_environment=False)
    assert rep.missing_from_lock == [], rep.render()
    assert rep.lock_violates_declaration == [], rep.render()


def test_nothing_is_imported_that_nothing_declares():
    rep = deps.check(check_environment=False)
    assert rep.undeclared_imports == [], rep.render()


def test_the_lock_covers_production_ai_and_dev_in_one_file():
    """Two requirements files disagree eventually. There is exactly one."""
    root = config.REPO_ROOT
    extra = [p.name for p in root.glob("requirements*.txt")
             if p.name != "requirements.lock.txt"]
    assert extra == [], f"a second requirements file appeared: {extra}"
    lock = deps.parse_lock()
    groups = deps.declared()
    assert set(groups) >= {"main", "ai", "dev"}
    for group in ("main", "ai", "dev"):
        for raw in groups[group]:
            name = deps._norm(raw.split(">")[0].split("<")[0].split("=")[0].split("[")[0])
            assert name in lock, f"{name} ({group}) is not locked"


def test_the_supported_python_range_is_narrow_and_matches_the_pin():
    data = tomllib.loads(
        (config.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = data["project"]["requires-python"]
    assert ">=" in spec and "<" in spec, \
        f"requires-python must be bounded at both ends, got {spec!r}"
    pinned = (config.REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    from packaging.specifiers import SpecifierSet
    assert SpecifierSet(spec).contains(f"{pinned}.0"), \
        f".python-version ({pinned}) is outside requires-python ({spec})"


def test_the_pulp_cap_is_still_in_place():
    """PuLP 4.0 removes two APIs the solver calls on every run."""
    groups = deps.declared()
    pulp = next(r for r in groups["main"] if r.lower().startswith("pulp"))
    assert "<4.0" in pulp, f"the deliberate PuLP cap is gone: {pulp}"
    assert deps.parse_lock()["pulp"].startswith("3."), "locked PuLP is not 3.x"


def test_ci_installs_the_same_thing_the_developer_does():
    """The refresh workflow must install from the lock, not re-resolve."""
    wf = (config.REPO_ROOT / ".github" / "workflows"
          / "refresh.yml").read_text(encoding="utf-8")
    assert "pip install -r requirements.lock.txt" in wf
    assert "pip install -e . --no-deps" in wf
    assert "python -m gaffer.deps" in wf, "CI must run the dependency check"
    assert "python-version-file: .python-version" in wf, \
        "CI must take its Python version from the pinned file"


def test_node_version_is_declared_once_and_used_by_ci():
    nvmrc = (config.REPO_ROOT / "web" / ".nvmrc").read_text(encoding="utf-8").strip()
    assert nvmrc.isdigit()
    import json
    pkg = json.loads(
        (config.REPO_ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    engines = pkg.get("engines", {}).get("node", "")
    assert engines.startswith(f">={nvmrc}"), \
        f"package.json engines ({engines}) disagrees with .nvmrc ({nvmrc})"
    deploy = (config.REPO_ROOT / ".github" / "workflows"
              / "deploy.yml").read_text(encoding="utf-8")
    assert "node-version-file: web/.nvmrc" in deploy


# --- the checker itself ------------------------------------------------------

def test_an_undeclared_import_is_reported(tmp_path, monkeypatch):
    pkg = tmp_path / "src" / "gaffer"
    pkg.mkdir(parents=True)
    (pkg / "x.py").write_text("import requests\nimport json\n", encoding="utf-8")
    found = deps.third_party_imports(tmp_path / "src")
    assert "requests" in found
    assert "json" not in found, "the standard library is not a dependency"


def test_import_names_that_differ_from_distribution_names_are_mapped(tmp_path):
    pkg = tmp_path / "src" / "gaffer"
    pkg.mkdir(parents=True)
    (pkg / "y.py").write_text("import yaml\n", encoding="utf-8")
    assert deps.third_party_imports(tmp_path / "src") == {"pyyaml"}


@pytest.mark.parametrize("line,expected", [
    ("pandas==3.0.5", {"pandas": "3.0.5"}),
    ('colorama==0.4.6 ; sys_platform == "win32"', {"colorama": "0.4.6"}),
    ("# a comment", {}),
    ("", {}),
    ("pandas>=3.0", {}),  # not a pin: a lock must be exact
    ("Typing_Extensions==4.16.0", {"typing-extensions": "4.16.0"}),
])
def test_lock_parsing(line, expected):
    assert deps.parse_lock(line) == expected


def test_environment_drift_is_detected():
    """The check must fail on a version that is not installed."""
    rep = deps.DepReport()
    rep.environment_drift = ["pandas: locked 3.0.5, installed 2.2.0"]
    rep.ok = False
    assert not rep.ok
    assert "pip install -r requirements.lock.txt" in rep.render()


def test_this_environment_matches_the_lock():
    """The machine producing artifacts must match the recipe that describes it."""
    rep = deps.check()
    assert rep.ok, rep.render()


# --- the lock must be installable on every platform CI uses ------------------

#: Distributions that exist only on Windows. A lock that pins one of these
#: unconditionally cannot be installed on Linux at all — which is how the first
#: PR CI run failed, on a Windows-only transitive of the MCP SDK that a
#: Windows-only clean-install proof could never have caught.
WINDOWS_ONLY = {"pywin32", "colorama", "pywinpty", "winkerberos"}


def test_every_windows_only_pin_carries_a_marker():
    for name, _version, marker in deps._lock_entries():
        if name in WINDOWS_ONLY:
            assert marker and "win32" in marker, (
                f"{name} is pinned without a platform marker; `pip install -r "
                f"requirements.lock.txt` would fail on Linux")


def test_a_marker_that_does_not_apply_is_not_reported_as_drift():
    """Otherwise every Linux run fails on a package correctly absent."""
    assert deps.applies_here(None) is True
    win = 'sys_platform == "win32"'
    linux = 'sys_platform == "linux"'
    assert deps.applies_here(win) != deps.applies_here(linux)


def test_a_malformed_marker_does_not_silently_skip_a_package():
    """Fail open: an unparseable marker must not excuse a missing dependency."""
    assert deps.applies_here("this is not a marker") is True


def test_lock_entries_keeps_the_marker_and_parse_lock_drops_it():
    text = 'pandas==3.0.5\npywin32==312 ; sys_platform == "win32"  # comment\n'
    assert deps.parse_lock(text) == {"pandas": "3.0.5", "pywin32": "312"}
    assert deps._lock_entries(text) == [
        ("pandas", "3.0.5", None),
        ("pywin32", "312", 'sys_platform == "win32"'),
    ]
