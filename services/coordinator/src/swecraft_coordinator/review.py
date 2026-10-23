"""Dispatch and collect end-of-run review batches.

A review is a wake-shaped turn with every write authority removed: the
agent gets its repository at its final state, its memory workspace mounted
(edits there are never published), and a browsable forge URL — but no
forge token and no push remote — and produces one structured JSON answer
through the standard
one-shot runner. The batch driver is idempotent — each job's inputs
are digested, completed answers are reused on re-runs, and the batch seals
(`record.json`) only when every job has a validated answer — so a partially
failed batch is re-run with the same command until it completes.

The batch directory is plain files (per-job answers and job receipts);
checkpoint capture picks it up like any other state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from swecraft_runner.checkpoint import export_run
from swecraft_runner.runner import RunnerError, RunRequest, RunResult, run_once

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import (
    AgentProfile,
    CoordinatorConfig,
    resolve_agent_env,
)
from swecraft_coordinator.retention import slim_run
from swecraft_coordinator.review_models import (
    PeerReviewSet,
    ReviewManifest,
    SocietyReview,
)
from swecraft_coordinator.review_surveys import (
    peer_survey,
    review_prompt,
    society_survey,
)
from swecraft_coordinator.wake import FORGE_URL_ENV

logger = logging.getLogger(__name__)

# The one in-turn tool every review turn gets: the staged survey CLI.
_CLI_SOURCE = Path(__file__).parent / "staged" / "review_cli.py"


@dataclass(frozen=True)
class ReviewBatchResult:
    batch_dir: Path
    peer_turns: int
    peer_reviews: int
    society_reviews: int
    failed_jobs: int


@dataclass(frozen=True)
class _ReviewJob:
    job_id: str
    kind: str
    agent: str
    revision: str
    prompt: str
    expected: Mapping[str, str]
    tools: Mapping[str, Mapping[str, Any]]
    model: type[PeerReviewSet] | type[SocietyReview]
    profile: AgentProfile
    include_memory: bool = True
    roster: tuple[str, ...] = ()
    max_targets: int = 0


def validate_review_manifest(
    manifest: ReviewManifest, config: CoordinatorConfig
) -> None:
    profiles = {profile.name for profile in config.agents}
    unknown: set[str] = set()
    if manifest.peer_reviews.enabled:
        peer_agents = manifest.peer_reviews.agents or tuple(sorted(profiles))
        unknown.update(set(peer_agents) - profiles)
    if manifest.society_review.enabled:
        society_agents = manifest.society_review.agents or tuple(sorted(profiles))
        unknown.update(set(society_agents) - profiles)
    if unknown:
        raise CoordinatorError("unknown review agent(s): " + ", ".join(sorted(unknown)))


def plan_review_jobs(
    manifest: ReviewManifest, config: CoordinatorConfig
) -> tuple[_ReviewJob, ...]:
    validate_review_manifest(manifest, config)
    profiles = {profile.name: profile for profile in config.agents}
    jobs: list[_ReviewJob] = []
    roster = tuple(profile.name for profile in config.agents)
    if manifest.peer_reviews.enabled:
        evaluators = manifest.peer_reviews.agents or roster
        for evaluator in evaluators:
            identity = {"run_id": manifest.run_id, "evaluator_agent": evaluator}
            jobs.append(
                _ReviewJob(
                    job_id=evaluator,
                    kind="peer",
                    agent=evaluator,
                    revision=(
                        manifest.peer_reviews.revision or manifest.default_revision
                    ),
                    prompt=review_prompt(
                        "peer", identity, memory=manifest.include_memory
                    ),
                    expected=identity,
                    tools={
                        "survey": peer_survey(identity),
                        "roster": {
                            "run_id": manifest.run_id,
                            "evaluator": evaluator,
                            "agents": list(roster),
                            "max_targets": manifest.peer_reviews.max_targets,
                        },
                    },
                    model=PeerReviewSet,
                    profile=profiles[evaluator],
                    include_memory=manifest.include_memory,
                    roster=roster,
                    max_targets=manifest.peer_reviews.max_targets,
                )
            )
    if manifest.society_review.enabled:
        society_agents = manifest.society_review.agents or tuple(
            profile.name for profile in config.agents
        )
        for agent in society_agents:
            identity = {"run_id": manifest.run_id, "agent": agent}
            jobs.append(
                _ReviewJob(
                    job_id=agent,
                    kind="society",
                    agent=agent,
                    revision=(
                        manifest.society_review.revision or manifest.default_revision
                    ),
                    prompt=review_prompt(
                        "society", identity, memory=manifest.include_memory
                    ),
                    expected=identity,
                    tools={"survey": society_survey(identity)},
                    model=SocietyReview,
                    profile=profiles[agent],
                    include_memory=manifest.include_memory,
                )
            )
    return tuple(jobs)


def run_review_batch(
    manifest: ReviewManifest,
    config: CoordinatorConfig,
    *,
    root: Path = Path("."),
    force: bool = False,
    parallel: int = 1,
    run: Callable[[RunRequest], RunResult] = run_once,
) -> ReviewBatchResult:
    """Run missing jobs and seal the batch directory when complete.

    ``parallel`` dispatches up to that many turns concurrently. Jobs are
    fully independent (own job directory, per-value agent environment),
    so this is safe; receipts keep the plan's job order regardless.
    """

    jobs = plan_review_jobs(manifest, config)
    root = root.expanduser().resolve()
    batch_dir = _resolve(root, manifest.output_dir) / manifest.record_batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    if force:
        _remove_completed_outputs(batch_dir)
    _prune_stale_outputs(batch_dir, jobs)

    if parallel > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            receipts = list(
                pool.map(
                    lambda job: _run_one(job, config, batch_dir, run=run, force=force),
                    jobs,
                )
            )
    else:
        receipts = [
            _run_one(job, config, batch_dir, run=run, force=force) for job in jobs
        ]
    counts = {"peer_turns": 0, "peer": 0, "society": 0}
    failed = 0
    for job, receipt in zip(jobs, receipts, strict=True):
        if receipt["status"] == "completed":
            if job.kind == "peer":
                counts["peer_turns"] += 1
                counts["peer"] += receipt.get("reviews_count", 0)
            else:
                counts["society"] += 1
        else:
            failed += 1
            logger.warning(
                "review job %s-%s failed: %s", job.kind, job.job_id, receipt["error"]
            )

    _write_json(
        batch_dir / "batch.json",
        {
            "schema_version": 1,
            "run_id": manifest.run_id,
            "batch_id": manifest.record_batch_id,
            "created_at": _now(),
            "jobs": receipts,
        },
    )
    if failed:
        (batch_dir / "record.json").unlink(missing_ok=True)
    else:
        _write_json(
            batch_dir / "record.json",
            {
                "schema_version": 1,
                "status": "completed",
                "run_id": manifest.run_id,
                "batch_id": manifest.record_batch_id,
                "completed_at": _now(),
                "peer_turns": counts["peer_turns"],
                "peer_reviews": counts["peer"],
                "society_reviews": counts["society"],
            },
        )
    return ReviewBatchResult(
        batch_dir=batch_dir,
        peer_turns=counts["peer_turns"],
        peer_reviews=counts["peer"],
        society_reviews=counts["society"],
        failed_jobs=failed,
    )


def _run_one(
    job: _ReviewJob,
    config: CoordinatorConfig,
    batch_dir: Path,
    *,
    run: Callable[[RunRequest], RunResult],
    force: bool,
) -> dict[str, Any]:
    output_path = batch_dir / "reviews" / job.kind / f"{job.job_id}.json"
    job_dir = batch_dir / "jobs" / f"{job.kind}-{job.job_id}"
    receipt_path = job_dir / "job.json"
    tools_dir = job_dir / "tools"
    # Rebuild from scratch: a reused batch directory must not leak files
    # staged by an earlier run or manifest shape into the turn — the
    # no-evidence contract holds on reruns, not just fresh batches.
    shutil.rmtree(tools_dir, ignore_errors=True)
    tools_dir.mkdir(parents=True)
    for name, payload in job.tools.items():
        _write_json(tools_dir / f"{name}.json", payload)
    (tools_dir / "review_cli.py").write_text(
        _CLI_SOURCE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    input_digest = _job_input_digest(job, config)

    if output_path.is_file() and not force:
        cached = _cached_receipt(job, output_path, receipt_path, input_digest)
        if cached is not None:
            return cached
        output_path.unlink(missing_ok=True)

    clone_url = f"{config.forge_url.rstrip('/')}/{job.profile.home_repo}.git"
    # Wake-shaped conditions, minus every write authority: the reviewer
    # gets its memory workspace and a browsable forge URL, but no forge
    # token and no push remote — the survey must not mutate the world.
    environment = dict(resolve_agent_env(config, job.profile))
    environment[FORGE_URL_ENV] = config.push_url
    receipt = {
        "id": job.job_id,
        "kind": job.kind,
        "agent": job.agent,
        "status": "failed",
        "cached": False,
        "input_digest": input_digest,
        "runner_run_id": "",
        "runner_run_dir": None,
        "review": str(output_path),
    }
    try:
        result = run(
            RunRequest(
                repository=clone_url,
                revision=job.revision,
                prompt=job.prompt,
                model=job.profile.model,
                agent=job.profile.agent,
                agent_kwargs=job.profile.agent_kwargs,
                model_pricing=config.pricing_for(job.profile.model),
                output_dir=config.runs_dir,
                image=job.profile.image,
                tool_directory=tools_dir,
                agent_env=environment,
                timeout_seconds=job.profile.timeout_seconds,
                memory_repository=clone_url if job.include_memory else None,
                memory_revision=config.memory_branch,
                structured_output=True,
                allow_remote_writes=False,
            )
        )
    except RunnerError as error:
        receipt["runner_run_id"] = error.run_id or ""
        receipt["runner_run_dir"] = str(error.run_dir) if error.run_dir else None
        receipt["error"] = f"runner failed: {error}"
        _write_json(receipt_path, receipt)
        return receipt
    except Exception as error:  # noqa: BLE001 - keep batch receipts inspectable
        receipt["error"] = f"{type(error).__name__}: {error}"
        _write_json(receipt_path, receipt)
        return receipt

    receipt["runner_run_id"] = result.run_id
    receipt["runner_run_dir"] = str(result.run_dir)
    try:
        review = job.model.model_validate(result.structured)
    except ValidationError as error:
        receipt["error"] = f"invalid structured review: {error}"
        _write_json(receipt_path, receipt)
        return receipt
    mismatch = _identity_mismatch(review, job.expected) or _selection_mismatch(
        review, job
    )
    if mismatch:
        receipt["error"] = mismatch
        _write_json(receipt_path, receipt)
        return receipt
    _write_json(output_path, review.model_dump(mode="json", exclude_none=True))
    receipt["status"] = "completed"
    if isinstance(review, PeerReviewSet):
        receipt["reviews_count"] = len(review.reviews)
    # The turn's trace joins the batch in the same lean projection
    # checkpoints apply to wakes, so a sealed batch carries its own
    # transcripts; then the raw run is slimmed like every other turn type.
    trace_dir = job_dir / "run"
    shutil.rmtree(trace_dir, ignore_errors=True)
    try:
        export_run(result.run_dir, trace_dir, include_harbor=False)
        receipt["trace"] = str(trace_dir)
    except RunnerError:
        logger.exception("could not export review run %s", result.run_id)
    try:
        slim_run(result.run_dir)
    except OSError:
        logger.exception("could not slim review run %s", result.run_id)
    _write_json(receipt_path, receipt)
    return receipt


def _cached_receipt(
    job: _ReviewJob,
    output_path: Path,
    receipt_path: Path,
    input_digest: str,
) -> dict[str, Any] | None:
    try:
        review = job.model.model_validate(_read_json(output_path))
        receipt = _read_json(receipt_path)
    except (ValidationError, CoordinatorError):
        return None
    if _identity_mismatch(review, job.expected):
        return None
    if receipt.get("input_digest") != input_digest:
        return None
    receipt = {
        "id": job.job_id,
        "kind": job.kind,
        "agent": job.agent,
        "status": "completed",
        "cached": True,
        "input_digest": input_digest,
        "review": str(output_path),
    }
    if isinstance(review, PeerReviewSet):
        receipt["reviews_count"] = len(review.reviews)
    return receipt


def _job_input_digest(job: _ReviewJob, config: CoordinatorConfig) -> str:
    payload = {
        "schema_version": 1,
        "job_id": job.job_id,
        "kind": job.kind,
        "agent": job.agent,
        "repository": job.profile.home_repo,
        "revision": job.revision,
        "prompt": job.prompt,
        "include_memory": job.include_memory,
        "forge_browse_url": config.push_url,
        "expected": dict(job.expected),
        "profile": {
            "model": job.profile.model,
            "agent": job.profile.agent,
            "agent_kwargs": job.profile.agent_kwargs,
            "image": job.profile.image,
        },
        "tools": {name: payload for name, payload in sorted(job.tools.items())},
        "schema": job.model.model_json_schema(),
        "cli": _CLI_SOURCE.read_text(encoding="utf-8"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _selection_mismatch(review: object, job: _ReviewJob) -> str:
    if not isinstance(review, PeerReviewSet):
        return ""
    targets = {selection.target for selection in review.selections}
    unknown = targets - set(job.roster)
    if unknown:
        return "selected target(s) not in the roster: " + ", ".join(sorted(unknown))
    if job.max_targets and len(targets) > job.max_targets:
        return f"{len(targets)} targets exceed the limit of {job.max_targets}"
    return ""


def _identity_mismatch(
    review: PeerReviewSet | SocietyReview, expected: Mapping[str, str]
) -> str:
    for field, expected_value in expected.items():
        if getattr(review, field) != expected_value:
            return (
                f"review identity mismatch for {field}: "
                f"{getattr(review, field)!r} != {expected_value!r}"
            )
    return ""


def _remove_completed_outputs(batch_dir: Path) -> None:
    for relative in ("reviews", "record.json", "batch.json"):
        target = batch_dir / relative
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)


def _prune_stale_outputs(batch_dir: Path, jobs: tuple[_ReviewJob, ...]) -> None:
    """Drop outputs for jobs no longer in the manifest, so a shrunk batch
    cannot seal with answers nothing asked for."""

    current = {(job.kind, f"{job.job_id}.json") for job in jobs}
    for kind in ("peer", "society"):
        directory = batch_dir / "reviews" / kind
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            if (kind, path.name) not in current:
                path.unlink(missing_ok=True)
    jobs_dir = batch_dir / "jobs"
    current_dirs = {f"{job.kind}-{job.job_id}" for job in jobs}
    if jobs_dir.is_dir():
        for path in jobs_dir.iterdir():
            if path.name not in current_dirs:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CoordinatorError(f"could not read JSON {path}: {error}") from None
    if not isinstance(value, dict):
        raise CoordinatorError(f"JSON file must contain an object: {path}")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
