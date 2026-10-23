"""Build and push a commit from agent files using trusted Git metadata."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from swecraft_common import HubError
from swecraft_common.git import git_environment, run_git


class StaleSourceError(HubError):
    """The base branch moved after the run resolved its source commit."""


@dataclass(frozen=True)
class ApplyOutcome:
    branch: str
    commit_sha: str


def apply_and_push(
    *,
    clone_url: str,
    base_branch: str,
    expected_sha: str,
    repository: Path,
    branch: str | None,
    commit_message: str,
    author_name: str,
    author_email: str,
    token: str | None = None,
) -> ApplyOutcome:
    """Commit the complete agent worktree against a fresh, matching base.

    The agent-produced ``.git`` directory is never used. A fresh clone supplies
    trusted Git metadata, while ``repository`` is attached only as GIT_WORK_TREE.
    Git therefore owns tree construction without running normal commit hooks or
    replaying paths in Python. With ``branch=None``, the commit is pushed to
    ``base_branch`` itself with an exact lease against ``expected_sha`` for a
    same-repository pull request update.
    """

    agent_worktree = repository.expanduser()
    if not agent_worktree.is_dir() or agent_worktree.is_symlink():
        raise HubError(f"agent repository is not a directory: {agent_worktree}")
    agent_worktree = agent_worktree.resolve()
    _validate_worktree(agent_worktree)

    workdir = Path(tempfile.mkdtemp(prefix="swecraft-apply-"))
    trusted = workdir / "trusted"
    try:
        authenticated_env = git_environment(
            isolation_dir=workdir / "git-environment",
            token=token,
            remote_url=clone_url,
        )
        run_git(
            (
                "clone",
                "--no-checkout",
                "--depth=1",
                "--branch",
                base_branch,
                clone_url,
                str(trusted),
            ),
            cwd=workdir,
            env=authenticated_env,
        )
        head = run_git(
            ("rev-parse", "HEAD"), cwd=trusted, env=authenticated_env
        ).strip()
        if head != expected_sha:
            raise StaleSourceError(
                f"{base_branch} moved from {expected_sha[:12]} to {head[:12]} "
                "during the run"
            )

        index_env = {
            **authenticated_env,
            "GIT_DIR": str(trusted / ".git"),
            "GIT_WORK_TREE": str(agent_worktree),
        }
        run_git(("read-tree", expected_sha), cwd=workdir, env=index_env)
        run_git(("add", "--all", "--", ":/"), cwd=workdir, env=index_env)
        _validate_index(workdir, index_env)

        tree = run_git(("write-tree",), cwd=workdir, env=index_env).strip()
        base_tree = run_git(
            ("rev-parse", f"{expected_sha}^{{tree}}"), cwd=workdir, env=index_env
        ).strip()
        if tree == base_tree:
            raise HubError("agent repository contains no committable changes")

        commit_env = {
            **index_env,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        commit_sha = run_git(
            ("commit-tree", tree, "-p", expected_sha, "-m", commit_message),
            cwd=workdir,
            env=commit_env,
        ).strip()
        push_ref = branch if branch is not None else base_branch
        lease = (
            (f"--force-with-lease=refs/heads/{push_ref}:{expected_sha}",)
            if branch is None
            else ()
        )
        run_git(
            ("push", *lease, "origin", f"{commit_sha}:refs/heads/{push_ref}"),
            cwd=trusted,
            env=authenticated_env,
        )
        return ApplyOutcome(branch=push_ref, commit_sha=commit_sha)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _validate_worktree(root: Path) -> None:
    """Reject entries that Git cannot publish as ordinary repository content."""

    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root)
                if entry.name.casefold() == ".git":
                    if current == root and entry.name == ".git":
                        continue
                    raise HubError(
                        f"agent repository contains nested Git metadata: {relative}"
                    )
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    pending.append(path)
                elif not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    raise HubError(
                        "agent repository contains unsupported special file: "
                        f"{relative}"
                    )


def _validate_index(cwd: Path, env: dict[str, str]) -> None:
    for entry in run_git(("ls-files", "--stage"), cwd=cwd, env=env).splitlines():
        fields = entry.split(maxsplit=3)
        if len(fields) < 3:
            raise HubError(f"Git returned an invalid index entry: {entry!r}")
        if fields[0] == "160000":
            path = fields[3] if len(fields) == 4 else "unknown"
            raise HubError(f"agent repository contains unsupported gitlink: {path}")
