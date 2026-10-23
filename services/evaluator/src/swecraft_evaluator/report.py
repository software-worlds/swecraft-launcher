"""Derived views over the records directory: series, releases, noise floors.

Everything the read API and the console show about measurements is derived
here, from the same two inputs the CLI's ``score`` command reads — the
manifest and the append-only records directory — so a number in a panel is
the number a script computes. Pure functions over loaded records; the API
caches on the set of record ids, because a record directory is complete once
``record.json`` exists and never changes afterwards.

Vocabulary:

- A *reading* is one record, classified by its ``environment_spec``:
  ``seed`` (the reference), ``latest`` (a cadence sweep of the world's
  heads), ``release`` (``world@seed+repo:tag`` — one repository at a tag on
  top of the seed), or ``other`` (an explicit ``run`` label such as the
  ``world@worked<N>`` cutoffs a trajectory script names).
- A *task* is one ``evaluator@task`` reference. Its datum is the
  evaluation metric's sample — instruction count for ``codspeed-cpu-sim``.
- A task's *speedup* is ``seed / current`` against the evaluation's
  reference record, exactly as :func:`swecraft_evaluator.score.score_record`
  computes it.
- A task's *noise floor* is the largest ``|ln(a / b)|`` between two complete
  readings of the same environment. Replicates cost nothing to find: the
  environment digest is part of every record id, so the seed pair and every
  repeated sweep of an unchanged world are replicate pairs by construction.
- A task *moves* in a reading when ``|ln speedup|`` clears
  ``max(2 * floor, MIN_EFFECT)``. Without a replicate pair the floor is
  unknown and ``ASSUMED_FLOOR`` stands in, flagged as assumed.
- A release's *marginal* effect on a task is its speedup over the previous
  measured release of the same repository (over the seed for the first) —
  the staircase that says which tag owns which step.
- A release's *control leak* is the geomean over the tasks its reading
  measured that are **not** in its ``downstream-<repo>`` battery: tasks the
  release cannot touch, so anything but ~1.00 is instrument noise.
- The *additivity residual* compares the newest sweep against the product of
  every repository's newest single-repo release vector on the tasks they
  share: a gap is interaction between repositories, or an unmeasured one.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from swecraft_evaluator.environment import LATEST, SEED
from swecraft_evaluator.manifest import Manifest
from swecraft_evaluator.score import task_sample

# A change smaller than this is never reported as movement, however clean
# the task: instruction counts on pure-compute bodies reproduce bit for bit,
# and 0.01% of a bit-identical task is real but not interesting.
MIN_EFFECT = math.log(1.005)
# Stands in for a task that has no replicate pair yet. I/O-touching bodies
# keep a ~0.5-1% floor under the simulation instrument; pure compute is 0.
ASSUMED_FLOOR = math.log(1.005)

_RELEASE_PREFIX = "world@seed+"
# Script-labelled cutoffs: the world as of wake N, measured whenever the
# script ran (``world@worked250`` from sim_trajectory.py, ``world@backfill-75``
# from a campaign's own backfill). The number is the reading's position.
_STEP = re.compile(r"^world@(?:worked|wakes?|backfill|cutoff|step)[-_]?(\d+)$")
DOWNSTREAM_PREFIX = "downstream-"

ReadingKind = str  # "seed" | "latest" | "release" | "other"


def classify_spec(spec: str) -> tuple[ReadingKind, str | None, str | None, int | None]:
    """``(kind, repo, tag, step)`` for an ``environment_spec`` label."""

    if spec == SEED:
        return "seed", None, None, None
    if spec == LATEST:
        return "latest", None, None, None
    if spec.startswith(_RELEASE_PREFIX):
        repo, _, tag = spec.removeprefix(_RELEASE_PREFIX).partition(":")
        if repo and tag:
            return "release", repo, tag, None
    matched = _STEP.match(spec)
    if matched:
        return "other", None, None, int(matched.group(1))
    return "other", None, None, None


@dataclass(frozen=True)
class Reading:
    """One record, classified and reduced to its per-task samples."""

    id: str
    evaluation: str
    metric: str
    spec: str
    kind: ReadingKind
    repo: str | None
    tag: str | None
    step: int | None
    digest: str
    started: str | None
    finished: str | None
    duration_seconds: float | None
    samples: dict[str, float | None]
    errors: tuple[str, ...]
    # Tasks whose evaluator ran and failed its gate: the consumer's own test
    # suite rejects this world. Distinct from a task that simply was not
    # measured (lost container, timeout) — a red gate is a *finding*, and
    # under adoption semantics it scores 1.0: the consumer declines the
    # upgrade, so no value is delivered and no harm ships.
    gated_tasks: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return all(value is not None for value in self.samples.values())

    @property
    def measured(self) -> int:
        return sum(1 for value in self.samples.values() if value is not None)

    @property
    def missing_tasks(self) -> tuple[str, ...]:
        """Unmeasured for instrument reasons — the incomparable kind."""

        gated = set(self.gated_tasks)
        return tuple(
            ref
            for ref, value in self.samples.items()
            if value is None and ref not in gated
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "evaluation": self.evaluation,
            "metric": self.metric,
            "spec": self.spec,
            "kind": self.kind,
            "repo": self.repo,
            "tag": self.tag,
            "step": self.step,
            "digest": self.digest,
            "started": self.started,
            "finished": self.finished,
            "duration_seconds": self.duration_seconds,
            "complete": self.complete,
            "measured": self.measured,
            "tasks": len(self.samples),
            "gated_tasks": list(self.gated_tasks),
            "missing_tasks": list(self.missing_tasks),
            "errors": list(self.errors),
        }


def reading_from_record(manifest: Manifest, record: dict[str, Any]) -> Reading | None:
    """Reduce one record to a reading, or None when its evaluation is unknown."""

    name = record.get("evaluation")
    evaluation = manifest.evaluations.get(name or "")
    if evaluation is None or not isinstance(record.get("id"), str):
        return None
    spec = str(record.get("environment_spec") or "")
    kind, repo, tag, step = classify_spec(spec)
    samples: dict[str, float | None] = {}
    errors: list[str] = []
    gated: list[str] = []
    evaluators = record.get("evaluators")
    evaluators = evaluators if isinstance(evaluators, dict) else {}
    seen_evaluators: set[str] = set()
    for ref in evaluation.tasks:
        samples[ref] = task_sample(record, ref, evaluation.metric)
        if samples[ref] is not None:
            continue
        evaluator_name, _, task_name = ref.partition("@")
        outcome = evaluators.get(evaluator_name)
        if not isinstance(outcome, dict):
            errors.append(f"{ref}: not measured")
            continue
        # `gate: false` is the evaluator saying it ran and the consumer's
        # tests rejected the world — a finding about the world. `gate: null`
        # means it never got that far — a finding about the instrument.
        if outcome.get("gate") is False:
            gated.append(ref)
        if evaluator_name not in seen_evaluators:
            seen_evaluators.add(evaluator_name)
            if outcome.get("gate") is not True:
                errors.append(
                    f"{evaluator_name}: gate failed"
                    + (f" ({outcome['error']})" if outcome.get("error") else "")
                )
                continue
            if outcome.get("error"):
                errors.append(f"{evaluator_name}: {outcome['error']}")
                continue
        elif outcome.get("gate") is not True or outcome.get("error"):
            continue
        task = (outcome.get("tasks") or {}).get(task_name)
        if isinstance(task, dict) and task.get("error"):
            errors.append(f"{ref}: {task['error']}")
        else:
            errors.append(f"{ref}: no {evaluation.metric} sample")
    started = record.get("started") if isinstance(record.get("started"), str) else None
    finished = (
        record.get("finished") if isinstance(record.get("finished"), str) else None
    )
    return Reading(
        id=record["id"],
        evaluation=name,
        metric=evaluation.metric,
        spec=spec,
        kind=kind,
        repo=repo,
        tag=tag,
        step=step,
        digest=str(record.get("environment_digest") or ""),
        started=started,
        finished=finished,
        duration_seconds=_duration(started, finished),
        samples=samples,
        errors=tuple(errors),
        gated_tasks=tuple(gated),
    )


def _duration(started: str | None, finished: str | None) -> float | None:
    if not started or not finished:
        return None
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except ValueError:
        return None
    return max(0.0, delta.total_seconds())


def _order_key(reading: Reading) -> tuple[str, str]:
    return (reading.started or "", reading.id)


def geomean(values: list[float]) -> float | None:
    values = [value for value in values if value is not None and value > 0]
    if not values:
        return None
    return math.exp(statistics.fmean(math.log(value) for value in values))


def median(values: list[float]) -> float | None:
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def ambient_subjects(manifest: Manifest, evaluation: str) -> list[str]:
    """Subjects whose ``downstream-*`` battery covers the whole evaluation.

    A test runner or packaging tool is a subject of every task, so its battery
    is the full battery and says nothing about which tasks a release can
    reach. Exposure is judged from the other batteries.
    """

    tasks = set(manifest.evaluations[evaluation].tasks)
    return sorted(
        name.removeprefix(DOWNSTREAM_PREFIX)
        for name, entry in manifest.evaluations.items()
        if name.startswith(DOWNSTREAM_PREFIX) and tasks <= set(entry.tasks)
    )


def exposure_source(manifest: Manifest, evaluation: str) -> str | None:
    """``manifest`` when the evaluation lists its exposed tasks, ``derived``
    when they come from the downstream batteries, None when neither."""

    if manifest.evaluations[evaluation].exposed is not None:
        return "manifest"
    return "derived" if exposed_tasks(manifest, evaluation) is not None else None


def exposed_tasks(manifest: Manifest, evaluation: str) -> list[str] | None:
    """The tasks a society release can reach, in the battery's order.

    The operator's explicit ``exposed`` list wins: it can encode a consumer
    census the manifest structure cannot. Failing that, the release-scoped
    batteries are read as the operator's statement of reach: their union —
    leaving out the ambient subjects above — is the exposed set; the rest
    cannot move and only drag an aggregate toward 1.00. None when the
    manifest says nothing either way.
    """

    listed = manifest.evaluations[evaluation].exposed
    if listed is not None:
        return [ref for ref in manifest.evaluations[evaluation].tasks if ref in listed]
    ambient = set(ambient_subjects(manifest, evaluation))
    downstream = [
        set(entry.tasks)
        for name, entry in manifest.evaluations.items()
        if name.startswith(DOWNSTREAM_PREFIX)
        and name.removeprefix(DOWNSTREAM_PREFIX) not in ambient
    ]
    if not downstream:
        return None
    exposed = set().union(*downstream)
    return [ref for ref in manifest.evaluations[evaluation].tasks if ref in exposed]


def noise_floors(readings: list[Reading]) -> dict[str, dict[str, Any]]:
    """Per task: the largest log-ratio between replicate readings, and how many."""

    groups: dict[tuple[str, str], list[Reading]] = {}
    for reading in readings:
        if reading.digest:
            groups.setdefault((reading.evaluation, reading.digest), []).append(reading)
    worst: dict[str, float] = {}
    pairs: dict[str, int] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                for ref, a in first.samples.items():
                    b = second.samples.get(ref)
                    if a is None or b is None or a <= 0 or b <= 0:
                        continue
                    spread = abs(math.log(a / b))
                    worst[ref] = max(worst.get(ref, 0.0), spread)
                    pairs[ref] = pairs.get(ref, 0) + 1
    floors: dict[str, dict[str, Any]] = {}
    for reading in readings:
        for ref in reading.samples:
            if ref in floors:
                continue
            known = ref in worst
            floors[ref] = {
                "floor": worst[ref] if known else None,
                "pairs": pairs.get(ref, 0),
                "assumed": not known,
                "threshold": threshold(worst.get(ref)),
            }
    return floors


def threshold(floor: float | None) -> float:
    """The log-ratio a task must clear to count as having moved."""

    return max(2 * (ASSUMED_FLOOR if floor is None else floor), MIN_EFFECT)


def build_report(manifest: Manifest, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything derivable from the records: readings, series, releases, floors.

    JSON-shaped throughout, so the API can serve slices of it unchanged and
    a script can dump the whole thing.
    """

    readings: list[Reading] = []
    unknown: list[str] = []
    for record in records:
        reading = reading_from_record(manifest, record)
        if reading is None:
            unknown.append(str(record.get("id") or "?"))
        else:
            readings.append(reading)
    readings.sort(key=_order_key)
    floors = noise_floors(readings)

    references = _references(readings)
    speedups: dict[str, dict[str, float | None]] = {}
    for reading in readings:
        reference = references.get(reading.evaluation)
        speedups[reading.id] = _speedups(reading, reference)

    evaluations = {
        name: _evaluation_summary(manifest, name, readings, references)
        for name in manifest.evaluations
    }
    series = {
        name: _series(name, readings, speedups, floors, evaluations[name])
        for name in manifest.evaluations
    }
    releases = _releases(manifest, readings, speedups, floors)
    owners = _owners(releases, floors)
    additivity = {
        name: _additivity(series[name], releases)
        for name in manifest.evaluations
        if series[name]
    }
    return {
        "evaluations": evaluations,
        "readings": [reading.as_json() for reading in readings],
        "tasks": floors,
        "series": series,
        "releases": releases,
        "owners": owners,
        "additivity": {name: value for name, value in additivity.items() if value},
        "health": _health(readings, unknown),
    }


