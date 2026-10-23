"""Host-side runner: materialize one environment and run evaluator tasks.

One container per evaluator, offline, single build. The workdir mounted at
``/eval`` carries the harness, the plan, and the subject sources fetched from
the Forge at the environment's exact commits. The outcome of every evaluator
lands in one append-only run record.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from swecraft_evaluator.environment import Environment, World, environment_digest
from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.executor import (
    DockerExecutor,
    EvaluatorExecutor,
    worker_provenance,
)
from swecraft_evaluator.manifest import Command, Manifest

HARNESS = Path(__file__).with_name("harness.py")
CONTAINER_MARGIN_SECONDS = 120.0


@dataclass(frozen=True)
class RunnerSettings:
    forge_url: str
    forge_token: str
    records_dir: Path
    docker_command: tuple[str, ...] = ("docker",)
    executor: EvaluatorExecutor | None = None
    transport: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class RunRecord:
    path: Path
    payload: dict[str, Any] = field(repr=False)


def run_evaluation(
    manifest: Manifest,
    world: World,
    evaluation_name: str,
    environment: Environment,
    environment_spec: str,
    settings: RunnerSettings,
) -> RunRecord:
    """Run one named evaluation on one environment; write one record."""

    evaluation = manifest.evaluations.get(evaluation_name)
    if evaluation is None:
        raise EvaluatorError(f"unknown evaluation {evaluation_name}")
    by_evaluator: dict[str, list[str]] = {}
    for ref in evaluation.tasks:
        evaluator_name, task_name, _ = manifest.task(ref)
        by_evaluator.setdefault(evaluator_name, []).append(task_name)
    for evaluator_name in by_evaluator:
        for subject in manifest.evaluators[evaluator_name].subjects:
            if subject not in environment:
                raise EvaluatorError(
                    f"environment lacks a commit for subject {subject}"
                )

    started = datetime.now(UTC)
    digest = environment_digest(environment)
    record_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{evaluation_name}-{digest}"
    record_dir = settings.records_dir / record_id
    record_dir.mkdir(parents=True, exist_ok=False)

    payload: dict[str, Any] = {
        "id": record_id,
        "evaluation": evaluation_name,
        "environment_spec": environment_spec,
        "environment": dict(sorted(environment.items())),
        "environment_digest": digest,
        "started": started.isoformat(timespec="seconds"),
        "evaluators": {},
    }
    executor = _container_executor(settings)
    for evaluator_name, task_names in by_evaluator.items():
        try:
            outcome = _run_evaluator(
                manifest,
                world,
                evaluator_name,
                task_names,
                environment,
                record_dir / evaluator_name,
                settings,
                executor,
            )
        except EvaluatorError as error:
            # A lost container fails one evaluator, never the whole record.
            outcome = {"gate": None, "tasks": {}, "error": str(error)}
        payload["evaluators"][evaluator_name] = outcome
    payload["finished"] = datetime.now(UTC).isoformat(timespec="seconds")

    # record.json appears atomically and last: its presence is the
    # completeness proof checkpoint capture relies on.
    path = record_dir / "record.json"
    scratch = path.with_suffix(".tmp")
    scratch.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    scratch.replace(path)
    return RunRecord(path=path, payload=payload)


def _run_evaluator(
    manifest: Manifest,
    world: World,
    evaluator_name: str,
    task_names: list[str],
    environment: Environment,
    workdir: Path,
    settings: RunnerSettings,
    executor: EvaluatorExecutor | None = None,
) -> dict[str, Any]:
    evaluator = manifest.evaluators[evaluator_name]
    container = executor or _container_executor(settings)
    _prepare_evaluator_cell(
        manifest,
        world,
        evaluator_name,
        task_names,
        environment,
        workdir,
        settings,
    )
    worker = worker_provenance(container, evaluator.image)
    try:
        plan = _evaluator_plan(manifest, evaluator_name, task_names)
        container.run(workdir, evaluator.image, _budget(plan))
        outcome = _read_outcome(workdir)
    except (OSError, json.JSONDecodeError) as error:
        outcome = {"gate": None, "tasks": {}, "error": f"no outcome: {error}"}
    except EvaluatorError as error:
        outcome = {"gate": None, "tasks": {}, "error": str(error)}
    finally:
        shutil.rmtree(workdir / "sources", ignore_errors=True)
    outcome["worker"] = worker
    return outcome


def _evaluation_task_map(
    manifest: Manifest, evaluation_name: str, environment: Environment
) -> dict[str, list[str]]:
    evaluation = manifest.evaluations.get(evaluation_name)
    if evaluation is None:
        raise EvaluatorError(f"unknown evaluation {evaluation_name}")
    by_evaluator: dict[str, list[str]] = {}
    for ref in evaluation.tasks:
        evaluator_name, task_name, _ = manifest.task(ref)
        by_evaluator.setdefault(evaluator_name, []).append(task_name)
    for evaluator_name in by_evaluator:
        for subject in manifest.evaluators[evaluator_name].subjects:
            if subject not in environment:
                raise EvaluatorError(
                    f"environment lacks a commit for subject {subject}"
                )
    return by_evaluator


def _prepare_evaluator_cell(
    manifest: Manifest,
    world: World,
    evaluator_name: str,
    task_names: list[str],
    environment: Environment,
    workdir: Path,
    settings: RunnerSettings,
) -> dict[str, Any]:
    evaluator = manifest.evaluators[evaluator_name]
    workdir.mkdir(parents=True)
    shutil.copy(HARNESS, workdir / "harness.py")
    (workdir / "out").mkdir()

    for subject in evaluator.subjects:
        _fetch_source(
            settings,
            world.org,
            subject,
            environment[subject],
            workdir / "sources" / subject,
        )

    plan = _evaluator_plan(manifest, evaluator_name, task_names)
    (workdir / "plan.json").write_text(
        json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8"
    )
    return plan


def _evaluator_plan(
    manifest: Manifest, evaluator_name: str, task_names: list[str]
) -> dict[str, Any]:
    evaluator = manifest.evaluators[evaluator_name]
    return {
        "setup": [_command(command) for command in evaluator.setup],
        "install": [
            _command(command)
            for subject in evaluator.subjects
            for command in manifest.subjects[subject].install
        ],
        "gate": _command(evaluator.gate),
        "tasks": [
            {
                "name": task_name,
                "command": _command(evaluator.tasks[task_name].command),
                "walltime_repeats": evaluator.tasks[task_name].walltime_repeats,
                "metrics": evaluator.tasks[task_name].metrics,
            }
            for task_name in task_names
        ],
    }


def _read_outcome(workdir: Path) -> dict[str, Any]:
    return json.loads((workdir / "out" / "outcome.json").read_text(encoding="utf-8"))


def _container_executor(settings: RunnerSettings) -> EvaluatorExecutor:
    return settings.executor or DockerExecutor(settings.docker_command)


def _command(command: Command) -> dict[str, Any]:
    return command.model_dump(mode="json", exclude_none=True)


def _budget(plan: dict[str, Any]) -> float:
    commands = [*plan["setup"], *plan["install"], plan["gate"]]
    total = sum(command["timeout_seconds"] for command in commands)
    for task in plan["tasks"]:
        metrics = task["metrics"]
        runs = task["walltime_repeats"] if "walltime" in metrics else 0
        runs += sum(
            1
            for metric in metrics
            if metric in ("codspeed-walltime", "codspeed-cpu-sim")
        )
        total += task["command"]["timeout_seconds"] * runs
    return total + CONTAINER_MARGIN_SECONDS


def _fetch_source(
    settings: RunnerSettings, org: str, repo: str, commit: str, dest: Path
) -> None:
    url = (
        f"{settings.forge_url.rstrip('/')}/api/v1/repos/{org}/{repo}"
        f"/archive/{commit}.tar.gz"
    )
    try:
        with httpx.Client(
            transport=settings.transport,
            headers={"Authorization": f"token {settings.forge_token}"},
            timeout=120.0,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            archive = response.content
    except httpx.HTTPError as error:
        raise EvaluatorError(
            f"could not fetch {org}/{repo}@{commit}: {error}"
        ) from None
    _extract_archive(archive, dest, f"{org}/{repo}@{commit}")


def _extract_archive(archive: bytes, dest: Path, label: str) -> None:
    dest.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            safe_members = []
            for member in members:
                # Forgejo archives wrap the tree in one top-level directory.
                member.name = member.name.partition("/")[2]
                if member.name and not _excluded_source_path(member.name):
                    safe_members.append(member)
            tar.extractall(dest, members=safe_members, filter="data")
    except tarfile.TarError as error:
        raise EvaluatorError(f"invalid archive for {label}: {error}") from None


def _excluded_source_path(name: str) -> bool:
    return name == ".swecraft" or name.startswith(".swecraft/")
