"""Hub and apparatus Git provenance without repository mutation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from swecraft_common import HubError
from swecraft_common.git import git_environment, hub_checkout_head, run_git

from swecraft_society.errors import SocietyError


def repository_head() -> str:
    """Return the producer Hub checkout commit."""

    try:
        return hub_checkout_head(Path(__file__))
    except HubError as error:
        raise SocietyError(f"could not record the Hub revision: {error}") from error


def apparatus_git_provenance(
    apparatus_root: Path, captured_paths: tuple[Path, ...]
) -> tuple[str | None, str | None, bool]:
    commit = _git(("rev-parse", "HEAD"), apparatus_root, required=False)
    if commit is None:
        return None, None, False
    ref = _git(("symbolic-ref", "--quiet", "--short", "HEAD"), apparatus_root)
    try:
        relative = tuple(
            path.resolve().relative_to(apparatus_root.resolve())
            for path in captured_paths
        )
    except ValueError:
        return ref, commit, True
    status = _git(
        ("status", "--porcelain", "--", *(str(path) for path in relative)),
        apparatus_root,
        required=True,
    )
    return ref, commit, bool(status)


def _git(
    arguments: tuple[str, ...], cwd: Path, *, required: bool = False
) -> str | None:
    with tempfile.TemporaryDirectory(prefix="swecraft-society-git-") as raw:
        environment = git_environment(isolation_dir=Path(raw) / ".git-environment")
        try:
            value = run_git(arguments, cwd=cwd, env=environment).strip()
        except HubError as error:
            if not required:
                return None
            raise SocietyError(
                f"could not run git {' '.join(arguments)} in {cwd}: {error}"
            ) from None
    return value or None
