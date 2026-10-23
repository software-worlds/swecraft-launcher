"""Coordinator resume preserves identities and relocates runtime paths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.resume import CoordinatorResume


class CoordinatorResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dest = self.root / "loaded"
        credentials = self.dest / "state/coordinator/credentials"
        credentials.mkdir(parents=True)
        (credentials / "alpha.token").write_text("copied-alpha\n")
        (credentials / "stale.token").write_text("copied-stale\n")
        (self.dest / "state/coordinator/api_token").write_text("copied-api\n")
        forge_state = self.dest / "state/forge/host"
        forge_state.mkdir(parents=True)
        (forge_state / "admin_token").write_text("copied-admin\n")
        self.config = self.dest / "apparatus/coordinator.yaml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            "forge_url: http://127.0.0.1:3400\n"
            "api_token: file:.swecraft/api_token\n"
            "checkpoint_command: [old-checkpoint-wrapper]\n"
            "checkpoint_publish: true\n"
            "publish_command: [old-publish-wrapper]\n"
            "model: test/model\n"
            "agent_env:\n  TOKEN: env:MODEL_TOKEN\n"
            "agents:\n  - name: alpha\n    repo: hub/demo\n"
        )

    def test_activation_preserves_credentials_and_relocates_paths(self) -> None:
        source = CoordinatorResume.load(self.config, destination=self.dest)
        checkpoint_config = self.dest / "runtime/checkpoint.yaml"
        report = source.activate(
            forge_url="http://127.0.0.1:3411",
            port=3411,
            checkpoint_config=checkpoint_config,
        )

        self.assertEqual(
            (self.dest / "state/coordinator/credentials/alpha.token").read_text(),
            "copied-alpha\n",
        )
        self.assertEqual(
            (self.dest / "state/coordinator/credentials/stale.token").read_text(),
            "copied-stale\n",
        )
        self.assertFalse((self.dest / "runtime/api_token").exists())
        runtime = yaml.safe_load(report.config_path.read_text())
        resolved = self.dest.resolve()
        self.assertEqual(runtime["forge_url"], "http://127.0.0.1:3411")
        self.assertEqual(runtime["push_url"], "http://host.docker.internal:3411")
        self.assertEqual(runtime["agent_env"]["TOKEN"], "env:MODEL_TOKEN")
        self.assertEqual(
            runtime["admin_token"],
            f"file:{resolved / 'state/forge/host/admin_token'}",
        )
        self.assertEqual(
            runtime["api_token"],
            f"file:{resolved / 'state/coordinator/api_token'}",
        )
        self.assertEqual(
            runtime["credentials_dir"],
            str(resolved / "state/coordinator/credentials"),
        )
        self.assertEqual(runtime["checkpoint_config"], str(checkpoint_config.resolve()))
        self.assertNotIn("checkpoint_command", runtime)
        # A resumed copy must never publish into the source society's dataset.
        self.assertNotIn("checkpoint_publish", runtime)
        self.assertNotIn("publish_command", runtime)
        self.assertEqual(report.agents, ("alpha",))

    def test_missing_copied_admin_token_is_refused(self) -> None:
        (self.dest / "state/forge/host/admin_token").unlink()
        source = CoordinatorResume.load(self.config, destination=self.dest)
        with self.assertRaisesRegex(CoordinatorError, "admin token is missing"):
            source.activate(
                forge_url="http://127.0.0.1:3411",
                port=3411,
                checkpoint_config=self.dest / "runtime/checkpoint.yaml",
            )


if __name__ == "__main__":
    unittest.main()
