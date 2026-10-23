"""Read and validate repositories inside a frozen Forge volume tar."""

from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from swecraft_common import HubError
from swecraft_common.git import git_environment, run_git

from swecraft_forge.errors import ForgeRuntimeError

_SHA = re.compile(r"^[0-9a-f]{40}$")


def read_heads(
    volume_tar: Path, org: str, branches: Mapping[str, str]
) -> dict[str, str]:
    """Resolve each requested branch directly from loose or packed refs."""

    wanted = _wanted(org, branches)
    bases: dict[str, str] = {}
    contents: dict[str, bytes] = {}
    try:
        with tarfile.open(volume_tar, mode="r:*") as archive:
            for member in archive:
                normalized = _normalized(member.name)
                _discover_bases(normalized, wanted, bases)
                if not member.isfile():
                    continue
                if normalized.endswith("/packed-refs") or "/refs/heads/" in normalized:
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        contents[normalized] = extracted.read()
    except (tarfile.TarError, OSError) as error:
        raise ForgeRuntimeError(
            f"could not read volume tar {volume_tar}: {error}"
        ) from None

    heads: dict[str, str] = {}
    for name, branch in branches.items():
        base = bases.get(name)
        if base is None:
            raise ForgeRuntimeError(
                f"repository {org}/{name} is not inside the captured volume"
            )
        heads[name] = _branch_head(name, branch, base, contents)
    return heads


def verify_lineage(
    volume_tar: Path,
    org: str,
    baselines: Mapping[str, str],
    heads: Mapping[str, str],
) -> None:
    """Prove every locked baseline exists and is an ancestor of captured HEAD.

    Repository names and refs alone can accidentally bind an experiment lock
    to another Compose project's volume. Git object ancestry ties the frozen
    bytes back to the seeder-authored baseline vector.
    """

    if set(baselines) != set(heads):
        raise ForgeRuntimeError("baseline and head repository sets differ")
    for value in (*baselines.values(), *heads.values()):
        if not _SHA.fullmatch(value):
            raise ForgeRuntimeError("repository lineage contains an invalid commit ID")

    wanted = _wanted(org, baselines)
    bases: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="swecraft-forge-repositories-") as raw:
        extraction = Path(raw)
        git_env = git_environment(isolation_dir=extraction / ".git-environment")
        seen: set[tuple[str, str]] = set()
        try:
            with tarfile.open(volume_tar, mode="r:*") as archive:
                for member in archive:
                    normalized = _normalized(member.name)
                    _discover_bases(normalized, wanted, bases)
                    for name, base in tuple(bases.items()):
                        if normalized != base and not normalized.startswith(f"{base}/"):
                            continue
                        relative = PurePosixPath(normalized).relative_to(
                            PurePosixPath(base)
                        )
                        key = (name, relative.as_posix())
                        if key in seen:
                            continue
                        seen.add(key)
                        target = extraction / f"{name}.git" / Path(*relative.parts)
                        if member.isdir():
                            target.mkdir(parents=True, exist_ok=True)
                        elif member.isfile():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            source = archive.extractfile(member)
                            if source is None:
                                raise ForgeRuntimeError(
                                    f"could not extract repository member {normalized}"
                                )
                            with target.open("wb") as stream:
                                shutil.copyfileobj(source, stream)
                        else:
                            raise ForgeRuntimeError(
                                f"repository {org}/{name} contains a non-regular "
                                f"archive member: {normalized}"
                            )
        except (tarfile.TarError, OSError, ValueError) as error:
            if isinstance(error, ForgeRuntimeError):
                raise
            raise ForgeRuntimeError(
                f"could not extract repositories from {volume_tar}: {error}"
            ) from None

        missing = sorted(set(baselines) - set(bases))
        if missing:
            raise ForgeRuntimeError(
                "captured volume is missing repositories: " + ", ".join(missing)
            )
        for name, baseline in baselines.items():
            repository = extraction / f"{name}.git"
            head = heads[name]
            _git(
                repository,
                ("cat-file", "-e", f"{baseline}^{{commit}}"),
                f"locked baseline for {org}/{name} is absent from the Forge volume",
                git_env,
            )
            _git(
                repository,
                ("cat-file", "-e", f"{head}^{{commit}}"),
                f"captured head for {org}/{name} is absent from the Forge volume",
                git_env,
            )
            _git(
                repository,
                ("merge-base", "--is-ancestor", baseline, head),
                f"captured head for {org}/{name} does not descend from its baseline",
                git_env,
            )


def _wanted(org: str, repositories: Mapping[str, object]) -> dict[str, str]:
    return {
        f"repositories/{org.lower()}/{name.lower()}.git": name for name in repositories
    }


def _normalized(value: str) -> str:
    normalized = value.removeprefix("./").rstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ForgeRuntimeError(f"unsafe Forge volume member: {value!r}")
    return path.as_posix()


def _discover_bases(
    normalized: str, wanted: Mapping[str, str], bases: dict[str, str]
) -> None:
    probe = f"/{normalized}/"
    for suffix, name in wanted.items():
        if name in bases:
            continue
        index = probe.find(f"/{suffix}/")
        if index >= 0:
            bases[name] = probe[1 : index + len(suffix) + 1]


def _branch_head(
    name: str, branch: str, base: str, contents: Mapping[str, bytes]
) -> str:
    loose = contents.get(f"{base}/refs/heads/{branch}")
    if loose is not None:
        value = loose.decode("ascii", "replace").strip()
        if not _SHA.fullmatch(value):
            raise ForgeRuntimeError(
                f"{name}: loose head for {branch} is not a commit id"
            )
        return value
    packed = contents.get(f"{base}/packed-refs")
    if packed is not None:
        for line in packed.decode("ascii", "replace").splitlines():
            line = line.strip()
            if line.startswith("#") or line.startswith("^"):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
                if not _SHA.fullmatch(parts[0]):
                    raise ForgeRuntimeError(
                        f"{name}: packed head for {branch} is not a commit id"
                    )
                return parts[0]
    raise ForgeRuntimeError(
        f"{name}: branch {branch} has no head in the captured volume"
    )


def _git(
    repository: Path,
    arguments: tuple[str, ...],
    error: str,
    environment: dict[str, str],
) -> None:
    try:
        run_git(arguments, cwd=repository, env=environment, timeout_seconds=60)
    except HubError as failure:
        raise ForgeRuntimeError(f"{error}: {failure}") from None
