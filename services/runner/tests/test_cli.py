import inspect
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from typer.testing import CliRunner

from swecraft_runner import cli as hub_cli
from swecraft_runner.cli import app
from swecraft_runner.runner import DEFAULT_HARBOR_COMMAND, RunRequest


class HarborCommandDefaultTests(unittest.TestCase):
    def test_the_cli_default_launcher_is_the_runner_default(self) -> None:
        # A site launcher named by SWECRAFT_HARBOR_COMMAND must reach the CLI path too;
        # a literal default here silently ran the stock uvx pin instead.
        default = (
            inspect.signature(hub_cli.run_command).parameters["harbor_command"].default
        )
        self.assertEqual(shlex.split(default), list(DEFAULT_HARBOR_COMMAND))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_help_exposes_run_command(self) -> None:
        result = self.runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("run", result.stdout)

    def test_requires_exactly_one_prompt_source(self) -> None:
        result = self.runner.invoke(
            app,
            ["run", "--repo", ".", "--model", "test/model"],
        )

        self.assertEqual(result.exit_code, 2)
        self.assertIn("exactly one", result.output)

    def test_agent_kwarg_requires_name_value_form(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "run",
                "--repo",
                ".",
                "--model",
                "test/model",
                "--prompt",
                "Task",
                "--agent-kwarg",
                "base_url",
            ],
        )

        self.assertEqual(result.exit_code, 2)
        self.assertIn("NAME=VALUE", result.output)

    def test_fallback_pricing_requires_input_and_output_rates(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "run",
                "--repo",
                ".",
                "--model",
                "test/model",
                "--prompt",
                "Task",
                "--input-cost-per-million",
                "1.0",
            ],
        )

        self.assertEqual(result.exit_code, 2)
        self.assertIn("input and output costs", result.output)

    def test_rejects_both_inline_and_file_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "prompt.md"
            prompt.write_text("Task", encoding="utf-8")
            result = self.runner.invoke(
                app,
                [
                    "run",
                    "--repo",
                    ".",
                    "--model",
                    "test/model",
                    "--prompt",
                    "Task",
                    "--prompt-file",
                    str(prompt),
                ],
            )

        self.assertEqual(result.exit_code, 2)
        self.assertIn("exactly one", result.output)


if __name__ == "__main__":
    unittest.main()


class AgentImportPathTests(unittest.TestCase):
    def test_agent_import_path_reaches_the_request(self) -> None:
        # The docs run SwecraftAntigravityCli through `swecraft-runner run`; the
        # flag was missing, so the class was reachable only through run_once.
        captured: dict[str, RunRequest] = {}

        with tempfile.TemporaryDirectory() as directory:
            response = Path(directory) / "response.md"
            response.write_text("done", encoding="utf-8")
            outcome = SimpleNamespace(
                response_path=response,
                run_id="run",
                source_sha="abc",
                changed_files=[],
                run_dir=Path(directory),
            )

            def fake_run_once(request: RunRequest) -> SimpleNamespace:
                captured["request"] = request
                return outcome

            with mock.patch.object(hub_cli, "run_once", fake_run_once):
                result = CliRunner().invoke(
                    app,
                    [
                        "run",
                        "--repo",
                        ".",
                        "--model",
                        "google/gemini-3.8-flash",
                        "--prompt",
                        "Task",
                        "--agent",
                        "antigravity-cli",
                        "--agent-import-path",
                        "swecraft_runner.harbor_agents:SwecraftAntigravityCli",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        request = captured["request"]
        self.assertEqual(request.agent, "antigravity-cli")
        self.assertEqual(
            request.agent_import_path,
            "swecraft_runner.harbor_agents:SwecraftAntigravityCli",
        )
