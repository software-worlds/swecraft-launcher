"""Local checkpoint configuration defaults and validation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swecraft_society.config import load_checkpoint_config
from swecraft_society.errors import SocietyError

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def test_missing_file_uses_local_defaults(self) -> None:
        document = load_checkpoint_config(root=self.root)
        self.assertEqual(
            document.path, self.root.resolve() / ".swecraft/checkpoint.yaml"
        )
        self.assertEqual(
            document.config.checkpoint.output_dir, Path(".swecraft/checkpoints")
        )
        self.assertEqual(document.config.load.output_dir, Path(".swecraft/loaded"))
        self.assertEqual(
            document.config.resume.compose_project_prefix, "swecraft-resumed"
        )
        self.assertEqual(
            document.config.checkpoint.evaluator_records_dir,
            Path(".swecraft/evaluator/records"),
        )
        self.assertEqual(
            document.config.checkpoint.evaluator_state,
            Path(".swecraft/evaluator/state.json"),
        )
        self.assertEqual(document.config.checkpoint.actions_wait_seconds, 300)
        self.assertIsNone(document.config.huggingface)

    def test_deprecated_include_runs_is_accepted_and_ignored(self) -> None:
        # Configs written before the harbor policy keep loading;
        # traces are no longer optional, so the value has no effect.
        path = self.root / ".swecraft/checkpoint.yaml"
        path.parent.mkdir()
        path.write_text("schema_version: 1\ncheckpoint:\n  include_runs: false\n")
        document = load_checkpoint_config(root=self.root)
        self.assertEqual(document.config.checkpoint.harbor, "omit")

    def test_explicit_config_loads_all_sections(self) -> None:
        path = self.root / ".swecraft/checkpoint.yaml"
        path.parent.mkdir()
        path.write_text(
            "schema_version: 1\n"
            "apparatus:\n  root: experiment\n  world: intent.yaml\n"
            "checkpoint:\n  output_dir: private/checkpoints\n"
            "  compose_project: experiment-forge\n"
            "  actions_wait_seconds: 45\n"
            "  evaluator_records_dir: runtime/records\n"
            "  evaluator_state: runtime/state.json\n"
            "  forge_compose_file: runtime/compose.yaml\n"
            "load:\n  output_dir: private/loaded\n"
            "resume:\n  compose_project_prefix: experiment-copy\n"
            "huggingface:\n  repo_id: software-worlds/experiment-3\n"
            "  private: true\n"
        )
        config = load_checkpoint_config(root=self.root).config
        self.assertEqual(config.apparatus.root, Path("experiment"))
        self.assertEqual(config.apparatus.world, Path("intent.yaml"))
        self.assertEqual(config.checkpoint.compose_project, "experiment-forge")
        self.assertEqual(config.checkpoint.actions_wait_seconds, 45)
        self.assertEqual(config.load.output_dir, Path("private/loaded"))
        self.assertEqual(config.resume.compose_project_prefix, "experiment-copy")
        self.assertEqual(
            config.checkpoint.evaluator_records_dir, Path("runtime/records")
        )
        self.assertEqual(config.checkpoint.evaluator_state, Path("runtime/state.json"))
        self.assertEqual(
            config.checkpoint.forge_compose_file, Path("runtime/compose.yaml")
        )
        self.assertEqual(config.huggingface.repo_id, "software-worlds/experiment-3")

    def test_experiment_checkpoint_template_validates(self) -> None:
        template = load_checkpoint_config(
            TEMPLATES / "experiment/checkpoint.yaml", root=self.root
        ).config
        self.assertEqual(
            template.apparatus.root, Path("experiments/FILL-experiment-name")
        )

    def test_unknown_keys_and_invalid_repo_are_refused(self) -> None:
        path = self.root / ".swecraft/checkpoint.yaml"
        path.parent.mkdir()
        path.write_text("surprise: true\n")
        with self.assertRaises(SocietyError):
            load_checkpoint_config(root=self.root)
        path.write_text("huggingface:\n  repo_id: no-owner\n")
        with self.assertRaises(SocietyError):
            load_checkpoint_config(root=self.root)
        path.write_text("checkpoint:\n  actions_wait_seconds: -1\n")
        with self.assertRaises(SocietyError):
            load_checkpoint_config(root=self.root)


if __name__ == "__main__":
    unittest.main()
