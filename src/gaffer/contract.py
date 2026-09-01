"""Artifact contract — the gate between a pipeline run and publishing.

The refresh workflow published nothing for 37 consecutive green runs because the
only check was ``git status --porcelain``, which detects *difference*, not
*validity*. This module asserts the exported JSON is actually publishable, and
is the step that must pass before anything is committed.

Run it directly::

    python -m gaffer.contract                 # validate config.DATA_DIR
    python -m gaffer.contract --data-dir web/public/data
    python -m gaffer.contract --json          # machine-readable report

Exit status is 0 when the artifact set is publishable, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gaffer import config, gameweek
from gaffer import season as season_mod

# Artifacts the front-end hard-requires (lib/data.ts rejects the bundle without
# these four); my_team/plan are optional-by-shape but still validated when present.
REQUIRED_ARTIFACTS = ("meta.json", "players.json", "fixtures.json", "recommendation.json")
OPTIONAL_ARTIFACTS = (
    "my_team.json", "plan.json", "strategy.json", "backtest.json",
    "decision.json", "live.json", "review.json", "notifications.json",
    # The AI layer's own output. It bypasses `export.write_all` and writes
    # directly, which is how it stayed off this list — and therefore out of the
    # contract — while shipping on every run.
    "verdict.json", "news.json",
)

MIN_PLAYERS = 400
MAX_META_AGE = timedelta(hours=1)
# Tolerate a little clock skew between the pipeline host and the validator.
MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass
class Violation:
    artifact: str
    field: str
    value: Any
    expected: str

    def __str__(self) -> str:
        return (
            f"{self.artifact}: field {self.field!r} = {self.value!r} "
            f"— expected {self.expected}"
        )


@dataclass
class Report:
    data_dir: str
    violations: list[Violation] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data_dir": self.data_dir,
            "checked": self.checked,
            "violations": [asdict(v) for v in self.violations],
        }

    def render(self) -> str:
        if self.ok:
            return (
                f"artifact contract OK — {len(self.checked)} artifacts validated "
                f"in {self.data_dir}"
            )
        lines = [
            f"artifact contract FAILED — {len(self.violations)} violation(s) "
            f"in {self.data_dir}:"
        ]
        lines += [f"  - {v}" for v in self.violations]
        return "\n".join(lines)


def parse_iso_utc(raw: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None when it isn't one.

    Accepts a trailing ``Z``; a naive timestamp is rejected rather than assumed
    to be UTC, because guessing the zone is how stale data reads as fresh.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def _load(data_dir: Path, name: str, report: Report) -> Any:
    """Read one artifact, recording existence/parse violations."""
    path = data_dir / name
    if not path.exists():
        report.violations.append(
            Violation(name, "<file>", str(path), "the file to exist after a pipeline run")
        )
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.violations.append(
            Violation(name, "<file>", f"{type(exc).__name__}: {exc}", "valid UTF-8 JSON")
        )
        return None
    report.checked.append(name)
    return data


def _check_squad_shape(rec: Any, players: Any, report: Report) -> None:
    """XI/bench size, duplicates, overlap, and player-id resolvability."""
    if not isinstance(rec, dict):
        report.violations.append(
            Violation("recommendation.json", "<root>", type(rec).__name__, "a JSON object")
        )
        return

    starting = rec.get("starting")
    bench = rec.get("bench")
    if not isinstance(starting, list):
        report.violations.append(
            Violation("recommendation.json", "starting", starting, "a list of 11 players")
        )
        return
    if not isinstance(bench, list):
        report.violations.append(
            Violation("recommendation.json", "bench", bench, "a list of bench players")
        )
        return

    if len(starting) != 11:
        report.violations.append(
            Violation("recommendation.json", "starting", f"{len(starting)} players",
                      "exactly 11 players in the starting XI")
        )

    expected_bench = config.SQUAD_SIZE - 11
    if len(bench) != expected_bench:
        report.violations.append(
            Violation("recommendation.json", "bench", f"{len(bench)} players",
                      f"exactly {expected_bench} bench players "
                      f"(squad size {config.SQUAD_SIZE})")
        )

    start_ids = [p.get("id") for p in starting if isinstance(p, dict)]
    bench_ids = [p.get("id") for p in bench if isinstance(p, dict)]

    dup_start = sorted({i for i in start_ids if start_ids.count(i) > 1})
    if dup_start:
        report.violations.append(
            Violation("recommendation.json", "starting", f"duplicate ids {dup_start}",
                      "every starting player to appear exactly once")
        )
    dup_bench = sorted({i for i in bench_ids if bench_ids.count(i) > 1})
    if dup_bench:
        report.violations.append(
            Violation("recommendation.json", "bench", f"duplicate ids {dup_bench}",
                      "every bench player to appear exactly once")
        )

    overlap = sorted(set(start_ids) & set(bench_ids))
    if overlap:
        report.violations.append(
            Violation("recommendation.json", "starting/bench", f"shared ids {overlap}",
                      "the XI and bench to be disjoint")
        )

    # Every referenced id must resolve in players.json.
    if isinstance(players, list):
        known = {p.get("id") for p in players if isinstance(p, dict)}
        referenced: set[Any] = set(start_ids) | set(bench_ids)
        for key in ("captain", "vice"):
            card = rec.get(key)
            if isinstance(card, dict) and card.get("id") is not None:
                referenced.add(card["id"])
        unknown = sorted(i for i in referenced if i not in known and i is not None)
        if unknown:
            report.violations.append(
                Violation("recommendation.json", "player ids", f"unknown ids {unknown}",
                          "every referenced player id to exist in players.json")
            )

    # Captain and vice must actually be in the XI, and must differ.
    cap = (rec.get("captain") or {}).get("id") if isinstance(rec.get("captain"), dict) else None
    vice = (rec.get("vice") or {}).get("id") if isinstance(rec.get("vice"), dict) else None
    if cap is not None and cap not in start_ids:
        report.violations.append(
            Violation("recommendation.json", "captain.id", cap,
                      "the captain to be one of the 11 starters")
        )
    if vice is not None and vice not in start_ids:
        report.violations.append(
            Violation("recommendation.json", "vice.id", vice,
                      "the vice-captain to be one of the 11 starters")
        )
    if cap is not None and vice is not None and cap == vice:
        report.violations.append(
            Violation("recommendation.json", "vice.id", vice,
                      "the vice-captain to differ from the captain")
        )


def _check_backtest(bt: Any, report: Report) -> None:
    """The published backtest must be a renderable, self-describing artifact."""
    from gaffer import backtest as bt_mod

    if isinstance(bt, dict):
        _check_minutes_model(bt, report)
        # G1/A18. The artifact is generated by hand and by nothing else, so it
        # can silently describe a superseded model.
        from gaffer.model import projection as _proj
        if bt.get("model_version") != _proj.MODEL_VERSION:
            report.violations.append(
                Violation("backtest.json", "model_version",
                          bt.get("model_version"),
                          f"{_proj.MODEL_VERSION!r} -- the published accuracy "
                          "describes a model that is not the one running. "
                          "Regenerate with `python -m gaffer.backtest --write`"))
    if not isinstance(bt, dict):
        report.violations.append(
            Violation("backtest.json", "<root>", type(bt).__name__, "a JSON object")
        )
        return
    version = bt.get("schema_version")
    if version != bt_mod.SCHEMA_VERSION:
        report.violations.append(
            Violation("backtest.json", "schema_version", version,
                      f"schema_version {bt_mod.SCHEMA_VERSION} — the front-end "
                      "refuses to render anything else, and a legacy artifact "
                      "describes a model that never shipped")
        )
        return
    for key in ("model_version", "season", "per_horizon", "coverage",
                "leakage_check", "limitations", "generated_at"):
        if key not in bt:
            report.violations.append(
                Violation("backtest.json", key, None, "to be present")
            )
    ph = bt.get("per_horizon")
    if not isinstance(ph, dict) or not ph:
        report.violations.append(
            Violation("backtest.json", "per_horizon", ph,
                      "a non-empty object of horizon results")
        )
    leak = bt.get("leakage_check")
    if isinstance(leak, dict):
        if not leak.get("enforced"):
            report.violations.append(
                Violation("backtest.json", "leakage_check.enforced",
                          leak.get("enforced"),
                          "true — an unchecked backtest must not be published")
            )
        found = leak.get("post_match_fields_in_features") or []
        if found:
            report.violations.append(
                Violation("backtest.json", "leakage_check", found,
                          "no post-match fields among the pre-deadline features")
            )
    if not bt.get("limitations"):
        report.violations.append(
            Violation("backtest.json", "limitations", bt.get("limitations"),
                      "a non-empty list — accuracy numbers must ship with caveats")
        )
    # Every model candidate must carry its own decision. Collapsing them into a
    # single verdict is how "ridge was inconclusive" became "trained models lose
    # every decision metric".
    cands = bt.get("model_candidates")
    if not isinstance(cands, dict) or not isinstance(cands.get("candidates"), list):
        report.violations.append(
            Violation("backtest.json", "model_candidates", cands,
                      "an object with a per-candidate `candidates` list")
        )
    else:
        seen: set[str] = set()
        for c in cands["candidates"]:
            if not isinstance(c, dict):
                continue
            name, decision = c.get("candidate"), c.get("decision")
            seen.add(str(name))
            if decision not in ("rejected", "inconclusive", "invalid_experiment",
                               "shipped"):
                report.violations.append(
                    Violation("backtest.json", f"model_candidates[{name}].decision",
                              decision,
                              "one of rejected / inconclusive / invalid_experiment "
                              "/ shipped — a candidate without its own verdict "
                              "gets flattened into somebody else's")
                )
            # A candidate claiming it lost everywhere must not carry a win.
            if c.get("worse_at_every_horizon") is True:
                wins = [h for h, r in (c.get("per_horizon") or {}).items()
                        if isinstance(r, dict) and (r.get("diff") or 0) > 0]
                if wins:
                    report.violations.append(
                        Violation("backtest.json",
                                  f"model_candidates[{name}].worse_at_every_horizon",
                                  f"true, but h={sorted(wins)} records a positive diff",
                                  "the claim and the numbers to agree")
                    )
        if len(seen) < 2:
            report.violations.append(
                Violation("backtest.json", "model_candidates.candidates",
                          sorted(seen),
                          "every evaluated candidate, not just the losing one")
            )

    # T-26: a withdrawn baseline must stay visible, and must never reappear as a
    # measured one. A retracted number that quietly vanishes reads as though it
    # was never claimed.
    withdrawn = bt.get("withdrawn_baselines")
    if not isinstance(withdrawn, dict) or not withdrawn:
        report.violations.append(
            Violation("backtest.json", "withdrawn_baselines", withdrawn,
                      "the record of which baselines were retracted and why")
        )
    else:
        names = {k for k in withdrawn if isinstance(withdrawn[k], dict)}
        for h, blk in (ph or {}).items():
            if not isinstance(blk, dict):
                continue
            for metric in ("mae", "rank_corr", "decisions", "transfers"):
                reported = set(blk.get(metric) or {})
                back = sorted(reported & names)
                if back:
                    report.violations.append(
                        Violation("backtest.json", f"per_horizon.{h}.{metric}", back,
                                  "no withdrawn baseline — these were retracted "
                                  "for leakage and must not be reported as measured")
                    )


def _check_minutes_model(bt: Any, report: Report) -> None:
    """The minutes block, and the two ways it could quietly stop meaning anything.

    `p_start` gates every projection and went unmeasured for the project's whole
    life. Now that it is measured, these keep the measurement honest.
    """
    name = "backtest.json"
    mm = bt.get("minutes_model")
    if mm is None:
        return
    if not isinstance(mm, dict) or not isinstance(mm.get("measured"), bool):
        report.violations.append(
            Violation(name, "minutes_model.measured", mm if not isinstance(mm, dict)
                      else mm.get("measured"),
                      "an explicit boolean -- 'not measured' is a state, not an "
                      "absent block"))
        return
    if not mm.get("measured"):
        return

    for key in ("per_horizon", "bands", "limitations", "verdict"):
        if not mm.get(key):
            report.violations.append(
                Violation(name, f"minutes_model.{key}", None,
                          "to be present once the block claims to be measured"))

    # G1/A11a. The band table must carry BOTH populations. Publishing only the
    # pool-wide one is exactly how the CAMEO? band looked calibrated (claims
    # 0.256, realises 0.269) while being wrong on everyone anybody owns (claims
    # 0.339, realises 0.574). A fifteen-man squad is drawn entirely from the
    # second population, and the error changes sign between them.
    bands = mm.get("bands")
    if isinstance(bands, dict):
        for key in ("overall", "considered"):
            if key not in bands:
                report.violations.append(
                    Violation(name, f"minutes_model.bands.{key}", None,
                              "both the whole pool and the owned sub-population "
                              "-- the pool-wide table alone hid the defect"))

    # G1/A11b. Prose must match numbers. A verdict saying the model loses, beside
    # a table where nothing beats it, is the failure mode this project has hit
    # from the other direction (an artifact asserting an assumption it never
    # measured).
    h1 = (mm.get("per_horizon") or {}).get("1") or (mm.get("per_horizon") or {}).get(1)
    brier = (h1 or {}).get("brier") or {}
    ours = brier.get("gaffer")
    others = [v for k, v in brier.items() if k != "gaffer" and isinstance(v, (int, float))]
    if isinstance(ours, (int, float)) and others:
        beaten = any(v < ours for v in others)
        says_loses = "loses" in str(mm.get("verdict") or "").lower()
        if says_loses and not beaten:
            report.violations.append(
                Violation(name, "minutes_model.verdict", "claims the model loses",
                          "a verdict consistent with its own h=1 Brier table"))
        # Every published column must be named as a baseline.
        known = set(mm.get("baselines") or {})
        for k in brier:
            if k != "gaffer" and k not in known:
                report.violations.append(
                    Violation(name, f"minutes_model.per_horizon.1.brier.{k}", k,
                              "a method named in minutes_model.baselines -- an "
                              "unlabelled column is not a comparison"))


def _check_ai_text(name: str, blob: Any, report: Report, *, body: str) -> None:
    """Shape check for the AI layer's own artifacts (verdict.json, news.json).

    Deliberately light on the prose, which is model-written and unpredictable.
    What must hold:

    * the page can render it;
    * the reader can tell a model from a template, and a fallback says why —
      `source` used to carry its own failure inside it
      (``"template (ai failed: APIStatusError)"``), which this check rejected
      while the pipeline kept publishing it;
    * no exception text, URL or key-shaped string reaches a public file;
    * for `news.json`, every generated claim cites an item that is present.
    """
    from gaffer.ai import grounding as G

    if not isinstance(blob, dict):
        report.violations.append(
            Violation(name, "<root>", type(blob).__name__, "a JSON object"))
        return
    if not isinstance(blob.get(body), str):
        report.violations.append(
            Violation(name, body, blob.get(body), "a markdown string to render"))

    src = blob.get("source")
    if src not in G.ALL_SOURCES:
        report.violations.append(
            Violation(name, "source", src,
                      f"one of {sorted(G.ALL_SOURCES)} — the reader must be "
                      "able to tell generated prose from a fallback, and the "
                      "reason belongs in `fallback_reason`, not in here"))
    reason = blob.get("fallback_reason")
    if src == G.SOURCE_AI and reason is not None:
        report.violations.append(
            Violation(name, "fallback_reason", reason,
                      "null — a successful generation did not fall back"))
    if src == G.SOURCE_TEMPLATE:
        if not isinstance(reason, str) or not reason:
            report.violations.append(
                Violation(name, "fallback_reason", reason,
                          "a stable reason code — a silent fallback reads as a "
                          "real briefing"))
        elif reason.split(":")[0] not in G.ALL_FALLBACK_REASONS:
            report.violations.append(
                Violation(name, "fallback_reason", reason,
                          f"one of {sorted(G.ALL_FALLBACK_REASONS)}"))
        if blob.get("model") is not None:
            report.violations.append(
                Violation(name, "model", blob.get("model"),
                          "null on the template path — naming a model beside "
                          "prose it did not write is a false attribution"))
    if not isinstance(blob.get("generated_at"), str):
        report.violations.append(
            Violation(name, "generated_at", blob.get("generated_at"),
                      "an ISO timestamp"))

    # Nothing from an exception, and nothing key-shaped, in a published file.
    text = json.dumps(blob)
    for marker in ("Traceback", "sk-ant-", "ANTHROPIC_API_KEY", "Bearer "):
        if marker in text:
            report.violations.append(
                Violation(name, "<content>", marker,
                          "no exception text or credential-shaped string in a "
                          "published artifact"))

    # news.json: every claim must be traceable to an item in the same file.
    claims = blob.get("claims")
    if claims is not None:
        if not isinstance(claims, list):
            report.violations.append(
                Violation(name, "claims", type(claims).__name__, "a list"))
            return
        ids = {i.get("id") for i in (blob.get("items") or [])
               if isinstance(i, dict)}
        for i, c in enumerate(claims):
            if not isinstance(c, dict):
                report.violations.append(
                    Violation(name, f"claims[{i}]", type(c).__name__, "an object"))
                continue
            cited = c.get("source_item_ids")
            if not isinstance(cited, list) or not cited:
                report.violations.append(
                    Violation(name, f"claims[{i}].source_item_ids", cited,
                              "at least one source item — an uncited claim has "
                              "no evidence behind it"))
                continue
            missing = [x for x in cited if x not in ids]
            if missing:
                report.violations.append(
                    Violation(name, f"claims[{i}].source_item_ids", missing,
                              "ids present in this artifact's own `items` — a "
                              "dangling id renders as a source that is not there"))
            body_text = c.get("text")
            if isinstance(body_text, str) and ("http://" in body_text
                                               or "https://" in body_text):
                report.violations.append(
                    Violation(name, f"claims[{i}].text", "contains a URL",
                              "no URL in generated text; links come from the "
                              "fetched items"))


def _check_strategy(
    strat: Any, report: Report, expected_league_ids: list[int] | None = None,
) -> None:
    """The strategy artifact must be self-describing, bounded and honest.

    Every probability must be a probability, every league must appear once and
    only once (one league's effective ownership rendered under another's name is
    the exact failure this layer exists to prevent), and every number must name
    the simulation and model it came from.
    """
    from gaffer import chips as CH
    from gaffer import league as LG
    from gaffer import multileague as ML
    from gaffer import strategy as ST

    name = "strategy.json"
    if not isinstance(strat, dict):
        report.violations.append(
            Violation(name, "<root>", type(strat).__name__, "a JSON object")
        )
        return

    # A run that failed loudly is publishable; one that failed silently is not.
    if strat.get("error"):
        for key in ("strategy_version", "generated_at", "gameweek"):
            if strat.get(key) in (None, ""):
                report.violations.append(
                    Violation(name, key, strat.get(key),
                              "to be present even on a failed strategy build")
                )
        return

    versions = {
        "strategy_version": ST.STRATEGY_VERSION,
        "league_version": LG.LEAGUE_VERSION,
        "multileague_version": ML.MULTILEAGUE_VERSION,
        "chips_version": CH.CHIPS_VERSION,
    }
    for key, expected in versions.items():
        if strat.get(key) != expected:
            report.violations.append(
                Violation(name, key, strat.get(key),
                          f"{expected!r} — the front-end refuses a version it "
                          "cannot interpret rather than rendering it wrongly")
            )

    # --- simulation provenance ---------------------------------------------
    sim = strat.get("simulation")
    if not isinstance(sim, dict):
        report.violations.append(
            Violation(name, "simulation", sim,
                      "an object naming the simulation behind every probability")
        )
    else:
        for key in ("sim_version", "n_sims", "seed", "model_version"):
            if sim.get(key) in (None, ""):
                report.violations.append(
                    Violation(name, f"simulation.{key}", sim.get(key), "to be present")
                )
        if isinstance(sim.get("n_sims"), int) and sim["n_sims"] < 1:
            report.violations.append(
                Violation(name, "simulation.n_sims", sim["n_sims"],
                          "at least one scenario behind any published probability")
            )

    # --- leagues: isolated, bounded, and probability-shaped ------------------
    leagues = strat.get("leagues")
    if not isinstance(leagues, list):
        report.violations.append(
            Violation(name, "leagues", leagues, "a list (empty when none are configured)")
        )
        leagues = []
    seen: set[Any] = set()
    for i, lg in enumerate(leagues):
        if not isinstance(lg, dict):
            report.violations.append(
                Violation(name, f"leagues[{i}]", type(lg).__name__, "an object")
            )
            continue
        lid = lg.get("league_id")
        if lid in seen:
            report.violations.append(
                Violation(name, f"leagues[{i}].league_id", lid,
                          "each league to appear exactly once — a duplicate means "
                          "one league's ownership is rendered under another's name")
            )
        seen.add(lid)
        if expected_league_ids and lid not in expected_league_ids:
            report.violations.append(
                Violation(name, f"leagues[{i}].league_id", lid,
                          f"one of the configured leagues {expected_league_ids}")
            )
        placing = lg.get("placing")
        if not isinstance(placing, dict):
            report.violations.append(
                Violation(name, f"leagues[{i}].placing", placing,
                          "an object of placing probabilities")
            )
            continue
        for key in ("p_first_after_gw", "p_target_after_gw"):
            v = placing.get(key)
            if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
                report.violations.append(
                    Violation(name, f"leagues[{i}].placing.{key}", v,
                              "a probability in [0, 1]")
                )
        if placing.get("basis") in (None, ""):
            report.violations.append(
                Violation(name, f"leagues[{i}].placing.basis", placing.get("basis"),
                          "a stated basis for the probability")
            )
        if "available" not in placing:
            report.violations.append(
                Violation(name, f"leagues[{i}].placing.available", None,
                          "an explicit availability flag — a probability with no "
                          "field behind it must be renderable as unknown, never "
                          "as a number")
            )
        # A certainty is almost always a bug (an empty field simulates as "you
        # win every scenario"), so it must be earned by an actual field.
        if (placing.get("available")
                and float(placing.get("p_first_after_gw") or 0) >= 1.0):
            dqx = lg.get("data_quality") or {}
            if not dqx.get("rivals"):
                report.violations.append(
                    Violation(name, f"leagues[{i}].placing.p_first_after_gw", 1.0,
                              "a probability below certainty, or an unavailable "
                              "placing — a 100% chance of winning a league with "
                              "no known rivals is an artefact, not a forecast")
                )
        dq = lg.get("data_quality")
        if not isinstance(dq, dict) or "coverage_pct" not in dq:
            report.violations.append(
                Violation(name, f"leagues[{i}].data_quality", dq,
                          "rival-data freshness and coverage, so a thin sample "
                          "cannot masquerade as a precise probability")
            )
        if lg.get("target_position") in (None, ""):
            report.violations.append(
                Violation(name, f"leagues[{i}].target_position", lg.get("target_position"),
                          "an explicit target the probability is measured against")
            )
        if lg.get("differs_from_neutral") and not lg.get("difference_reason"):
            report.violations.append(
                Violation(name, f"leagues[{i}].difference_reason", "",
                          "a stated reason whenever a league departs from the "
                          "neutral recommendation")
            )

    # --- options and conflicts ----------------------------------------------
    for i, opt in enumerate(strat.get("options") or []):
        if not isinstance(opt, dict):
            continue
        for lid, p in (opt.get("p_target") or {}).items():
            if not isinstance(p, (int, float)) or not (0.0 <= float(p) <= 1.0):
                report.violations.append(
                    Violation(name, f"options[{i}].p_target.{lid}", p,
                              "a probability in [0, 1]")
                )
    res = strat.get("resolution")
    if not isinstance(res, dict) or not res.get("reason"):
        report.violations.append(
            Violation(name, "resolution", res,
                      "a resolution with a stated reason — an unexplained default "
                      "across conflicting leagues is an invented answer")
        )

    # --- chips ---------------------------------------------------------------
    ch = strat.get("chips")
    if not isinstance(ch, dict):
        report.violations.append(
            Violation(name, "chips", ch, "a chip plan object")
        )
    else:
        for key in ("recommendation", "available", "used", "reason"):
            if key not in ch:
                report.violations.append(
                    Violation(name, f"chips.{key}", None, "to be present")
                )
    # G1/A7. `evaluate_wildcard` multiplied the gain by `weeks_retained` and
    # published the un-multiplied one-week means beside it, so the headline
    # (0.47) disagreed with baseline/with_chip (implying 0.11) by exactly the
    # multiplier. Three of four chips reconciled; only the wildcard did not.
        for _alt in (ch.get("alternatives") or []):
            _b, _w, _g = (_alt.get("baseline_points"), _alt.get("with_chip_points"),
                          _alt.get("expected_gain"))
            if None in (_b, _w, _g):
                continue
            if abs(float(_g) - (float(_w) - float(_b))) > 0.05:
                report.violations.append(
                    Violation(name, f"chips.alternatives[{_alt.get('chip')}].expected_gain",
                              _g,
                              f"{float(_w) - float(_b):.2f} -- the gain must equal "
                              "with_chip_points minus baseline_points, or the "
                              "reader checking the arithmetic gets a different "
                              "answer from the headline"))

        rec = ch.get("recommendation")
        known = {CH.WILDCARD, CH.FREEHIT, CH.BENCH_BOOST, CH.TRIPLE_CAPTAIN, "hold"}
        if rec is not None and rec not in known:
            report.violations.append(
                Violation(name, "chips.recommendation", rec, f"one of {sorted(known)}")
            )
        used = ch.get("used") or []
        if rec in used:
            report.violations.append(
                Violation(name, "chips.recommendation", rec,
                          f"a chip that has not already been played (used: {used})")
            )

    if not strat.get("limitations"):
        report.violations.append(
            Violation(name, "limitations", strat.get("limitations"),
                      "a non-empty list — probabilities must ship with caveats")
        )


def _check_one_canonical_first_move(data_dir: Path, report: Report) -> None:
    """1.5 -- CARDINALITY. Exactly one object may answer "what is the move".

    `decision.json` and `plan.json` are produced by one pipeline run and were
    published from two different solvers over two different horizons. On
    2026-09-01 the home page offered four transfers for -12 while the planner
    offered three for -8, from the same run, and nothing noticed: every shape
    invariant passed, because each artifact was individually well formed.

    A contract that validates shape will pass any self-contradiction that is
    correctly shaped. This asserts the two agree.

    Compared as SETS of element ids, because ordering is a rendering choice.
    An empty move on both sides is agreement -- that is a roll.
    """
    dec_p, plan_p = data_dir / "decision.json", data_dir / "plan.json"
    if not (dec_p.exists() and plan_p.exists()):
        return
    dec = _load(data_dir, "decision.json", report)
    plan = _load(data_dir, "plan.json", report)
    if not isinstance(dec, dict) or not isinstance(plan, dict):
        return
    d = dec.get("decision")
    if not isinstance(d, dict):
        return

    def ids(blob: Any, key: str) -> set:
        rows = (blob or {}).get(key) or []
        out = set()
        for r in rows:
            out.add(r.get("id") if isinstance(r, dict) else r)
        return {i for i in out if i is not None}

    # The decision publishes its move as primary transfers when it recommends
    # one, and as `candidate_move` when it holds one back as evidence. Either
    # way it is a published move and must match the plan.
    #
    # When it publishes NEITHER, the decision is a plain roll and is claiming
    # nothing about a move: the plan's first step is then the only published
    # answer, which is a silence rather than a second one. Comparing against an
    # empty set there would fire on every legitimate roll.
    src = d if d.get("transfers_in") or d.get("transfers_out") else (
        d.get("candidate_move") or {})
    if not (src.get("transfers_in") or src.get("transfers_out")):
        return
    steps = plan.get("steps") or []
    first = plan.get("first_move") or (steps[0] if steps else {})
    if not isinstance(first, dict):
        return
    for key in ("transfers_in", "transfers_out"):
        a, b = ids(src, key), ids(first, key)
        if a != b:
            report.violations.append(Violation(
                "decision.json", f"decision.{key}", sorted(a),
                f"the same set as plan.json first_move.{key} ({sorted(b)}) — "
                "one run must not publish two different first moves; the "
                "multi-period path is canonical for transfers"))


def _check_decision(
    dec: Any, report: Report, meta: Any = None, expected_entry_id: int | None = None,
) -> None:
    """The weekly decision must be one clear action with its evidence attached.

    The audited home page led with a solver table; the contract now refuses to
    publish a decision that has no action, no comparison against holding, or no
    stated reason it could be wrong.
    """
    from gaffer import decision as D
    from gaffer import weekly as W

    name = "decision.json"
    if not isinstance(dec, dict):
        report.violations.append(
            Violation(name, "<root>", type(dec).__name__, "a JSON object"))
        return

    for key, expected in (("weekly_version", W.WEEKLY_VERSION),
                          ("decision_version", D.DECISION_VERSION)):
        if dec.get(key) != expected:
            report.violations.append(
                Violation(name, key, dec.get(key),
                          f"{expected!r} — the front-end refuses a version it "
                          "cannot interpret"))

    body = dec.get("decision")
    if not isinstance(body, dict):
        report.violations.append(
            Violation(name, "decision", body, "the week's decision object"))
        return

    action = body.get("action")
    if action not in D.ALL_ACTIONS:
        report.violations.append(
            Violation(name, "decision.action", action,
                      f"one of {sorted(D.ALL_ACTIONS)}"))
    for key in ("headline", "reason"):
        if not body.get(key):
            report.violations.append(
                Violation(name, f"decision.{key}", body.get(key),
                          "a plain-English statement of the week's answer"))
    if action in (D.ACTION_TRANSFER, D.ACTION_ROLL, D.ACTION_TOO_CLOSE) and \
            not body.get("biggest_risk"):
        report.violations.append(
            Violation(name, "decision.biggest_risk", body.get("biggest_risk"),
                      "the single most likely way this recommendation is wrong"))

    # Hold-versus-move must be explicit, and its probability must be one.
    cmp_ = body.get("comparison")
    if action != D.ACTION_UNAVAILABLE:
        if not isinstance(cmp_, dict):
            report.violations.append(
                Violation(name, "decision.comparison", cmp_,
                          "an explicit comparison against holding — a "
                          "recommendation with no baseline is not a decision"))
        else:
            p = cmp_.get("p_move_beats_hold")
            if not isinstance(p, (int, float)) or not (0.0 <= float(p) <= 1.0):
                report.violations.append(
                    Violation(name, "decision.comparison.p_move_beats_hold", p,
                              "a probability in [0, 1]"))
            for key in ("delta", "hold_expected", "move_expected", "simulations"):
                if key not in cmp_:
                    report.violations.append(
                        Violation(name, f"decision.comparison.{key}", None,
                                  "to be present"))
            ci = cmp_.get("delta_ci95")
            if not (isinstance(ci, list) and len(ci) == 2 and ci[0] <= ci[1]):
                report.violations.append(
                    Violation(name, "decision.comparison.delta_ci95", ci,
                              "an ordered [low, high] interval"))

    # A non-action must not carry a transfer prescription in the action fields.
    # This is the A4 invariant: the live site said ``too_close`` while naming
    # five transfers, a -16 hit and an executable plan immediately underneath.
    def refs(value: Any) -> list[Any]:
        rows = value if isinstance(value, list) else ([] if value is None else [value])
        return [row.get("id") if isinstance(row, dict) else row for row in rows]

    direct_transfers = {
        "in": refs(body.get("transfers_in")),
        "out": refs(body.get("transfers_out")),
    }
    if action != D.ACTION_TRANSFER:
        if direct_transfers["in"] or direct_transfers["out"]:
            report.violations.append(
                Violation(name, "decision.transfers", direct_transfers,
                          "no primary transfer list when the action is not "
                          "'transfer' — rejected plans belong in the labelled "
                          "candidate_move block"))
        if body.get("executability") is not None:
            report.violations.append(
                Violation(name, "decision.executability",
                          body.get("executability"),
                          "null when no transfer is being recommended"))

    candidate = body.get("candidate_move")
    if action == D.ACTION_TRANSFER and candidate is not None:
        report.violations.append(
            Violation(name, "decision.candidate_move", candidate,
                      "null when the candidate is already the action"))
    if candidate is not None:
        if not isinstance(candidate, dict):
            report.violations.append(
                Violation(name, "decision.candidate_move", candidate,
                          "a labelled evidence-only object or null"))
        else:
            if candidate.get("status") != D.CANDIDATE_STATUS_EVIDENCE_ONLY:
                report.violations.append(
                    Violation(name, "decision.candidate_move.status",
                              candidate.get("status"),
                              D.CANDIDATE_STATUS_EVIDENCE_ONLY))
            if candidate.get("basis") != D.CANDIDATE_BASIS_FUTURE_HORIZON:
                report.violations.append(
                    Violation(name, "decision.candidate_move.basis",
                              candidate.get("basis"),
                              D.CANDIDATE_BASIS_FUTURE_HORIZON))
            for key in ("label", "reason"):
                if not candidate.get(key):
                    report.violations.append(
                        Violation(name, f"decision.candidate_move.{key}",
                                  candidate.get(key),
                                  "plain language that this is evidence, not "
                                  "the action"))
            if not candidate.get("transfers_in"):
                report.violations.append(
                    Violation(name, "decision.candidate_move.transfers_in",
                              candidate.get("transfers_in"),
                              "the future plan being retained as evidence"))

    # A current loss that loses most scenarios is a hold, not ``too_close``.
    # If a positive horizon is what created the conflict, retain it explicitly.
    if isinstance(cmp_, dict):
        current = cmp_.get("delta")
        p = cmp_.get("p_move_beats_hold")
        future = cmp_.get("horizon_delta")
        current_loss = (
            isinstance(current, (int, float)) and float(current) < 0
            and isinstance(p, (int, float)) and float(p) < 0.5
        )
        if current_loss and action != D.ACTION_ROLL:
            report.violations.append(
                Violation(name, "decision.action", action,
                          "'roll' when the move loses this gameweek and in most "
                          "scenarios — that is a negative result, not a close one"))
        hit = cmp_.get("hit_cost")
        if (current_loss and isinstance(future, (int, float))
                and float(future) > 0
                and isinstance(hit, (int, float)) and float(hit) > 0
                and not isinstance(candidate, dict)):
            report.violations.append(
                Violation(name, "decision.candidate_move", candidate,
                          "the positive-horizon plan, labelled evidence_only, "
                          "when it conflicts with a current-week hold"))

    # Provenance: which model, objective and simulation produced this.
    versions = dec.get("versions")
    if not isinstance(versions, dict):
        report.violations.append(
            Violation(name, "versions", versions,
                      "model/objective/simulation provenance"))
    else:
        for key in ("model_version", "objective_version", "sim_version",
                    "n_sims", "seed"):
            if versions.get(key) in (None, ""):
                report.violations.append(
                    Violation(name, f"versions.{key}", versions.get(key),
                              "to be present"))

    # Identity and ordering.
    if expected_entry_id is not None and isinstance(meta, dict):
        if meta.get("entry_id") != expected_entry_id:
            report.violations.append(
                Violation(name, "entry_id", meta.get("entry_id"),
                          f"the configured entry {expected_entry_id}"))
    gw = dec.get("gameweek")
    if not isinstance(gw, int):
        report.violations.append(
            Violation(name, "gameweek", gw, "the target event as an integer"))
    elif isinstance(meta, dict) and meta.get("current_gw") is not None:
        try:
            if int(meta["current_gw"]) != gw:
                report.violations.append(
                    Violation(name, "gameweek", gw,
                              f"the same event as meta.current_gw "
                              f"({meta['current_gw']})"))
        except (TypeError, ValueError):
            pass

    # A pre-deadline record must not contain anything only knowable afterwards.
    forbidden = {"actual_points", "realised", "final_points", "result",
                 "outcome_percentile"}
    leaked = sorted(forbidden & set(dec) | (forbidden & set(body)))
    if leaked:
        report.violations.append(
            Violation(name, "decision", leaked,
                      "no post-deadline fields in a pre-deadline record"))

    if not dec.get("freshness"):
        report.violations.append(
            Violation(name, "freshness", dec.get("freshness"),
                      "the provenance of the squad, bank and free-transfer values"))


def _check_live(live: Any, report: Report, meta: Any = None) -> None:
    """Live points must keep confirmed, provisional and predicted apart."""
    from gaffer import live as L

    name = "live.json"
    if not isinstance(live, dict):
        report.violations.append(
            Violation(name, "<root>", type(live).__name__, "a JSON object"))
        return
    if live.get("live_version") != L.LIVE_VERSION:
        report.violations.append(
            Violation(name, "live_version", live.get("live_version"),
                      f"{L.LIVE_VERSION!r}"))
    if "available" not in live:
        report.violations.append(
            Violation(name, "available", None,
                      "an explicit availability flag — 'no live data' is a "
                      "state, not an empty scoreboard"))
    if not live.get("available"):
        if not live.get("unavailable_reason"):
            report.violations.append(
                Violation(name, "unavailable_reason", None,
                          "a machine-readable reason when live data is absent"))
        return

    for s in live.get("fixtures") or []:
        if s.get("state") not in L.ALL_STATES:
            report.violations.append(
                Violation(name, "fixtures[].state", s.get("state"),
                          f"one of {sorted(L.ALL_STATES)}"))
    # G1/A5. The manager is a member of his own mini-league, so the league
    # fetch returns him; prepending a synthetic "You" row put him in twice,
    # displaced every rival below him, and made him his own closest rival --
    # which silently returned `largest_swing: null` on every run.
    _rivals = live.get("rivals") or []
    _ids = [r.get("entry_id") for r in _rivals if isinstance(r, dict)]
    if len(_ids) != len(set(_ids)):
        _dupes = sorted({i for i in _ids if _ids.count(i) > 1})
        report.violations.append(
            Violation(name, "rivals[].entry_id", _dupes,
                      "each entry to appear exactly once -- a duplicated entry "
                      "displaces every rival below it and makes the manager his "
                      "own closest rival"))

    # G1/A5b. Requested by the fix: one duplicate row sat in the artifact in
    # plain sight for a fortnight. Assert the shape that made it visible.
    _you = [r for r in _rivals if isinstance(r, dict) and r.get("you")]
    if len(_you) != 1:
        report.violations.append(
            Violation(name, "rivals[].you", len(_you),
                      "exactly one row flagged as the manager himself"))

    # G1/A6. `me` was published as null on every run because nothing wrote it.
    # Now that it exists, stop it drifting from the table it was lifted out of.
    _me = live.get("me")
    if not isinstance(_me, dict):
        report.violations.append(
            Violation(name, "me", _me,
                      "the manager's own row -- a permanently null field trains "
                      "a reader to ignore the schema"))
    elif _you:
        for _k in ("entry_id", "provisional_position"):
            if _me.get(_k) != _you[0].get(_k):
                report.violations.append(
                    Violation(name, f"me.{_k}", _me.get(_k),
                              f"{_you[0].get(_k)!r}, matching the 'you' row"))

    # G1/B3. The artifact must carry the rows its totals are built from. Before
    # this, `live.json` could publish a squad total with no per-player rows at
    # all, and nothing noticed -- which is why the recompute the working method
    # depends on had to be rebuilt by hand from the raw API every week.
    _players = live.get("players")
    if not isinstance(_players, list) or not _players:
        report.violations.append(
            Violation(name, "players", _players,
                      "the per-player rows the published totals are built from"))
    else:
        _have = {r.get("id") for r in _players if isinstance(r, dict)}
        _subs = (live.get("squad") or {}).get("autosubs") or {}
        _need = set(_subs.get("xi") or []) | set(_subs.get("bench") or [])
        _missing = sorted(x for x in _need if x not in _have)
        if _missing:
            report.violations.append(
                Violation(name, "players[].id", _missing,
                          "a row for every player in autosubs.xi and .bench -- "
                          "a missing row under-counts silently"))

    # G1/B3. An absent `hits` reads as 0, and `live.entry_baseline_and_hits`
    # records exactly that defect: "a -8 week read four points better than it
    # was". Absence must not be spellable as zero.
    _hits = (live.get("squad") or {}).get("hits")
    if not isinstance(_hits, int):
        report.violations.append(
            Violation(name, "squad.hits", _hits,
                      "an integer -- an absent hits figure reads as 0 and "
                      "flatters the week by the size of the hit"))

    sep = live.get("separation")
    if not isinstance(sep, dict) or not {
            "confirmed", "provisional_bonus", "predicted_remaining"} <= set(sep):
        report.violations.append(
            Violation(name, "separation", sep,
                      "confirmed, provisional and predicted reported separately "
                      "— merging them presents unearned bonus as banked points"))

    squad = live.get("squad")
    if isinstance(squad, dict):
        for key in ("confirmed", "provisional_bonus", "predicted_remaining",
                    "current", "projected"):
            if key not in squad:
                report.violations.append(
                    Violation(name, f"squad.{key}", None, "to be present"))
        subs = squad.get("autosubs") or {}
        xi = subs.get("xi") or []
        if xi and len(xi) != 11:
            report.violations.append(
                Violation(name, "squad.autosubs.xi", f"{len(xi)} players",
                          "exactly 11 after substitutions — an illegal XI means "
                          "the autosub rules were not applied"))
        if subs.get("captain_source") not in (None, "captain", "vice", "none"):
            report.violations.append(
                Violation(name, "squad.autosubs.captain_source",
                          subs.get("captain_source"),
                          "one of captain | vice | none"))
        if set(subs.get("subs_in") or []) & set(subs.get("subs_out") or []):
            report.violations.append(
                Violation(name, "squad.autosubs", "overlapping subs",
                          "a player cannot be both substituted on and off"))


def _check_review(review: Any, report: Report) -> None:
    """A review must be linked to the decision it scores, and free of hindsight."""
    from gaffer import review as R

    name = "review.json"
    if not isinstance(review, dict):
        report.violations.append(
            Violation(name, "<root>", type(review).__name__, "a JSON object"))
        return
    if review.get("review_version") != R.REVIEW_VERSION:
        report.violations.append(
            Violation(name, "review_version", review.get("review_version"),
                      f"{R.REVIEW_VERSION!r}"))
    for key in ("event", "entry_id", "generated_at", "comparison", "quality",
                "attribution"):
        if key not in review:
            report.violations.append(
                Violation(name, key, None, "to be present"))

    q = review.get("quality") or {}
    if q.get("verdict") is None:
        report.violations.append(
            Violation(name, "quality.verdict", None,
                      "an explicit decision-quality verdict"))
    pct = q.get("outcome_percentile")
    if pct is not None and not (0.0 <= float(pct) <= 1.0):
        report.violations.append(
            Violation(name, "quality.outcome_percentile", pct,
                      "a percentile in [0, 1]"))

    # G1/A10a. No record at all means nothing to grade.
    if not review.get("has_snapshot") and pct is not None:
        report.violations.append(
            Violation(name, "quality.outcome_percentile", pct,
                      "to be withheld when no pre-deadline snapshot exists -- a "
                      "grade may not be computed from a record that is absent"))

    # G1/A10b. The defect was a review asserting the record was absent while
    # reporting `has_snapshot: true` two fields above. Tie the sentence to the
    # state it describes.
    if (review.get("has_snapshot")
            and "No pre-deadline record exists" in str(q.get("explanation") or "")):
        report.violations.append(
            Violation(name, "quality.explanation", "claims no pre-deadline record",
                      "a sentence consistent with has_snapshot=true -- name the "
                      "missing field instead of denying the record"))

    # G1/A10c. A published percentile must say what it is a percentile OF.
    # The published key is `outcome_percentile_basis`, matching
    # `outcome_percentile`. Reading `percentile_basis` -- the dataclass attribute
    # name -- made this fire on every review regardless of content, which is a
    # gate that blocks publishing while proving nothing.
    if pct is not None and not str(q.get("outcome_percentile_basis") or "").strip():
        report.violations.append(
            Violation(name, "quality.outcome_percentile_basis",
                      q.get("outcome_percentile_basis"),
                      "a stated reference class -- the number is a position in "
                      "the distribution of the squad Gaffer RECOMMENDED, not a "
                      "rank against other managers"))

    # G1/E1. Every statistic in the season-calibration block must carry its own
    # sample size, at whatever depth it sits.
    _STAT_KEYS = {"mean", "mae", "ks_d", "median", "baseline_mae",
                  "skill_vs_pool_mean", "bias"}

    def _stats_carry_n(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if _STAT_KEYS & set(node) and "n" not in node:
                report.violations.append(
                    Violation(name, f"season_calibration.{path}",
                              sorted(_STAT_KEYS & set(node)),
                              "an accompanying 'n' -- a calibration statistic "
                              "without its sample size reads as a finding when "
                              "it may be noise"))
                return
            for k, v in node.items():
                _stats_carry_n(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _stats_carry_n(v, f"{path}[{i}]")

    cal = review.get("season_calibration")
    if isinstance(cal, dict):
        _stats_carry_n(cal, "")
        basis = str(cal.get("distribution", {}).get("basis") or "").strip()
        if isinstance(cal.get("distribution"), dict) and not basis:
            report.violations.append(
                Violation(name, "season_calibration.distribution.basis", None,
                          "a stated reference class, the same rule already "
                          "enforced on quality.outcome_percentile_basis"))

    # A review that claims to assess a decision must name the snapshot it read.
    if review.get("has_snapshot") and not review.get("snapshot_as_of"):
        report.violations.append(
            Violation(name, "snapshot_as_of", None,
                      "the exact pre-deadline snapshot this review scores"))
    if not review.get("has_snapshot") and q.get("verdict") != R.VERDICT_UNKNOWN:
        report.violations.append(
            Violation(name, "quality.verdict", q.get("verdict"),
                      f"{R.VERDICT_UNKNOWN!r} — without a pre-deadline record "
                      "there is nothing to assess, only a result"))

    cmp_ = review.get("comparison") or {}
    if "hindsight_points" in cmp_ and not cmp_.get("hindsight_is_unknowable"):
        report.violations.append(
            Violation(name, "comparison.hindsight_is_unknowable",
                      cmp_.get("hindsight_is_unknowable"),
                      "true — a hindsight column must be labelled as such"))


def _check_notifications(notif: Any, report: Report) -> None:
    """Published notification state must be dry-run and credential-free."""
    from gaffer.notify import rules as NR
    from gaffer.notify.engine import ALL_SEVERITIES, ALL_STATES

    name = "notifications.json"
    if not isinstance(notif, dict):
        report.violations.append(
            Violation(name, "<root>", type(notif).__name__, "a JSON object"))
        return
    result = notif.get("result")
    if not isinstance(result, dict):
        report.violations.append(
            Violation(name, "result", result, "the engine's run result"))
        return
    if result.get("dry_run") is not True:
        report.violations.append(
            Violation(name, "result.dry_run", result.get("dry_run"),
                      "true — Batch 5 ships the engine inactive, and a "
                      "published artifact claiming live delivery is a bug"))
    for i, a in enumerate(result.get("alerts") or []):
        if a.get("kind") not in NR.ALL_KINDS:
            report.violations.append(
                Violation(name, f"result.alerts[{i}].kind", a.get("kind"),
                          f"one of {sorted(NR.ALL_KINDS)}"))
        if a.get("severity") not in ALL_SEVERITIES:
            report.violations.append(
                Violation(name, f"result.alerts[{i}].severity", a.get("severity"),
                          f"one of {sorted(ALL_SEVERITIES)}"))
        if a.get("state") not in ALL_STATES:
            report.violations.append(
                Violation(name, f"result.alerts[{i}].state", a.get("state"),
                          f"one of {sorted(ALL_STATES)}"))
        if not str(a.get("deep_link") or "").startswith("#/"):
            report.violations.append(
                Violation(name, f"result.alerts[{i}].deep_link", a.get("deep_link"),
                          "an in-app deep link so the alert is actionable"))
    # A credential must never reach a published artifact.
    blob = json.dumps(notif)
    for marker in ("http://", "https://hooks", "token=", "Bearer "):
        if marker in blob:
            report.violations.append(
                Violation(name, "<payload>", marker,
                          "no endpoint or credential in a published artifact"))


def validate(
    data_dir: Path | str | None = None,
    now: datetime | None = None,
    *,
    min_players: int = MIN_PLAYERS,
    max_age: timedelta = MAX_META_AGE,
    expected_entry_id: int | None = None,
    require_personalised: bool | None = None,
) -> Report:
    """Validate the exported artifact set.

    ``now`` is injectable so tests never depend on the wall clock.
    ``expected_entry_id`` / ``require_personalised`` default to the resolved
    Settings, so CI checks the entry it was configured with.
    """
    data_dir = Path(data_dir) if data_dir is not None else config.DATA_DIR
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    report = Report(data_dir=str(data_dir))

    if expected_entry_id is None or require_personalised is None:
        try:
            settings = config.Settings.load()
        except config.ConfigError as exc:
            report.violations.append(
                Violation("<config>", "settings", str(exc), "personal config to parse")
            )
            settings = config.Settings()
        if expected_entry_id is None:
            expected_entry_id = settings.entry_id
        if require_personalised is None:
            require_personalised = settings.personalised

    meta = _load(data_dir, "meta.json", report)
    players = _load(data_dir, "players.json", report)
    rec = _load(data_dir, "recommendation.json", report)
    _load(data_dir, "fixtures.json", report)
    # Optional by shape: present-but-null is legitimate for a generic build.
    my_team = _load(data_dir, "my_team.json", report) if (
        data_dir / "my_team.json").exists() else None
    plan = _load(data_dir, "plan.json", report) if (data_dir / "plan.json").exists() else None

    # --- players.json ------------------------------------------------------
    if players is not None:
        if not isinstance(players, list):
            report.violations.append(
                Violation("players.json", "<root>", type(players).__name__, "a JSON array")
            )
        elif len(players) < min_players:
            report.violations.append(
                Violation("players.json", "<length>", len(players),
                          f"at least {min_players} player entries")
            )
        else:
            # G1/A0. `next_gw_xp` and `dist` come from two different models --
            # the published number is a blend with FPL's `ep_next`, the
            # distribution is Gaffer's own simulation. When the blend overrides
            # the model hard enough, the artifact ships an expectation the same
            # artifact says is nearly impossible: a keeper published at 7.27
            # beside a 90th percentile of 2.0 and a 0.2% chance of a haul.
            #
            # The bound is deliberately loose rather than `xp <= ceiling`. For a
            # right-skewed distribution the mean can sit above the 90th
            # percentile, so a strict test would be wrong. With p90 = c and a
            # generous 20-point cap on a single gameweek,
            #     E[X] <= 0.9*c + 0.1*20 = 0.9*c + 2.0
            # Anything above that is not skew, it is two models disagreeing.
            # G1/A13. `model_xp` and `dist` are two readings of ONE set of
            # fixture rates, so the point estimate must sit inside its own
            # distribution. It did not: the sampler drew six of the projection's
            # eleven components, omitting goals conceded, saves, cards, own
            # goals and penalties, which nets negative for a defender and
            # positive for a keeper -- three keepers published ABOVE their own
            # 90th percentile and seven players below their own 25th.
            # The 0.05 is publishing slack: `model_xp` carries 2dp and
            # floor/ceiling carry 1dp. It is not modelling slack.
            for _p in players:
                if not isinstance(_p, dict):
                    continue
                _dd = _p.get("dist")
                if isinstance(_dd, dict):
                    _m = _p.get("model_xp")
                    _lo, _hi = _dd.get("floor"), _dd.get("ceiling")
                    if _m is not None and _lo is not None and _hi is not None:
                        _who = _p.get("name", _p.get("id"))
                        if float(_m) > float(_hi) + 0.05 + 1e-9:
                            report.violations.append(
                                Violation("players.json", f"[{_who}].model_xp", _m,
                                          f"at most its own simulated ceiling "
                                          f"{_hi} -- the point estimate and the "
                                          "distribution are built from the same "
                                          "rates and must agree"))
                        elif float(_m) < float(_lo) - 0.05 - 1e-9:
                            report.violations.append(
                                Violation("players.json", f"[{_who}].model_xp", _m,
                                          f"at least its own simulated floor "
                                          f"{_lo} -- same rates, same answer"))

            for _p in players:
                if not isinstance(_p, dict):
                    continue
                _d = _p.get("dist")
                _c = _d.get("ceiling") if isinstance(_d, dict) else None
                _x = _p.get("next_gw_xp")
                if _c is None or _x is None:
                    continue
                _bound = 0.9 * float(_c) + 2.0
                if float(_x) > _bound:
                    report.violations.append(
                        Violation("players.json",
                                  f"[{_p.get('name', _p.get('id'))}].next_gw_xp",
                                  _x,
                                  f"at most {_bound:.2f}, the most an expectation "
                                  f"can be given this player's own simulated "
                                  f"ceiling of {_c} -- the published projection "
                                  f"and the simulation disagree"))

    # G1/A0b. Nothing asserted that the regime was actually applied to the
    # rows. A regime recorded in meta while the rows say otherwise is the same
    # class of defect as the reason string that described an assumption.
    if isinstance(players, list) and isinstance(meta, dict):
        if str(meta.get("projection_regime")) == "component_only":
            for _p in players:
                if not isinstance(_p, dict):
                    continue
                _m, _x = _p.get("model_xp"), _p.get("next_gw_xp")
                if _m is None or _x is None:
                    continue
                if abs(float(_x) - float(_m)) > 0.01:
                    report.violations.append(
                        Violation("players.json",
                                  f"[{_p.get('name', _p.get('id'))}].next_gw_xp",
                                  _x,
                                  f"{_m} -- the regime is 'component_only', so the "
                                  "published number must BE the model"))
                    break

    # --- meta.json ---------------------------------------------------------
    run_stamp: datetime | None = None
    if meta is not None:
        if not isinstance(meta, dict):
            report.violations.append(
                Violation("meta.json", "<root>", type(meta).__name__, "a JSON object")
            )
        else:
            raw = meta.get("generated_at")
            run_stamp = parse_iso_utc(raw)
            if run_stamp is None:
                report.violations.append(
                    Violation("meta.json", "generated_at", raw,
                              "an ISO 8601 timestamp with an explicit UTC offset")
                )
            else:
                age = now - run_stamp
                if age > max_age:
                    report.violations.append(
                        Violation("meta.json", "generated_at", raw,
                                  f"a timestamp no more than {max_age} old "
                                  f"(this run is {age} old — the pipeline did not "
                                  f"regenerate the artifacts)")
                    )
                if -age > MAX_CLOCK_SKEW:
                    report.violations.append(
                        Violation("meta.json", "generated_at", raw,
                                  f"a timestamp not in the future by more than "
                                  f"{MAX_CLOCK_SKEW} (clock skew)")
                    )

            # Personalisation must be explicit, never implied.
            mode = meta.get("build_mode")
            if mode not in ("personalised", "generic"):
                report.violations.append(
                    Violation("meta.json", "build_mode", mode,
                              "either 'personalised' or 'generic'")
                )
            if require_personalised:
                if mode != "personalised":
                    report.violations.append(
                        Violation("meta.json", "build_mode", mode,
                                  "'personalised' — an entry id is configured")
                    )
                if meta.get("entry_id") != expected_entry_id:
                    report.violations.append(
                        Violation("meta.json", "entry_id", meta.get("entry_id"),
                                  f"the configured entry id {expected_entry_id}")
                    )
            league_ids = meta.get("league_ids")
            if league_ids is not None and not isinstance(league_ids, list):
                report.violations.append(
                    Violation("meta.json", "league_ids", league_ids,
                              "a list of league ids (never a bare scalar)")
                )

    # --- timestamps agree across the run ------------------------------------
    for name, blob in (("recommendation.json", rec), ("plan.json", plan)):
        if not isinstance(blob, dict):
            continue
        raw = blob.get("generated_at")
        stamp = parse_iso_utc(raw)
        if stamp is None:
            report.violations.append(
                Violation(name, "generated_at", raw,
                          "an ISO 8601 timestamp with an explicit UTC offset")
            )
        elif run_stamp is not None and stamp != run_stamp:
            report.violations.append(
                Violation(name, "generated_at", raw,
                          f"the same run timestamp as meta.json "
                          f"({run_stamp.isoformat()})")
            )

    # --- squad shape --------------------------------------------------------
    if rec is not None:
        _check_squad_shape(rec, players, report)

    # --- backtest artifact ---------------------------------------------------
    # Optional (it is a manual step), but if present it must be a schema the
    # front-end can render. A legacy artifact would otherwise keep publishing
    # accuracy claims about a model that never shipped.
    if (data_dir / "backtest.json").exists():
        bt = _load(data_dir, "backtest.json", report)
        if bt is not None:
            _check_backtest(bt, report)

    # --- strategy artifact ----------------------------------------------------
    # Optional by presence (a --skip-strategy run writes none), but when present
    # it publishes probabilities, so it must name its simulation, keep leagues
    # isolated and never recommend a spent chip.
    if (data_dir / "strategy.json").exists():
        strat = _load(data_dir, "strategy.json", report)
        if strat is not None:
            configured = (
                meta.get("league_ids") if isinstance(meta, dict) else None
            )
            _check_strategy(
                strat, report,
                expected_league_ids=configured if isinstance(configured, list) else None,
            )

    # --- Batch 5 artifacts ----------------------------------------------------
    # All optional by presence. When present they publish advice, live scores,
    # judgements and alert state, so each has its own invariants.
    for fname, checker in (
        ("decision.json", lambda d: _check_decision(
            d, report, meta, expected_entry_id if require_personalised else None)),
        ("live.json", lambda d: _check_live(d, report, meta)),
        ("review.json", lambda d: _check_review(d, report)),
        ("notifications.json", lambda d: _check_notifications(d, report)),
        ("verdict.json", lambda d: _check_ai_text(
            "verdict.json", d, report, body="briefing_md")),
        ("news.json", lambda d: _check_ai_text(
            "news.json", d, report, body="digest_md")),
    ):
        if (data_dir / fname).exists():
            blob = _load(data_dir, fname, report)
            if blob is not None:
                checker(blob)

    _check_one_canonical_first_move(data_dir, report)

    # --- one season, everywhere ----------------------------------------------
    # T-29: FPL reuses element ids, so an artifact from last season parses
    # perfectly and renders as current. Every artifact carries the season it
    # describes, and they must all name the same one. `backtest.json` is the
    # deliberate exception: it reports a HISTORICAL season by design.
    declared = meta.get("season") if isinstance(meta, dict) else None
    if not (isinstance(declared, str) and season_mod.is_valid(declared)):
        report.violations.append(
            Violation("meta.json", "season", declared,
                      "a valid 'YYYY-YY' season label — without it a stale "
                      "artifact cannot be told from a current one")
        )
    else:
        for name in sorted(set(REQUIRED_ARTIFACTS) | set(OPTIONAL_ARTIFACTS)):
            if name in ("meta.json", "backtest.json"):
                continue
            path = data_dir / name
            if not path.exists():
                continue
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # already reported by _load
            if not isinstance(blob, dict):
                continue  # players.json / fixtures.json carry no envelope
            got = blob.get("season")
            if got is not None and got != declared:
                report.violations.append(
                    Violation(name, "season", got,
                              f"{declared!r}, matching meta.json — artifacts "
                              f"from two seasons must never publish together")
                )

    # --- nothing is published unvalidated ------------------------------------
    # T-27: REQUIRED_ARTIFACTS and OPTIONAL_ARTIFACTS were pure documentation —
    # declared, never read, free to drift from what the pipeline actually wrote.
    # Now they are the contract: a JSON file appearing in the data directory that
    # no checker above claimed is a violation, so a new artifact cannot ship
    # ahead of its validation.
    known = set(REQUIRED_ARTIFACTS) | set(OPTIONAL_ARTIFACTS)
    for path in sorted(data_dir.glob("*.json")):
        if path.name not in known:
            report.violations.append(
                Violation(path.name, "<file>", "unrecognised",
                          "an artifact named in REQUIRED_ARTIFACTS or "
                          "OPTIONAL_ARTIFACTS — this one is published but "
                          "validated by nothing")
            )
    unchecked = [n for n in REQUIRED_ARTIFACTS if n not in report.checked]
    if unchecked:
        report.violations.append(
            Violation("<contract>", "required_artifacts", unchecked,
                      "every required artifact to have been loaded and checked")
        )

    # --- squad state must be machine-readable and self-consistent ------------
    if isinstance(meta, dict):
        status = meta.get("squad_status")
        source_event = meta.get("squad_source_event")
        if status is not None and status not in gameweek.ALL_STATUSES:
            report.violations.append(
                Violation("meta.json", "squad_status", status,
                          f"one of {sorted(gameweek.ALL_STATUSES)}")
            )
        # A status claiming a squad exists must name where it came from, and a
        # status claiming none must not. This is the invariant that stops stale
        # rows masquerading as current holdings.
        if status in gameweek.STATUSES_WITH_SQUAD:
            if source_event in (None, ""):
                report.violations.append(
                    Violation("meta.json", "squad_source_event", source_event,
                              f"the event the squad was read from "
                              f"(squad_status is {status!r})")
                )
            if my_team is None:
                report.violations.append(
                    Violation("my_team.json", "<root>", None,
                              f"a squad object — meta.squad_status is {status!r}, "
                              "which asserts a squad is stored")
                )
        elif status in gameweek.STATUSES_WITHOUT_SQUAD:
            if source_event not in (None, ""):
                report.violations.append(
                    Violation("meta.json", "squad_source_event", source_event,
                              f"no source event — squad_status is {status!r}, "
                              "which asserts no squad is stored")
                )
            if my_team is not None:
                report.violations.append(
                    Violation("my_team.json", "<root>", "a squad object",
                              f"null — meta.squad_status is {status!r}, so any "
                              "stored squad would be stale and misleading")
                )
        if status is not None and not meta.get("squad_status_reason"):
            report.violations.append(
                Violation("meta.json", "squad_status_reason",
                          meta.get("squad_status_reason"),
                          "a human-readable reason accompanying squad_status")
            )

    # --- personalised runs must resolve a squad, or explain why not ----------
    # FPL keeps picks private until a gameweek's deadline passes, so having no
    # squad is legitimate pre-GW1 — but only when the status says exactly that.
    # `not_found`/`fetch_failed`/`malformed` are real problems and must not be
    # hidden behind a generic "unavailable".
    if require_personalised and my_team is None:
        status = (meta or {}).get("squad_status") if isinstance(meta, dict) else None
        benign = {gameweek.STATUS_NO_PUBLIC_SQUAD_YET}
        if status not in benign:
            report.violations.append(
                Violation("my_team.json", "<root>", None,
                          f"a squad object, or squad_status "
                          f"{gameweek.STATUS_NO_PUBLIC_SQUAD_YET!r} (no deadline has "
                          f"passed yet). Entry {expected_entry_id} is configured "
                          f"but squad_status is {status!r}")
            )

    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate Gaffer's exported artifacts")
    ap.add_argument("--data-dir", default=None, help="directory holding the JSON artifacts")
    ap.add_argument("--min-players", type=int, default=MIN_PLAYERS)
    ap.add_argument("--max-age-hours", type=float, default=MAX_META_AGE.total_seconds() / 3600)
    ap.add_argument("--allow-generic", action="store_true",
                    help="do not require a personalised build even if an entry id is set")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    report = validate(
        args.data_dir,
        min_players=args.min_players,
        max_age=timedelta(hours=args.max_age_hours),
        require_personalised=False if args.allow_generic else None,
    )
    print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
