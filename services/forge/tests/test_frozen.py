"""Heads read from a frozen volume tar: loose refs, packed refs, absences."""

from __future__ import annotations

import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from swecraft_forge.errors import ForgeRuntimeError
from swecraft_forge.frozen import read_heads, verify_lineage


def git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return completed.stdout.strip()


class FrozenForgeTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        # A volume-like tree: repositories nested under an image-specific
        # prefix, exactly as `tar -C /var/lib/gitea .` would capture it.
        self.repos = self.scratch / "volume/git/repositories/hubworld"
        self.repos.mkdir(parents=True)
        self.baseline, self.head = self.seed_bare("demo")

    def seed_bare(self, name: str) -> tuple[str, str]:
        work = self.scratch / f"work-{name}"
        work.mkdir()
        git("init", "-b", "main", str(work), cwd=self.scratch)
        (work / "file.txt").write_text(f"{name}\n")
        git("add", "file.txt", cwd=work)
        git("commit", "-m", "seed", cwd=work)
        baseline = git("rev-parse", "HEAD", cwd=work)
        bare = self.repos / f"{name}.git"
        git("init", "--bare", "-b", "main", str(bare), cwd=self.scratch)
        # Push instead of clone --bare: pushes write loose refs, keeping the
        # loose and packed cases genuinely distinct.
        git("push", str(bare), "main", cwd=work)
        (work / "file.txt").write_text("evolved\n")
        git("commit", "-am", "evolve", cwd=work)
        git("push", str(bare), "main", cwd=work)
        return baseline, git("rev-parse", "HEAD", cwd=work)

    def volume_tar(self) -> Path:
        path = self.scratch / "forgejo-data.tar"
        with tarfile.open(path, "w") as tar:
            root = self.scratch / "volume"
            for member in sorted(root.rglob("*")):
                tar.add(
                    member,
                    arcname=f"./{member.relative_to(root).as_posix()}",
                    recursive=False,
                )
        return path

    def test_loose_head_is_read(self) -> None:
        heads = read_heads(self.volume_tar(), "hubworld", {"demo": "main"})
        self.assertEqual(heads, {"demo": self.head})

    def test_packed_head_is_read(self) -> None:
        git("pack-refs", "--all", cwd=self.repos / "demo.git")
        self.assertFalse((self.repos / "demo.git/refs/heads/main").exists())
        heads = read_heads(self.volume_tar(), "hubworld", {"demo": "main"})
        self.assertEqual(heads, {"demo": self.head})

    def test_locked_baseline_is_present_and_ancestor_of_head(self) -> None:
        verify_lineage(
            self.volume_tar(),
            "hubworld",
            {"demo": self.baseline},
            {"demo": self.head},
        )

    def test_wrong_world_baseline_is_refused_even_when_names_overlap(self) -> None:
        other_baseline, _ = self.seed_bare("other")
        with self.assertRaisesRegex(ForgeRuntimeError, "locked baseline"):
            verify_lineage(
                self.volume_tar(),
                "hubworld",
                {"demo": other_baseline},
                {"demo": self.head},
            )

    def test_repository_names_are_matched_case_insensitively(self) -> None:
        heads = read_heads(self.volume_tar(), "HubWorld", {"Demo": "main"})
        self.assertEqual(heads, {"Demo": self.head})

    def test_missing_repository_is_refused(self) -> None:
        with self.assertRaises(ForgeRuntimeError):
            read_heads(self.volume_tar(), "hubworld", {"absent": "main"})

    def test_missing_branch_is_refused(self) -> None:
        with self.assertRaises(ForgeRuntimeError):
            read_heads(self.volume_tar(), "hubworld", {"demo": "develop"})


if __name__ == "__main__":
    unittest.main()
