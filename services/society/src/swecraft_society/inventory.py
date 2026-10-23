"""Build, write, and semantically verify checkpoint inventories."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError
from swecraft_forge import ForgeRuntimeError
from swecraft_forge.frozen import read_heads, verify_lineage
from swecraft_world.errors import WorldError
from swecraft_world.io import load_lock, validate_lock
from swecraft_world.io import load_manifest as load_world_manifest

from swecraft_society.errors import SocietyError
from swecraft_society.layout import (
    APPARATUS_DIR,
    CARD_NAME,
    EVALUATION_MANIFEST,
    FORGE_VOLUME_TAR,
    MANIFEST_NAME,
    WORLD_LOCK,
    WORLD_MANIFEST,
    render_card,
)
from swecraft_society.models import CheckpointManifest, FileEntry

_CHUNK = 1 << 20


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def inventory_digest(entries: Iterable[FileEntry]) -> str:
    """Digest an ordered inventory independently of its containing manifest."""

    payload = [
        {"path": entry.path, "sha256": entry.sha256, "size": entry.size}
        for entry in sorted(entries, key=lambda item: item.path)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def collect_entries(root: Path, subpath: str) -> tuple[FileEntry, ...]:
    """Inventory one staged file or directory from the checkpoint root."""

    target = root / subpath
    if target.is_symlink():
        raise SocietyError(
            f"checkpoints hold regular files only; {subpath} is a symlink"
        )
    if target.is_file():
        paths = [target]
    elif target.is_dir():
        paths = []
        for path in sorted(target.rglob("*")):
            if path.is_symlink():
                raise SocietyError(
                    "checkpoints hold regular files only; "
                    f"{path.relative_to(root).as_posix()} is a symlink"
                )
            if path.is_file():
                paths.append(path)
    else:
        raise SocietyError(f"staged checkpoint content is missing: {subpath}")
    entries = []
    for path in paths:
        sha256, size = hash_file(path)
        entries.append(
            FileEntry(path=path.relative_to(root).as_posix(), sha256=sha256, size=size)
        )
    return tuple(entries)


def write_manifest(root: Path, manifest: CheckpointManifest) -> None:
    """Atomically write ``manifest.json`` and its generated dataset card."""

    payload = manifest.model_dump(mode="json", exclude_none=True)
    # A complete checkpoint's manifest stays byte-compatible with readers
    # that predate partial checkpoints; only a genuinely partial one
    # carries the omissions field.
    if not payload.get("omissions"):
        payload.pop("omissions", None)
    _write_atomic(
        root / MANIFEST_NAME,
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
    )
    _write_atomic(root / CARD_NAME, render_card(manifest))


def load_manifest(root: Path) -> CheckpointManifest:
    path = root / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SocietyError(
            f"could not read checkpoint manifest {path}: {error}"
        ) from None
    except json.JSONDecodeError as error:
        raise SocietyError(f"invalid checkpoint manifest {path}: {error}") from None
    if isinstance(payload, dict) and payload.get("schema_version") == 1:
        raise SocietyError(
            f"checkpoint manifest {path} uses schema 1, which predates the "
            "evaluation.lock.json removal; load this checkpoint with "
            "a checkout from before that change"
        )
    try:
        return CheckpointManifest.model_validate(payload)
    except ValidationError as error:
        raise SocietyError(f"invalid checkpoint manifest {path}: {error}") from None


# The only path capture may degrade on: the regenerable evaluation manifest.
# Recovery essentials are never omissible.
OMISSIBLE_PATHS = frozenset({EVALUATION_MANIFEST})


def verify_checkpoint(root: Path) -> CheckpointManifest:
    """Prove byte-level inventory and embedded provenance agree.

    A partial checkpoint states its omissions in the manifest; the
    invariant verified here is that everything *listed* is intact and
    bound, never that everything imaginable is present.
    """

    manifest = load_manifest(root)
    listed = {entry.path: entry for entry in manifest.files}
    omitted = set(manifest.omissions)
    if omitted and omitted != OMISSIBLE_PATHS:
        raise SocietyError(
            "checkpoint omissions may only be the evaluation manifest, "
            f"got: {', '.join(sorted(omitted))}"
        )
    for required in (
        WORLD_MANIFEST,
        WORLD_LOCK,
        EVALUATION_MANIFEST,
        FORGE_VOLUME_TAR,
    ):
        if required not in listed and required not in omitted:
            raise SocietyError(f"checkpoint inventory does not list {required}")

    for entry in listed.values():
        path = root / entry.path
        if path.is_symlink() or not path.is_file():
            raise SocietyError(
                f"checkpoint file is missing or not regular: {entry.path}"
            )
        sha256, size = hash_file(path)
        if size != entry.size or sha256 != entry.sha256:
            raise SocietyError(
                f"checkpoint file does not match its manifest: {entry.path}"
            )

    apparatus_entries = tuple(
        entry for entry in listed.values() if entry.path.startswith(f"{APPARATUS_DIR}/")
    )
    if inventory_digest(apparatus_entries) != manifest.apparatus.sha256:
        raise SocietyError("apparatus inventory does not match its manifest digest")
    if listed[WORLD_LOCK].sha256 != manifest.world_lock_sha256:
        raise SocietyError(
            f"{WORLD_LOCK} does not hash to the manifest's world_lock_sha256"
        )
    _verify_provenance(root, manifest, listed)

    card = root / CARD_NAME
    if card.is_symlink() or not card.is_file():
        raise SocietyError(f"checkpoint is missing its generated {CARD_NAME}")
    try:
        card_content = card.read_text(encoding="utf-8")
    except OSError as error:
        raise SocietyError(f"could not read checkpoint card {card}: {error}") from None
    if card_content != render_card(manifest):
        raise SocietyError(f"generated {CARD_NAME} does not match the manifest")

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SocietyError(
                f"checkpoints hold regular files only; {relative} is a symlink"
            )
        if not path.is_file():
            continue
        if relative in (MANIFEST_NAME, CARD_NAME) or relative in listed:
            continue
        raise SocietyError(
            f"checkpoint contains a file the manifest never listed: {relative}"
        )
    return manifest


# Keep the prototype name internal callers used while the open stack is restacked.
verify_bundle = verify_checkpoint


def _verify_provenance(
    root: Path, manifest: CheckpointManifest, listed: dict[str, FileEntry]
) -> None:
    try:
        world_document = load_world_manifest(root / WORLD_MANIFEST)
        world_lock = validate_lock(
            world_document, load_lock(root / WORLD_LOCK), require_complete=True
        )
    except WorldError as error:
        raise SocietyError(f"invalid embedded world provenance: {error}") from None
    if manifest.world_id != world_lock.world_id:
        raise SocietyError(
            f"manifest world {manifest.world_id!r} does not match the embedded lock"
        )

    expected_names = tuple(repository.name for repository in world_lock.repos)
    observed_names = tuple(head.name for head in manifest.heads)
    if observed_names != expected_names:
        raise SocietyError(
            "manifest repository heads do not match the world lock order"
        )
    for head, repository in zip(manifest.heads, world_lock.repos, strict=True):
        if head.baseline_commit != repository.baseline_commit:
            raise SocietyError(
                f"manifest baseline for {head.name} does not match the world lock"
            )

    try:
        current = read_heads(
            root / FORGE_VOLUME_TAR,
            world_lock.org,
            {
                repository.name: repository.default_branch
                for repository in world_lock.repos
            },
        )
        verify_lineage(
            root / FORGE_VOLUME_TAR,
            world_lock.org,
            {
                repository.name: repository.baseline_commit
                for repository in world_lock.repos
            },
            current,
        )
    except ForgeRuntimeError as error:
        raise SocietyError(f"invalid frozen Forge state: {error}") from None
    for head in manifest.heads:
        if current[head.name] != head.current_head:
            raise SocietyError(
                f"manifest head for {head.name} does not match the frozen Forge volume"
            )


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
