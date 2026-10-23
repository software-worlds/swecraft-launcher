"""Scores: candidate records against the seed reference, task by task.

A task's sample is one number per record: the walltime mean over repeats,
the codspeed-walltime robust total, or the codspeed-cpu-sim instruction
total. The task speedup is ``seed / candidate`` (>1 = candidate is faster).
An evaluation's score aggregates its task speedups with the aggregates the
manifest declares. Tasks whose gate failed, errored, or lack a seed reference
score ``null`` and stay visible.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.manifest import Manifest

METRIC_SAMPLE_FIELDS = {
    "codspeed-walltime": "seconds",
    "codspeed-cpu-sim": "instructions",
}


def load_records(records_dir: Path) -> list[dict[str, Any]]:
    """Every readable run record, oldest first (ids sort chronologically)."""

    records = []
    for path in sorted(records_dir.glob("*/record.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and "id" in payload:
            records.append(payload)
    return records


def task_sample(record: dict[str, Any], ref: str, metric: str) -> float | None:
    """One number for ``evaluator@task`` from one record, or None."""

    measured = _task_metric(record, ref, metric)
    if measured is None:
        return None
    if metric == "walltime":
        samples = measured.get("samples")
        if not samples:
            return None
        return statistics.fmean(samples)
    field = METRIC_SAMPLE_FIELDS.get(metric)
    if field is None:
        return None
    sample = measured.get(field)
    return float(sample) if isinstance(sample, int | float) else None


def task_cv_percent(record: dict[str, Any], ref: str, metric: str) -> float | None:
    """Coefficient of variation of the task's samples, when there are any.

    The honesty signal beside every speedup: a delta smaller than the CVs
    that produced it is noise, whatever the ratio says. Codspeed reduces to
    one robust number in-container, so it carries no CV here.
    """

    measured = _task_metric(record, ref, metric)
    if measured is None or metric != "walltime":
        return None
    samples = measured.get("samples")
    if not samples or len(samples) < 2:
        return None
    mean = statistics.fmean(samples)
    return statistics.stdev(samples) / mean * 100 if mean > 0 else None


def _task_metric(
    record: dict[str, Any], ref: str, metric: str
) -> dict[str, Any] | None:
    evaluator_name, _, task_name = ref.partition("@")
    evaluator = record.get("evaluators", {}).get(evaluator_name)
    if not isinstance(evaluator, dict) or evaluator.get("gate") is not True:
        return None
    task = evaluator.get("tasks", {}).get(task_name)
    if not isinstance(task, dict) or task.get("error"):
        return None
    measured = task.get(metric)
    return measured if isinstance(measured, dict) else None


def score_record(
    manifest: Manifest,
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    """Score one candidate record against one reference record."""

    name = candidate.get("evaluation")
    evaluation = manifest.evaluations.get(name or "")
    if evaluation is None:
        raise EvaluatorError(f"record {candidate.get('id')} has no evaluation")
    tasks = {}
    speedups = []
    for ref in evaluation.tasks:
        seed = task_sample(reference, ref, evaluation.metric)
        current = task_sample(candidate, ref, evaluation.metric)
        worker_guard = _worker_class_guard(reference, candidate, ref)
        speedup = (
            seed / current
            if seed is not None
            and current is not None
            and current > 0
            and _worker_classes_match(worker_guard)
            else None
        )
        task = {
            "seed": seed,
            "current": current,
            "speedup": speedup,
            "seed_cv_percent": task_cv_percent(reference, ref, evaluation.metric),
            "current_cv_percent": task_cv_percent(candidate, ref, evaluation.metric),
        }
        if worker_guard is not None:
            task.update(worker_guard)
        tasks[ref] = task
        if speedup is not None:
            speedups.append(speedup)
    aggregates = {
        aggregate: _aggregate(aggregate, speedups) if speedups else None
        for aggregate in evaluation.aggregates
    }
    return {
        "evaluation": name,
        "metric": evaluation.metric,
        "candidate": candidate.get("id"),
        "reference": reference.get("id"),
        "environment_spec": candidate.get("environment_spec"),
        "tasks": tasks,
        "scored_tasks": len(speedups),
        "aggregates": aggregates,
    }


def latest_reference(
    manifest: Manifest,
    records: list[dict[str, Any]],
    evaluation: str,
    seed_digest: str,
    *,
    candidate: dict[str, Any] | None = None,
    require_worker: bool = False,
) -> dict[str, Any] | None:
    """The newest seed record that carries the current metric and provenance.

    Legacy records without worker provenance can still score against legacy
    candidates, but a new candidate with worker provenance needs a seed
    reference that can participate in the worker-class guard.
    """

    for record in reversed(records):
        if (
            record.get("evaluation") == evaluation
            and record.get("environment_digest") == seed_digest
            and _record_has_metric(manifest, record, evaluation)
            and _reference_has_required_worker_classes(
                manifest, record, evaluation, candidate, require_worker
            )
        ):
            return record
    return None


def _record_has_metric(
    manifest: Manifest, record: dict[str, Any], evaluation_name: str
) -> bool:
    evaluation = manifest.evaluations.get(evaluation_name)
    if evaluation is None:
        return False
    return all(
        _task_metric(record, ref, evaluation.metric) is not None
        for ref in evaluation.tasks
    )


def _reference_has_required_worker_classes(
    manifest: Manifest,
    reference: dict[str, Any],
    evaluation_name: str,
    candidate: dict[str, Any] | None,
    require_worker: bool,
) -> bool:
    evaluation = manifest.evaluations.get(evaluation_name)
    if evaluation is None:
        return False
    return all(
        _task_worker_class(reference, ref) is not None
        for ref in evaluation.tasks
        if require_worker
        or (candidate is not None and _task_worker_class(candidate, ref) is not None)
    )


def _worker_class_guard(
    reference: dict[str, Any], candidate: dict[str, Any], ref: str
) -> dict[str, Any] | None:
    seed_class = _task_worker_class(reference, ref)
    current_class = _task_worker_class(candidate, ref)
    if seed_class is None and current_class is None:
        return None
    matches = seed_class is not None and seed_class == current_class
    return {
        "seed_worker_class": seed_class,
        "current_worker_class": current_class,
        "worker_class_match": matches,
        "worker_class_error": None
        if matches
        else "seed and candidate were measured on different worker classes",
    }


def _worker_classes_match(guard: dict[str, Any] | None) -> bool:
    return guard is None or guard["worker_class_match"] is True


def _task_worker_class(record: dict[str, Any], ref: str) -> str | None:
    evaluator_name, _, _ = ref.partition("@")
    evaluator = record.get("evaluators", {}).get(evaluator_name)
    if not isinstance(evaluator, dict):
        return None
    worker = evaluator.get("worker")
    if not isinstance(worker, dict):
        return None
    value = worker.get("class")
    return value if isinstance(value, str) and value else None


def _aggregate(kind: str, speedups: list[float]) -> float:
    if kind == "geomean":
        return math.exp(statistics.fmean(math.log(value) for value in speedups))
    return statistics.fmean(speedups)
