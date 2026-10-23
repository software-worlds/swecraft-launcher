"""The two operational modes, as one reconcile function and a plain loop.

- Cadence: every N seconds, run the cadence evaluation on ``world@latest``.
  Wake-based cadence belongs to the coordinator, which fires the evaluation
  command at a quiet step boundary (``evaluate_every_wakes``); the evaluator
  does not know what a wake is.
- Releases: every release tag not yet named by the cursor is evaluated
  oldest-first on ``world@seed+repo:tag``, using the repository's evaluation
  (``downstream-<repo>`` when the manifest defines it, the cadence
  evaluation otherwise). The cursor advances only once a release's record
  exists, so an infrastructure failure is retried on the next pass instead
  of leaving a hole.

Both modes score against the seed reference, which is measured on demand:
before the first candidate run of an evaluation, its ``world@seed`` record is
produced if none exists. State is one JSON file of cursors; records are the
runner's append-only directory. The first reconcile initializes release
cursors without evaluating history — replaying old releases is an explicit
``run`` invocation, not a surprise.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swecraft_evaluator.environment import (
    LATEST,
    SEED,
    Forge,
    World,
    environment_digest,
    latest_environment,
    release_environment,
    seed_environment,
)
from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.manifest import Manifest
from swecraft_evaluator.runner import RunnerSettings, run_evaluation
from swecraft_evaluator.score import latest_reference, load_records, score_record

Report = Callable[[str], object]

# Newest tags fetched per repository each pass; every unseen tag among them
# is evaluated oldest-first, so intermediate releases are never skipped.
RELEASE_LOOKBACK = 10


@dataclass(frozen=True)
class LoopSettings:
    state_path: Path
    cadence_evaluation: str
    interval_seconds: float = 10800.0


def reconcile(
    manifest: Manifest,
    world: World,
    forge: Forge,
    runner: RunnerSettings,
    loop_settings: LoopSettings,
    report: Report = print,
) -> list[dict[str, Any]]:
    """One pass over both modes; returns the scores it produced."""

    state = _load_state(loop_settings.state_path)
    scores: list[dict[str, Any]] = []

    if _sweep_due(state, loop_settings):
        environment = latest_environment(world, forge)
        scores.append(
            run_scored(
                manifest,
                world,
                loop_settings.cadence_evaluation,
                environment,
                LATEST,
                runner,
                report,
            )
        )
        state["last_sweep"] = datetime.now(UTC).isoformat(timespec="seconds")
        _write_state(loop_settings.state_path, state)

    releases = state.setdefault("releases", {})
    # Bootstrap is a persisted fact, never inferred from cursor emptiness: a
    # first scan that finds zero releases must still count as initialized, or
    # the first future release would be swallowed as history.
    bootstrap = not state.get("releases_initialized", bool(releases))
    for repo in world.repos:
        unseen = _unseen_tags(forge, world.org, repo.name, releases.get(repo.name))
        if not unseen:
            continue
        if bootstrap:
            releases[repo.name] = unseen[0]
            _write_state(loop_settings.state_path, state)
            continue
        evaluation = f"downstream-{repo.name}"
        if evaluation not in manifest.evaluations:
            evaluation = loop_settings.cadence_evaluation
        for tag in reversed(unseen):  # oldest first, exactly once each
            spec = f"world@seed+{repo.name}:{tag}"
            try:
                environment = release_environment(world, forge, repo.name, tag)
                scores.append(
                    run_scored(
                        manifest,
                        world,
                        evaluation,
                        environment,
                        spec,
                        runner,
                        report,
                    )
                )
            except EvaluatorError as error:
                # No record exists, so the cursor stays put and the next
                # pass retries this tag before any newer one.
                report(f"release {spec} failed; will retry: {error}")
                break
            # The cursor names a release only after its record exists. A
            # record whose measurements failed still advances it: the hole
            # is visible in the record, and an explicit `run` re-measures.
            releases[repo.name] = tag
            _write_state(loop_settings.state_path, state)
    if bootstrap:
        state["releases_initialized"] = True
        _write_state(loop_settings.state_path, state)
        report(f"initialized release cursors for {len(releases)} repositories")
    return scores


def _unseen_tags(forge: Forge, org: str, repo: str, cursor: str | None) -> list[str]:
    """Every release tag newer than the cursor, newest first.

    Pages until the cursor is found or history is exhausted, so a cursor
    older than one page can never cause the tags in the gap to be skipped.
    """

    unseen: list[str] = []
    page = 1
    while True:
        tags = forge.releases(org, repo, limit=RELEASE_LOOKBACK, page=page)
        for tag in tags:
            if cursor is not None and tag == cursor:
                return unseen
            unseen.append(tag)
        if len(tags) < RELEASE_LOOKBACK:
            # End of history: the cursor tag is gone (or was never set), so
            # everything fetched is genuinely unseen.
            return unseen
        page += 1


def loop(
    manifest: Manifest,
    world: World,
    forge: Forge,
    runner: RunnerSettings,
    loop_settings: LoopSettings,
    poll_seconds: float = 300.0,
    report: Report = print,
) -> None:
    while True:
        try:
            reconcile(manifest, world, forge, runner, loop_settings, report)
        except EvaluatorError as error:
            report(f"reconcile failed: {error}")
        time.sleep(poll_seconds)


def run_scored(
    manifest: Manifest,
    world: World,
    evaluation: str,
    environment: dict[str, str],
    spec: str,
    runner: RunnerSettings,
    report: Report,
) -> dict[str, Any]:
    seed = seed_environment(world)
    seed_digest = environment_digest(seed)
    records = load_records(runner.records_dir)
    reference = latest_reference(
        manifest, records, evaluation, seed_digest, require_worker=True
    )
    if reference is None:
        report(f"measuring {SEED} reference for {evaluation}")
        reference = run_evaluation(
            manifest, world, evaluation, seed, SEED, runner
        ).payload
    if environment_digest(environment) == seed_digest:
        record = reference
    else:
        report(f"running {evaluation} on {spec}")
        record = run_evaluation(
            manifest, world, evaluation, environment, spec, runner
        ).payload
    score = score_record(manifest, record, reference)
    report(json.dumps(score["aggregates"], sort_keys=True))
    return score


def _sweep_due(state: dict[str, Any], settings: LoopSettings) -> bool:
    last = state.get("last_sweep")
    if last is None:
        return True
    elapsed = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
    return elapsed >= settings.interval_seconds


def _load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    scratch.replace(path)
