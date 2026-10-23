"""Forge owns Compose and volume checkpoint mechanics."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from swecraft_forge.runtime import ForgeRuntime


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, arguments, **_kwargs):
        self.calls.append(list(arguments))
        stdout = ""
        if "ps" in arguments:
            stdout = "forgejo\nrunner\n"
        elif arguments[1:3] == ["image", "inspect"]:
            stdout = json.dumps(
                {"Id": "sha256:image", "Os": "linux", "Architecture": "arm64"}
            )
        elif arguments[1] == "run":
            mount = next(value for value in arguments if value.endswith(":/backup"))
            backup = Path(mount.removesuffix(":/backup"))
            name = next(
                value.removeprefix("/backup/")
                for value in arguments
                if value.startswith("/backup/")
            )
            (backup / name).write_bytes(b"tar")
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")


class ForgeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.compose = self.root / "compose.yaml"
        self.compose.write_text(
            "services:\n  forgejo:\n    image: forgejo@sha256:test\n"
        )
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "admin_token").write_text("token\n")
        self.runner = RecordingRunner()
        self.forge = ForgeRuntime(
            project="experiment",
            compose_file=self.compose,
            state_dir=self.state,
            run=self.runner,
        )

    def test_lifecycle_uses_one_explicit_project(self) -> None:
        self.assertEqual(self.forge.running_services(), ("forgejo", "runner"))
        self.forge.stop(("forgejo", "runner"))
        self.forge.start(("forgejo",))
        self.assertIn("experiment", self.runner.calls[0])
        self.assertIn("stop", self.runner.calls[1])
        self.assertIn("start", self.runner.calls[2])

    def test_capture_copies_host_state_and_exports_the_named_volume(self) -> None:
        destination = self.root / "checkpoint/forge"
        self.forge.capture(destination)
        self.assertEqual((destination / "host/admin_token").read_text(), "token\n")
        self.assertEqual((destination / "forgejo-data.tar").read_bytes(), b"tar")
        command = self.runner.calls[-1]
        self.assertIn("experiment_forgejo-data:/volume:ro", command)
        self.assertIn("forgejo@sha256:test", command)

    def test_capture_excludes_the_runner_action_cache(self) -> None:
        """The act cache must never reach a checkpoint.

        It is ephemeral, absent from the documented host-state layout
        (`.runner`, `config.yml`, `labels`), and reaches thousands of files on
        a society whose agents write CI. Capturing it also broke Hugging Face
        publication outright, because HF rejects any path under `.cache/` --
        so the local checkpoint succeeded while the dataset silently never
        updated.
        """

        cache = self.forge.state_dir / "runner/.cache/act/aa/bb"
        cache.mkdir(parents=True)
        (cache / "devcontainer.json").write_text("junk\n")
        (self.forge.state_dir / "runner/config.yml").write_text("real\n")

        destination = self.root / "checkpoint/forge"
        self.forge.capture(destination)

        self.assertFalse((destination / "host/runner/.cache").exists())
        self.assertEqual((destination / "host/runner/config.yml").read_text(), "real\n")

    def test_image_identity_is_observed_through_the_runtime(self) -> None:
        self.assertEqual(
            self.forge.inspect_image("agent:1"),
            ("sha256:image", "linux/arm64"),
        )


if __name__ == "__main__":
    unittest.main()
