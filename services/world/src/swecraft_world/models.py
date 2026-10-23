"""Pydantic models and structural invariants for worlds and seed locks."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
# Seed transforms, versioned. Both neutralize upstream GitHub workflows
# (they target github.com and would fire uselessly on the forge, which
# reads .github/workflows too). "strip" deletes them; "relocate" — the
# default — moves them to .github/workflows-upstream/ so agents keep the
# upstream CI as reference instead of spending wakes rebuilding it.
STRIP_PROTOCOL = "strip-github-actions-v1"
RELOCATE_PROTOCOL = "relocate-github-actions-v1"
SEED_PROTOCOL = RELOCATE_PROTOCOL
SeedProtocol = Literal["strip-github-actions-v1", "relocate-github-actions-v1"]
RepositoryVisibility = Literal["public", "private"]

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PinSpec(ProtocolModel):
    """An unambiguous source ref; the seeder later resolves it to a commit."""

    type: Literal["tag", "commit"]
    value: str

    @field_validator("value")
    @classmethod
    def valid_value(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("pin value must be non-empty and contain no whitespace")
        return value

    @model_validator(mode="after")
    def valid_for_type(self) -> Self:
        if self.type == "commit":
            if len(self.value) != 40 or any(
                character not in "0123456789abcdef" for character in self.value
            ):
                raise ValueError("commit pins must be full lowercase commit IDs")
        elif self.value.startswith("refs/"):
            raise ValueError("tag pins contain the tag name, not a refs/tags path")
        return self

    @property
    def ref(self) -> str:
        return f"refs/tags/{self.value}" if self.type == "tag" else self.value


class ManifestRepository(ProtocolModel):
    """One repository the society can see and maintain."""

    name: Name
    upstream: str
    pin: PinSpec
    visibility: RepositoryVisibility = "public"
    default_branch: str = "main"
    seed_protocol: SeedProtocol = SEED_PROTOCOL
    duplicate_pin_of: Name | None = None

    @field_validator("upstream", "default_branch")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("value must be non-empty and contain no whitespace")
        if value.startswith("-"):
            raise ValueError("value must not look like a command-line option")
        return value

    @field_validator("upstream")
    @classmethod
    def no_embedded_http_credentials(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https"):
            if not parts.netloc:
                raise ValueError("HTTP(S) upstream must be an absolute URL")
            if parts.username or parts.password:
                raise ValueError("upstream URL must not contain credentials")
        return value


class WorldManifest(ProtocolModel):
    """Human-authored intent for one reproducible society world."""

    schema_version: Literal[1] = SCHEMA_VERSION
    world_id: Name
    org: Name
    repos: tuple[ManifestRepository, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def repository_relationships(self) -> Self:
        by_name = {repository.name: repository for repository in self.repos}
        positions = {
            repository.name: position for position, repository in enumerate(self.repos)
        }
        if len(by_name) != len(self.repos):
            raise ValueError("manifest repository names must be unique")
        for repository in self.repos:
            target_name = repository.duplicate_pin_of
            if target_name is None:
                continue
            target = by_name.get(target_name)
            if target is None:
                raise ValueError(
                    f"{repository.name}: duplicate_pin_of references unknown "
                    f"repository {target_name}"
                )
            if target is repository:
                raise ValueError(
                    f"{repository.name}: duplicate_pin_of cannot reference itself"
                )
            if target.duplicate_pin_of is not None:
                raise ValueError(
                    f"{repository.name}: duplicate_pin_of must reference a primary"
                )
            if positions[target.name] >= positions[repository.name]:
                raise ValueError(
                    f"{repository.name}: duplicate-pin primary must appear first"
                )
        return self


class WorldLockRepository(ProtocolModel):
    """Seeder-observed provenance for one repository."""

    name: Name
    upstream: str
    pin: PinSpec
    visibility: RepositoryVisibility = "public"
    pin_ref: str
    # Provenance: the pin's own tag among the upstream tag history the
    # seed pushes (tag pins only; tags that descend from the pin are
    # withheld), all inside an Actions-disabled
    # window. None for commit pins and for worlds seeded before tags were
    # anchored — which is what keeps old worlds verifying. Seed-time
    # enforcement derives from the upstream cache's live tag set;
    # post-seed checks never constrain tags at all.
    seed_tag: str | None = None
    pin_commit: Sha
    pin_tree: Sha
    default_branch: str
    seed_protocol: SeedProtocol
    baseline_commit: Sha
    baseline_tree: Sha
    stripped_paths: tuple[str, ...]
    # Observed, not asserted. Seeding deliberately leaves Actions off for the
    # whole world and `swecraft-world enable-actions` turns it on afterwards, so a
    # freshly seeded lock records False and becomes True only once that
    # separate step has run. Requiring True here would have forced the seeder
    # to enable Actions while its own ref pushes were still being evaluated,
    # which is the race this split exists to remove.
    actions_enabled: bool
    duplicate_pin_of: Name | None = None
    current_head_at_adoption: Sha | None = None

    @field_validator("upstream", "pin_ref", "default_branch")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must be non-empty")
        return value

    @field_validator("stripped_paths")
    @classmethod
    def valid_stripped_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(paths))) != paths:
            raise ValueError("stripped_paths must be sorted and unique")
        for value in paths:
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.as_posix() != value
                or not value.startswith(".github/workflows/")
            ):
                raise ValueError(f"invalid stripped workflow path: {value!r}")
        return paths

    @model_validator(mode="after")
    def baseline_shape(self) -> Self:
        if self.seed_tag is not None and (
            self.pin.type != "tag" or self.seed_tag != self.pin.value
        ):
            raise ValueError(f"seed_tag {self.seed_tag!r} must be the tag pin value")
        if self.pin_ref != self.pin.ref:
            raise ValueError(
                f"pin_ref {self.pin_ref!r} does not match pin {self.pin.ref!r}"
            )
        has_transform = bool(self.stripped_paths)
        if has_transform == (self.baseline_commit == self.pin_commit):
            raise ValueError(
                "baseline commit must differ from the pin exactly when workflows "
                "were stripped"
            )
        if has_transform == (self.baseline_tree == self.pin_tree):
            raise ValueError(
                "baseline tree must differ from the pin exactly when workflows "
                "were stripped"
            )
        return self


class WorldLock(ProtocolModel):
    """Machine-written observation of a partial or complete seeded world."""

    schema_version: Literal[1] = SCHEMA_VERSION
    status: Literal["partial", "complete"]
    world_id: Name
    seeded_at: datetime | None = None
    adopted_at: datetime | None = None
    forge: str
    org: Name
    manifest_path: str
    manifest_sha256: Digest
    hub_commit: Sha
    repos: tuple[WorldLockRepository, ...] = Field(min_length=1)

    @field_validator("seeded_at", "adopted_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("lock timestamps must include a timezone")
        return value

    @field_validator("forge")
    @classmethod
    def http_forge_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("forge must be an absolute HTTP(S) URL")
        if parts.username or parts.password:
            raise ValueError("forge URL must not contain credentials")
        return value

    @field_validator("manifest_path")
    @classmethod
    def manifest_path_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("manifest_path must not be empty")
        return value

    @model_validator(mode="after")
    def lock_invariants(self) -> Self:
        if (self.seeded_at is None) == (self.adopted_at is None):
            raise ValueError("lock needs exactly one of seeded_at or adopted_at")
        by_name = {repository.name: repository for repository in self.repos}
        if len(by_name) != len(self.repos):
            raise ValueError("lock repository names must be unique")

        by_pin: dict[str, list[WorldLockRepository]] = defaultdict(list)
        for repository in self.repos:
            by_pin[repository.pin_commit].append(repository)
            target_name = repository.duplicate_pin_of
            if target_name is None:
                continue
            target = by_name.get(target_name)
            if target is None:
                raise ValueError(
                    f"{repository.name}: duplicate_pin_of target is absent from lock"
                )
            if target.duplicate_pin_of is not None:
                raise ValueError(
                    f"{repository.name}: duplicate_pin_of must reference a primary"
                )
            if target.pin_commit != repository.pin_commit:
                raise ValueError(
                    f"{repository.name}: duplicate_pin_of resolves to another commit"
                )

        for commit, repositories in by_pin.items():
            if len(repositories) == 1:
                continue
            primaries = [repo for repo in repositories if repo.duplicate_pin_of is None]
            if len(primaries) != 1:
                names = ", ".join(repo.name for repo in repositories)
                raise ValueError(
                    f"ambiguous duplicate pin commit {commit} across {names}"
                )
            primary = primaries[0]
            for repository in repositories:
                if (
                    repository is not primary
                    and repository.duplicate_pin_of != primary.name
                ):
                    raise ValueError(
                        f"{repository.name}: duplicate_pin_of must be {primary.name}"
                    )
        return self
