"""Load, bind, and atomically write world protocol documents."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from swecraft_world.errors import WorldError
from swecraft_world.models import WorldLock, WorldManifest


@dataclass(frozen=True)
class ManifestDocument:
    """A parsed manifest together with the digest of its exact source bytes."""

    path: Path
    sha256: str
    manifest: WorldManifest


def load_manifest(path: Path) -> ManifestDocument:
    """Load and validate human-authored intent without rewriting it."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise WorldError(f"could not read world manifest {path}: {error}") from None
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise WorldError(f"invalid world manifest {path}: {error}") from None
    try:
        manifest = WorldManifest.model_validate(payload)
    except ValidationError as error:
        raise WorldError(f"invalid world manifest {path}: {error}") from None
    return ManifestDocument(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        manifest=manifest,
    )


def load_lock(path: Path) -> WorldLock:
    """Load a machine-written world lock."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WorldError(f"could not read world lock {path}: {error}") from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise WorldError(f"invalid world lock {path}: {error}") from None
    try:
        return WorldLock.model_validate(payload)
    except ValidationError as error:
        raise WorldError(f"invalid world lock {path}: {error}") from None


def validate_lock(
    document: ManifestDocument,
    lock: WorldLock,
    *,
    require_complete: bool = True,
) -> WorldLock:
    """Prove that a lock describes this exact manifest.

    Partial locks must contain a prefix of manifest repositories, matching the
    seeder's journal order. Complete locks must contain every repository. Live
    Git ancestry and tree verification belong to the seeder engine; this layer
    establishes the document and structural provenance contract first.
    """

    manifest = document.manifest
    if lock.manifest_sha256 != document.sha256:
        raise WorldError("world lock does not match the manifest digest")
    if lock.world_id != manifest.world_id:
        raise WorldError(
            f"world lock is for {lock.world_id!r}, expected {manifest.world_id!r}"
        )
    if lock.org != manifest.org:
        raise WorldError(f"world lock org is {lock.org!r}, expected {manifest.org!r}")
    if require_complete and lock.status != "complete":
        raise WorldError("world lock is partial")

    expected = manifest.repos
    actual = lock.repos
    if lock.status == "complete" and len(actual) != len(expected):
        raise WorldError("complete world lock does not contain every repository")
    if lock.status == "partial" and len(actual) == len(expected):
        raise WorldError("partial world lock already contains every repository")
    if len(actual) > len(expected):
        raise WorldError("world lock contains unexpected repositories")
    expected_prefix = expected[: len(actual)]
    if tuple(repo.name for repo in actual) != tuple(
        repo.name for repo in expected_prefix
    ):
        raise WorldError("world lock repositories are not the manifest prefix")

    fields = (
        "name",
        "upstream",
        "pin",
        "visibility",
        "default_branch",
        "seed_protocol",
        "duplicate_pin_of",
    )
    for intended, observed in zip(expected_prefix, actual, strict=True):
        for field in fields:
            if getattr(observed, field) != getattr(intended, field):
                raise WorldError(
                    f"world lock repository {observed.name!r} has a different "
                    f"{field} than the manifest"
                )
    return lock


def write_lock_atomic(path: Path, lock: WorldLock) -> None:
    """Write a complete JSON document and atomically replace the old lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = lock.model_dump(mode="json", exclude_none=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    """Persist the rename where directory fsync is supported."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
