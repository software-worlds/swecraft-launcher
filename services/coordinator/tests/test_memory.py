from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.memory import (
    DEFAULT_GOAL,
    ensure_memory_files,
    publish_memory,
)

BRANCH = "agent/memory"


def git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class MemoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.origin = self.root / "origin.git"
        self.origin.mkdir()
        git("init", "--bare", str(self.origin), cwd=self.root)
        self.clone_url = str(self.origin)

    def seed(self) -> None:
        added = ensure_memory_files(
            clone_url=self.clone_url,
            branch=BRANCH,
            branch_exists=False,
            token="test-token",
            author="demo-bot",
            goal=None,
        )
        self.assertEqual(
            sorted(added), ["GOAL.md", "MEMORY.md", "PROJECT.md", "notebook.md"]
        )

    def checkout(self, destination: Path) -> str:
        git(
            "clone",
            "--branch",
            BRANCH,
            self.clone_url,
            str(destination),
            cwd=self.root,
        )
        return git("rev-parse", "HEAD", cwd=destination)

    def branch_file(self, name: str) -> str:
        probe = self.root / "probe"
        if probe.exists():
            shutil.rmtree(probe)
        self.checkout(probe)
        path = probe / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


class EnsureMemoryFilesTests(MemoryFixture):
    def test_seeds_a_fresh_branch(self) -> None:
        self.seed()
        self.assertEqual(self.branch_file("GOAL.md"), DEFAULT_GOAL)
        self.assertIn("Notebook", self.branch_file("notebook.md"))

    def test_reseeding_preserves_existing_files(self) -> None:
        self.seed()
        workspace = self.root / "edit"
        self.checkout(workspace)
        (workspace / "GOAL.md").write_text("# Custom charter\n", encoding="utf-8")
        git("add", "GOAL.md", cwd=workspace)
        git(
            "-c",
            "user.name=op",
            "-c",
            "user.email=op@localhost",
            "commit",
            "-m",
            "operator goal",
            cwd=workspace,
        )
        git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=workspace)
        added = ensure_memory_files(
            clone_url=self.clone_url,
            branch=BRANCH,
            branch_exists=True,
            token="test-token",
            author="demo-bot",
            goal="# Ignored\n",
        )
        self.assertEqual(added, [])
        self.assertEqual(self.branch_file("GOAL.md"), "# Custom charter\n")


class PublishMemoryTests(MemoryFixture):
    def prepare_wake(self) -> tuple[Path, Path, str]:
        """Simulate a wake: input checkout plus an edited collected copy."""

        self.seed()
        input_memory = self.root / "input-memory"
        source_sha = self.checkout(input_memory)
        workspace = self.root / "workspace"
        shutil.copytree(input_memory, workspace, symlinks=True)
        return input_memory, workspace, source_sha

    def test_publishes_agent_edits(self) -> None:
        input_memory, workspace, source_sha = self.prepare_wake()
        (workspace / "notebook.md").write_text("# Notebook\n\n- next\n")
        commit = publish_memory(
            clone_url=self.clone_url,
            branch=BRANCH,
            token="test-token",
            author="demo-bot",
            workspace=workspace,
            input_memory=input_memory,
            source_sha=source_sha,
            changed_files=("notebook.md",),
            commit_message="demo-bot: wake notes",
        )
        self.assertIsNotNone(commit)
        self.assertIn("- next", self.branch_file("notebook.md"))

    def test_goal_edits_are_discarded(self) -> None:
        input_memory, workspace, source_sha = self.prepare_wake()
        (workspace / "GOAL.md").write_text("# Hijacked\n")
        commit = publish_memory(
            clone_url=self.clone_url,
            branch=BRANCH,
            token="test-token",
            author="demo-bot",
            workspace=workspace,
            input_memory=input_memory,
            source_sha=source_sha,
            changed_files=("GOAL.md",),
            commit_message="demo-bot: wake notes",
        )
        self.assertIsNone(commit)
        self.assertEqual(self.branch_file("GOAL.md"), DEFAULT_GOAL)

    def test_goal_edit_alongside_real_edit_publishes_only_the_real_one(self) -> None:
        input_memory, workspace, source_sha = self.prepare_wake()
        (workspace / "GOAL.md").write_text("# Hijacked\n")
        (workspace / "notebook.md").write_text("# Notebook\n\n- kept\n")
        commit = publish_memory(
            clone_url=self.clone_url,
            branch=BRANCH,
            token="test-token",
            author="demo-bot",
            workspace=workspace,
            input_memory=input_memory,
            source_sha=source_sha,
            changed_files=("GOAL.md", "notebook.md"),
            commit_message="demo-bot: wake notes",
        )
        self.assertIsNotNone(commit)
        self.assertEqual(self.branch_file("GOAL.md"), DEFAULT_GOAL)
        self.assertIn("- kept", self.branch_file("notebook.md"))

    def _race(self, name: str, content: str) -> None:
        racer = self.root / "racer"
        if racer.exists():
            shutil.rmtree(racer)
        self.checkout(racer)
        (racer / name).write_text(content, encoding="utf-8")
        git("add", name, cwd=racer)
        git(
            "-c",
            "user.name=other",
            "-c",
            "user.email=other@localhost",
            "commit",
            "-m",
            "racing change",
            cwd=racer,
        )
        git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=racer)

    def test_lease_race_replays_disjoint_edits(self) -> None:
        input_memory, workspace, source_sha = self.prepare_wake()
        (workspace / "notebook.md").write_text("# Notebook\n\n- mine\n")
        self._race("daily.md", "- theirs\n")
        commit = publish_memory(
            clone_url=self.clone_url,
            branch=BRANCH,
            token="test-token",
            author="demo-bot",
            workspace=workspace,
            input_memory=input_memory,
            source_sha=source_sha,
            changed_files=("notebook.md",),
            commit_message="demo-bot: wake notes",
        )
        self.assertIsNotNone(commit)
        self.assertIn("- mine", self.branch_file("notebook.md"))
        self.assertEqual(self.branch_file("daily.md"), "- theirs\n")

    def test_lease_race_on_the_same_file_is_a_conflict(self) -> None:
        input_memory, workspace, source_sha = self.prepare_wake()
        (workspace / "notebook.md").write_text("# Notebook\n\n- mine\n")
        self._race("notebook.md", "# Notebook\n\n- theirs\n")
        with self.assertRaisesRegex(CoordinatorError, "conflict"):
            publish_memory(
                clone_url=self.clone_url,
                branch=BRANCH,
                token="test-token",
                author="demo-bot",
                workspace=workspace,
                input_memory=input_memory,
                source_sha=source_sha,
                changed_files=("notebook.md",),
                commit_message="demo-bot: wake notes",
            )


if __name__ == "__main__":
    unittest.main()
