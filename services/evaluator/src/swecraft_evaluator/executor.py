"""Container executors and worker provenance for evaluator cells."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from swecraft_evaluator.errors import EvaluatorError


class EvaluatorExecutor(Protocol):
    @property
    def name(self) -> str: ...

    def run(self, workdir: Path, image: str, timeout_seconds: float) -> None: ...


@dataclass(frozen=True)
class DockerExecutor:
    command: tuple[str, ...] = ("docker",)

    @property
    def name(self) -> str:
        return "docker"

    def run(self, workdir: Path, image: str, timeout_seconds: float) -> None:
        command = (
            *self.command,
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{workdir.resolve()}:/eval",
            image,
            "python3",
            "/eval/harness.py",
        )
        _run_command(command, workdir, self.command[0], timeout_seconds)


# Apptainer also takes bind requests from its own environment (APPTAINER_BIND,
# APPTAINER_BINDPATH and their SINGULARITY_ twins). Site modules set them to
# expose shared filesystems to every container, which would hand the
# evaluation container the operator's home, scratch and pool trees despite
# --containall. The cell sees the explicit /eval bind and nothing else.
_ENVIRONMENT_BINDS = (
    "APPTAINER_BIND",
    "APPTAINER_BINDPATH",
    "SINGULARITY_BIND",
    "SINGULARITY_BINDPATH",
)


@dataclass(frozen=True)
class ApptainerExecutor:
    command: tuple[str, ...] = ("apptainer",)

    @property
    def name(self) -> str:
        return "apptainer"

    def run(self, workdir: Path, image: str, timeout_seconds: float) -> None:
        # Unlike Docker, a SIF rootfs is immutable: an image whose gate or
        # benchmark writes inside its own tree (caches, scratch files) fails
        # with EROFS without an ephemeral overlay. --writable-tmpfs restores
        # Docker's writable-container-layer semantics; writes vanish on exit.
        command = (
            *self.command,
            "exec",
            "--containall",
            "--cleanenv",
            "--writable-tmpfs",
            "--net",
            "--network",
            "none",
            "--bind",
            f"{workdir.resolve()}:/eval",
            image,
            "python3",
            "/eval/harness.py",
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in _ENVIRONMENT_BINDS
        }
        _run_command(command, workdir, self.command[0], timeout_seconds, environment)


def worker_provenance(executor: EvaluatorExecutor, image: str) -> dict[str, Any]:
    """Describe the worker and the comparison class it belongs to.

    By default the worker class is host-specific, which blocks cross-host
    comparisons. Operators can set ``SWECRAFT_EVALUATOR_WORKER_CLASS`` to a
    homogeneous Slurm partition/class after calibration.
    """

    declared_class = os.environ.get("SWECRAFT_EVALUATOR_WORKER_CLASS")
    class_source = "env:SWECRAFT_EVALUATOR_WORKER_CLASS" if declared_class else "host"
    host_class = declared_class or _host_class()
    worker_class = f"{executor.name}:{image}:{host_class}"
    details = {
        "executor": executor.name,
        "image": image,
        "hostname": platform.node(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "slurm_cluster": os.environ.get("SLURM_CLUSTER_NAME"),
        "slurm_partition": os.environ.get("SLURM_JOB_PARTITION"),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "slurm_array_job_id": os.environ.get("SLURM_ARRAY_JOB_ID"),
        "slurm_array_task_id": os.environ.get("SLURM_ARRAY_TASK_ID"),
    }
    encoded = json.dumps(
        {"class": worker_class, "details": details},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "class": worker_class,
        "class_source": class_source,
        "fingerprint": hashlib.sha256(encoded).hexdigest()[:16],
        "details": details,
    }


def _host_class() -> str:
    hostname = platform.node() or "unknown-host"
    machine = platform.machine() or "unknown-machine"
    return f"{hostname}:{machine}"


def _run_command(
    command: tuple[str, ...],
    workdir: Path,
    executable: str,
    timeout_seconds: float,
    environment: dict[str, str] | None = None,
) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except FileNotFoundError:
        raise EvaluatorError(f"could not execute {executable!r}") from None
    except subprocess.TimeoutExpired:
        raise EvaluatorError(
            f"evaluation container exceeded its {timeout_seconds:.0f}s budget"
        ) from None
    (workdir / "container.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    if completed.returncode:
        raise EvaluatorError(f"evaluation container exited with {completed.returncode}")
