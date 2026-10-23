from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swecraft_common import HubError

import swecraft_coordinator.applier as applier_module
from swecraft_coordinator.applier import (
    StaleSourceError,
    _validate_index,
    apply_and_push,
)


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ("git", *arguments), cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def seed_origin(root: Path) -> tuple[str, str]:
    """Create a bare origin with one commit on main; return (URL, head SHA)."""

    bare = root / "forge" / "hub" / "demo.git"
    bare.parent.mkdir(parents=True)
    git("init", "--bare", "--initial-branch=main", str(bare), cwd=root)
    seed = root / "seed"
    git("clone", f"file://{bare}", str(seed), cwd=root)
    git("config", "user.name", "Seed", cwd=seed)
    git("config", "user.email", "seed@localhost", cwd=seed)
    (seed / "README.md").write_text("original\n", encoding="utf-8")
    (seed / "delete-me.txt").write_text("obsolete\n", encoding="utf-8")
    (seed / "cfg").mkdir()
    (seed / "cfg" / "old.txt").write_text("old\n", encoding="utf-8")
    (seed / "single").write_text("file\n", encoding="utf-8")
    git("add", ".", cwd=seed)
    git("commit", "-m", "seed", cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return f"file://{bare}", git("rev-parse", "HEAD", cwd=seed)


class ApplierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.bare = self.root / "forge" / "hub" / "demo.git"
        self.clone_url, self.head = seed_origin(self.root)
        self.repository = self.root / "agent-repository"
        git(
            "clone",
            "--branch",
            "main",
            self.clone_url,
            str(self.repository),
            cwd=self.root,
        )

    def apply(self, expected_sha: str | None = None):
        return apply_and_push(
            clone_url=self.clone_url,
            base_branch="main",
            expected_sha=expected_sha or self.head,
            repository=self.repository,
            branch="hub/implement-7-abc123",
            commit_message="feat: apply agent tree",
            author_name="hub-bot",
            author_email="hub-bot@localhost",
        )

    def apply_update(self):
        return apply_and_push(
            clone_url=self.clone_url,
            base_branch="main",
            expected_sha=self.head,
            repository=self.repository,
            branch=None,
            commit_message="fix: update in place",
            author_name="hub-bot",
            author_email="hub-bot@localhost",
        )

    def clone_branch(self, branch: str) -> Path:
        check = self.root / "check"
        git("clone", "--branch", branch, self.clone_url, str(check), cwd=self.root)
        return check

    def assert_update_rejected_after_remote_move(self, remote_sha: str | None) -> None:
        original_git = applier_module.run_git
        moved = False

        def racing_git(
            arguments: tuple[str, ...], *, cwd: Path, env: dict[str, str]
        ) -> str:
            nonlocal moved
            if arguments[0] == "push" and not moved:
                moved = True
                if remote_sha is None:
                    git("update-ref", "-d", "refs/heads/main", cwd=self.bare)
                else:
                    git(
                        "update-ref",
                        "refs/heads/main",
                        remote_sha,
                        cwd=self.bare,
                    )
            return original_git(arguments, cwd=cwd, env=env)

        with (
            patch.object(applier_module, "run_git", side_effect=racing_git),
            self.assertRaisesRegex(HubError, "git push .* failed"),
        ):
            self.apply_update()
        self.assertTrue(moved)

    def test_git_builds_commit_from_complete_agent_worktree(self) -> None:
        (self.repository / "README.md").write_text("modified\n", encoding="utf-8")
        (self.repository / "new.bin").write_bytes(b"\x00new\xff\n")
        (self.repository / "delete-me.txt").unlink()
        executable = self.repository / "run.sh"
        executable.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        executable.chmod(0o755)
        (self.repository / "outside-link").symlink_to("/etc/hosts")

        outcome = self.apply()
        check = self.clone_branch(outcome.branch)

        self.assertEqual((check / "README.md").read_text(), "modified\n")
        self.assertEqual((check / "new.bin").read_bytes(), b"\x00new\xff\n")
        self.assertFalse((check / "delete-me.txt").exists())
        self.assertTrue((check / "outside-link").is_symlink())
        self.assertEqual(os.readlink(check / "outside-link"), "/etc/hosts")
        self.assertTrue((check / "run.sh").stat().st_mode & stat.S_IXUSR)
        self.assertEqual(git("rev-parse", "HEAD", cwd=check), outcome.commit_sha)
        self.assertEqual(
            git("log", "-1", "--format=%s", cwd=check),
            "feat: apply agent tree",
        )

    def test_publishing_does_not_mutate_the_agent_repository(self) -> None:
        """The agent worktree is a source to read, never a place to commit.

        ``apply_and_push`` attaches it as GIT_WORK_TREE against the *trusted*
        clone's GIT_DIR, so staging and commit land in the throwaway clone
        and the agent's own index, HEAD, and working tree come back
        untouched. Losing that would silently rewrite the caller's directory
        -- and for the memory workspace, would let a publish that fails
        later leave a half-committed local state behind.
        """

        (self.repository / "README.md").write_text("modified\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("new\n", encoding="utf-8")
        before_head = git("rev-parse", "HEAD", cwd=self.repository)
        before_status = git("status", "--porcelain", cwd=self.repository)

        outcome = self.apply()

        # Publishing does not mutate or commit the collected workspace itself.
        self.assertTrue(git("status", "--porcelain", cwd=self.repository))
        self.assertEqual(
            git("status", "--porcelain", cwd=self.repository), before_status
        )
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.repository), before_head)
        self.assertNotEqual(outcome.commit_sha, before_head)

        # The published branch still carries the edits that stayed uncommitted.
        check = self.clone_branch(outcome.branch)
        self.assertEqual((check / "README.md").read_text(), "modified\n")
        self.assertEqual((check / "untracked.txt").read_text(), "new\n")

    def test_update_mode_commits_onto_the_existing_branch(self) -> None:
        (self.repository / "new.txt").write_text("new\n", encoding="utf-8")

        outcome = self.apply_update()

        self.assertEqual(outcome.branch, "main")
        check = self.root / "check-update"
        git("clone", "--branch", "main", self.clone_url, str(check), cwd=self.root)
        self.assertEqual((check / "new.txt").read_text(), "new\n")
        self.assertEqual(git("rev-parse", "HEAD", cwd=check), outcome.commit_sha)
        self.assertEqual(git("rev-parse", "HEAD~1", cwd=check), self.head)

    def test_update_mode_rejects_a_deleted_remote_branch(self) -> None:
        (self.repository / "new.txt").write_text("new\n", encoding="utf-8")

        self.assert_update_rejected_after_remote_move(None)

        result = subprocess.run(
            ("git", "show-ref", "--verify", "--quiet", "refs/heads/main"),
            cwd=self.bare,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_update_mode_rejects_a_rewound_remote_branch(self) -> None:
        ancestor = self.head
        git("config", "user.name", "Agent", cwd=self.repository)
        git("config", "user.email", "agent@localhost", cwd=self.repository)
        (self.repository / "tip.txt").write_text("tip\n", encoding="utf-8")
        git("add", ".", cwd=self.repository)
        git("commit", "-m", "advance feature", cwd=self.repository)
        git("push", "origin", "main", cwd=self.repository)
        self.head = git("rev-parse", "HEAD", cwd=self.repository)
        (self.repository / "new.txt").write_text("new\n", encoding="utf-8")

        self.assert_update_rejected_after_remote_move(ancestor)

        self.assertEqual(
            git("rev-parse", "refs/heads/main", cwd=self.bare),
            ancestor,
        )

    def test_git_handles_file_directory_type_transitions(self) -> None:
        (self.repository / "cfg" / "old.txt").unlink()
        (self.repository / "cfg").rmdir()
        (self.repository / "cfg").write_text("now a file\n", encoding="utf-8")
        (self.repository / "single").unlink()
        (self.repository / "single").mkdir()
        (self.repository / "single" / "nested.txt").write_text(
            "now nested\n", encoding="utf-8"
        )

        outcome = self.apply()
        check = self.clone_branch(outcome.branch)

        self.assertEqual((check / "cfg").read_text(), "now a file\n")
        self.assertEqual((check / "single" / "nested.txt").read_text(), "now nested\n")

    def test_agent_git_hooks_and_config_are_not_used(self) -> None:
        marker = self.root / "hook-ran"
        hook = self.repository / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        git("config", "core.fsmonitor", str(hook), cwd=self.repository)
        (self.repository / "safe.txt").write_text("safe\n", encoding="utf-8")

        outcome = self.apply()
        check = self.clone_branch(outcome.branch)

        self.assertFalse(marker.exists())
        self.assertEqual((check / "safe.txt").read_text(), "safe\n")

    def test_case_alias_for_git_metadata_never_executes(self) -> None:
        marker = self.root / "alias-hook-ran"
        alias = self.repository / ".GIT"
        aliases_real_git = alias.exists() and os.path.samefile(
            alias, self.repository / ".git"
        )
        hook = alias / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        (self.repository / "safe.txt").write_text("safe\n", encoding="utf-8")

        if aliases_real_git:
            outcome = self.apply()
            check = self.clone_branch(outcome.branch)
            self.assertEqual((check / "safe.txt").read_text(), "safe\n")
        else:
            with self.assertRaisesRegex(HubError, "nested Git metadata"):
                self.apply()
        self.assertFalse(marker.exists())

    def test_special_files_are_rejected_before_partial_publish(self) -> None:
        os.mkfifo(self.repository / "agent.pipe")
        (self.repository / "safe.txt").write_text("safe\n", encoding="utf-8")

        with self.assertRaisesRegex(HubError, "unsupported special file"):
            self.apply()

    def test_nested_repositories_are_rejected_before_staging(self) -> None:
        nested = self.repository / "nested"
        git("init", "--initial-branch=main", str(nested), cwd=self.repository)
        git("config", "user.name", "Nested", cwd=nested)
        git("config", "user.email", "nested@localhost", cwd=nested)
        (nested / "file.txt").write_text("nested\n", encoding="utf-8")
        git("add", ".", cwd=nested)
        git("commit", "-m", "nested", cwd=nested)
        (self.repository / "safe.txt").write_text("safe\n", encoding="utf-8")

        with self.assertRaisesRegex(HubError, "nested Git metadata"):
            self.apply()

    def test_gitlinks_are_rejected_from_the_resulting_index(self) -> None:
        git(
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            self.head,
            "embedded",
            cwd=self.repository,
        )

        with self.assertRaisesRegex(HubError, "unsupported gitlink"):
            _validate_index(self.repository, dict(os.environ))

    def test_host_attributes_do_not_rewrite_agent_content(self) -> None:
        xdg = self.root / "host-xdg"
        attributes = xdg / "git" / "attributes"
        attributes.parent.mkdir(parents=True)
        attributes.write_text("*.txt ident\n", encoding="utf-8")
        content = "value $Id: host-expanded $\n"
        (self.repository / "attribute.txt").write_text(content, encoding="utf-8")

        with patch.dict(
            os.environ,
            {"HOME": str(self.root / "host-home"), "XDG_CONFIG_HOME": str(xdg)},
        ):
            outcome = self.apply()
        check = self.clone_branch(outcome.branch)

        self.assertEqual((check / "attribute.txt").read_text(), content)

    def test_trailing_whitespace_is_valid_repository_content(self) -> None:
        content = "trailing whitespace  \n"
        (self.repository / "whitespace.txt").write_text(content, encoding="utf-8")

        outcome = self.apply()
        check = self.clone_branch(outcome.branch)

        self.assertEqual((check / "whitespace.txt").read_text(), content)

    def test_stale_base_branch_is_rejected(self) -> None:
        (self.repository / "new.txt").write_text("new\n", encoding="utf-8")
        with self.assertRaisesRegex(StaleSourceError, "moved"):
            self.apply(expected_sha="f" * 40)

    def test_unchanged_tree_is_rejected(self) -> None:
        with self.assertRaisesRegex(HubError, "no committable changes"):
            self.apply()

    def test_missing_agent_repository_is_rejected(self) -> None:
        missing = self.root / "missing"
        with self.assertRaisesRegex(HubError, "not a directory"):
            apply_and_push(
                clone_url=self.clone_url,
                base_branch="main",
                expected_sha=self.head,
                repository=missing,
                branch="hub/missing",
                commit_message="missing",
                author_name="hub-bot",
                author_email="hub-bot@localhost",
            )


if __name__ == "__main__":
    unittest.main()
