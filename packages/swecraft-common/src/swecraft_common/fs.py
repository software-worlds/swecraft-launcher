"""Filesystem helpers shared by Hub services."""

from __future__ import annotations

import shutil
from collections.abc import Container
from pathlib import Path

from swecraft_common import HubError


def resolve_path(root: Path, path: Path) -> Path:
    """Resolve ``path`` against ``root`` unless it is already absolute."""

    return (
        path.expanduser().resolve() if path.is_absolute() else (root / path).resolve()
    )


def copy_regular_tree(
    source: Path,
    destination: Path,
    *,
    label: str,
    ignore: Container[str] = (),
    prune: Container[str] = (),
) -> None:
    """Copy regular files from ``source`` while rejecting symlinks.

    ``ignore`` matches file names or source-relative POSIX paths, and an
    ignored **directory** prunes its whole subtree -- naming a directory and
    still copying everything inside it would surprise every caller. Symlinks
    are rejected before ignore rules are applied so ignored names cannot hide
    them. ``prune`` is the deliberate exception: a pruned directory is not
    walked at all, symlinks included -- for subtrees that are reproducible
    by construction (an evaluation record's injected ``sources/`` is a
    checkout of commits the record names) and may legitimately carry
    symlinks the checkpoint must never see. Empty directories are
    intentionally not materialized, matching the previous service-local
    checkpoint copy behavior.
    """

    if source.is_symlink():
        raise HubError(f"{label} contains a symlink: {source}")
    if not source.is_dir():
        raise HubError(f"{label} is not a directory: {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if prune and _is_ignored(relative, prune):
            continue
        if path.is_symlink():
            raise HubError(f"{label} contains a symlink: {path}")
        if _is_ignored(relative, ignore):
            continue
        if not path.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _is_ignored(relative: Path, ignore: Container[str]) -> bool:
    """Whether a source-relative path is ignored, directly or by an ancestor."""

    if relative.name in ignore or relative.as_posix() in ignore:
        return True
    return any(
        parent.name in ignore or parent.as_posix() in ignore
        for parent in relative.parents
        if parent.as_posix() != "."
    )
