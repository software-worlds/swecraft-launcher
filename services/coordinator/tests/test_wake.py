from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from swecraft_runner.pricing import ModelPricing
from swecraft_runner.runner import RunResult

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import (
    AgentProfile,
    BelowRate,
    CollaborationNudge,
    CoordinatorConfig,
    LedgerFile,
    Nudges,
    PromptPolicy,
    RunBudget,
    WakeBudget,
)
from swecraft_coordinator.state import StateStore
from swecraft_coordinator.wake import wake_agent


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


def make_result(root: Path, structured: dict | None) -> RunResult:
    run_dir = root / "runs" / "run-1"
    output = run_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    response = output / "response.md"
    response.write_text("Did a thing.\n", encoding="utf-8")
    result_path = output / "result.json"
    result_path.write_text(json.dumps({"usage": {"cost_usd": 0.42}}), encoding="utf-8")
    return RunResult(
        run_id="run-1",
        run_dir=run_dir,
        source_sha="a" * 40,
        response_path=response,
        changes_path=None,
        changed_files=(),
        result_path=result_path,
        structured=structured,
    )


class WakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = make_config(self.root)
        self.state = StateStore(self.config.database)
        self.config.credentials_dir.mkdir(parents=True)
        self.config.credentials_path("demo-bot").write_text(
            "bot-token\n", encoding="utf-8"
        )
        self.state.mark_bootstrapped("demo-bot")

    def _forge(self) -> MagicMock:
        forge = MagicMock()
        forge.get_repository.return_value = {"default_branch": "main"}
        forge.branch_exists.return_value = True
        return forge

    def test_missing_credentials_record_a_failed_wake_with_backoff(self) -> None:
        self.config.credentials_path("demo-bot").unlink()
        with self.assertRaisesRegex(CoordinatorError, "onboard"):
            wake_agent(
                self.config,
                self.config.agent("demo-bot"),
                self.state,
                run=lambda request: make_result(self.root, None),
            )
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertEqual(status.consecutive_failures, 1)
        self.assertIsNone(status.active_started)
        self.assertFalse(self.state.is_due("demo-bot"))
        wakes, _ = self.state.day_usage("demo-bot")
        self.assertEqual(wakes, 1)

    def test_missing_admin_token_is_a_recorded_preflight_failure(self) -> None:
        self.config = replace(self.config, admin_token_source="")
        with self.assertRaisesRegex(CoordinatorError, "publish memory as the operator"):
            wake_agent(
                self.config,
                self.config.agent("demo-bot"),
                self.state,
                run=lambda request: make_result(self.root, None),
            )
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertEqual(status.consecutive_failures, 1)
        self.assertIsNone(status.active_started)

    def test_unbootstrapped_agent_is_refused_with_backoff(self) -> None:
        with sqlite3.connect(self.state._path) as connection:
            connection.execute(
                "UPDATE agent_state SET bootstrapped_at = NULL WHERE agent = ?",
                ("demo-bot",),
            )
        with self.assertRaisesRegex(CoordinatorError, "bootstrap"):
            wake_agent(
                self.config,
                self.config.agent("demo-bot"),
                self.state,
                run=lambda request: make_result(self.root, None),
            )
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertEqual(status.consecutive_failures, 1)
        self.assertFalse(self.state.is_due("demo-bot"))

    def test_successful_wake_records_envelope_and_schedules_next(self) -> None:
        profile = replace(
            self.config.agents[0],
            agent_kwargs={"base_url": "https://example.invalid/v1"},
        )
        self.config = replace(self.config, agents=(profile,))
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, {"outcome": "worked", "summary": "shipped"})

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            report = wake_agent(
                self.config, self.config.agent("demo-bot"), self.state, run=run
            )

        self.assertEqual(report.outcome, "worked")
        self.assertEqual(report.summary, "shipped")
        self.assertAlmostEqual(report.cost_usd, 0.42)
        request = requests[0]
        self.assertTrue(request.allow_remote_writes)
        self.assertTrue(request.lenient_structured)
        self.assertEqual(
            request.push_remote_url, "http://forge.internal:3000/hub/demo.git"
        )
        self.assertEqual(request.git_auth_token_env, "SWECRAFT_FORGE_TOKEN")
        self.assertEqual(request.fetch_auth_token_env, "SWECRAFT_FORGE_TOKEN")
        # The one-repository maintainer stages nothing beside its home.
        self.assertEqual(request.work_repositories, ())
        # Credentials travel with the request, not through the coordinator's
        # process environment: concurrent wakes carry different tokens, and a
        # shared global would let one wake act as another agent.
        self.assertEqual(request.agent_env["SWECRAFT_FORGE_TOKEN"], "bot-token")
        self.assertEqual(
            request.agent_env["SWECRAFT_FORGE_URL"], "http://forge.internal:3000"
        )
        self.assertEqual(request.agent_env_names, ())
        self.assertEqual(
            request.agent_kwargs, {"base_url": "https://example.invalid/v1"}
        )
        self.assertEqual(request.memory_revision, "agent/memory")
        self.assertIsNone(request.agent_import_path)
        self.assertEqual(request.agent_env["SWECRAFT_WAKE_MODE"], "single-task")
        self.assertNotIn("SWECRAFT_WAKE_TURNS", request.agent_env)
        self.assertNotIn("SWECRAFT_FORGE_TOKEN", os.environ)
        self.assertIn("do one\ncoherent unit of work, and stop", request.prompt)
        self.assertNotIn("[SWECRAFT WAKE]", request.prompt)
        self.assertIn("demo-bot", request.prompt)
        self.assertIn("hub/demo", request.prompt)
        self.assertIn("forge-react allowed", request.prompt)
        self.assertIn("do not react mechanically", request.prompt)
        self.assertIn("read access to the rest of the forge", request.prompt)
        self.assertIn("Adopt sibling releases", request.prompt)
        self.assertIn('"git+$SWECRAFT_FORGE_URL/<owner>/<repo>@<tag>"', request.prompt)
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertEqual(status.last_outcome, "worked")
        self.assertIsNone(status.active_started)
        self.assertFalse(self.state.is_due("demo-bot"))
        wakes, cost = self.state.day_usage("demo-bot")
        self.assertEqual(wakes, 1)
        self.assertAlmostEqual(cost, 0.42)

    def test_configured_agent_class_reaches_the_run_request(self) -> None:
        profile = replace(
            self.config.agents[0],
            agent_import_path="swecraft_runner.harbor_agents:SwecraftPi",
        )
        self.config = replace(self.config, agents=(profile,))
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, {"outcome": "idle", "summary": "quiet"})

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)
        self.assertEqual(
            requests[0].agent_import_path, "swecraft_runner.harbor_agents:SwecraftPi"
        )

    def test_long_wakes_state_their_budget(self) -> None:
        self.config = replace(
            self.config,
            agent_import_path="swecraft_runner.harbor_agents:SwecraftPi",
            wake_mode="long-wake",
            wake_budget=WakeBudget(turns=1000, usd=40),
        )
        requests = []

        def run(request):
            requests.append(request)
            return make_result(
                self.root, {"outcome": "worked", "summary": "kept going"}
            )

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            report = wake_agent(
                self.config, self.config.agent("demo-bot"), self.state, run=run
            )

        self.assertEqual(report.outcome, "worked")
        request = requests[0]
        self.assertEqual(request.agent_env["SWECRAFT_WAKE_MODE"], "long-wake")
        self.assertEqual(request.agent_env["SWECRAFT_WAKE_TURNS"], "1000")
        self.assertEqual(request.agent_env["SWECRAFT_WAKE_USD"], "40.0")
        # The deadline sits under this agent's own timeout (300 s here).
        self.assertEqual(request.agent_env["SWECRAFT_WAKE_DEADLINE_SECONDS"], "270")
        self.assertIn("This is a **long wake**", request.prompt)
        self.assertIn("(1,000 turns or $40)", request.prompt)
        self.assertIn("[SWECRAFT WAKE]", request.prompt)
        self.assertIn("Reply without calling any tool", request.prompt)
        self.assertNotIn("one unit of work per wake", request.prompt)
        # The shared contract is intact.
        self.assertIn("Adopt sibling releases", request.prompt)
        self.assertIn('"outcome": "worked" | "idle" | "blocked"', request.prompt)

    def test_isolated_collaboration_prompt_removes_sibling_work(self) -> None:
        self.config = replace(
            self.config, prompt=PromptPolicy(collaboration="isolated")
        )
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, {"outcome": "idle", "summary": "quiet"})

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)

        prompt = " ".join(requests[0].prompt.split())
        # The treatment is never named; only the access boundary is stated.
        self.assertNotIn("isolat", prompt.lower())
        self.assertIn("You have write access to `hub/demo`.", prompt)
        self.assertIn("Other society repositories may be private", prompt)
        self.assertIn("- **Converse**: answer questions in issues. - **React**", prompt)
        self.assertNotIn("read access to the rest of the forge", prompt)
        self.assertNotIn("forge GET /notifications", prompt)
        self.assertNotIn("Adopt sibling releases", prompt)
        self.assertNotIn('"git+$SWECRAFT_FORGE_URL/<owner>/<repo>@<tag>"', prompt)

    def test_run_budget_is_stated_unless_silent(self) -> None:
        self.config = replace(self.config, budget=RunBudget(wakes=3))
        prompts = []

        def run(request):
            prompts.append(request.prompt)
            return make_result(self.root, {"outcome": "idle", "summary": "quiet"})

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)
        # The wake being prompted is already counted.
        self.assertIn(
            "Your budget for this run is 3 wakes: 1 used, this\none included, and "
            "2 left.",
            prompts[0],
        )
        self.config = replace(self.config, prompt=PromptPolicy(budget="silent"))
        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)
        self.assertNotIn("## Budget", prompts[1])
        self.assertNotIn("budget for this run", prompts[1])
        self.config = replace(
            self.config,
            prompt=PromptPolicy(),
            budget=RunBudget(wakes=3, ledger=LedgerFile("hub/demo", "budget.yaml")),
        )
        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)
        self.assertIn(
            "the file `budget.yaml` on `hub/demo`", " ".join(prompts[2].split())
        )

    def test_collaboration_check_rides_every_nth_wake(self) -> None:
        self.config = replace(
            self.config,
            nudges=Nudges(collaboration=CollaborationNudge(every_wakes=2)),
        )
        seen = []

        def run(request):
            seen.append("## Collaboration check" in request.prompt)
            return make_result(self.root, {"outcome": "idle", "summary": "quiet"})

        for _ in range(3):
            with patch(
                "swecraft_coordinator.wake.ForgeClient", return_value=self._forge()
            ):
                wake_agent(
                    self.config, self.config.agent("demo-bot"), self.state, run=run
                )
        # Wakes 1, 2, 3: the second carries the section.
        self.assertEqual(seen, [False, True, False])
        wakes = self.state.list_wakes(agent="demo-bot")
        marks = [self.state.treatment(wake.id) for wake in reversed(wakes)]
        self.assertEqual(marks, [None, ("collaboration-check", "every 2 wakes"), None])

    def test_below_rate_states_the_forge_record_in_the_check(self) -> None:
        self.config = replace(
            self.config,
            nudges=Nudges(
                collaboration=CollaborationNudge(
                    below_rate=BelowRate(target=0.5, window=4)
                )
            ),
        )
        prompts: list[str] = []

        def run(request):
            prompts.append(request.prompt)
            return make_result(self.root, {"outcome": "worked", "summary": "ok"})

        for _ in range(5):
            with patch(
                "swecraft_coordinator.wake.ForgeClient", return_value=self._forge()
            ):
                wake_agent(
                    self.config, self.config.agent("demo-bot"), self.state, run=run
                )
        # Four completed wakes fill the window; none reached out, so the
        # fifth wake carries the check with the record stated, and the
        # choice of work stays free.
        for prompt in prompts[:4]:
            self.assertNotIn("## Collaboration check", prompt)
        self.assertIn("## Collaboration check", prompts[4])
        self.assertIn(
            "in 0 of them, by the forge's record; this society aims for at least "
            "2 in 4.",
            " ".join(prompts[4].split()),
        )
        self.assertIn("## Choosing this wake's work", prompts[4])
        latest = self.state.list_wakes(agent="demo-bot", limit=1)[0]
        self.assertEqual(
            self.state.treatment(latest.id),
            ("collaboration-check", "0 of 4 reached out"),
        )
        self.assertIsNone(self.state.treatment(latest.id - 1))
        # The runs' ledger now shows a comment on another repository: the
        # rate recovers and the next wake carries nothing.
        ledger = self.root / "runs" / "run-1" / "output" / "forge-actions.jsonl"
        ledger.write_text(
            json.dumps({"repo": "hub/lib", "action": "issue.comment"}) + "\n",
            encoding="utf-8",
        )
        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)
        self.assertNotIn("## Collaboration check", prompts[5])

    def test_malformed_envelope_is_advisory(self) -> None:
        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            report = wake_agent(
                self.config,
                self.config.agent("demo-bot"),
                self.state,
                run=lambda request: make_result(
                    self.root, {"outcome": "amazing", "summary": 7}
                ),
            )
        self.assertIsNone(report.outcome)
        self.assertIsNone(report.summary)

    def test_missing_memory_branch_fails_the_wake(self) -> None:
        forge = self._forge()
        forge.branch_exists.return_value = False
        with patch("swecraft_coordinator.wake.ForgeClient", return_value=forge):
            with self.assertRaisesRegex(CoordinatorError, "memory branch"):
                wake_agent(
                    self.config,
                    self.config.agent("demo-bot"),
                    self.state,
                    run=lambda request: make_result(self.root, None),
                )
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertEqual(status.consecutive_failures, 1)

    def test_uncertain_failure_surfaces_in_the_next_prompt(self) -> None:
        def failing_run(request):
            raise RuntimeError("memory conflict after pushing a branch")

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            with self.assertRaises(RuntimeError):
                wake_agent(
                    self.config,
                    self.config.agent("demo-bot"),
                    self.state,
                    run=failing_run,
                )
        with sqlite3.connect(self.state._path) as connection:
            connection.execute(
                "UPDATE agent_state SET next_due = '2000-01-01T00:00:00+00:00' "
                "WHERE agent = ?",
                ("demo-bot",),
            )
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, {"outcome": "worked", "summary": "ok"})

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)
        prompt = requests[0].prompt
        self.assertIn("did not finish cleanly", prompt)
        self.assertIn("memory conflict after pushing a branch", prompt)
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertFalse(status.last_uncertain)

    def test_failed_wake_bills_trial_receipt_cost_and_redacts_secrets(self) -> None:
        from swecraft_runner.runner import RunnerError

        run_dir = self.root / "runs" / "run-failed"
        receipt = run_dir / "harbor" / "jobs" / "run-failed" / "trial-1" / "result.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps({"agent_result": {"cost_usd": 3.27}}), encoding="utf-8"
        )

        def run(request):
            error = RunnerError("agent timed out; token bot-token leaked here")
            error.run_id = "run-failed"
            error.run_dir = run_dir
            raise error

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            with self.assertRaises(RunnerError):
                wake_agent(
                    self.config, self.config.agent("demo-bot"), self.state, run=run
                )
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertIn("[REDACTED]", status.last_error or "")
        self.assertNotIn("bot-token", status.last_error or "")
        with sqlite3.connect(self.state._path) as connection:
            row = connection.execute(
                "SELECT run_id, cost_usd, error FROM wakes ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row[0], "run-failed")
        self.assertAlmostEqual(row[1], 3.27)
        self.assertNotIn("bot-token", row[2])
        _, cost = self.state.day_usage("demo-bot")
        self.assertAlmostEqual(cost, 3.27)

    def test_failed_wake_uses_configured_pricing_when_cost_is_missing(self) -> None:
        from swecraft_runner.runner import RunnerError

        pricing = ModelPricing(
            input_per_million=0.95,
            cached_input_per_million=0.19,
            output_per_million=4.0,
        )
        self.config = replace(self.config, model_pricing={"test/model": pricing})
        run_dir = self.root / "runs" / "run-failed"
        receipt = run_dir / "harbor" / "jobs" / "run-failed" / "trial-1" / "result.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                {
                    "agent_result": {
                        "n_input_tokens": 2_000_000,
                        "n_cache_tokens": 1_000_000,
                        "n_output_tokens": 500_000,
                        "cost_usd": None,
                    }
                }
            ),
            encoding="utf-8",
        )

        def run(request):
            self.assertEqual(request.model_pricing, pricing)
            error = RunnerError("agent timed out")
            error.run_id = "run-failed"
            error.run_dir = run_dir
            raise error

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            with self.assertRaises(RunnerError):
                wake_agent(
                    self.config, self.config.agent("demo-bot"), self.state, run=run
                )

        _, cost = self.state.day_usage("demo-bot")
        self.assertAlmostEqual(cost, 3.14)

    def test_run_failure_backs_off_and_reraises(self) -> None:
        def run(request):
            raise RuntimeError("harbor exploded")

        with patch("swecraft_coordinator.wake.ForgeClient", return_value=self._forge()):
            with self.assertRaisesRegex(RuntimeError, "harbor exploded"):
                wake_agent(
                    self.config, self.config.agent("demo-bot"), self.state, run=run
                )
        status = self.state.status("demo-bot")
        assert status is not None
        self.assertEqual(status.consecutive_failures, 1)
        self.assertIsNone(status.active_started)
        self.assertFalse(self.state.is_due("demo-bot"))


