from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from swecraft_runner.runner import RunResult

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.bootstrap import bootstrap_agent
from swecraft_coordinator.config import AgentProfile, CoordinatorConfig
from swecraft_coordinator.state import StateStore


def make_config(root: Path) -> CoordinatorConfig:
    return CoordinatorConfig(
        forge_url="http://127.0.0.1:3400",
        push_url="http://forge.internal:3000",
        model="test/model",
        agents=(
            AgentProfile(
                name="demo-bot",
                home_repo="hub/demo",
                write_repos=("hub/demo",),
                image="image:1",
                model="test/model",
                interval_seconds=600,
                timeout_seconds=300,
            ),
        ),
        agent_env_sources={},
        runs_dir=root / "runs",
        database=root / "state.db",
        credentials_dir=root / "credentials",
        admin_token_source="admin-token",
    )


def make_result(root: Path, changed: tuple[str, ...]) -> RunResult:
    run_dir = root / "runs" / "run-1"
    output = run_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    response = output / "response.md"
    response.write_text("Surveyed the repository.\n", encoding="utf-8")
    result_path = output / "result.json"
    result_path.write_text("{}", encoding="utf-8")
    workspace = run_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    return RunResult(
        run_id="run-1",
        run_dir=run_dir,
        source_sha="a" * 40,
        response_path=response,
        changes_path=None,
        changed_files=(),
        result_path=result_path,
        memory_source_sha="b" * 40,
        memory_workspace=workspace,
        memory_changed_files=changed,
    )


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = make_config(self.root)
        self.config.credentials_dir.mkdir(parents=True)
        self.config.credentials_path("demo-bot").write_text(
            "bot-token\n", encoding="utf-8"
        )
        self.state = StateStore(self.config.database)

    def _forge(self) -> MagicMock:
        forge = MagicMock()
        forge.get_repository.return_value = {"default_branch": "main"}
        forge.branch_exists.return_value = True
        return forge

    def test_bootstrap_requires_memory_edits(self) -> None:
        with patch(
            "swecraft_coordinator.bootstrap.ForgeClient", return_value=self._forge()
        ):
            with self.assertRaisesRegex(CoordinatorError, "wrote no memory"):
                bootstrap_agent(
                    self.config,
                    self.config.agent("demo-bot"),
                    self.state,
                    run=lambda request: make_result(self.root, ()),
                )
            with self.assertRaisesRegex(CoordinatorError, "wrote no memory"):
                bootstrap_agent(
                    self.config,
                    self.config.agent("demo-bot"),
                    self.state,
                    run=lambda request: make_result(self.root, ("GOAL.md",)),
                )

    def test_bootstrap_runs_without_direct_action_and_publishes(self) -> None:
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, ("PROJECT.md", "notebook.md"))

        with (
            patch(
                "swecraft_coordinator.bootstrap.ForgeClient",
                return_value=self._forge(),
            ),
            patch(
                "swecraft_coordinator.bootstrap.publish_memory", return_value="c" * 40
            ) as publish,
        ):
            report = bootstrap_agent(
                self.config, self.config.agent("demo-bot"), self.state, run=run
            )

        request = requests[0]
        # Memory is published as the operator, the agent as author.
        self.assertEqual(publish.call_args.kwargs["token"], "admin-token")
        self.assertEqual(publish.call_args.kwargs["author"], "demo-bot")
        self.assertFalse(request.allow_remote_writes)
        self.assertFalse(request.structured_output)
        self.assertEqual(request.memory_revision, "agent/memory")
        # An orientation turn is one task whatever the society's wake mode.
        self.assertEqual(request.agent_env["SWECRAFT_WAKE_MODE"], "single-task")
        self.assertIn("orientation", request.prompt)
        self.assertEqual(request.work_repositories, ())
        # No direct action, but the fetches carry the agent's token: a
        # dedicated home is private.
        self.assertEqual(request.fetch_auth_token_env, "SWECRAFT_FORGE_TOKEN")
        self.assertEqual(report.memory_commit, "c" * 40)
        self.assertTrue(self.state.is_bootstrapped("demo-bot"))
        self.assertEqual(report.changed_files, ("PROJECT.md", "notebook.md"))
        self.assertEqual(
            publish.call_args.kwargs["changed_files"],
            ("PROJECT.md", "notebook.md"),
        )

    def test_bootstrap_uses_the_configured_agent_class(self) -> None:
        profile = replace(
            self.config.agents[0],
            agent_import_path="swecraft_runner.harbor_agents:SwecraftPi",
        )
        self.config = replace(self.config, agents=(profile,))
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, ("PROJECT.md",))

        with (
            patch(
                "swecraft_coordinator.bootstrap.ForgeClient",
                return_value=self._forge(),
            ),
            patch(
                "swecraft_coordinator.bootstrap.publish_memory", return_value="c" * 40
            ),
        ):
            bootstrap_agent(
                self.config, self.config.agent("demo-bot"), self.state, run=run
            )
        self.assertEqual(
            requests[0].agent_import_path, "swecraft_runner.harbor_agents:SwecraftPi"
        )

    def test_unpublishable_bootstrap_fails(self) -> None:
        with (
            patch(
                "swecraft_coordinator.bootstrap.ForgeClient",
                return_value=self._forge(),
            ),
            patch("swecraft_coordinator.bootstrap.publish_memory", return_value=None),
        ):
            with self.assertRaisesRegex(CoordinatorError, "nothing publishable"):
                bootstrap_agent(
                    self.config,
                    self.config.agent("demo-bot"),
                    self.state,
                    run=lambda request: make_result(self.root, ("notebook.md",)),
                )


class WorkRepositoryBootstrapTests(BootstrapTests):
    """The orientation turn stages the maintained repositories read-only."""

    def test_work_repositories_are_staged_without_push_remotes(self) -> None:
        profile = replace(
            self.config.agents[0],
            home_repo="demo-bot/home",
            write_repos=("hub/demo", "hub/lib"),
        )
        self.config = replace(self.config, agents=(profile,))
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, ("PROJECT.md", "notebook.md"))

        with (
            patch(
                "swecraft_coordinator.bootstrap.ForgeClient",
                return_value=self._forge(),
            ),
            patch(
                "swecraft_coordinator.bootstrap.publish_memory", return_value="c" * 40
            ),
        ):
            bootstrap_agent(
                self.config, self.config.agent("demo-bot"), self.state, run=run
            )

        request = requests[0]
        self.assertFalse(request.allow_remote_writes)
        self.assertEqual(request.repository, "http://127.0.0.1:3400/demo-bot/home.git")
        self.assertEqual(
            [(work.name, work.push_remote_url) for work in request.work_repositories],
            [("demo", None), ("lib", None)],
        )
        self.assertIn(
            "Explore the staged repositories — `/app/repos/demo` is `hub/demo`, "
            "`/app/repos/lib` is `hub/lib` —",
            request.prompt,
        )
        self.assertIn("`/app/repos/lib` is `hub/lib`", request.prompt)
        self.assertNotIn("git clone", request.prompt)


if __name__ == "__main__":
    unittest.main()
