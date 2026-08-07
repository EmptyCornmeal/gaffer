"""The rejected architecture must stay rejected — and only that architecture.

The first version of this guard banned `sklearn`, `lightgbm`, `xgboost` and all
model loading anywhere in `src/gaffer/`. That was broader than the evidence
supports and it contradicted the one experiment the study explicitly left open: a
minutes/appearance classifier feeding the existing `p_start` gate. A guard that
forbids the documented next step is not protecting a finding, it is freezing one.

So this file guards five specific things:

1. `gaffer/ml.py` — the deleted, orphaned, never-wired points model.
2. `gaffer_gbm.joblib` — the deleted committed model artifact.
3. Unverified deserialisation at runtime. `pickle.load` / `joblib.load` execute
   whatever is inside the file; that is a security property, not a statistical
   one, and it stays banned whatever the model is.
4. A trained points-model column reaching a published recommendation without a
   candidate marked `shipped` in the evidence block.
5. Withdrawn `xP` / `fpl_xp` / `ensemble` metrics reappearing as measured.

Importing a statistics library is not on that list. Building a minutes model is
not on that list. Neither is required now, and neither is forbidden later.
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from gaffer import backtest, config, leakage

SRC = Path(backtest.__file__).resolve().parent
REPO = config.REPO_ROOT

MODEL_SUFFIXES = (".joblib", ".pkl", ".pickle", ".h5", ".onnx", ".pt", ".pth",
                  ".cbm", ".safetensors")


def _py_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


# --- 1. the deleted module ---------------------------------------------------

def test_the_orphaned_ml_module_is_gone():
    assert not (SRC / "ml.py").exists(), "src/gaffer/ml.py is back"
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("gaffer.ml")


# --- 2. the deleted artifact -------------------------------------------------

def test_no_model_artifact_would_be_committed():
    """Asks git for tracked + untracked-not-ignored, keeps what still exists —
    exactly the set a commit of this working tree would contain."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO, capture_output=True, text=True, check=False)
    if out.returncode != 0:  # pragma: no cover - not a git checkout
        pytest.skip("not a git working tree")
    bad = [ln for ln in out.stdout.splitlines()
           if ln.endswith(MODEL_SUFFIXES) and (REPO / ln).exists()]
    assert bad == [], f"a serialised model is back in the repository: {bad}"


# --- 3. unverified deserialisation ------------------------------------------

def test_no_unverified_deserialisation_at_runtime():
    """`pickle.load` and friends execute the file's contents.

    This is the one blanket ban worth keeping, and it is a security property.
    A future model may ship — through a format that is parsed, not executed, and
    with an integrity check — but not through this door.
    """
    banned_calls = {("pickle", "load"), ("pickle", "loads"),
                    ("joblib", "load"), ("torch", "load"),
                    ("dill", "load"), ("cloudpickle", "load")}
    offenders: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                val = node.value
                if isinstance(val, ast.Name) and (val.id, node.attr) in banned_calls:
                    offenders.append(f"{path.name}: {val.id}.{node.attr}")
    assert offenders == [], f"unverified deserialisation is back: {offenders}"


def test_a_statistics_library_import_is_not_itself_forbidden():
    """The guard must not block the documented next experiment.

    `numpy`, `pandas` and `pulp` are imported all over the package. A rule that
    banned "statistical libraries" would have to ban those too, which shows the
    rule was never about statistics.
    """
    imported: set[str] = set()
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert {"numpy", "pandas", "pulp"} <= imported


# --- 4. no unevidenced model column in a published recommendation ------------

TRAINED_COLUMN_HINTS = ("ml_points", "ml_xp", "gbm_points", "model_points_ml",
                        "trained_points", "ml_prediction")


def test_no_trained_points_column_is_published_without_a_shipped_candidate():
    """A trained points column may only appear once the evidence says it shipped.

    The gate is the evidence block, not this file: add a candidate with
    `decision: "shipped"` and the numbers behind it, and this test lets it
    through. Publish the column without that, and it does not.
    """
    shipped = [c for c in backtest.MODEL_CANDIDATES["candidates"]
               if c["decision"] == "shipped"]
    for name in ("players.json", "recommendation.json", "decision.json"):
        path = config.DATA_DIR / name
        if not path.exists():  # pragma: no cover - artifact-free checkout
            continue
        blob = path.read_text(encoding="utf-8")
        for hint in TRAINED_COLUMN_HINTS:
            if f'"{hint}"' in blob:
                assert shipped, (
                    f"{name} publishes a trained-model column {hint!r}, but no "
                    f"candidate in backtest.MODEL_CANDIDATES is marked shipped")


def test_the_projection_exposes_its_components_separately():
    """Whatever ships, the model's own number and the external one stay apart."""
    from gaffer.model import projection
    src = Path(projection.__file__).read_text(encoding="utf-8")
    assert "exp_points_model" in src
    assert "exp_points_ep_next" in src


# --- 5. withdrawn metrics stay withdrawn ------------------------------------

def test_the_archives_xp_column_is_inadmissible():
    assert leakage.is_post_match("xP") is True
    assert "xP" not in leakage.PRE_DEADLINE_FIELDS
    with pytest.raises(leakage.LeakageError):
        leakage.assert_no_leakage(["element", "GW", "xP"])


def test_the_live_ep_next_is_still_a_legal_pre_deadline_field():
    """The live field is genuine — only the archive's stand-in is not."""
    assert leakage.is_post_match("ep_next") is False
    assert "ep_next" in leakage.LIVE_ONLY_FIELDS
    leakage.assert_no_leakage(["element", "ep_next"])  # must not raise


def test_the_blend_weight_is_labelled_unfitted():
    assert config.EP_NEXT_BLEND_IS_FITTED is False
    assert config.EP_NEXT_BLEND_WEIGHT == 0.7


def test_withdrawn_baselines_are_recorded_not_deleted():
    for name in ("fpl_xp", "ensemble"):
        entry = backtest.WITHDRAWN_BASELINES[name]
        assert entry["withdrawn_in_schema"] == 4
        assert entry["previously_reported"], "the retracted numbers must be kept"


def test_the_backtest_never_reports_a_withdrawn_baseline_as_measured():
    banned = {"fpl_xp", "ensemble", "ml"}
    path = config.DATA_DIR / "backtest.json"
    if not path.exists():  # pragma: no cover - artifact-free checkout
        pytest.skip("no published backtest")
    bt = json.loads(path.read_text(encoding="utf-8"))
    assert bt["schema_version"] == backtest.SCHEMA_VERSION
    for h, block in bt["per_horizon"].items():
        for metric in ("mae", "rank_corr", "decisions", "transfers"):
            got = set(block.get(metric) or {})
            assert not (got & banned), f"h={h} {metric} reports {got & banned}"
    assert bt["withdrawn_baselines"], "the withdrawal must ship with the artifact"


def test_pyproject_declares_no_ml_dependency_right_now():
    """No ML dependency is needed today.

    Deliberately phrased as "not now" rather than "never": adding one later means
    updating this test alongside the evidence that justifies it, which is the
    point at which somebody has to argue for it.
    """
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"].get("optional-dependencies", {})
    flat = [d for group in extras.values() for d in group] + data["project"]["dependencies"]
    for dep in flat:
        assert not dep.lower().startswith(("scikit-learn", "joblib", "lightgbm",
                                           "xgboost", "torch", "tensorflow")), \
            f"an ML dependency appeared without an evidence update: {dep}"
