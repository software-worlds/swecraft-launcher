"""Directory-native private Hugging Face checkpoint transport."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from huggingface_hub import HfApi, snapshot_download

from swecraft_society.errors import SocietyError
from swecraft_society.inventory import verify_checkpoint


class HubApi(Protocol):
    def repo_info(self, **kwargs: Any) -> Any: ...

    def create_repo(self, **kwargs: Any) -> Any: ...

    def upload_folder(self, **kwargs: Any) -> Any: ...

    def create_tag(self, repo_id: str, **kwargs: Any) -> None: ...


Download = Callable[..., str]


@dataclass(frozen=True)
class HfLocation:
    repo_id: str
    revision: str | None = None

    @property
    def uri(self) -> str:
        suffix = f"@{self.revision}" if self.revision else ""
        return f"hf://{self.repo_id}{suffix}"


@dataclass(frozen=True)
class DownloadedCheckpoint:
    path: Path
    location: HfLocation
    resolved_revision: str


@dataclass(frozen=True)
class PublishedCheckpoint:
    location: HfLocation
    resolved_revision: str


class HuggingFaceTransport:
    def __init__(
        self,
        *,
        api: HubApi | None = None,
        download: Download = snapshot_download,
    ) -> None:
        self.api = api or HfApi()
        self.download = download

    def fetch(self, location: HfLocation, *, cache_dir: Path) -> DownloadedCheckpoint:
        try:
            info = self.api.repo_info(
                repo_id=location.repo_id,
                repo_type="dataset",
                revision=location.revision,
            )
            revision = getattr(info, "sha", None)
            if not isinstance(revision, str) or not revision:
                raise SocietyError(
                    "Hugging Face returned no resolved repository commit"
                )
            snapshot = self.download(
                location.repo_id,
                repo_type="dataset",
                revision=revision,
                cache_dir=cache_dir,
            )
        except Exception as error:
            raise SocietyError(
                f"could not download private dataset {location.uri}: {error}"
            ) from None
        path = _materialize_snapshot(
            Path(snapshot),
            cache_dir,
            identity=f"{location.repo_id}@{revision}",
        )
        return DownloadedCheckpoint(
            path=path,
            location=location,
            resolved_revision=revision,
        )

    def publish(
        self,
        checkpoint: Path,
        *,
        repo_id: str,
        private: bool,
    ) -> PublishedCheckpoint:
        if not private:
            raise SocietyError("society checkpoints may only be published privately")
        manifest = verify_checkpoint(checkpoint)
        try:
            self.api.create_repo(
                repo_id=repo_id,
                repo_type="dataset",
                private=private,
                exist_ok=True,
            )
            info = self.api.repo_info(repo_id=repo_id, repo_type="dataset")
            if getattr(info, "private", None) is not True:
                raise SocietyError(
                    f"refusing to upload checkpoint to public dataset hf://{repo_id}"
                )
            commit = self.api.upload_folder(
                repo_id=repo_id,
                repo_type="dataset",
                revision="main",
                folder_path=checkpoint,
                delete_patterns="*",
                commit_message=f"Checkpoint {manifest.checkpoint_id}",
            )
            revision = getattr(commit, "oid", None)
            if not isinstance(revision, str) or not revision:
                raise SocietyError("Hugging Face upload returned no commit identity")
            uploaded = self.api.repo_info(
                repo_id=repo_id,
                repo_type="dataset",
                revision=revision,
            )
            _assert_remote_inventory(checkpoint, uploaded)
            self.api.create_tag(
                repo_id,
                repo_type="dataset",
                tag=manifest.checkpoint_id,
                revision=revision,
                tag_message=f"Society checkpoint {manifest.checkpoint_id}",
                exist_ok=True,
            )
        except Exception as error:
            raise SocietyError(
                f"could not publish checkpoint to hf://{repo_id}: {error}"
            ) from None
        return PublishedCheckpoint(
            location=HfLocation(repo_id=repo_id),
            resolved_revision=revision,
        )


def _assert_remote_inventory(checkpoint: Path, info: Any) -> None:
    siblings = getattr(info, "siblings", None)
    if not isinstance(siblings, list):
        raise SocietyError("Hugging Face returned no uploaded file inventory")
    remote = {
        name
        for sibling in siblings
        if isinstance((name := getattr(sibling, "rfilename", None)), str)
    }
    local = {
        path.relative_to(checkpoint).as_posix()
        for path in checkpoint.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    missing = sorted(local - remote)
    if missing:
        preview = ", ".join(missing[:5])
        suffix = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        raise SocietyError(
            f"uploaded commit is missing {len(missing)} checkpoint file(s): "
            f"{preview}{suffix}"
        )


def _materialize_snapshot(snapshot: Path, cache_dir: Path, *, identity: str) -> Path:
    """Dereference an HF cache snapshot into an atomic, symlink-free tree."""

    snapshot = snapshot.expanduser().resolve()
    cache_dir = cache_dir.expanduser().resolve()
    if not snapshot.is_dir():
        raise SocietyError(f"downloaded HF snapshot is not a directory: {snapshot}")
    digest = hashlib.sha256(identity.encode()).hexdigest()
    parent = cache_dir / "swecraft-society-materialized"
    destination = parent / digest
    parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise SocietyError(f"invalid HF materialization cache entry: {destination}")
    if destination.is_dir():
        return destination

    stage = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=parent))
    try:
        _copy_snapshot(snapshot, stage, cache_dir)
        try:
            os.rename(stage, destination)
        except OSError:
            # Another fetch may have atomically installed the same immutable
            # revision while this process copied it.
            if not destination.is_dir() or destination.is_symlink():
                raise
            shutil.rmtree(stage, ignore_errors=True)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return destination


def _copy_snapshot(snapshot: Path, destination: Path, cache_dir: Path) -> None:
    for directory, directories, filenames in os.walk(
        snapshot, topdown=True, followlinks=False
    ):
        source_directory = Path(directory)
        relative = source_directory.relative_to(snapshot)
        target_directory = destination / relative
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in directories:
            source = source_directory / name
            if source.is_symlink() or not source.is_dir():
                raise SocietyError(
                    f"HF snapshot contains an unsupported directory entry: {source}"
                )
            (target_directory / name).mkdir(exist_ok=True)
        for name in filenames:
            source = source_directory / name
            copied_from = source
            if source.is_symlink():
                try:
                    copied_from = source.resolve(strict=True)
                except OSError as error:
                    raise SocietyError(
                        f"HF snapshot contains a broken symlink {source}: {error}"
                    ) from None
                if not copied_from.is_relative_to(cache_dir):
                    raise SocietyError(
                        f"HF snapshot symlink escapes its cache: {source}"
                    )
            if not copied_from.is_file():
                raise SocietyError(f"HF snapshot contains a non-regular file: {source}")
            shutil.copy2(copied_from, target_directory / name)


def parse_hf_uri(value: str) -> HfLocation:
    """Parse only an HF dataset root, never a path to an archive within it."""

    if not value.startswith("hf://"):
        raise SocietyError(f"not a Hugging Face dataset URI: {value!r}")
    raw = value.removeprefix("hf://")
    repo_id, separator, revision = raw.partition("@")
    if repo_id.count("/") != 1 or not all(repo_id.split("/")):
        raise SocietyError("HF checkpoint URI must be hf://owner/dataset[@revision]")
    if any(character.isspace() for character in repo_id):
        raise SocietyError("HF checkpoint repository must not contain whitespace")
    if separator and (not revision or "@" in revision):
        raise SocietyError("HF checkpoint revision is empty or malformed")
    return HfLocation(repo_id=repo_id, revision=revision or None)


def record_load_provenance(dest: Path, downloaded: DownloadedCheckpoint) -> Path:
    """Record the resolved remote commit beside, not inside, the loaded copy."""

    path = dest.parent / f"{dest.name}.load.json"
    payload = {
        "source": downloaded.location.uri,
        "resolved_revision": downloaded.resolved_revision,
        "loaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path
