"""Capture the experiment apparatus into its canonical checkpoint layout."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from swecraft_common.fs import copy_regular_tree, resolve_path

from swecraft_society import layout
from swecraft_society.config import ApparatusConfig
from swecraft_society.errors import SocietyError
from swecraft_society.inventory import collect_entries, inventory_digest
from swecraft_society.models import ApparatusInfo, ContributorRecord
from swecraft_society.provenance import apparatus_git_provenance

NAME = "apparatus"


@dataclass(frozen=True)
class ApparatusCapture:
    contributor: ContributorRecord
    provenance: ApparatusInfo
    # Checkpoint-root paths a complete capture would carry but this one
    # could not; recorded in the manifest so the gap is explicit.
    omissions: tuple[str, ...] = ()


def capture(
    root: Path,
    stage: Path,
    config: ApparatusConfig,
    *,
    include_evaluation: bool = True,
) -> ApparatusCapture:
    """Copy the apparatus, degrading on absent research metadata.

    The world manifest, world lock, and coordinator config are recovery
    essentials — without them the checkpoint cannot be verified or
    resumed, so their absence still aborts. The evaluation manifest is
    regenerable measurement metadata: when missing, the capture proceeds
    and records it under ``omissions`` instead of losing irreplaceable
    state to a derived file.
    """

    apparatus_root = resolve_path(root, config.root)
    required = (
        (config.world, layout.WORLD_MANIFEST),
        (config.world_lock, layout.WORLD_LOCK),
        (config.coordinator, layout.COORDINATOR_CONFIG),
    )
    degradable = ((config.evaluation, layout.EVALUATION_MANIFEST),)
    optional = (
        (config.charters, layout.CHARTERS_DIR),
        (config.images, layout.IMAGES_DIR),
    )
    captured_sources: list[Path] = []
    for configured, destination in required:
        source = resolve_path(apparatus_root, configured)
        if not source.is_file() or source.is_symlink():
            raise SocietyError(f"required apparatus file is missing: {source}")
        _copy_file(source, stage / destination)
        captured_sources.append(source)
    omissions: list[str] = []
    degradable_present = all(
        resolve_path(apparatus_root, configured).is_file()
        and not resolve_path(apparatus_root, configured).is_symlink()
        for configured, _ in degradable
    )
    # Opaque research metadata: absent, it is omitted loudly rather
    # than aborting the capture of recoverable state.
    if include_evaluation and degradable_present:
        for configured, destination in degradable:
            source = resolve_path(apparatus_root, configured)
            _copy_file(source, stage / destination)
            captured_sources.append(source)
    else:
        omissions = [destination for _, destination in degradable]
    for configured, destination in optional:
        source = resolve_path(apparatus_root, configured)
        if not source.exists():
            continue
        if not source.is_dir() or source.is_symlink():
            raise SocietyError(f"apparatus directory is not regular: {source}")
        copy_regular_tree(source, stage / destination, label="apparatus")
        captured_sources.append(source)

    entries = collect_entries(stage, layout.APPARATUS_DIR)
    git_ref, git_commit, dirty = apparatus_git_provenance(
        apparatus_root, tuple(captured_sources)
    )
    return ApparatusCapture(
        contributor=ContributorRecord(name=NAME, files=entries),
        provenance=ApparatusInfo(
            sha256=inventory_digest(entries),
            git_ref=git_ref,
            git_commit=git_commit,
            dirty=dirty,
        ),
        omissions=tuple(sorted(omissions)),
    )


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