def _references(readings: list[Reading]) -> dict[str, Reading]:
    """The newest complete seed reading per evaluation; a partial one failing that."""

    best: dict[str, Reading] = {}
    for reading in readings:  # oldest first, so later ones win
        if reading.kind != "seed":
            continue
        current = best.get(reading.evaluation)
        if current is None or reading.complete or not current.complete:
            best[reading.evaluation] = reading
    return best


def _speedups(reading: Reading, reference: Reading | None) -> dict[str, float | None]:
    if reference is None:
        return {ref: None for ref in reading.samples}
    result: dict[str, float | None] = {}
    for ref, current in reading.samples.items():
        seed = reference.samples.get(ref)
        result[ref] = (
            seed / current
            if seed is not None and current is not None and current > 0
            else None
        )
    return result


def _evaluation_summary(
    manifest: Manifest,
    name: str,
    readings: list[Reading],
    references: dict[str, Reading],
) -> dict[str, Any]:
    entry = manifest.evaluations[name]
    mine = [reading for reading in readings if reading.evaluation == name]
    reference = references.get(name)
    return {
        "name": name,
        "metric": entry.metric,
        "aggregates": list(entry.aggregates),
        "tasks": list(entry.tasks),
        "exposed_tasks": exposed_tasks(manifest, name),
        "exposure_source": exposure_source(manifest, name),
        "ambient_subjects": ambient_subjects(manifest, name),
        "downstream_of": (
            name.removeprefix(DOWNSTREAM_PREFIX)
            if name.startswith(DOWNSTREAM_PREFIX)
            else None
        ),
        "reference": reference.id if reference else None,
        "reference_complete": bool(reference and reference.complete),
        "readings": {
            kind: sum(1 for reading in mine if reading.kind == kind)
            for kind in ("seed", "latest", "release", "other")
        },
    }


