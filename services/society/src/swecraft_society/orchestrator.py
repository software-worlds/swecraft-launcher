"""Transactional checkpoint creation and pure filesystem loading.

Checkpointing is a recovery operation first: the quiescent wake boundary
makes the coordinator ledger, run traces, and evaluator results static,
so all of them are copied **while the Forge keeps serving**. Only the
Forge volume — Forgejo's SQLite and Git repositories share one Docker
volume with no synchronized live-snapshot story — needs the services
stopped, so the outage covers exactly that one tar. Finalization then
derives heads, builds the manifest, and runs full semantic verification
against the already-copied bytes with the society fully live.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from swecraft_common import HubError
from swecraft_common.forge import ForgeClient
from swecraft_common.fs import copy_regular_tree, resolve_path
from swecraft_common.lifecycle import (
    LifecycleBusy,
    lifecycle_lock_path,
    society_lifecycle_lock,
)
from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.checkpoint import CoordinatorCheckpoint
from swecraft_evaluator.checkpoint import EvaluatorCheckpoint
from swecraft_evaluator.errors import EvaluatorError
from swecraft_forge import ForgeRuntime, ForgeRuntimeError, standard_compose_file
from swecraft_forge.frozen import read_heads
from swecraft_world.errors import WorldError
from swecraft_world.io import load_lock, validate_lock
from swecraft_world.io import load_manifest as load_world_manifest

from swecraft_society import apparatus, layout
from swecraft_society.archive import opened_bundle, pack_archive
from swecraft_society.config import DEFAULT_CONFIG_PATH, load_checkpoint_config
from swecraft_society.errors import SocietyError
from swecraft_society.inventory import (
    collect_entries,
    hash_file,
    load_manifest,
    verify_checkpoint,
    write_manifest,
)
from swecraft_society.models import (
    CheckpointManifest,
    ContributorRecord,
    FileEntry,
    ForgeInfo,
    ImageIdentity,
    RepoHead,
)
from swecraft_society.provenance import repository_head

logger = logging.getLogger(__name__)

ACTIONS_POLL_SECONDS = 5.0
# The lifecycle lock has transient holders — an evaluator claim transaction
# lasts milliseconds, a retention sweep seconds. A bounded wait absorbs them
# so a boundary checkpoint (whose cadence marker is already spent) is never
# consumed by a millisecond collision, while a genuine conflict such as a
# concurrent checkpoint still fails once the wait is exhausted.
LIFECYCLE_WAIT_SECONDS = 30.0


@dataclass(frozen=True)
class CheckpointOptions:
    root: Path = Path(".")
    config_path: Path = DEFAULT_CONFIG_PATH
    output: Path | None = None
    archive: Path | None = None
    include_harbor: bool | None = None
    compose_project: str | None = None
    # Injectable for tests; the CLI uses the Forge package's standard file.
    compose_file: Path | None = None


@dataclass(frozen=True)
class LoadOptions:
    source: Path
    root: Path = Path(".")
    config_path: Path = DEFAULT_CONFIG_PATH
    dest: Path | None = None


@dataclass(frozen=True)
class LoadResult:
    dest: Path
    manifest: CheckpointManifest


def checkpoint(
    options: CheckpointOptions, forge_runtime: ForgeRuntime | None = None
) -> Path:
    """Create one verified checkpoint directory and optional local archive."""

    root = options.root.expanduser().resolve()
    config = load_checkpoint_config(options.config_path, root=root).config
    apparatus_root = resolve_path(root, config.apparatus.root)
    coordinator_config = resolve_path(apparatus_root, config.apparatus.coordinator)
    try:
        coordinator = CoordinatorCheckpoint.load(coordinator_config, root=root)
    except CoordinatorError as error:
        raise SocietyError(f"invalid coordinator configuration: {error}") from None

    # World provenance is a recovery essential and is proven directly;
    # capture must never depend on regenerable evaluation metadata.
    world_lock_path = resolve_path(apparatus_root, config.apparatus.world_lock)
    try:
        world_document = load_world_manifest(
            resolve_path(apparatus_root, config.apparatus.world)
        )
        world_lock = validate_lock(
            world_document, load_lock(world_lock_path), require_complete=True
        )
    except WorldError as error:
        raise SocietyError(f"invalid world apparatus: {error}") from None
    include_evaluation = _evaluation_apparatus_ok(
        manifest_path=resolve_path(apparatus_root, config.apparatus.evaluation),
    )

    evaluator = EvaluatorCheckpoint(
        records_dir=resolve_path(root, config.checkpoint.evaluator_records_dir),
        state_path=resolve_path(root, config.checkpoint.evaluator_state),
    )
    if lifecycle_lock_path(coordinator.database) != lifecycle_lock_path(
        evaluator.state_path
    ):
        raise SocietyError(
            "coordinator and evaluator state must share one .swecraft root for "
            "transactional checkpointing"
        )

    configured_compose = (
        resolve_path(root, config.checkpoint.forge_compose_file)
        if config.checkpoint.forge_compose_file is not None
        else None
    )
    compose_file = (
        options.compose_file or configured_compose or standard_compose_file()
    ).resolve()
    project = options.compose_project or config.checkpoint.compose_project
    forge_state_dir = (
        resolve_path(root, config.checkpoint.forge_state_dir)
        if config.checkpoint.forge_state_dir is not None
        else compose_file.parent / "state"
    )
    forge = forge_runtime or ForgeRuntime(
        project=project,
        compose_file=compose_file,
        state_dir=forge_state_dir,
    )
    if forge.project != project:
        raise SocietyError("injected Forge runtime does not match the capture project")

    checkpoint_id = (
        f"{world_lock.world_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    )
    output = resolve_path(
        root, options.output or config.checkpoint.output_dir / checkpoint_id
    )
    if output.exists():
        raise SocietyError(f"checkpoint output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    archive = (
        resolve_path(root, options.archive) if options.archive is not None else None
    )
    if archive is not None and archive.exists():
        raise SocietyError(f"checkpoint archive already exists: {archive}")
    include_harbor = (
        config.checkpoint.harbor == "include"
        if options.include_harbor is None
        else options.include_harbor
    )

    try:
        with society_lifecycle_lock(
            coordinator.database, timeout_seconds=LIFECYCLE_WAIT_SECONDS
        ):
            coordinator.assert_quiescent()
            stage, apparatus_capture, image_identity, captured_at = _capture_stage(
                root=root,
                output=output,
                checkpoint_id=checkpoint_id,
                config=config,
                coordinator=coordinator,
                evaluator=evaluator,
                forge=forge,
                include_evaluation=include_evaluation,
                include_harbor=include_harbor,
            )
    except LifecycleBusy:
        raise SocietyError("another society lifecycle operation is running") from None
    except (CoordinatorError, EvaluatorError, ForgeRuntimeError, HubError) as error:
        raise SocietyError(str(error)) from None

    # The society is live again; everything below reads only the stage.
    try:
        _finalize(
            stage=stage,
            output=output,
            checkpoint_id=checkpoint_id,
            coordinator=coordinator,
            forge=forge,
            world_lock=world_lock,
            apparatus_capture=apparatus_capture,
            image_identity=image_identity,
            captured_at=captured_at,
        )
    except (CoordinatorError, EvaluatorError, ForgeRuntimeError, HubError) as error:
        shutil.rmtree(stage, ignore_errors=True)
        raise SocietyError(str(error)) from None
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    if archive is not None:
        pack_archive(output, archive)
    return output


def _capture_stage(
    *,
    root: Path,
    output: Path,
    checkpoint_id: str,
    config,
    coordinator: CoordinatorCheckpoint,
    evaluator: EvaluatorCheckpoint,
    forge: ForgeRuntime,
    include_evaluation: bool,
    include_harbor: bool,
) -> tuple[Path, apparatus.ApparatusCapture, ImageIdentity, datetime]:
    """Copy raw bytes and cheap identities; expensive proofs wait.

    The lifecycle lock is held and the coordinator is quiescent, so the
    apparatus, ledger, run traces, and evaluator results are static and
    are copied while the Forge keeps serving. Only the Forge volume needs
    the services stopped: the stop/tar/start window at the end is the
    entire outage. ``created_at`` is taken at that stop, the moment the
    frozen Forge state was true; the agent image identity is also pinned
    here so a rebuild during finalization cannot change the epoch.
    """

    stage = Path(tempfile.mkdtemp(prefix=f".{checkpoint_id}.", dir=output.parent))
    try:
        image_id, platform = forge.inspect_image(coordinator.agent_image)
        image_identity = ImageIdentity(
            reference=coordinator.agent_image,
            image_id=image_id,
            platform=platform,
        )
        apparatus_capture = apparatus.capture(
            root, stage, config.apparatus, include_evaluation=include_evaluation
        )
        skipped_runs = coordinator.capture(
            state_destination=stage / "state/coordinator",
            runs_destination=stage / layout.RUNS_DIR,
            include_harbor=include_harbor,
        )
        if skipped_runs:
            logger.warning(
                "checkpoint excludes %d non-terminal/non-run coordinator entry(s): %s",
                len(skipped_runs),
                ", ".join(skipped_runs),
            )
        skipped = evaluator.capture(
            state_destination=stage / "state/evaluator",
            results_destination=stage / layout.EVALUATIONS_DIR,
        )
        if skipped:
            logger.warning(
                "checkpoint excludes %d in-flight/unindexed evaluation bundle(s): %s",
                len(skipped),
                ", ".join(skipped),
            )
        reviews_source = resolve_path(root, config.checkpoint.reviews_dir)
        if reviews_source.is_dir():
            copy_regular_tree(
                reviews_source, stage / layout.REVIEWS_DIR, label="reviews"
            )
        _wait_for_active_actions(
            coordinator, timeout_seconds=config.checkpoint.actions_wait_seconds
        )
        captured_at = datetime.now(UTC)
        running = forge.running_services()
        try:
            forge.stop(running)
        except Exception:
            forge.start(running)
            raise
        try:
            forge.capture(stage / "state/forge")
        finally:
            forge.start(running)
        return stage, apparatus_capture, image_identity, captured_at
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _finalize(
    *,
    stage: Path,
    output: Path,
    checkpoint_id: str,
    coordinator: CoordinatorCheckpoint,
    forge: ForgeRuntime,
    world_lock,
    apparatus_capture: apparatus.ApparatusCapture,
    image_identity: ImageIdentity,
    captured_at: datetime,
) -> None:
    """Prove and publish the staged bytes; the society is already running."""

    contributors = (
        apparatus_capture.contributor,
        _contributor("coordinator", stage, ("state/coordinator",)),
        _contributor("evaluator", stage, ("state/evaluator", layout.EVALUATIONS_DIR)),
        _contributor("forge", stage, ("state/forge",)),
        _contributor("runs", stage, (layout.RUNS_DIR,)),
        _contributor("reviews", stage, (layout.REVIEWS_DIR,)),
    )
    volume_tar = stage / layout.FORGE_VOLUME_TAR
    heads = read_heads(
        volume_tar,
        world_lock.org,
        {repository.name: repository.default_branch for repository in world_lock.repos},
    )
    manifest = CheckpointManifest(
        checkpoint_id=checkpoint_id,
        created_at=captured_at,
        hub_commit=repository_head(),
        apparatus=apparatus_capture.provenance,
        world_id=world_lock.world_id,
        world_lock_sha256=hash_file(stage / layout.WORLD_LOCK)[0],
        agent_image=image_identity,
        forge=ForgeInfo(
            source_url=coordinator.forge_url,
            compose_project=forge.project,
            image=forge.image,
            volume="forgejo-data",
        ),
        heads=tuple(
            RepoHead(
                name=repository.name,
                baseline_commit=repository.baseline_commit,
                current_head=heads[repository.name],
            )
            for repository in world_lock.repos
        ),
        contributors=contributors,
        omissions=apparatus_capture.omissions,
    )
    write_manifest(stage, manifest)
    verify_checkpoint(stage)
    os.replace(stage, output)


def _evaluation_apparatus_ok(*, manifest_path: Path) -> bool:
    """Decide whether the evaluation manifest is capturable, degrading loudly.

    The manifest is captured whenever it exists as a regular file — the
    society treats it as an opaque hashed artifact and never parses it,
    so any present bytes verify. Only a re-seeded world with no evaluation
    apparatus degrades to a partial checkpoint,
    with a warning rather than an aborted capture.
    """

    if not manifest_path.is_file() or manifest_path.is_symlink():
        logger.warning(
            "capturing without evaluation apparatus (missing: %s); "
            "checkpoint will be partial",
            manifest_path,
        )
        return False
    return True


def load(options: LoadOptions) -> LoadResult:
    """Materialize and verify a checkpoint using filesystem operations only."""

    root = options.root.expanduser().resolve()
    config = load_checkpoint_config(options.config_path, root=root).config
    with opened_bundle(options.source.expanduser().resolve()) as source:
        source = source.resolve()
        manifest = load_manifest(source)
        dest = resolve_path(
            root, options.dest or config.load.output_dir / manifest.checkpoint_id
        )
        if dest == source or dest.is_relative_to(source):
            raise SocietyError(
                "load destination must be outside the source checkpoint directory"
            )
        _materialize(source, dest, manifest)
    return LoadResult(dest=dest, manifest=manifest)


def _materialize(source: Path, dest: Path, manifest: CheckpointManifest) -> None:
    if dest.exists() and not dest.is_dir():
        raise SocietyError(f"load destination is not a directory: {dest}")
    if dest.exists() and any(dest.iterdir()):
        raise SocietyError(f"load destination is not empty: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    existed = dest.exists()
    stage = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        for relative in (
            layout.MANIFEST_NAME,
            layout.CARD_NAME,
            *(entry.path for entry in manifest.files),
        ):
            source_path = source / relative
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
        verify_checkpoint(stage)
        if existed:
            dest.rmdir()
        os.replace(stage, dest)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        if existed and not dest.exists():
            dest.mkdir()
        raise


class _ActionsProbe(ForgeClient):
    def active_jobs(self) -> list:
        listed = self._request("GET", "/admin/actions/runners/jobs")
        if isinstance(listed, dict):
            listed = listed.get("entries") or listed.get("jobs") or []
        return list(listed or [])


def _wait_for_active_actions(
    coordinator: CoordinatorCheckpoint, *, timeout_seconds: int
) -> None:
    """Let the asynchronous CI tail finish before freezing Forge.

    Coordinator wakes are already drained at a cadence boundary, but a push
    near the end of a wake can leave a queued or running Forgejo Actions job.
    Checkpointing is lower priority than that work: wait for it, then abort
    the checkpoint on timeout rather than stopping the job's Forge/Dind.

    Probing remains best-effort because manual checkpoints may use apparatus
    without an admin token. In that case capture retains V1's documented
    crash-consistent behavior and emits a warning when waiting was requested.
    """

    try:
        token = coordinator.config.resolved_admin_token()
    except CoordinatorError as error:
        if timeout_seconds:
            logger.warning("cannot wait for Forgejo Actions: %s", error)
        return
    probe = _ActionsProbe(coordinator.forge_url, token)
    try:
        _wait_until_actions_idle(probe, timeout_seconds=timeout_seconds)
    except SocietyError:
        raise
    except Exception as error:
        if timeout_seconds:
            logger.warning("cannot wait for Forgejo Actions: %s", error)
        else:
            logger.debug("could not probe Forgejo Actions jobs: %s", error)


def _wait_until_actions_idle(
    probe: _ActionsProbe,
    *,
    timeout_seconds: int,
    poll_seconds: float = ACTIONS_POLL_SECONDS,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> None:
    """Poll one Forgejo probe; abort the checkpoint if CI outlives the wait."""

    deadline = monotonic() + timeout_seconds
    announced = False
    while True:
        jobs = probe.active_jobs()
        if not jobs:
            if announced:
                logger.info("Forgejo Actions is idle; continuing checkpoint")
            return
        if timeout_seconds <= 0:
            logger.warning(
                "Forgejo Actions reports %d queued/active job(s); "
                "actions_wait_seconds=0, so capture proceeds crash-consistently",
                len(jobs),
            )
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SocietyError(
                f"checkpoint skipped: {len(jobs)} Forgejo Actions job(s) remain "
                f"queued or active after {timeout_seconds}s"
            )
        if not announced:
            logger.info(
                "waiting up to %ds for %d queued/active Forgejo Actions job(s)",
                timeout_seconds,
                len(jobs),
            )
            announced = True
        sleep(min(poll_seconds, remaining))


def _contributor(name: str, root: Path, subpaths: tuple[str, ...]) -> ContributorRecord:
    entries: list[FileEntry] = []
    for subpath in subpaths:
        if (root / subpath).exists():
            entries.extend(collect_entries(root, subpath))
    return ContributorRecord(
        name=name, files=tuple(sorted(entries, key=lambda entry: entry.path))
    )
