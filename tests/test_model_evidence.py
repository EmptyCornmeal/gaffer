"""The published evidence must match the measurements it came from.

Two things went wrong in Batch 6 and neither was caught by a test:

* `decision.py` justified its thresholds with a rank correlation of ~0.76 and
  ~85 legal-XI points per gameweek. Both numbers came from baselines that were
  withdrawn in the same batch.
* The summary said trained models "lose every decision metric". Ridge beat the
  heuristic at h=1 on legal-XI points *and* on captaincy. Not decisively — but
  "not selected" and "rejected" are different findings, and one of them was
  reported as the other.

Both are prose-vs-numbers failures. Tests for those have to read the prose.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gaffer import backtest, config, decision

DOCS = config.REPO_ROOT / "docs"
SRC = Path(backtest.__file__).resolve().parent

#: Figures that came from withdrawn baselines. Any of these appearing as a live
#: justification is the bug.
WITHDRAWN_FIGURES = ("0.76", "0.760", "84.2", "84.6", "85")


def _user_facing_threshold_text() -> dict[str, str]:
    """Everywhere a reader could learn why the threshold is what it is."""
    out = {"decision.py": (SRC / "decision.py").read_text(encoding="utf-8")}
    for rel in ("web/src/lib/weekly.ts", "web/src/pages/Home.svelte"):
        p = config.REPO_ROOT / rel
        if p.exists():
            out[rel] = p.read_text(encoding="utf-8")
    return out


# --- the decision threshold --------------------------------------------------

def test_the_threshold_no_longer_cites_withdrawn_metrics():
    """A justification may be withdrawn or replaced, never left standing."""
    for where, text in _user_facing_threshold_text().items():
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not any(f in line for f in WITHDRAWN_FIGURES):
                continue
            # Naming the number in order to retract it is the point. The
            # retraction may sit a line or two either side of the figure.
            window = "\n".join(lines[max(0, i - 3):i + 4])
            assert re.search(r"withdraw|no longer|inadmissible|originally|gone",
                             window, re.I), \
                f"{where} cites a withdrawn figure as live justification: {line.strip()}"


def test_the_threshold_declares_itself_unfitted():
    st = decision.THRESHOLD_STATUS
    assert st["fitted"] is False
    assert st["basis"] == "policy"
    assert st["min_actionable_points"] == decision.MIN_ACTIONABLE_POINTS
    assert st["min_actionable_probability"] == decision.MIN_ACTIONABLE_PROBABILITY
    assert "6" in st["reassess_after"], "say when it becomes fittable"
    assert st["withdrawn_justification"]


def test_the_threshold_values_are_unchanged():
    """Relabelling is not retuning. Moving these on withdrawn evidence would be
    as unfounded as the reasoning that set them."""
    assert decision.MIN_ACTIONABLE_POINTS == 1.0
    assert decision.MIN_ACTIONABLE_PROBABILITY == 0.55


def test_the_decision_artifact_publishes_the_threshold_status():
    path = config.DATA_DIR / "decision.json"
    if not path.exists():  # pragma: no cover - artifact-free checkout
        pytest.skip("no published decision")
    blob = json.loads(path.read_text(encoding="utf-8"))
    st = (blob.get("decision") or {}).get("threshold_status") or blob.get("threshold_status")
    assert st, "the screen must be able to say the bars are unfitted"
    assert st["fitted"] is False


# --- one verdict per candidate ----------------------------------------------

def test_every_candidate_has_its_own_decision():
    decisions = backtest.candidate_decisions()
    assert decisions["gbm"] == "rejected"
    assert decisions["ridge"] == "inconclusive"
    assert decisions["xp_models"] == "invalid_experiment"
    assert len(set(decisions.values())) > 1, \
        "distinct findings were flattened into one verdict"


def test_a_candidate_claiming_it_lost_everywhere_records_no_win():
    for c in backtest.MODEL_CANDIDATES["candidates"]:
        if c.get("worse_at_every_horizon") is not True:
            continue
        wins = {h: r["diff"] for h, r in (c.get("per_horizon") or {}).items()
                if r.get("diff", 0) > 0}
        assert not wins, (
            f"{c['candidate']} claims it was worse at every horizon but records "
            f"a positive difference at {sorted(wins)}")


def test_ridge_beat_the_heuristic_at_h1_and_the_record_says_so():
    """The specific fact the old summary contradicted."""
    ridge = backtest.candidate("ridge")
    h1 = ridge["per_horizon"]["1"]
    heur = backtest.MODEL_CANDIDATES["heuristic_reference"]["xi_points_per_gw"]["1"]
    assert h1["candidate_xi"] > heur
    assert h1["diff"] > 0
    assert ridge["decision"] != "rejected", \
        "a candidate that beat the heuristic at h=1 is not 'rejected'"
    # ...and it is still not a win, which is why nothing ships.
    assert h1["ci95"][0] < 0 < h1["ci95"][1], "the interval must span zero"
    assert ridge["captain_accuracy_pct_h1"] > \
        backtest.MODEL_CANDIDATES["heuristic_reference"]["captain_accuracy_pct_h1"]


def test_ridge_does_not_hold_up_past_h1():
    """The reason it is not selected, stated as a number rather than a feeling."""
    ridge = backtest.candidate("ridge")
    later = [r["diff"] for h, r in ridge["per_horizon"].items() if h != "1"]
    assert later and all(d < 0 for d in later)


def test_the_invalid_experiment_is_not_recorded_as_a_loss():
    xp = backtest.candidate("xp_models")
    assert xp["decision"] == "invalid_experiment"
    assert xp["per_horizon"] == {}, "an unscoreable experiment has no score"
    assert "inadmissible" in xp["reason"]


PROSE_FILES = ("docs/MODEL-EVALUATION.md", "README.md", "docs/TRACEABILITY.md",
               "docs/ARCHITECTURE.md")

#: Phrasings that are false now that ridge is in scope.
FORBIDDEN_CLAIMS = (
    r"trained models?[^.]{0,80}los[et][^.]{0,40}every decision metric",
    r"los[et] (?:to the heuristic )?on every decision metric at every horizon",
)


def test_no_document_claims_every_trained_model_lost():
    ridge_h1 = backtest.candidate("ridge")["per_horizon"]["1"]["diff"]
    assert ridge_h1 > 0, "fixture assumption"
    for rel in PROSE_FILES:
        p = config.REPO_ROOT / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_CLAIMS:
            for m in re.finditer(pattern, text, re.I | re.S):
                window = text[max(0, m.start() - 300):m.end() + 300]
                assert re.search(r"\bgbm\b|gradient-boost", window, re.I), (
                    f"{rel} says a trained model lost every decision metric "
                    f"without naming GBM, but ridge was +{ridge_h1} at h=1: "
                    f"...{m.group(0)[:90]}...")


def test_every_prose_file_that_discusses_the_experiment_names_ridge():
    for rel in ("docs/MODEL-EVALUATION.md", "README.md"):
        text = (config.REPO_ROOT / rel).read_text(encoding="utf-8")
        if "GBM" not in text and "gbm" not in text:
            continue
        assert re.search(r"\bridge\b", text, re.I), \
            f"{rel} discusses the experiment without mentioning ridge"


# --- the UI must not hide a candidate ---------------------------------------

def test_the_accuracy_page_renders_every_candidate():
    """Showing only the rejected one is how the wrong summary happened."""
    # Renamed Accuracy.svelte -> Model.svelte on 2026-08-21 when the page
    # absorbed Help. This path crosses a language boundary, so neither
    # svelte-check nor mypy can catch it moving again — say so out loud rather
    # than dying on a bare FileNotFoundError inside a scheduled refresh.
    page_file = config.REPO_ROOT / "web" / "src" / "pages" / "Model.svelte"
    assert page_file.exists(), (
        f"{page_file.name} is missing — if the model page was renamed again, "
        "update this path. The front-end suite cannot catch this: it never "
        "reads Svelte files by name."
    )
    page = page_file.read_text(encoding="utf-8")
    assert "modelCandidates" in page, "the page must read the candidate list"
    assert "{#each candidates as c}" in page, "it must loop, not pick one"
    for token in ("c.decision", "c.reason", "c.per_horizon"):
        assert token in page, f"the page drops {token}"
    # No hard-coded single-candidate rendering.
    assert "gbm_minus_heuristic" not in page
    assert "rejected_models" not in page


def test_the_frontend_distinguishes_the_decisions():
    lib = (config.REPO_ROOT / "web" / "src" / "lib"
           / "backtest.ts").read_text(encoding="utf-8")
    for d in ("rejected", "inconclusive", "invalid_experiment"):
        assert d in lib, f"the front-end cannot label {d!r}"


def test_the_published_artifact_carries_every_candidate():
    path = config.DATA_DIR / "backtest.json"
    if not path.exists():  # pragma: no cover
        pytest.skip("no published backtest")
    bt = json.loads(path.read_text(encoding="utf-8"))
    names = {c["candidate"] for c in bt["model_candidates"]["candidates"]}
    assert names == set(backtest.candidate_decisions())


# --- xP provenance wording ---------------------------------------------------

PROVENANCE_FILES = ("src/gaffer/leakage.py", "src/gaffer/backtest.py",
                    "src/gaffer/config.py", "docs/MODEL-EVALUATION.md",
                    "README.md")


def test_the_xp_exclusion_rests_on_provenance_not_on_a_correlation():
    """Correlation with the result is corroboration. The grounds are the
    upstream warning that the value may be post-match and cannot be certified."""
    for rel in PROVENANCE_FILES:
        text = (config.REPO_ROOT / rel).read_text(encoding="utf-8")
        if "xP" not in text:
            continue
        assert re.search(r"cannot certify|inadmissible|may contain post-match",
                         text, re.I), \
            f"{rel} does not state the provenance grounds for excluding xP"
        # It must not claim the correlation is proof.
        for m in re.finditer(r"\b(proves?|proven|proof|demonstrates?)\b[^.\n]{0,120}",
                             text, re.I):
            window = m.group(0)
            if re.search(r"correlat", window, re.I) and not re.search(
                    r"\bnot\b|rather than|does not", window, re.I):
                pytest.fail(f"{rel} claims a correlation proves the timing: {window}")


def test_the_diagnostic_is_labelled_corroborating():
    entry = backtest.WITHDRAWN_BASELINES["fpl_xp"]
    assert "provenance" in entry
    assert "corroboration" in entry
    assert "Not proof" in entry["corroboration"]
