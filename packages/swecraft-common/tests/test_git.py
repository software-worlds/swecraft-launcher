from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swecraft_common import HubError
from swecraft_common.git import (
    ensure_git_revision,
    git_environment,
    git_mirror_path,
    hub_checkout_head,
    materialize_git_revision,
    run_git,
)


def git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "swecraft-common-test",
            "GIT_AUTHOR_EMAIL": "swecraft-common@example.test",
            "GIT_COMMITTER_NAME": "swecraft-common-test",
            "GIT_COMMITTER_EMAIL": "swecraft-common@example.test",
            "HOME": str(cwd),
            "PATH": os.environ.get("PATH", ""),
        },
    )
    return completed.stdout.strip()


class GitEnvironmentTests(unittest.TestCase):
    def test_isolates_host_controls_and_scopes_the_token_to_the_origin(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "GIT_DIR": "/host/repository",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "credential.helper",
                    "GIT_CONFIG_VALUE_0": "dangerous-helper",
                    "GIT_SSH_COMMAND": "host-command",
                    "GIT_TRACE_CURL": "/tmp/credentials.log",
                },
            ),
        ):
            env = git_environment(
                isolation_dir=Path(directory) / "isolation",
                token="secret-token",
                remote_url="https://forge.example:3443/hub/demo.git",
            )

        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_SSH_COMMAND", env)
        self.assertNotIn("GIT_TRACE_CURL", env)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(
            env["GIT_CONFIG_KEY_0"],
            "http.https://forge.example:3443/.extraheader",
        )
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: token secret-token")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_token_requires_an_explicit_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(HubError, "remote_url"):
                git_environment(
                    isolation_dir=Path(directory) / "isolation", token="secret"
                )

    def test_non_http_remote_does_not_receive_an_http_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = git_environment(
                isolation_dir=Path(directory) / "isolation",
                token="unused",
                remote_url="file:///tmp/origin.git",
            )
        self.assertNotIn("GIT_CONFIG_COUNT", env)

    def test_environment_directory_can_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            isolation = Path(directory) / "isolation"
            first = git_environment(isolation_dir=isolation)
            second = git_environment(isolation_dir=isolation)
        self.assertEqual(first["HOME"], second["HOME"])


class RunGitTests(unittest.TestCase):
    def test_timeout_is_normalized(self) -> None:
        with patch(
            "swecraft_common.git.subprocess.run",
            side_effect=subprocess.TimeoutExpired(("git", "status"), 1),
        ):
            with self.assertRaisesRegex(HubError, "exceeded its 1s timeout"):
                run_git(("status",), cwd=Path.cwd(), env={}, timeout_seconds=1)

    def test_missing_git_is_normalized(self) -> None:
        with patch("swecraft_common.git.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(HubError, "could not execute git"):
                run_git(("status",), cwd=Path.cwd(), env={})

    def test_command_failure_is_bounded_and_normalized(self) -> None:
        failed = subprocess.CompletedProcess(
            ("git", "status"), 1, stdout="", stderr="bad repository"
        )
        with patch("swecraft_common.git.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(HubError, "git status failed: bad repository"):
                run_git(("status",), cwd=Path.cwd(), env={})


class GitCacheTests(unittest.TestCase):
    def test_materializes_a_self_contained_checkout_from_a_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            _seed_repository(origin)
            head = git("rev-parse", "HEAD", cwd=origin)

            resolved = materialize_git_revision(
                cache_root=root / "cache",
                remote_url=str(origin),
                revision="v1.0",
                destination=root / "checkout",
            )

            self.assertEqual(resolved.commit, head)
            self.assertTrue((root / "checkout" / "module.py").is_file())
            self.assertFalse(
                (
                    root / "checkout" / ".git" / "objects" / "info" / "alternates"
                ).exists()
            )

    def test_relative_cache_and_destination_paths_are_resolved_from_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            _seed_repository(origin)
            previous = Path.cwd()
            try:
                os.chdir(root)
                resolved = materialize_git_revision(
                    cache_root=Path("cache"),
                    remote_url=str(origin),
                    revision="v1.0",
                    destination=Path("checkout"),
                )
            finally:
                os.chdir(previous)

            self.assertTrue((root / "checkout" / "module.py").is_file())
            self.assertTrue(resolved.mirror_path.is_absolute())

    def test_exact_sha_warm_hit_does_not_need_the_upstream_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            _seed_repository(origin)
            head = git("rev-parse", "HEAD", cwd=origin)
            cache_root = root / "cache"
            materialize_git_revision(
                cache_root=cache_root,
                remote_url=str(origin),
                revision="v1.0",
                destination=root / "checkout",
            )
            shutil.rmtree(origin)

            resolved = ensure_git_revision(
                cache_root=cache_root,
                remote_url=str(origin),
                revision=head,
            )

            self.assertEqual(resolved.commit, head)
            self.assertFalse(resolved.fetched)

    def test_mirror_paths_remap_decoded_dot_segments_inside_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_root = Path(directory) / "cache"
            for remote_url in (
                "https://example.com/org/%2e%2e/repo.git",
                "https://example.com/org/%2e%2e.git",
            ):
                mirror = git_mirror_path(cache_root, remote_url)
                relative = mirror.relative_to(cache_root.resolve())

                self.assertNotIn("..", relative.parts)
                self.assertTrue(mirror.name.endswith(".git"))


class HubCheckoutHeadTests(unittest.TestCase):
    def test_git_discovers_the_checkout_from_a_nested_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git("init", "-b", "main", cwd=root)
            nested = root / "src/package/module.py"
            nested.parent.mkdir(parents=True)
            nested.write_text("value = 1\n")
            git("add", ".", cwd=root)
            git("commit", "-m", "seed", cwd=root)
            head = git("rev-parse", "HEAD", cwd=root)

            self.assertEqual(hub_checkout_head(nested), head)


def _seed_repository(origin: Path) -> None:
    origin.mkdir()
    git("init", "-b", "main", cwd=origin)
    (origin / "module.py").write_text("answer = 42\n")
    git("add", ".", cwd=origin)
    git("commit", "-m", "initial", cwd=origin)
    git("tag", "v1.0", cwd=origin)


if __name__ == "__main__":
    unittest.main()
