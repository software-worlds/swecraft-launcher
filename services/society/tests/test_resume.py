"""Society resume only sequences Forge and coordinator adapters."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from swecraft_coordinator import CoordinatorError
from swecraft_forge import ForgeRuntimeError
from test_orchestrator import SocietyFixture

from swecraft_society.errors import SocietyError
from swecraft_society.orchestrator import LoadOptions, checkpoint, load
from swecraft_society.resume import ResumeOptions, resume


class FakeResumedForge:
    def __init__(self, project: str, image: str) -> None:
        self.project = project
        self.image = image
        self.compose_file = Path("/tmp/forge compose.yaml")
        self.calls = []

    def resume(self, **kwargs):
        self.calls.append(("resume", kwargs))
        return SimpleNamespace(url="http://127.0.0.1:3411")


class FakeCoordinatorResume:
    loaded = None

    @classmethod
    def load(cls, apparatus_config, *, destination):
        cls.loaded = (apparatus_config, destination)
        return cls()

    def activate(self, **kwargs):
        return SimpleNamespace(
            config_path=Path(FakeCoordinatorResume.loaded[1])
            / "runtime/coordinator.yaml",
            agents=("demo-bot",),
        )


class FailingCoordinatorResume(FakeCoordinatorResume):
    def activate(self, **kwargs):
        raise CoordinatorError("runtime relocation failed")


class ResumeOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = SocietyFixture(self)
        source = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        self.loaded = load(
            LoadOptions(
                source=source,
                root=self.fixture.root,
                dest=Path("loaded-copy"),
            )
        ).dest
        self.forge = FakeResumedForge("copy", "forgejo@sha256:fixture")

    def test_resume_sequences_owned_adapters(self) -> None:
        with patch("swecraft_society.resume.CoordinatorResume", FakeCoordinatorResume):
            report = resume(
                ResumeOptions(
                    root=self.fixture.root,
                    dest=self.loaded,
                    compose_project="copy",
                    port=3411,
                ),
                forge_runtime=self.forge,
            )
        self.assertEqual(report.forge_url, "http://127.0.0.1:3411")
        self.assertEqual(report.agents, ("demo-bot",))
        self.assertEqual([call[0] for call in self.forge.calls], ["resume"])
        self.assertEqual(
            FakeCoordinatorResume.loaded[0],
            self.loaded / "apparatus/coordinator.yaml",
        )
        runtime_checkpoint = yaml.safe_load(
            (self.loaded / "runtime/checkpoint.yaml").read_text()
        )
        self.assertEqual(
            runtime_checkpoint["apparatus"]["root"],
            str((self.loaded / "apparatus").resolve()),
        )
        self.assertEqual(
            runtime_checkpoint["apparatus"]["coordinator"],
            str((self.loaded / "runtime/coordinator.yaml").resolve()),
        )
        self.assertEqual(runtime_checkpoint["checkpoint"]["compose_project"], "copy")
        self.assertEqual(
            runtime_checkpoint["checkpoint"]["evaluator_records_dir"],
            str((self.loaded / "evaluations").resolve()),
        )
        self.assertEqual(
            runtime_checkpoint["checkpoint"]["evaluator_state"],
            str((self.loaded / "state/evaluator/state.json").resolve()),
        )

    def test_broken_standard_forge_preflight_is_a_society_error(self) -> None:
        with (
            patch(
                "swecraft_society.resume.standard_compose_file",
                side_effect=ForgeRuntimeError("standard Compose is missing"),
            ),
            self.assertRaisesRegex(SocietyError, "resume preflight failed"),
        ):
            resume(
                ResumeOptions(
                    root=self.fixture.root,
                    dest=self.loaded,
                    compose_project="copy",
                    port=3411,
                )
            )

    def test_post_start_failure_reports_compose_recovery_handle(self) -> None:
        with (
            patch(
                "swecraft_society.resume.CoordinatorResume", FailingCoordinatorResume
            ),
            self.assertRaisesRegex(
                SocietyError, "resume project copy failed.*docker compose -p copy"
            ),
        ):
            resume(
                ResumeOptions(
                    root=self.fixture.root,
                    dest=self.loaded,
                    compose_project="copy",
                    port=3411,
                ),
                forge_runtime=self.forge,
            )

    def test_forge_image_mismatch_is_refused_before_import(self) -> None:
        self.forge.image = "forgejo@sha256:other"
        with self.assertRaisesRegex(SocietyError, "refusing to import its database"):
            resume(
                ResumeOptions(
                    root=self.fixture.root,
                    dest=self.loaded,
                    compose_project="copy",
                    port=3411,
                ),
                forge_runtime=self.forge,
            )
        self.assertEqual(self.forge.calls, [])

    def test_hub_commit_mismatch_warns_before_forge_mutation(self) -> None:
        observed = []

        def warning(message):
            self.assertEqual(self.forge.calls, [])
            observed.append(message)

        with (
            patch("swecraft_society.resume.CoordinatorResume", FakeCoordinatorResume),
            patch("swecraft_society.resume.repository_head", return_value="f" * 40),
        ):
            resume(
                ResumeOptions(
                    root=self.fixture.root,
                    dest=self.loaded,
                    compose_project="copy",
                    port=3411,
                ),
                forge_runtime=self.forge,
                warn=warning,
            )
        self.assertEqual(len(observed), 1)
        self.assertIn("does not enforce commit compatibility", observed[0])


if __name__ == "__main__":
    unittest.main()
