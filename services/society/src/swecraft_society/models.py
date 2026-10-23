"""Pydantic models for the society checkpoint manifest."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from swecraft_world.models import Digest, Name, Sha

SCHEMA_VERSION = 2
RESERVED_PATHS = ("manifest.json", "README.md")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or value != path.as_posix()
        or path.is_absolute()
        or "\\" in value
        or ".." in path.parts
    ):
        raise ValueError(f"unsafe checkpoint path: {value!r}")
    return value


def _nonempty_no_whitespace(value: str) -> str:
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("value must be non-empty and contain no whitespace")
    return value


class FileEntry(ProtocolModel):
    """One captured regular file, addressed from the checkpoint root."""

    path: str
    sha256: Digest
    size: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return _safe_relative_path(value)


class ContributorRecord(ProtocolModel):
    """The files one capture adapter contributed to the checkpoint."""

    name: Name
    files: tuple[FileEntry, ...]

    @field_validator("files")
    @classmethod
    def sorted_unique_paths(cls, files: tuple[FileEntry, ...]) -> tuple[FileEntry, ...]:
        paths = [entry.path for entry in files]
        if sorted(set(paths)) != paths:
            raise ValueError("contributor files must be sorted and unique by path")
        return files


class ApparatusInfo(ProtocolModel):
    """Provenance for the exact apparatus bytes carried by the checkpoint."""

    sha256: Digest
    git_ref: str | None = None
    git_commit: Sha | None = None
    dirty: bool = False

    @field_validator("git_ref")
    @classmethod
    def valid_ref(cls, value: str | None) -> str | None:
        return None if value is None else _nonempty_no_whitespace(value)


class ImageIdentity(ProtocolModel):
    """The locally observed agent sandbox image."""

    reference: str
    image_id: str
    platform: str

    @field_validator("reference", "platform")
    @classmethod
    def valid_text(cls, value: str) -> str:
        return _nonempty_no_whitespace(value)

    @field_validator("image_id")
    @classmethod
    def valid_image_id(cls, value: str) -> str:
        if not _IMAGE_ID.fullmatch(value):
            raise ValueError("image_id must be a sha256 Docker image ID")
        return value


class RepoHead(ProtocolModel):
    """One repository's seed baseline and head in the frozen Forge volume."""

    name: Name
    baseline_commit: Sha
    current_head: Sha


class ForgeInfo(ProtocolModel):
    """The standard Hub Forge observed at checkpoint time."""

    source_url: str
    compose_project: str
    image: str
    volume: str

    @field_validator("compose_project", "image", "volume")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        return _nonempty_no_whitespace(value)

    @field_validator("source_url")
    @classmethod
    def valid_source_url(cls, value: str) -> str:
        value = _nonempty_no_whitespace(value).rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("source_url must be an HTTP(S) URL")
        return value


class CheckpointManifest(ProtocolModel):
    """The versioned, self-describing record of one society checkpoint.

    A checkpoint is a recovery artifact first and a research dataset
    second: capture degrades on absent research metadata instead of
    refusing to save recoverable state. ``omissions`` lists the expected
    paths this capture could not include — verification then proves
    *everything listed is intact* rather than *everything imaginable is
    present*, and the gap stays visible instead of implied.
    """

    schema_version: Literal[2] = SCHEMA_VERSION
    checkpoint_id: Name
    created_at: datetime
    hub_commit: Sha
    apparatus: ApparatusInfo
    world_id: Name
    world_lock_sha256: Digest
    agent_image: ImageIdentity
    forge: ForgeInfo
    heads: tuple[RepoHead, ...] = Field(min_length=1)
    contributors: tuple[ContributorRecord, ...] = Field(min_length=1)
    omissions: tuple[str, ...] = ()

    @field_validator("omissions")
    @classmethod
    def valid_omissions(cls, omissions: tuple[str, ...]) -> tuple[str, ...]:
        for value in omissions:
            _safe_relative_path(value)
        if sorted(set(omissions)) != list(omissions):
            raise ValueError("omissions must be sorted and unique")
        return omissions

    @field_validator("created_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def checkpoint_invariants(self) -> Self:
        head_names = [head.name for head in self.heads]
        if len(set(head_names)) != len(head_names):
            raise ValueError("head repository names must be unique")
        contributor_names = [contributor.name for contributor in self.contributors]
        if len(set(contributor_names)) != len(contributor_names):
            raise ValueError("contributor names must be unique")
        seen: set[str] = set()
        for contributor in self.contributors:
            for entry in contributor.files:
                if entry.path in RESERVED_PATHS:
                    raise ValueError(
                        f"{entry.path} is reserved for the checkpoint root"
                    )
                if entry.path in seen:
                    raise ValueError(f"file path claimed twice: {entry.path}")
                seen.add(entry.path)
        for omitted in self.omissions:
            if omitted in seen:
                raise ValueError(f"omitted path was also captured: {omitted}")
        return self

    @property
    def partial(self) -> bool:
        return bool(self.omissions)

    @property
    def files(self) -> tuple[FileEntry, ...]:
        return tuple(
            entry for contributor in self.contributors for entry in contributor.files
        )
