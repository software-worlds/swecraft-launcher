"""Offline evaluator bundles for stateless Slurm/Apptainer workers."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swecraft_evaluator.environment import Environment, World, environment_digest
from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.executor import EvaluatorExecutor, worker_provenance
from swecraft_evaluator.manifest import Manifest
from swecraft_evaluator.runner import (
    RunnerSettings,
    RunRecord,
    _budget,
    _evaluation_task_map,
    _evaluator_plan,
    _prepare_evaluator_cell,
    _read_outcome,
)

BUNDLE_VERSION = 1
BUNDLE_NAME = "bundle.json"
CELL_NAME = "cell.json"
CELL_RESULT_NAME = "cell-result.json"
CELLS_LIST_NAME = "cells.txt"


@dataclass(frozen=True)
class BundleExport:
    path: Path
    payload: dict[str, Any] = field(repr=False)


def export_evaluation_bundle(
    manifest: Manifest,
    world: World,
    evaluation_name: str,
    environment: Environment,
    environment_spec: str,
    settings: RunnerSettings,
    bundle_dir: Path,
) -> BundleExport:
    """Export one credential-free evaluation bundle for offline workers."""

    by_evaluator = _evaluation_task_map(manifest, evaluation_name, environment)
    created_at = datetime.now(UTC)
    created = created_at.isoformat(timespec="seconds")
    digest = environment_digest(environment)
    record_id = f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{evaluation_name}-{digest}"
    root = bundle_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)

    expected_cells = []
    for evaluator_name, task_names in by_evaluator.items():
        evaluator = manifest.evaluators[evaluator_name]
        plan = _evaluator_plan(manifest, evaluator_name, task_names)
        cell_id = _cell_id(
            evaluation_name,
            environment,
            evaluator_name,
            task_names,
            evaluator.image,
            plan,
        )
        cell_dir = root / "cells" / cell_id
        workdir = cell_dir / "work"
        _prepare_evaluator_cell(
            manifest,
            world,
            evaluator_name,
            task_names,
            environment,
            workdir,
            settings,
        )
        cell_payload = {
            "id": cell_id,
            "bundle_id": record_id,
            "evaluation": evaluation_name,
            "environment_digest": digest,
            "evaluator": evaluator_name,
            "tasks": task_names,
            "image": evaluator.image,
            "image_sha256": _file_sha256(Path(evaluator.image)),
            "budget_seconds": _budget(plan),
            "plan_sha256": _json_sha256(plan),
        }
        _write_json(cell_dir / CELL_NAME, cell_payload)
        expected_cells.append(
            {
                "id": cell_id,
                "evaluator": evaluator_name,
                "tasks": task_names,
                "image": evaluator.image,
                "budget_seconds": cell_payload["budget_seconds"],
            }
        )

    payload = {
        "version": BUNDLE_VERSION,
        "id": record_id,
        "record_id": record_id,
        "created": created,
        "evaluation": evaluation_name,
        "environment_spec": environment_spec,
        "environment": dict(sorted(environment.items())),
        "environment_digest": digest,
        "world": {
            "org": world.org,
            "repos": [
                {
                    "name": repo.name,
                    "baseline_commit": repo.baseline_commit,
                    "default_branch": repo.default_branch,
                }
                for repo in world.repos
            ],
        },
        "expected_cells": expected_cells,
        "sanitization": {
            "forge_token": "excluded",
            "society_checkpoint": "excluded",
            "source": "forge archives at exact commits",
        },
    }
    (root / CELLS_LIST_NAME).write_text(
        "".join(f"{cell['id']}\n" for cell in expected_cells), encoding="utf-8"
    )
    _write_slurm_adapter(root, expected_cells)
    _write_json(root / BUNDLE_NAME, payload)
    return BundleExport(path=root / BUNDLE_NAME, payload=payload)


def run_bundle_cell(
    bundle_dir: Path,
    cell_id: str,
    results_dir: Path | None,
    executor: EvaluatorExecutor,
) -> dict[str, Any]:
    """Run one exported cell and write one terminal result bundle."""

    root = bundle_dir.resolve()
    bundle = load_bundle(root)
    cell = _find_cell(bundle, cell_id)
    destination = (results_dir.resolve() if results_dir else root / "results") / cell_id
    result_path = destination / CELL_RESULT_NAME
    if result_path.exists():
        payload = _read_json(result_path)
        if payload.get("terminal") is True:
            return payload

    source_workdir = root / "cells" / cell_id / "work"
    if not source_workdir.is_dir():
        raise EvaluatorError(f"bundle cell {cell_id} has no work directory")
    destination.mkdir(parents=True, exist_ok=True)
    workdir = destination / "work"
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(source_workdir, workdir)

    started = datetime.now(UTC).isoformat(timespec="seconds")
    worker = worker_provenance(executor, cell["image"])
    try:
        executor.run(workdir, cell["image"], float(cell["budget_seconds"]))
        outcome = _read_outcome(workdir)
    except (OSError, json.JSONDecodeError) as error:
        outcome = {"gate": None, "tasks": {}, "error": f"no outcome: {error}"}
    except EvaluatorError as error:
        outcome = {"gate": None, "tasks": {}, "error": str(error)}
    finally:
        shutil.rmtree(workdir / "sources", ignore_errors=True)
    finished = datetime.now(UTC).isoformat(timespec="seconds")
    payload = {
        "bundle_id": bundle["id"],
        "cell_id": cell_id,
        "evaluation": bundle["evaluation"],
        "environment_digest": bundle["environment_digest"],
        "evaluator": cell["evaluator"],
        "tasks": cell["tasks"],
        "terminal": True,
        "started": started,
        "finished": finished,
        "worker": worker,
        "outcome": outcome,
    }
    _write_json(result_path, payload)
    return payload


def collect_bundle_results(
    bundle_dir: Path,
    records_dir: Path,
    results_dir: Path | None = None,
) -> RunRecord:
    """Collect terminal cell results into one aggregate run record."""

    root = bundle_dir.resolve()
    bundle = load_bundle(root)
    destination = records_dir.resolve() / bundle["record_id"]
    record_path = destination / "record.json"
    if record_path.exists():
        return RunRecord(path=record_path, payload=_read_json(record_path))

    result_root = results_dir.resolve() if results_dir else root / "results"
    results = {}
    pending = []
    for cell in bundle["expected_cells"]:
        path = result_root / cell["id"] / CELL_RESULT_NAME
        if not path.exists():
            pending.append(cell["id"])
            continue
        result = _read_json(path)
        if result.get("bundle_id") != bundle["id"]:
            raise EvaluatorError(f"cell {cell['id']} belongs to another bundle")
        if result.get("terminal") is not True:
            pending.append(cell["id"])
            continue
        results[cell["id"]] = result
    if pending:
        raise EvaluatorError(
            f"bundle {bundle['id']} is waiting for {len(pending)} cell(s): "
            + ", ".join(pending)
        )

    destination.mkdir(parents=True, exist_ok=True)
    evaluators = {}
    started = []
    finished = []
    for cell in bundle["expected_cells"]:
        result = results[cell["id"]]
        outcome = dict(result.get("outcome") or {})
        outcome["worker"] = result.get("worker")
        evaluators[cell["evaluator"]] = outcome
        started.append(result["started"])
        finished.append(result["finished"])
        workdir = result_root / cell["id"] / "work"
        if workdir.is_dir():
            shutil.copytree(
                workdir,
                destination / cell["evaluator"],
                dirs_exist_ok=True,
            )

    payload = {
        "id": bundle["record_id"],
        "evaluation": bundle["evaluation"],
        "environment_spec": bundle["environment_spec"],
        "environment": bundle["environment"],
        "environment_digest": bundle["environment_digest"],
        "started": min(started) if started else bundle["created"],
        "finished": max(finished) if finished else bundle["created"],
        "collected": datetime.now(UTC).isoformat(timespec="seconds"),
        "bundle": {"id": bundle["id"], "version": bundle["version"]},
        "evaluators": evaluators,
    }
    _write_json(record_path, payload)
    return RunRecord(path=record_path, payload=payload)


def load_bundle(bundle_dir: Path) -> dict[str, Any]:
    payload = _read_json(bundle_dir / BUNDLE_NAME)
    if payload.get("version") != BUNDLE_VERSION:
        raise EvaluatorError(
            f"unsupported evaluator bundle version {payload.get('version')}"
        )
    cells = payload.get("expected_cells")
    if not isinstance(cells, list) or not cells:
        raise EvaluatorError(f"bundle {bundle_dir} has no expected cells")
    return payload


def cell_id_from_array_index(bundle_dir: Path, array_index: int) -> str:
    if array_index < 1:
        raise EvaluatorError("Slurm array indexes are 1-based")
    cells = load_bundle(bundle_dir)["expected_cells"]
    try:
        cell = cells[array_index - 1]
    except IndexError:
        raise EvaluatorError(
            f"Slurm array index {array_index} exceeds {len(cells)} cell(s)"
        ) from None
    return str(cell["id"])


def _find_cell(bundle: dict[str, Any], cell_id: str) -> dict[str, Any]:
    for cell in bundle["expected_cells"]:
        if cell.get("id") == cell_id:
            return cell
    raise EvaluatorError(f"bundle {bundle['id']} has no cell {cell_id}")


def _cell_id(
    evaluation_name: str,
    environment: Environment,
    evaluator_name: str,
    task_names: list[str],
    image: str,
    plan: dict[str, Any],
) -> str:
    digest = _json_sha256(
        {
            "evaluation": evaluation_name,
            "environment": dict(sorted(environment.items())),
            "evaluator": evaluator_name,
            "tasks": task_names,
            "image": image,
            "plan": plan,
        }
    )[:12]
    return f"{_safe(evaluator_name)}-{digest}"


def _safe(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in ".-" else "-"
        for character in value
    )


def _json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluatorError(f"could not read {path}: {error}") from None
    if not isinstance(payload, dict):
        raise EvaluatorError(f"{path} is not a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    scratch.replace(path)


def _write_slurm_adapter(bundle_dir: Path, cells: list[dict[str, Any]]) -> None:
    # Slurm copies the submitted script into slurmd's spool directory, so
    # BASH_SOURCE cannot locate the bundle from inside a batch job; the
    # exported bundle's absolute path is baked in as the default instead,
    # and SWECRAFT_EVALUATOR_BUNDLE stays the relocation override.
    script = bundle_dir / "slurm" / "swecraft-evaluator-worker.sbatch"
    script.parent.mkdir(parents=True, exist_ok=True)
    budget = max(float(cell["budget_seconds"]) for cell in cells)
    script.write_text(
        f"""#!/usr/bin/env bash
#SBATCH --job-name=swecraft-eval
#SBATCH --array=1-{len(cells)}
#SBATCH --output=%x-%A_%a.out
#SBATCH --time={_slurm_time_limit(budget)}

set -euo pipefail

# Site bootstrap — adjust for your cluster, then submit:
# module load apptainer
# source /path/to/swecraft-launcher/.venv/bin/activate

BUNDLE="${{SWECRAFT_EVALUATOR_BUNDLE:-{bundle_dir}}}"
RESULTS="${{SWECRAFT_EVALUATOR_RESULTS:-$BUNDLE/results}}"
swecraft-evaluator --executor apptainer worker "$BUNDLE" \\
  --array-index "${{SLURM_ARRAY_TASK_ID:?}}" --results "$RESULTS"
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _slurm_time_limit(budget_seconds: float) -> str:
    # The largest cell budget plus staging margin (image load from a parallel
    # filesystem, the workdir copy), rounded up to a whole minute.
    total = int(budget_seconds) + 900
    minutes, seconds = divmod(total, 60)
    if seconds:
        minutes += 1
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}-{hours:02d}:{minutes:02d}:00"
    return f"{hours:02d}:{minutes:02d}:00"
