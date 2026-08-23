"""E3 -- which expected-XI model should own the quantity, decided on evidence.

Both projects estimate the same latent thing: will this player start. Ledger
predicts it to decide how much a team is weakened; Gaffer predicts it to gate
every point a player can score. Two implementations of one quantity is how they
start disagreeing, so one of them should own it -- and which one is a question
with an answer, not a matter of code age.

Ledger's prospective harness has **2 predictions and 0 scored outcomes** and
accumulates ten a week. The archive settles the same question on 139,039 rows
this afternoon. That is the entire argument for doing it retrospectively.

PRE-REGISTERED
==============

**Contenders.**

``X0``  started-last-match, mapped through conditional start rates estimated on
        strictly earlier fixtures. The baseline that any model must beat to
        justify existing.
``X1``  Ledger ``xi_statistical_v1``: ``0.75*start_rate + 0.25*minute_share``,
        normalised within a position group and capped, exactly as
        ``ledger/xi/expected.py`` does it.
``X2``  Gaffer's shipped gate: current-season ``starts/fixtures_played`` once
        three fixtures exist, else last season's ``starts/38``, else a
        price-based prior.

**Target.** FPL ``starts`` for that player in that fixture. It arrives strictly
after every input, and no contender can see it.

**Metrics.** Brier and log loss on P(start); calibration gap; and XI hit rate --
of the eleven highest-probability players at a club, how many actually started.
The last one is the decision metric: a probability that ranks the right eleven
is worth more to both projects than one that is merely well calibrated over
fringe players who will never be picked.

**Stratified by regime**, because they carry different amounts of information
and averaging them flatters one and libels the other:
``COLD_START`` a club's first fixture of a season; ``EARLY_SEASON`` fixtures
2-5; ``ESTABLISHED`` from the sixth.

**Expected minutes is scored separately.** P(start) and expected minutes are
different questions and only one of them is a probability. Conflating them is
how a model that is good at one gets credit for the other.

**Success condition, fixed in advance.** A contender wins if it leads on XI hit
rate AND is not worse on Brier, in the ESTABLISHED regime, on seasons the model
was not fitted on. The winner becomes the single owner and **the loser's
implementation is deleted rather than kept "just in case"**. If no contender
clearly wins, both stay where they are.

**Leakage.** Every input for a fixture comes from rows whose own kickoff is
strictly earlier, or from a completed prior season. ``starts`` is the target and
is never an input to its own fixture.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from gaffer.model import features as F

EXCHANGE = Path.home() / "Projects" / "Football Exchange"
VERSION = "2026-08-23"

#: `starts` does not exist before this season; rows there are unusable as a
#: target and only usable as a prior where the column could be reported.
FIRST_SEASON_WITH_STARTS = "2022-23"
SEASON_ORDER = ["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]

#: Gaffer names the goalkeeper position GKP; the archive says GK. The cameo and
#: p60 curves are keyed the Gaffer way, so the lookup is translated rather than
#: allowed to miss and silently fall back to a default.
TO_GAFFER_POS = {"GK": "GKP", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}

#: Gaffer's own constants, reproduced so the contender is the shipped model.
CAMEO_MINUTES = 20.0
START_MINUTES = 78.0
MAX_UNSTARTED_MINUTES = 38 * CAMEO_MINUTES
START_CEILING = {"GK": 0.9, "DEF": 0.85, "MID": 0.8, "FWD": 0.8}

#: Ledger's own constants.
SLOTS = {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}

#: The archive's position vocabulary is not stable across seasons: 2021-22
#: carries both `GK` and `GKP`, and 2024-25 carries 322 rows labelled `AM`.
#: Folded here rather than left to fall through a dict lookup, because an
#: unmapped position silently drops a player out of the squad his teammates are
#: being normalised against -- which changes everyone else's probability, not
#: just his.
POSITION_MAP = {"GK": "GK", "GKP": "GK", "DEF": "DEF", "MID": "MID",
                "FWD": "FWD", "AM": "MID"}

EPS = 1e-9


def normalise(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9]+", " ", stripped).lower()).strip()


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class Row:
    season: str
    element: int
    name: str
    position: str
    team: str
    gw: int
    fixture: int
    kickoff: str
    value: float
    minutes: float | None
    starts: float | None
    # filled in during the prior-information pass
    starts_before: float = 0.0
    minutes_before: float = 0.0
    team_fixtures_before: int = 0
    base_starts: float | None = None
    base_minutes: float | None = None
    base_season: str | None = None
    started_previous: int | None = None


def load_absences(version: str = VERSION) -> dict[tuple[str, str], int]:
    """(season, normalised name) -> fixtures the player was AVAILABLE for.

    Keyed by name because the consumer joins a prior season, and FPL reuses
    element ids every summer. A missing key means UNKNOWN, and the caller falls
    back to 38 rather than assuming nobody was injured.
    """
    path = EXCHANGE / "ledger" / "absences" / version / "data.csv"
    if not path.exists():
        return {}
    out: dict[tuple[str, str], int] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            out[(raw["season"], raw["fpl_name_normalised"])] = int(
                raw["fixtures_available"])
    return out


ABSENCES: dict[tuple[str, str], int] = {}


def load_rows() -> list[Row]:
    path = EXCHANGE / "gaffer" / "player_history" / VERSION / "data.csv"
    out: list[Row] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            def num(key: str, raw=raw) -> float | None:
                text = (raw.get(key) or "").strip()
                if not text:
                    return None
                try:
                    return float(text)
                except ValueError:
                    return None
            try:
                element = int(raw["element"])
            except (KeyError, ValueError):
                continue
            out.append(Row(
                season=raw["season"], element=element, name=raw["name"],
                position=POSITION_MAP.get(raw["position"], ""), team=raw["team"],
                gw=int(num("GW") or 0), fixture=int(num("fixture") or 0),
                kickoff=raw["kickoff_time"], value=num("value") or 40.0,
                minutes=num("minutes"), starts=num("starts"),
            ))
    return out


def annotate(rows: list[Row]) -> None:
    """Attach strictly-prior information to every row. No row sees itself."""
    # Prior-season totals, matched by name because element ids are reused.
    season_totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        key = (row.season, normalise(row.name))
        season_totals[key][0] += (row.starts or 0.0)
        season_totals[key][1] += (row.minutes or 0.0)

    by_player: dict[tuple[str, int], list[Row]] = defaultdict(list)
    by_team: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        by_player[(row.season, row.element)].append(row)
        by_team[(row.season, row.team)].append(row)

    # Team fixture ordering: how many fixtures the club had completed.
    for group in by_team.values():
        order = sorted({(r.kickoff, r.fixture) for r in group})
        index = {fixture: i for i, (_, fixture) in enumerate(order)}
        for row in group:
            row.team_fixtures_before = index.get(row.fixture, 0)

    for (season, _element), group in by_player.items():
        group.sort(key=lambda r: (r.kickoff, r.fixture))
        starts = minutes = 0.0
        previous: int | None = None
        for row in group:
            row.starts_before = starts
            row.minutes_before = minutes
            row.started_previous = previous
            if row.starts is not None:
                starts += row.starts
                previous = int(row.starts > 0)
            if row.minutes is not None:
                minutes += row.minutes
        # Prior completed season, by name.
        position = SEASON_ORDER.index(season) if season in SEASON_ORDER else -1
        if position > 0:
            earlier = SEASON_ORDER[position - 1]
            totals = season_totals.get((earlier, normalise(group[0].name)))
            if totals:
                for row in group:
                    row.base_starts = totals[0]
                    row.base_minutes = totals[1]
                    row.base_season = earlier


# --------------------------------------------------------------------------
# contenders


def x2_gaffer(row: Row) -> float:
    """Gaffer's shipped minutes gate, with availability neutral.

    The historical archive carries no status or chance-of-playing column, so
    every player resolves to the model's available branch -- the same
    documented limitation Gaffer's own backtest runs under, applied identically
    to every contender so it cannot favour one.
    """
    zero_is_evidence = (
        row.base_season is not None and row.base_season >= FIRST_SEASON_WITH_STARTS
    )
    zero_starts_possible = (row.base_minutes or 0.0) <= MAX_UNSTARTED_MINUTES
    have_base = row.base_starts is not None
    if row.team_fixtures_before >= 3 and row.minutes_before > 0:
        return clamp(row.starts_before / row.team_fixtures_before, 0.0, 0.98)
    if have_base and (
        (row.base_starts or 0.0) > 0
        or (zero_is_evidence and zero_starts_possible)
    ):
        return clamp((row.base_starts or 0.0) / 38.0, 0.0, 0.98)
    frac = clamp((row.value - 40.0) / 60.0, 0.0, 1.0)
    ceiling = START_CEILING.get(row.position, 0.8)
    return 0.25 + frac * (ceiling - 0.25)


def x3_gaffer_corrected(row: Row) -> float:
    """E4 -- Gaffer's gate with the denominator Gaffer says is wrong, corrected.

    Identical to ``x2_gaffer`` except in the prior-season branch, where
    ``base_starts / 38`` becomes ``base_starts / fixtures_available``. Ledger's
    per-fixture corpus supplies the availability count, so a player who missed
    three months no longer reads as a player nobody picked.

    Where the corpus has no row the denominator stays 38, which means this
    contender can only ever differ from X2 on the rows the crossover data
    actually covers -- and the comparison below is restricted to exactly those.
    """
    zero_is_evidence = (
        row.base_season is not None and row.base_season >= FIRST_SEASON_WITH_STARTS
    )
    zero_starts_possible = (row.base_minutes or 0.0) <= MAX_UNSTARTED_MINUTES
    have_base = row.base_starts is not None
    if row.team_fixtures_before >= 3 and row.minutes_before > 0:
        return clamp(row.starts_before / row.team_fixtures_before, 0.0, 0.98)
    if have_base and (
        (row.base_starts or 0.0) > 0
        or (zero_is_evidence and zero_starts_possible)
    ):
        available = ABSENCES.get(
            (row.base_season or "", normalise(row.name)), 38)
        return clamp((row.base_starts or 0.0) / float(available), 0.0, 0.98)
    frac = clamp((row.value - 40.0) / 60.0, 0.0, 1.0)
    ceiling = START_CEILING.get(row.position, 0.8)
    return 0.25 + frac * (ceiling - 0.25)


def x1_ledger(group: list[Row]) -> dict[int, float]:
    """Ledger's rule, including its within-position normalisation.

    ``ledger/xi/expected.py`` ranks inside a position group rather than across
    the squad, on the stated grounds that a club fields exactly one keeper
    whatever the scores look like. Reproduced rather than approximated, because
    the normalisation is most of what the model is.
    """
    scores: dict[int, float] = {}
    for row in group:
        played = row.team_fixtures_before
        if played > 0:
            start_rate = row.starts_before / played
            minute_share = row.minutes_before / (played * 90.0)
            scores[row.element] = 0.75 * start_rate + 0.25 * minute_share
        else:
            # COLD_START. Ledger leans on ep_next and ownership here, neither of
            # which the archive carries, so the branch degenerates to zero for
            # everyone. Recorded as a genuine limitation of the comparison in
            # that regime rather than substituted with a different model.
            scores[row.element] = 0.0

    out: dict[int, float] = {}
    ranked_xi: set[int] = set()
    for position, slots in SLOTS.items():
        members = sorted(
            (r for r in group if r.position == position),
            key=lambda r: -scores[r.element],
        )
        for row in members[:slots]:
            ranked_xi.add(row.element)
        total = sum(max(scores[r.element], 0.0) for r in members) or 1.0
        for rank, row in enumerate(members):
            share = max(scores[row.element], 0.0) / total
            probability = min(0.97, share * slots)
            if row.element in ranked_xi:
                probability = max(probability, 0.55)
            elif rank >= slots + 2:
                probability = min(probability, 0.15)
            out[row.element] = probability
    return out


class StartedLastMatch:
    """X0 -- conditional start rates learned from strictly earlier fixtures."""

    def __init__(self) -> None:
        self.counts = {1: [1.0, 2.0], 0: [1.0, 2.0], None: [1.0, 2.0]}  # Laplace

    def probability(self, row: Row) -> float:
        hits, total = self.counts[row.started_previous]
        return hits / total

    def observe(self, row: Row) -> None:
        if row.starts is None:
            return
        bucket = self.counts[row.started_previous]
        bucket[0] += float(row.starts > 0)
        bucket[1] += 1.0


# --------------------------------------------------------------------------
# scoring


def evaluate(rows: list[Row], seasons: set[str]) -> dict:
    contenders = ("X0 started-last-match", "X1 ledger", "X2 gaffer",
                  "X3 gaffer+absences")
    regimes = ("COLD_START", "EARLY_SEASON", "ESTABLISHED", "ALL")
    stats: dict[tuple[str, str], dict] = {
        (c, r): {"n": 0, "brier": 0.0, "logloss": 0.0, "xi_hits": 0, "xi_slots": 0,
                 "bins": defaultdict(lambda: [0.0, 0.0, 0])}
        for c in contenders for r in regimes
    }
    minutes_stats = {"n": 0, "mae": 0.0, "naive_mae": 0.0}

    x0 = StartedLastMatch()
    # E4 is only meaningful where the two denominators actually differ. Scoring
    # it over the whole population would dilute a real effect into 100,000 rows
    # on which the two contenders are the same number by construction.
    e4: dict[str, list[float]] = {"x2": [], "x3": [], "seasons": []}

    groups: dict[tuple[str, str, int], list[Row]] = defaultdict(list)
    for row in rows:
        if not row.position:
            continue          # not a player: managers and unmapped labels
        groups[(row.season, row.team, row.fixture)].append(row)

    for key in sorted(groups, key=lambda k: (groups[k][0].kickoff, k)):
        group = groups[key]
        season = group[0].season
        played = group[0].team_fixtures_before
        if played == 0:
            regime = "COLD_START"
        elif played < 5:
            regime = "EARLY_SEASON"
        else:
            regime = "ESTABLISHED"

        scoring = season in seasons and all(r.starts is not None for r in group)
        ledger_p = x1_ledger(group)

        predictions: dict[str, dict[int, float]] = {
            "X0 started-last-match": {r.element: x0.probability(r) for r in group},
            "X1 ledger": ledger_p,
            "X2 gaffer": {r.element: x2_gaffer(r) for r in group},
            "X3 gaffer+absences": {
                r.element: x3_gaffer_corrected(r) for r in group},
        }

        if scoring:
            for contender, probabilities in predictions.items():
                ranked = sorted(group, key=lambda r: -probabilities[r.element])[:11]
                hits = sum(1 for r in ranked if (r.starts or 0) > 0)
                for target in (regime, "ALL"):
                    cell = stats[(contender, target)]
                    cell["xi_hits"] += hits
                    cell["xi_slots"] += 11
                    for row in group:
                        p = clamp(probabilities[row.element], 1e-6, 1 - 1e-6)
                        actual = int((row.starts or 0) > 0)
                        cell["n"] += 1
                        cell["brier"] += (p - actual) ** 2
                        cell["logloss"] -= math.log(p if actual else 1 - p)
                        bucket = cell["bins"][min(9, int(p * 10))]
                        bucket[0] += p
                        bucket[1] += actual
                        bucket[2] += 1

            for row in group:
                p2 = predictions["X2 gaffer"][row.element]
                p3 = predictions["X3 gaffer+absences"][row.element]
                if abs(p2 - p3) < 1e-12:
                    continue
                actual = int((row.starts or 0) > 0)
                e4["x2"].append((clamp(p2, 1e-6, 1 - 1e-6) - actual) ** 2)
                e4["x3"].append((clamp(p3, 1e-6, 1 - 1e-6) - actual) ** 2)
                e4["seasons"].append(row.season)

            # Expected minutes, scored on its own terms against a naive rule.
            for row in group:
                if row.minutes is None:
                    continue
                p_start = predictions["X2 gaffer"][row.element]
                # Gaffer's REAL expected-minutes formula, with the fitted cameo
                # curve imported rather than approximated. An earlier version of
                # this script used a flat 0.15 cameo term, which understates the
                # shipped model -- and a finding about a model has to be about
                # the model, not about a convenient stand-in for it.
                pos = TO_GAFFER_POS.get(row.position, "MID")
                cameo = F.cameo_probability(p_start, pos)
                p_play = clamp(p_start + (1 - p_start) * cameo, 0.0, 0.99)
                predicted = (p_start * START_MINUTES
                             + (p_play - p_start) * CAMEO_MINUTES)
                naive = (row.minutes_before / row.team_fixtures_before
                         if row.team_fixtures_before else 45.0)
                minutes_stats["n"] += 1
                minutes_stats["mae"] += abs(predicted - row.minutes)
                minutes_stats["naive_mae"] += abs(naive - row.minutes)

        for row in group:
            x0.observe(row)

    return {"stats": stats, "minutes": minutes_stats,
            "contenders": contenders, "regimes": regimes,
            "e4": e4}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2022-23,2023-24,2024-25,2025-26")
    args = ap.parse_args(argv)
    seasons = set(args.seasons.split(","))

    print(__doc__)
    print("=" * 100)
    global ABSENCES
    ABSENCES = load_absences()
    rows = load_rows()
    annotate(rows)
    print(f"  absence coverage: {len(ABSENCES):,} player-seasons")
    print(f"  archive rows {len(rows):,}, scoring seasons {sorted(seasons)}")

    result = evaluate(rows, seasons)
    stats = result["stats"]

    for regime in result["regimes"]:
        first = stats[(result["contenders"][0], regime)]
        if not first["n"]:
            continue
        print("\n" + "=" * 100)
        print(f"REGIME: {regime}")
        print("=" * 100)
        print(f"  {'contender':<26}{'n':>9}{'brier':>10}{'log loss':>11}"
              f"{'cal gap':>10}{'XI hit rate':>14}")
        print("  " + "-" * 96)
        for contender in result["contenders"]:
            cell = stats[(contender, regime)]
            n = cell["n"]
            if not n:
                continue
            gap = 0.0
            for sum_p, sum_a, count in cell["bins"].values():
                if count:
                    gap = max(gap, abs(sum_p - sum_a) / count)
            hit = cell["xi_hits"] / cell["xi_slots"] if cell["xi_slots"] else 0.0
            print(f"  {contender:<26}{n:>9,}{cell['brier'] / n:>10.5f}"
                  f"{cell['logloss'] / n:>11.5f}{gap:>10.4f}{hit:>13.1%}")

    e4 = result["e4"]
    if e4["x2"]:
        import statistics
        n = len(e4["x2"])
        diffs = [a - b for a, b in zip(e4["x3"], e4["x2"], strict=True)]
        mean = sum(diffs) / n
        sd = statistics.pstdev(diffs) if n > 1 else 0.0
        stderr = sd / math.sqrt(n) if n else 0.0
        t = mean / stderr if stderr else 0.0
        print()
        print("=" * 100)
        print("E4 — starts/fixtures_available vs starts/38, on the rows where "
              "they differ")
        print("=" * 100)
        print(f"  rows affected                 {n:>9,}")
        print(f"  X2 Brier (starts / 38)        {sum(e4['x2']) / n:>9.5f}")
        print(f"  X3 Brier (starts / available) {sum(e4['x3']) / n:>9.5f}")
        print(f"  paired difference             {mean:>+9.5f}"
              f"   [{mean - 1.96 * stderr:+.5f}, {mean + 1.96 * stderr:+.5f}]")
        print(f"  t                             {t:>+9.2f}   "
              f"{'X3 BETTER' if t < -1.96 else 'X3 worse' if t > 1.96 else 'not distinguishable'}")
        by_season: dict[str, list[float]] = {}
        for season, d in zip(e4["seasons"], diffs, strict=True):
            by_season.setdefault(season, []).append(d)
        print()
        print("  by season (negative favours the corrected denominator)")
        for season in sorted(by_season):
            values = by_season[season]
            m_s = sum(values) / len(values)
            sd_s = statistics.pstdev(values) if len(values) > 1 else 0.0
            se_s = sd_s / math.sqrt(len(values)) if values else 0.0
            print(f"    {season}  n={len(values):>6,}  d={m_s:>+9.5f}  "
                  f"t={m_s / se_s if se_s else 0:>+6.2f}")

    m = result["minutes"]
    if m["n"]:
        print("\n" + "=" * 100)
        print("EXPECTED MINUTES — scored separately, because it is not a probability")
        print("=" * 100)
        print(f"  rows {m['n']:,}")
        print(f"  Gaffer expected minutes   MAE {m['mae'] / m['n']:.3f}")
        print(f"  naive minutes-per-fixture MAE {m['naive_mae'] / m['n']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
