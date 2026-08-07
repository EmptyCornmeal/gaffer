"""T-01 / T-02 — path resolution and personal-config resolution.

The bug these guard against: a non-editable install made ``REPO_ROOT`` resolve
into ``site-packages``, so the pipeline wrote artifacts outside the checkout and
37 consecutive scheduled runs reported success while publishing nothing.
"""

from __future__ import annotations

import pytest

from gaffer import config


def make_repo(tmp_path, name="repo"):
    """A directory that looks like a Gaffer checkout to resolve_repo_root()."""
    root = tmp_path / name
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='gaffer'\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# Repo root / data dir resolution
# --------------------------------------------------------------------------

def test_repo_root_from_env_wins(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    config.reload_paths()
    assert config.REPO_ROOT == root.resolve()
    assert config.REPO_ROOT_SOURCE == "env:GAFFER_REPO_ROOT"


def test_repo_root_discovered_from_source_checkout(monkeypatch):
    """With no env override we find the real checkout via its markers."""
    monkeypatch.delenv("GAFFER_REPO_ROOT", raising=False)
    config.reload_paths()
    assert (config.REPO_ROOT / "pyproject.toml").is_file()
    assert (config.REPO_ROOT / "src" / "gaffer").is_dir()
    assert config.REPO_ROOT_SOURCE in ("source-checkout", "working-directory")


def test_data_dir_defaults_under_repo_root(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.delenv("GAFFER_DATA_DIR", raising=False)
    config.reload_paths()
    assert config.DATA_DIR == root.resolve() / "data"
    assert config.DATA_DIR_SOURCE == "repo-root"
    # The derived paths must follow, not stay pinned to the old root.
    assert config.DB_PATH.parent == config.DATA_DIR
    assert config.CACHE_DIR.parent == config.DATA_DIR


def test_data_dir_env_override(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    out = root / "custom-data"
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_DATA_DIR", str(out))
    config.reload_paths()
    assert config.DATA_DIR == out.resolve()
    assert config.DATA_DIR_SOURCE == "env:GAFFER_DATA_DIR"


# --------------------------------------------------------------------------
# The containment guard
# --------------------------------------------------------------------------

def test_verify_accepts_data_dir_inside_repo(tmp_path):
    root = make_repo(tmp_path)
    config.verify_publish_paths(repo_root=root, data_dir=root / "data")
    config.verify_publish_paths(repo_root=root, data_dir=root)  # equal is fine


def test_verify_rejects_site_packages_layout(tmp_path):
    """The exact production failure: artifacts under the Python install."""
    root = make_repo(tmp_path)
    site = tmp_path / "hostedtoolcache" / "Python" / "3.12.13" / "x64" / "lib" / "python3.12"
    bad = site / "data"
    bad.mkdir(parents=True)
    with pytest.raises(config.PathResolutionError) as exc:
        config.verify_publish_paths(repo_root=root, data_dir=bad)
    msg = str(exc.value)
    # The error has to be actionable on its own, in a CI log.
    assert str(bad) in msg
    assert str(root) in msg
    assert "not inside" in msg
    assert "pip install -e" in msg
    assert "GAFFER_DATA_DIR" in msg


def test_verify_uses_path_containment_not_string_prefix(tmp_path):
    """'/x/repo-backup' starts with '/x/repo' but is a different tree."""
    root = make_repo(tmp_path, "repo")
    sibling = make_repo(tmp_path, "repo-backup")
    with pytest.raises(config.PathResolutionError):
        config.verify_publish_paths(repo_root=root, data_dir=sibling / "data")


def test_describe_paths_reports_provenance(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    config.reload_paths()
    d = config.describe_paths()
    assert d["repo_root"] == str(root.resolve())
    assert d["repo_root_source"] == "env:GAFFER_REPO_ROOT"
    assert set(d) >= {"data_dir", "data_dir_source", "package_dir", "cwd"}


# --------------------------------------------------------------------------
# Settings resolution (T-02)
# --------------------------------------------------------------------------

def _write_toml(root, body):
    (root / "gaffer.local.toml").write_text(body, encoding="utf-8")


def test_env_overrides_toml(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    _write_toml(root, "[fpl]\nentry_id = 999\nleague_ids = [1, 2]\n")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_ENTRY_ID", "1066421")
    monkeypatch.setenv("GAFFER_LEAGUE_IDS", "271619")
    config.reload_paths()
    s = config.Settings.load()
    assert s.entry_id == 1066421
    assert s.league_ids == [271619]
    assert s.sources["entry_id"] == "env:GAFFER_ENTRY_ID"


def test_toml_used_when_env_absent(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    _write_toml(root, "[fpl]\nentry_id = 1066421\nleague_ids = [271619, 314]\n")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    for k in ("GAFFER_ENTRY_ID", "GAFFER_LEAGUE_IDS", "GAFFER_FREE_TRANSFERS"):
        monkeypatch.delenv(k, raising=False)
    config.reload_paths()
    s = config.Settings.load()
    assert s.entry_id == 1066421
    assert s.league_ids == [271619, 314]
    assert s.sources["entry_id"] == "gaffer.local.toml"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("271619", [271619]),
        ("271619,314", [271619, 314]),
        ("271619, 314 , 1", [271619, 314, 1]),
        ("271619;314", [271619, 314]),
        ("271619,271619", [271619]),  # de-duplicated, order preserved
    ],
)
def test_multiple_league_ids_parse(tmp_path, monkeypatch, raw, expected):
    """Multi-league is a first-class case; never assume a single id."""
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_LEAGUE_IDS", raw)
    config.reload_paths()
    assert config.Settings.load().league_ids == expected


def test_invalid_entry_id_fails_loudly(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_ENTRY_ID", "not-a-number")
    config.reload_paths()
    with pytest.raises(config.ConfigError) as exc:
        config.Settings.load()
    assert "entry_id" in str(exc.value)
    assert "env:GAFFER_ENTRY_ID" in str(exc.value)


def test_negative_entry_id_rejected(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_ENTRY_ID", "-5")
    config.reload_paths()
    with pytest.raises(config.ConfigError):
        config.Settings.load()


def test_invalid_league_id_fails_loudly(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_LEAGUE_IDS", "271619,banana")
    config.reload_paths()
    with pytest.raises(config.ConfigError):
        config.Settings.load()


def test_missing_entry_id_is_explicitly_generic(tmp_path, monkeypatch):
    """No entry id must produce a *labelled* generic build, never a silent one."""
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    for k in ("GAFFER_ENTRY_ID", "GAFFER_LEAGUE_IDS"):
        monkeypatch.delenv(k, raising=False)
    config.reload_paths()
    s = config.Settings.load()
    assert s.entry_id is None
    assert s.personalised is False
    assert s.build_mode == "generic"


def test_configured_entry_is_personalised(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_ENTRY_ID", "1066421")
    config.reload_paths()
    s = config.Settings.load()
    assert s.personalised is True
    assert s.build_mode == "personalised"


def test_free_transfers_clamped(tmp_path, monkeypatch):
    root = make_repo(tmp_path)
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_FREE_TRANSFERS", "99")
    config.reload_paths()
    assert config.Settings.load().free_transfers == config.MAX_FREE_TRANSFERS
    monkeypatch.setenv("GAFFER_FREE_TRANSFERS", "0")
    assert config.Settings.load().free_transfers == 1


def test_example_config_is_committed_and_secret_free():
    """gaffer.example.toml must document config without leaking anything."""
    example = config.REPO_ROOT / "gaffer.example.toml"
    assert example.is_file(), "gaffer.example.toml should be committed"
    text = example.read_text(encoding="utf-8")
    for field_name in ("entry_id", "league_ids", "free_transfers"):
        assert field_name in text
    lowered = text.lower()
    for marker in ("sk-ant-", "ghp_", "github_pat_", "anthropic_api_key ="):
        assert marker not in lowered, f"example config must not contain {marker}"


# --- an unsafe root is refused before containment is considered --------------

def test_a_non_editable_install_outside_a_checkout_refuses_to_publish(
        tmp_path, monkeypatch):
    """The original Tier-1 failure, reproduced and now caught.

    With no checkout to find, `resolve_repo_root` falls back to a guess derived
    from the package's own location — and `data_dir` then defaults to a
    directory *inside* that guess, so a containment check passes trivially and
    the pipeline publishes into site-packages. Found by installing the built
    wheel and running it from an empty directory.
    """
    monkeypatch.delenv("GAFFER_REPO_ROOT", raising=False)
    monkeypatch.delenv("GAFFER_DATA_DIR", raising=False)
    monkeypatch.setattr(config, "REPO_ROOT_SOURCE", "unsafe:package-relative")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "Lib")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "Lib" / "data")

    with pytest.raises(config.PathResolutionError) as exc:
        config.verify_publish_paths()
    msg = str(exc.value)
    assert "no repository checkout could be found" in msg
    assert "green-but-empty" in msg


def test_an_explicit_data_dir_overrides_the_unsafe_root_refusal(
        tmp_path, monkeypatch):
    """Deliberate is different from accidental.

    Setting GAFFER_DATA_DIR is how an operator runs the pipeline into a scratch
    directory on purpose. That is allowed; guessing is not.
    """
    out = tmp_path / "Lib" / "data"
    out.mkdir(parents=True)
    monkeypatch.setenv("GAFFER_DATA_DIR", str(out))
    monkeypatch.setattr(config, "REPO_ROOT_SOURCE", "unsafe:package-relative")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "Lib")
    monkeypatch.setattr(config, "DATA_DIR", out)
    config.verify_publish_paths()  # must not raise


def test_a_real_checkout_still_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT_SOURCE", "source-checkout")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.verify_publish_paths()  # must not raise