class WorkRepositoryWakeTests(WakeTests):
    """A dedicated home: the maintained repositories are staged beside it."""

    def test_work_repositories_are_staged_with_push_remotes(self) -> None:
        profile = replace(
            self.config.agents[0],
            home_repo="demo-bot/home",
            write_repos=("hub/demo", "hub/lib"),
        )
        self.config = replace(self.config, agents=(profile,))
        requests = []

        def run(request):
            requests.append(request)
            return make_result(self.root, {"outcome": "worked", "summary": "ok"})

        forge = self._forge()
        forge.get_repository.side_effect = lambda repo: {
            "default_branch": "develop" if repo == "hub/lib" else "main"
        }
        with patch("swecraft_coordinator.wake.ForgeClient", return_value=forge):
            wake_agent(self.config, self.config.agent("demo-bot"), self.state, run=run)

        request = requests[0]
        self.assertEqual(request.repository, "http://127.0.0.1:3400/demo-bot/home.git")
        self.assertEqual(
            request.push_remote_url, "http://forge.internal:3000/demo-bot/home.git"
        )
        self.assertEqual(
            [
                (work.name, work.repository, work.revision, work.push_remote_url)
                for work in request.work_repositories
            ],
            [
                (
                    "demo",
                    "http://127.0.0.1:3400/hub/demo.git",
                    "main",
                    "http://forge.internal:3000/hub/demo.git",
                ),
                (
                    "lib",
                    "http://127.0.0.1:3400/hub/lib.git",
                    "develop",
                    "http://forge.internal:3000/hub/lib.git",
                ),
            ],
        )
        self.assertIn("`demo-bot/home` is your private home", request.prompt)
        self.assertIn("`/app/repos/lib` is `hub/lib`", request.prompt)
        self.assertNotIn("git clone", request.prompt)


if __name__ == "__main__":
    unittest.main()
