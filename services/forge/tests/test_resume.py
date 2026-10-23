"""Forge-owned volume import and copied-service startup."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from swecraft_forge.errors import ForgeRuntimeError
from swecraft_forge.runtime import ForgeRuntime


class ResumeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.volume_exists = False
        self.fail_start = False

    def __call__(self, arguments, **_kwargs):
        self.calls.append(list(arguments))
        if arguments[1:4] == ["volume", "inspect", "copy_forgejo-data"]:
            return subprocess.CompletedProcess(
                arguments, 0 if self.volume_exists else 1, stdout="", stderr=""
            )
        if self.fail_start and "up" in arguments:
            return subprocess.CompletedProcess(
                arguments, 1, stdout="", stderr="start failed"
            )
        return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")


class ForgeResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        (self.state / "runner").mkdir(parents=True)
        (self.state / "admin_token").write_text("copied-admin\n")
        (self.state / "admin_password").write_text("copied-password\n")
        (self.state / "runner/.runner").write_text("copied registration\n")
        self.compose = self.root / "compose.yaml"
        self.compose.write_text(
            "services:\n  forgejo:\n    image: forgejo@sha256:test\n"
        )
        self.volume_tar = self.root / "forgejo-data.tar"
        self.volume_tar.write_bytes(b"tar")
        self.runner = ResumeRunner()
        self.forge = ForgeRuntime(
            project="copy",
            compose_file=self.compose,
            state_dir=self.state,
            run=self.runner,
        )

    def test_resume_imports_volume_and_starts_copied_services(self) -> None:
        live = self.forge.resume(
            volume_tar=self.volume_tar,
            image="forgejo@sha256:test",
            host="127.0.0.1",
            port=3411,
        )
        self.assertEqual(live.url, "http://127.0.0.1:3411")
        self.assertEqual((self.state / "admin_token").read_text(), "copied-admin\n")
        self.assertEqual(
            (self.state / "runner/.runner").read_text(), "copied registration\n"
        )
        commands = [" ".join(call) for call in self.runner.calls]
        self.assertTrue(
            any("volume create copy_forgejo-data" in call for call in commands)
        )
        self.assertTrue(any("chown -R 1000:1000" in call for call in commands))
        self.assertTrue(
            any("up -d --wait forgejo dind runner" in call for call in commands)
        )
        self.assertFalse(any("change-password" in call for call in commands))
        self.assertFalse(any(" register " in call for call in commands))

    def test_existing_resume_volume_is_refused_before_mutation(self) -> None:
        self.runner.volume_exists = True
        with self.assertRaisesRegex(ForgeRuntimeError, "already exists"):
            self.forge.resume(
                volume_tar=self.volume_tar,
                image="forgejo@sha256:test",
                host="127.0.0.1",
                port=3411,
            )
        self.assertEqual(len(self.runner.calls), 1)

    def test_start_failure_cleans_the_new_project_and_volume(self) -> None:
        self.runner.fail_start = True
        with self.assertRaisesRegex(ForgeRuntimeError, "start failed"):
            self.forge.resume(
                volume_tar=self.volume_tar,
                image="forgejo@sha256:test",
                host="127.0.0.1",
                port=3411,
            )
        commands = [" ".join(call) for call in self.runner.calls]
        self.assertTrue(any("down --remove-orphans" in call for call in commands))
        self.assertTrue(any("volume rm copy_forgejo-data" in call for call in commands))


if __name__ == "__main__":
    unittest.main()
