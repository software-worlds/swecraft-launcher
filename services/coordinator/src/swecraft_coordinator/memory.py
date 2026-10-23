"""Seed and publish the agent's durable memory branch.

The coordinator reuses the shared `agent/memory` layout and swecraft-common's
trusted publish path (fresh clone, commit-tree, exact-lease push).
Publication here is mandatory: a wake whose memory cannot be published
fails loudly. A lease race with another writer is retried once by replaying
the agent's file changes onto the new head; a genuine conflict fails.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from swecraft_common import HubError
from swecraft_common.git import git_environment, run_git

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.applier import StaleSourceError, apply_and_push

logger = logging.getLogger(__name__)

GOAL_FILE = "GOAL.md"
NOTEBOOK_FILE = "notebook.md"

DEFAULT_GOAL = """# Goal

You maintain this repository. Keep it healthy: respond to issues and pull
requests, improve the code and its tests, and ship changes when they are
ready.

(Operator: edit this file on the memory branch to set a concrete charter.
Agent edits to this file are discarded.)
"""

NOTEBOOK_SEED = """# Notebook

Working notes for the maintainer agent. Organize this file however serves
your next wake best; it is yours.
"""

PROJECT_SEED = (
    "# Project context\n\n"
    "Slow-changing goals, constraints, and direction for this repository.\n"
    "Maintained by agents and humans; keep it short and current.\n"
)

MEMORY_SEED = "# Memory\n\n## Durable\n"


def ensure_memory_files(
    *,
    clone_url: str,
    branch: str,
    branch_exists: bool,
    token: str,
    author: str,
    goal: str | None,
) -> list[str]:
    """Create the branch if missing and add any missing coordinator files.

    Returns the file names added. Existing files are never overwritten, so
    re-onboarding is safe and an operator-edited GOAL.md is preserved.
    """

    seeds = {
        GOAL_FILE: goal or DEFAULT_GOAL,
        NOTEBOOK_FILE: NOTEBOOK_SEED,
        "PROJECT.md": PROJECT_SEED,
        "MEMORY.md": MEMORY_SEED,
    }
    workdir = Path(tempfile.mkdtemp(prefix="swecraft-coordinator-seed-"))
    try:
        env = git_environment(
            isolation_dir=workdir / "git-environment",
            token=token,
            remote_url=clone_url,
        )
        checkout = workdir / "memory"
        if branch_exists:
            _git(
                (
                    "clone",
                    "--depth=1",
                    "--branch",
                    branch,
                    clone_url,
                    str(checkout),
                ),
                cwd=workdir,
                env=env,
            )
        else:
            checkout.mkdir()
            _git(("init", "--initial-branch", branch), cwd=checkout, env=env)
        added = []
        for name, content in seeds.items():
            path = checkout / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")
                added.append(name)
        if not added:
            return []
        _git(("add", "--all"), cwd=checkout, env=env)
        _git(
            (
                "-c",
                f"user.name={author}",
                "-c",
                f"user.email={author}@localhost",
                "commit",
                "-m",
                f"coordinator: seed memory files ({', '.join(added)})",
            ),
            cwd=checkout,
            env=env,
        )
        _git(
            ("push", clone_url, f"HEAD:refs/heads/{branch}"),
            cwd=checkout,
            env=env,
        )
        return added
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def publish_memory(
    *,
    clone_url: str,
    branch: str,
    token: str,
    author: str,
    workspace: Path,
    input_memory: Path,
    source_sha: str,
    changed_files: tuple[str, ...],
    commit_message: str,
) -> str | None:
    """Publish the wake's memory edits; returns the commit SHA or None.

    GOAL.md is operator-owned: any agent edit to it is reverted before the
    commit, and a wake whose only edit was GOAL.md publishes nothing.
    """

    effective = _guard_goal(workspace, input_memory, changed_files)
    if not effective:
        return None
    try:
        outcome = apply_and_push(
            clone_url=clone_url,
            base_branch=branch,
            expected_sha=source_sha,
            repository=workspace,
            branch=None,
            commit_message=commit_message,
            author_name=author,
            author_email=f"{author}@localhost",
            token=token,
        )
        return outcome.commit_sha
    except StaleSourceError:
        logger.warning("memory branch moved during the wake; replaying once")
        return _replay_once(
            clone_url=clone_url,
            branch=branch,
            token=token,
            author=author,
            workspace=workspace,
            input_memory=input_memory,
            changed_files=effective,
            commit_message=commit_message,
        )
    except HubError as error:
        raise CoordinatorError(f"memory publish failed: {error}") from error


def _guard_goal(
    workspace: Path, input_memory: Path, changed_files: tuple[str, ...]
) -> tuple[str, ...]:
    if GOAL_FILE in changed_files:
        source = input_memory / GOAL_FILE
        target = workspace / GOAL_FILE
        if source.is_file():
            shutil.copyfile(source, target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        logger.warning("discarded agent edit to operator-owned %s", GOAL_FILE)
    return tuple(path for path in changed_files if path != GOAL_FILE)


def _replay_once(
    *,
    clone_url: str,
    branch: str,
    token: str,
    author: str,
    workspace: Path,
    input_memory: Path,
    changed_files: tuple[str, ...],
    commit_message: str,
) -> str | None:
    workdir = Path(tempfile.mkdtemp(prefix="swecraft-coordinator-replay-"))
    try:
        env = git_environment(
            isolation_dir=workdir / "git-environment",
            token=token,
            remote_url=clone_url,
        )
        checkout = workdir / "memory"
        _git(
            ("clone", "--depth=1", "--branch", branch, clone_url, str(checkout)),
            cwd=workdir,
            env=env,
        )
        head = _git(("rev-parse", "HEAD"), cwd=checkout, env=env).strip()
        identical = True
        for relative in changed_files:
            parts = PurePosixPath(relative).parts
            if not parts or ".." in parts or parts[0] == ".git":
                raise CoordinatorError(f"unsafe memory path: {relative!r}")
            agent_version = workspace.joinpath(*parts)
            input_version = input_memory.joinpath(*parts)
            current_version = checkout.joinpath(*parts)
            if _differs(current_version, input_version):
                raise CoordinatorError(
                    f"memory conflict on {relative}: the branch changed the "
                    "same file during the wake"
                )
            if _differs(current_version, agent_version):
                identical = False
            if agent_version.is_file() and not agent_version.is_symlink():
                current_version.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(agent_version, current_version)
            elif current_version.exists() or current_version.is_symlink():
                current_version.unlink()
        if identical:
            return None
        outcome = apply_and_push(
            clone_url=clone_url,
            base_branch=branch,
            expected_sha=head,
            repository=checkout,
            branch=None,
            commit_message=commit_message,
            author_name=author,
            author_email=f"{author}@localhost",
            token=token,
        )
        return outcome.commit_sha
    except HubError as error:
        raise CoordinatorError(f"memory publish failed after retry: {error}") from error
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _differs(left: Path, right: Path) -> bool:
    left_exists = left.is_file() and not left.is_symlink()
    right_exists = right.is_file() and not right.is_symlink()
    if left_exists != right_exists:
        return True
    if not left_exists:
        return left.exists() != right.exists()
    return left.read_bytes() != right.read_bytes()


def _git(arguments: tuple[str, ...], *, cwd: Path, env: dict[str, str]) -> str:
    """Run git through swecraft-common's hardened helper, in coordinator terms."""

    try:
        return run_git(arguments, cwd=cwd, env=env)
    except HubError as error:
        raise CoordinatorError(str(error)) from error