def _movement(
    speedup: float | None, floor: dict[str, Any] | None
) -> tuple[bool, float]:
    """Whether a speedup clears its task's threshold, and the log effect."""

    if speedup is None or speedup <= 0:
        return False, 0.0
    effect = math.log(speedup)
    limit = floor["threshold"] if floor else threshold(None)
    return abs(effect) > limit, effect


def _series(
    name: str,
    readings: list[Reading],
    speedups: dict[str, dict[str, float | None]],
    floors: dict[str, dict[str, Any]],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    exposed = summary["exposed_tasks"]
    points = []
    for reading in readings:
        if reading.evaluation != name or reading.kind not in ("latest", "other"):
            continue
        tasks = speedups[reading.id]
        movers_up: list[dict[str, Any]] = []
        movers_down: list[dict[str, Any]] = []
        for ref, speedup in tasks.items():
            moved, effect = _movement(speedup, floors.get(ref))
            if not moved:
                continue
            (movers_up if effect > 0 else movers_down).append(
                {"ref": ref, "speedup": speedup}
            )
        movers_up.sort(key=lambda item: -item["speedup"])
        movers_down.sort(key=lambda item: item["speedup"])
        values = [value for value in tasks.values() if value is not None]
        # Adoption semantics: a gate-red task scores exactly 1.0 — the
        # consumer declines the upgrade, so no speedup and no harm. That
        # keeps the aggregate's composition fixed across readings, so a
        # gated reading stays comparable with the rest of the curve. A task
        # missing for instrument reasons has no defensible score, so any
        # missing task voids the adoption aggregate instead of skewing it.
        gated = set(reading.gated_tasks)

        def adoption(refs: list[str], tasks=tasks, gated=gated) -> float | None:
            scored: list[float] = []
            for ref in refs:
                value = tasks.get(ref)
                if value is not None:
                    scored.append(value)
                elif ref in gated:
                    scored.append(1.0)
                else:
                    return None
            return geomean(scored)

        points.append(
            {
                "id": reading.id,
                "spec": reading.spec,
                "kind": reading.kind,
                "step": reading.step,
                "started": reading.started,
                "finished": reading.finished,
                "duration_seconds": reading.duration_seconds,
                "digest": reading.digest,
                "complete": reading.complete,
                "measured": reading.measured,
                "tasks": len(tasks),
                "gated_tasks": list(reading.gated_tasks),
                "missing_tasks": list(reading.missing_tasks),
                "aggregates": {
                    "geomean": geomean(values),
                    "median": median(values),
                    "geomean_exposed": (
                        geomean([tasks[ref] for ref in exposed if ref in tasks])
                        if exposed is not None
                        else None
                    ),
                    "geomean_adoption": adoption(list(tasks)),
                    "geomean_exposed_adoption": (
                        adoption([ref for ref in exposed if ref in tasks])
                        if exposed is not None
                        else None
                    ),
                },
                "movers_up": movers_up,
                "movers_down": movers_down,
                "speedups": tasks,
            }
        )
    return points


def _releases(
    manifest: Manifest,
    readings: list[Reading],
    speedups: dict[str, dict[str, float | None]],
    floors: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    chains: dict[str, list[Reading]] = {}
    for reading in readings:
        if reading.kind == "release" and reading.repo:
            chains.setdefault(reading.repo, []).append(reading)
    result: dict[str, list[dict[str, Any]]] = {}
    for repo, chain in chains.items():
        downstream = manifest.evaluations.get(f"{DOWNSTREAM_PREFIX}{repo}")
        downstream_tasks = set(downstream.tasks) if downstream else None
        previous: dict[str, float] = {}  # newest measured speedup per task so far
        entries = []
        for reading in chain:
            tasks = speedups[reading.id]
            marginal: dict[str, float | None] = {}
            moved: list[dict[str, Any]] = []
            # A repository with no battery of its own has no task it is known
            # to reach: every task it was measured on is a control, and the
            # reading's whole value is the leak it shows (ideally none).
            in_battery = [
                ref for ref in tasks if downstream_tasks and ref in downstream_tasks
            ]
            controls = [ref for ref in tasks if ref not in in_battery]
            for ref, speedup in tasks.items():
                if speedup is None:
                    marginal[ref] = None
                    continue
                base = previous.get(ref, 1.0)
                marginal[ref] = speedup / base if base > 0 else None
                is_move, _ = _movement(marginal[ref], floors.get(ref))
                if is_move:
                    # A control that moved is a leak, and is flagged as one so
                    # it is never read as the release's downstream effect.
                    moved.append(
                        {
                            "ref": ref,
                            "speedup": speedup,
                            "marginal": marginal[ref],
                            "control": ref not in in_battery,
                        }
                    )
            moved.sort(key=lambda item: -abs(math.log(item["marginal"])))
            for ref, speedup in tasks.items():
                if speedup is not None:
                    previous[ref] = speedup
            entries.append(
                {
                    "repo": repo,
                    "tag": reading.tag,
                    "id": reading.id,
                    "evaluation": reading.evaluation,
                    "started": reading.started,
                    "finished": reading.finished,
                    "duration_seconds": reading.duration_seconds,
                    "digest": reading.digest,
                    "complete": reading.complete,
                    "measured": reading.measured,
                    "tasks": len(tasks),
                    "battery_tasks": len(in_battery),
                    "battery": in_battery,
                    "aggregates": {
                        "geomean": geomean([tasks[ref] for ref in in_battery]),
                        "geomean_all": geomean(list(tasks.values())),
                        "marginal_geomean": geomean(
                            [marginal[ref] for ref in in_battery]
                        ),
                        "control_leak": geomean([tasks[ref] for ref in controls]),
                        "controls": len(controls),
                    },
                    "moved": moved,
                    "speedups": tasks,
                    "marginal": marginal,
                    "errors": list(reading.errors),
                }
            )
        result[repo] = entries
    return result


def _owners(
    releases: dict[str, list[dict[str, Any]]], floors: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any] | None]:
    """Per task, the release whose marginal step on it is largest (and real)."""

    owners: dict[str, dict[str, Any] | None] = {ref: None for ref in floors}
    for repo, chain in releases.items():
        for entry in chain:
            for item in entry["moved"]:
                if item["control"]:
                    continue
                effect = abs(math.log(item["marginal"]))
                current = owners.get(item["ref"])
                if current is None or effect > current["effect"]:
                    owners[item["ref"]] = {
                        "repo": repo,
                        "tag": entry["tag"],
                        "id": entry["id"],
                        "marginal": item["marginal"],
                        "speedup": item["speedup"],
                        "effect": effect,
                    }
    return owners


def _additivity(
    series: list[dict[str, Any]],
    releases: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Newest sweep against the product of each repo's newest release vector."""

    # The newest complete sweep of the world's heads; a script's cutoff
    # reading only stands in when no sweep exists at all.
    sweeps = [point for point in series if point["complete"]]
    heads = [point for point in sweeps if point["kind"] == "latest"] or sweeps
    if not heads:
        return None
    latest = heads[-1]
    contributions: dict[str, dict[str, float]] = {}
    for repo, chain in releases.items():
        if not chain:
            continue
        newest = chain[-1]
        for ref, speedup in newest["speedups"].items():
            if speedup is not None and ref in latest["speedups"]:
                contributions.setdefault(ref, {})[repo] = speedup
    tasks: dict[str, dict[str, Any]] = {}
    observed: list[float] = []
    predicted: list[float] = []
    for ref, parts in contributions.items():
        actual = latest["speedups"].get(ref)
        if actual is None:
            continue
        expected = math.exp(sum(math.log(value) for value in parts.values()))
        tasks[ref] = {
            "observed": actual,
            "predicted": expected,
            "residual": actual / expected,
            "contributions": parts,
        }
        observed.append(actual)
        predicted.append(expected)
    if not tasks:
        return None
    observed_g = geomean(observed)
    predicted_g = geomean(predicted)
    return {
        "reading": latest["id"],
        "tasks": tasks,
        "observed_geomean": observed_g,
        "predicted_geomean": predicted_g,
        "residual": (observed_g / predicted_g if observed_g and predicted_g else None),
    }


def _health(readings: list[Reading], unknown: list[str]) -> dict[str, Any]:
    errored = [
        {"reading": reading.id, "error": error}
        for reading in readings
        for error in reading.errors
    ]
    return {
        "readings": len(readings),
        "complete": sum(1 for reading in readings if reading.complete),
        "partial": sum(1 for reading in readings if not reading.complete),
        "by_kind": {
            kind: sum(1 for reading in readings if reading.kind == kind)
            for kind in ("seed", "latest", "release", "other")
        },
        "unknown_evaluation_records": len(unknown),
        "unknown_evaluations": sorted(
            {_evaluation_of(record_id) for record_id in unknown}
        ),
        "errors": errored[-50:],
        "error_count": len(errored),
    }


def _evaluation_of(record_id: str) -> str:
    """The evaluation name a record id carries: ``<stamp>-<evaluation>-<digest>``."""

    parts = record_id.split("-")
    return "-".join(parts[1:-1]) if len(parts) >= 3 else record_id
