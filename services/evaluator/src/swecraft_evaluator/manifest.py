"""Evaluation manifest: subjects, evaluators, tasks, and named evaluations.

The manifest is the complete human-authored surface of the evaluator:

- ``subjects``  — how each world repository installs into a container
- ``evaluators`` — frozen downstream projects whose benchmarks produce numbers
- ``evaluations`` — named shortcuts selecting ``evaluator@task`` pairs and the
  metric that aggregates them into one series
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from swecraft_evaluator.errors import EvaluatorError

Metric = Literal["walltime", "codspeed-walltime", "codspeed-cpu-sim"]
Aggregate = Literal["geomean", "average"]
CODSPEED_METRICS = {"codspeed-walltime", "codspeed-cpu-sim"}


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    env: dict[str, str] = {}
    cwd: str | None = None
    timeout_seconds: float = Field(default=600.0, gt=0)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: Command
    walltime_repeats: int = Field(default=3, ge=1)
    metrics: list[Metric] = Field(min_length=1)

    @model_validator(mode="after")
    def _codspeed_commands_are_direct_pytest(self) -> Task:
        if not any(metric in CODSPEED_METRICS for metric in self.metrics):
            return self
        argv = self.command.argv
        if not _is_pytest_argv(argv):
            raise ValueError(
                "codspeed-* metrics require a direct pytest argv "
                "(pytest ... or python -m pytest ...)"
            )
        if _has_codspeed_instrumentation(argv):
            raise ValueError(
                "codspeed-* task commands must be instrument-neutral; "
                "remove --codspeed, --codspeed-mode, and pytest-codspeed "
                "plugin flags"
            )
        return self


class Subject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    install: list[Command] = Field(min_length=1)


class Evaluator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str
    subjects: list[str] = Field(min_length=1)
    setup: list[Command] = []
    gate: Command
    tasks: dict[str, Task] = Field(min_length=1)


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # An empty task list is a legal, explicit declaration: nothing
    # downstream measures this — the release lane still runs it, writing
    # an instantly-empty record so the cursor advances through the normal
    # mechanism instead of a code-side skip. Scoring already yields
    # aggregates of None for a record with no tasks.
    tasks: list[str]
    metric: Metric
    aggregates: list[Aggregate] = ["geomean"]
    # The tasks a society release can reach at all, as the operator judges
    # it (from a consumer census, say). Reports aggregate over these as
    # well as over every task; absent, the report derives the set from the
    # ``downstream-*`` batteries instead.
    exposed: list[str] | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_lock: Path
    subjects: dict[str, Subject] = Field(min_length=1)
    evaluators: dict[str, Evaluator] = Field(min_length=1)
    evaluations: dict[str, Evaluation] = Field(min_length=1)

    @model_validator(mode="after")
    def _references_resolve(self) -> Manifest:
        for name, evaluator in self.evaluators.items():
            for subject in evaluator.subjects:
                if subject not in self.subjects:
                    raise ValueError(
                        f"evaluator {name} references unknown subject {subject}"
                    )
        for name, evaluation in self.evaluations.items():
            for ref in evaluation.tasks:
                evaluator_name, _, task_name = ref.partition("@")
                evaluator = self.evaluators.get(evaluator_name)
                if evaluator is None or task_name not in evaluator.tasks:
                    raise ValueError(f"evaluation {name} references unknown task {ref}")
                task = evaluator.tasks[task_name]
                if evaluation.metric not in task.metrics:
                    raise ValueError(
                        f"evaluation {name}: task {ref} does not measure "
                        f"{evaluation.metric}"
                    )
            for ref in evaluation.exposed or ():
                if ref not in evaluation.tasks:
                    raise ValueError(
                        f"evaluation {name}: exposed task {ref} is not one of its tasks"
                    )
        return self

    def task(self, ref: str) -> tuple[str, str, Task]:
        """Split ``evaluator@task`` and return (evaluator, task name, task)."""

        evaluator_name, _, task_name = ref.partition("@")
        evaluator = self.evaluators.get(evaluator_name)
        if evaluator is None or task_name not in evaluator.tasks:
            raise EvaluatorError(f"unknown task reference {ref}")
        return evaluator_name, task_name, evaluator.tasks[task_name]


def load_manifest(path: Path) -> Manifest:
    """Parse the manifest and resolve ``world_lock`` relative to it."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise EvaluatorError(f"could not read manifest {path}: {error}") from None
    if not isinstance(payload, dict):
        raise EvaluatorError(f"manifest {path} is not a mapping")
    try:
        manifest = Manifest.model_validate(payload)
    except ValueError as error:
        raise EvaluatorError(f"invalid manifest {path}: {error}") from None
    if not manifest.world_lock.is_absolute():
        manifest.world_lock = (path.parent / manifest.world_lock).resolve()
    return manifest


def _is_pytest_argv(argv: list[str]) -> bool:
    executable = Path(argv[0]).name
    if executable in {"pytest", "py.test"} or executable.startswith("pytest-"):
        return True
    return (
        executable.startswith("python")
        and len(argv) >= 3
        and argv[1] == "-m"
        and argv[2] == "pytest"
    )


def _has_codspeed_instrumentation(argv: list[str]) -> bool:
    for index, argument in enumerate(argv):
        if argument == "--codspeed" or argument.startswith("--codspeed-"):
            return True
        if argument.startswith("--codspeed="):
            return True
        if "pytest_codspeed" in argument:
            return True
        if argument == "codspeed" and index > 0 and argv[index - 1] == "-p":
            return True
    return False
