from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import (
    CoordinatorConfig,
    resolve_agent_env,
    wake_deadline,
)

MINIMAL = """
model: test/model
agents:
  - name: demo-bot
    repo: hub/demo
"""


def load(text: str) -> CoordinatorConfig:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "coordinator.yaml"
        path.write_text(text, encoding="utf-8")
        return CoordinatorConfig.load(path)


class ConfigTests(unittest.TestCase):
    def test_minimal_config_gets_defaults(self) -> None:
        config = load(MINIMAL)
        self.assertEqual(config.forge_url, "http://127.0.0.1:3400")
        self.assertEqual(config.push_url, "http://host.docker.internal:3400")
        self.assertEqual(config.memory_branch, "agent/memory")
        self.assertEqual(config.prompt.collaboration, "shared")
        self.assertEqual(config.prompt.budget, "stated")
        self.assertEqual(config.budget.wakes, 0)
        self.assertEqual(config.daily_wake_limit, 48)
        self.assertEqual(config.checkpoint_config, Path(".swecraft/checkpoint.yaml"))
        profile = config.agent("demo-bot")
        self.assertEqual(profile.home_repo, "hub/demo")
        self.assertEqual(profile.write_repos, ("hub/demo",))
        self.assertFalse(profile.dedicated_home)
        self.assertEqual(profile.work_repos, ())
        self.assertEqual(profile.interval_seconds, 1_800)
        self.assertEqual(profile.model, "test/model")
        self.assertEqual(
            config.agent_env_sources, {"ANTHROPIC_API_KEY": "env:ANTHROPIC_API_KEY"}
        )

    def test_agent_overrides_and_push_url(self) -> None:
        config = load(
            MINIMAL
            + "    interval_seconds: 600\n    image: custom:1\npush_url: http://forge:3000\n"
        )
        profile = config.agent("demo-bot")
        self.assertEqual(profile.interval_seconds, 600)
        self.assertEqual(profile.image, "custom:1")
        self.assertEqual(config.push_url, "http://forge:3000")

    def test_prompt_policy_is_explicitly_bounded(self) -> None:
        config = load(MINIMAL + "prompt:\n  collaboration: isolated\n")
        self.assertEqual(config.prompt.collaboration, "isolated")
        with self.assertRaisesRegex(CoordinatorError, "prompt.collaboration"):
            load(MINIMAL + "prompt:\n  collaboration: open-ended\n")
        with self.assertRaisesRegex(CoordinatorError, "unknown prompt keys"):
            load(MINIMAL + "prompt:\n  tone: brisk\n")
        with self.assertRaisesRegex(CoordinatorError, "prompt must be a mapping"):
            load(MINIMAL + "prompt: isolated\n")
        # The setting lives under prompt:, not at the top level.
        with self.assertRaisesRegex(CoordinatorError, "collaboration"):
            load(MINIMAL + "collaboration: isolated\n")

    def test_collaboration_nudge_conditions(self) -> None:
        config = load(MINIMAL)
        self.assertFalse(config.nudges.collaboration.enabled)
        cadence = load(MINIMAL + "nudges:\n  collaboration:\n    every_wakes: 3\n")
        self.assertEqual(cadence.nudges.collaboration.every_wakes, 3)
        self.assertTrue(cadence.nudges.collaboration.enabled)
        self.assertFalse(cadence.nudges.collaboration.below_rate.enabled)
        record = load(
            MINIMAL
            + "nudges:\n  collaboration:\n    below_rate:\n      target: 0.4\n"
            + "      window: 8\n"
        )
        below = record.nudges.collaboration.below_rate
        self.assertEqual((below.target, below.window), (0.4, 8))
        self.assertTrue(below.enabled)
        self.assertTrue(record.nudges.collaboration.enabled)
        self.assertEqual(record.nudges.collaboration.every_wakes, 0)
        both = load(
            MINIMAL
            + "nudges:\n  collaboration:\n    below_rate: {target: 0.5}\n"
            + "    every_wakes: 2\n"
        )
        self.assertEqual(both.nudges.collaboration.below_rate.window, 10)
        self.assertEqual(both.nudges.collaboration.every_wakes, 2)
        # The nudge is a prompt section only: nothing reaches the harness env.
        self.assertEqual(
            both.wake_environment(60), {"SWECRAFT_WAKE_MODE": "single-task"}
        )
        for bad, why in (
            ("nudges: 3\n", "nudges must be a mapping"),
            ("nudges:\n  reminders: {}\n", "unknown nudges keys"),
            ("nudges:\n  collaboration: 3\n", "must be a mapping"),
            (
                "nudges:\n  collaboration:\n    every_turns: 30\n",
                "unknown nudges.collaboration keys",
            ),
            (
                "nudges:\n  collaboration:\n    every_wakes: -1\n",
                "non-negative whole number",
            ),
            (
                "nudges:\n  collaboration:\n    every_wakes: true\n",
                "non-negative whole number",
            ),
            (
                "nudges:\n  collaboration:\n    below_rate:\n      target: 1.5\n",
                "between 0 and 1",
            ),
            (
                "nudges:\n  collaboration:\n    below_rate:\n      target: yes\n",
                "between 0 and 1",
            ),
            (
                "nudges:\n  collaboration:\n    below_rate:\n      window: 2\n",
                "at least 3",
            ),
            (
                "nudges:\n  collaboration:\n    below_rate:\n      window: 2.5\n",
                "at least 3",
            ),
            (
                "nudges:\n  collaboration:\n    below_rate:\n      then: ask\n",
                "unknown nudges.collaboration.below_rate keys",
            ),
            (
                "prompt:\n  collaboration: isolated\nnudges:\n  collaboration:\n"
                "    every_wakes: 3\n",
                "no meaning under prompt.collaboration: isolated",
            ),
            # The setting's old home says where it went.
            ("prompt:\n  collaboration_check:\n    every_wakes: 3\n", "has moved"),
        ):
            with self.assertRaisesRegex(CoordinatorError, why):
                load(MINIMAL + bad)

    def test_finalize_nudge_places_the_end_of_wake_notice(self) -> None:
        long = (
            "agent: pi\nagent_import_path: swecraft_runner.harbor_agents:SwecraftPi\n"
            "wake_mode: long-wake\ntimeout_seconds: 604800\n"
        )
        config = load(
            MINIMAL
            + long
            + "wake_budget: {turns: 100}\nnudges:\n  finalize: {at: 0.9}\n"
        )
        self.assertEqual(config.nudges.finalize_at, 0.9)
        self.assertEqual(config.wake_budget.finalize_at, 0.9)
        self.assertEqual(
            config.wake_environment(604800)["SWECRAFT_WAKE_FINALIZE_AT"], "0.9"
        )
        # The old spelling still works, and agreeing spellings may coexist.
        old = load(MINIMAL + long + "wake_budget: {turns: 100, finalize_at: 0.9}\n")
        self.assertEqual(old.wake_budget.finalize_at, 0.9)
        self.assertIsNone(old.nudges.finalize_at)
        both = load(
            MINIMAL
            + long
            + "wake_budget: {turns: 100, finalize_at: 0.9}\n"
            + "nudges:\n  finalize: {at: 0.9}\n"
        )
        self.assertEqual(both.wake_budget.finalize_at, 0.9)
        self.assertIsNone(load(MINIMAL).nudges.finalize_at)
        for bad, why in (
            (
                "wake_budget: {turns: 100, finalize_at: 0.8}\n"
                "nudges:\n  finalize: {at: 0.9}\n",
                "disagree",
            ),
            (
                "wake_budget: {turns: 100}\nnudges:\n  finalize: {at: 1.2}\n",
                "finalize_at",
            ),
            (
                "wake_budget: {turns: 100}\nnudges:\n  finalize: {at: soon}\n",
                "must be a number",
            ),
            (
                "wake_budget: {turns: 100}\nnudges:\n  finalize: {after: 0.9}\n",
                "unknown nudges.finalize keys",
            ),
            (
                "wake_budget: {turns: 100}\nnudges:\n  finalize: 0.9\n",
                "nudges.finalize must be a mapping",
            ),
        ):
            with self.assertRaisesRegex(CoordinatorError, why):
                load(MINIMAL + long + bad)

    def test_run_budget_is_a_whole_number_of_wakes(self) -> None:
        self.assertEqual(load(MINIMAL + "budget:\n  wakes: 100\n").budget.wakes, 100)
        self.assertEqual(load(MINIMAL + "budget: {}\n").budget.wakes, 0)
        for bad in (
            "budget: 100\n",
            "budget:\n  wakes: -1\n",
            "budget:\n  wakes: true\n",
            "budget:\n  wakes: 1.5\n",
            "budget:\n  usd: 5\n",
        ):
            with self.assertRaisesRegex(CoordinatorError, "budget"):
                load(MINIMAL + bad)
        # The disclosure is a prompt setting, apart from the cap.
        config = load(MINIMAL + "budget:\n  wakes: 100\nprompt:\n  budget: silent\n")
        self.assertEqual(config.prompt.budget, "silent")
        with self.assertRaisesRegex(CoordinatorError, "prompt.budget"):
            load(MINIMAL + "prompt:\n  budget: loud\n")

    def test_budget_ledger_names_a_file_every_agent_maintains(self) -> None:
        board = MINIMAL.replace(
            "    repo: hub/demo\n", "    repo: hub/demo\n    repos: [hub/society]\n"
        )
        config = load(
            board + "budget:\n  wakes: 100\n  ledger: hub/society/budget.yaml\n"
        )
        assert config.budget.ledger is not None
        self.assertEqual(config.budget.ledger.repo, "hub/society")
        self.assertEqual(config.budget.ledger.path, "budget.yaml")
        self.assertEqual(str(config.budget.ledger), "hub/society/budget.yaml")
        nested = load(
            board + "budget:\n  wakes: 100\n  ledger: hub/society/ops/budget.yaml\n"
        )
        self.assertEqual(nested.budget.ledger.path, "ops/budget.yaml")  # type: ignore[union-attr]
        # It needs a budget to move, a real path, and a repository the agents maintain.
        with self.assertRaisesRegex(CoordinatorError, "set budget.wakes"):
            load(board + "budget:\n  ledger: hub/society/budget.yaml\n")
        for bad in ("hub/society", "hub/society/../x", "budget.yaml"):
            with self.assertRaisesRegex(CoordinatorError, "budget.ledger must be"):
                load(board + f"budget:\n  wakes: 100\n  ledger: {bad}\n")
        with self.assertRaisesRegex(
            CoordinatorError, "not in write_repos of: demo-bot"
        ):
            load(MINIMAL + "budget:\n  wakes: 100\n  ledger: hub/society/budget.yaml\n")

    def test_evaluate_cadence_requires_a_command(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "evaluate_command"):
            load(MINIMAL + "evaluate_every_wakes: 20\n")
        config = load(
            MINIMAL
            + "evaluate_every_wakes: 20\n"
            + "evaluate_command: [swecraft-evaluator, run, society-general]\n"
        )
        self.assertEqual(config.evaluate_every_wakes, 20)
        self.assertEqual(
            config.evaluate_command, ("swecraft-evaluator", "run", "society-general")
        )

    def test_rejects_unknown_keys_and_bad_agents(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "unknown config keys"):
            load(MINIMAL + "surprise: 1\n")
        with self.assertRaisesRegex(CoordinatorError, "required setting"):
            load("agents:\n  - name: a\n    repo: hub/a\n")
        with self.assertRaisesRegex(CoordinatorError, "non-empty list"):
            load("model: m\nagents: []\n")
        with self.assertRaisesRegex(CoordinatorError, "lowercase forge username"):
            load("model: m\nagents:\n  - name: Bad_Name\n    repo: hub/a\n")
        with self.assertRaisesRegex(CoordinatorError, "owner/name"):
            load("model: m\nagents:\n  - name: a-bot\n    repo: nope\n")
        with self.assertRaisesRegex(CoordinatorError, "unique"):
            load(
                "model: m\nagents:\n"
                "  - name: a-bot\n    repo: hub/a\n"
                "  - name: a-bot\n    repo: hub/b\n"
            )

    def test_per_agent_overrides_are_validated_like_globals(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "interval_seconds"):
            load(MINIMAL + "    interval_seconds: 1\n")
        with self.assertRaisesRegex(CoordinatorError, "timeout_seconds"):
            load(MINIMAL + "    timeout_seconds: 0\n")
        with self.assertRaisesRegex(CoordinatorError, "must be an integer"):
            load(MINIMAL + "    interval_seconds: soon\n")

    def test_import_path_agents(self) -> None:
        config = load(MINIMAL)
        self.assertEqual(config.agent_import_path, "")
        self.assertEqual(config.agent("demo-bot").run_request_overrides(), {})
        config = load(
            MINIMAL
            + "agent: pi\nagent_import_path: swecraft_runner.harbor_agents:SwecraftPi\n"
            + "agent_kwargs: {thinking: high}\n"
        )
        self.assertEqual(
            config.agent_import_path, "swecraft_runner.harbor_agents:SwecraftPi"
        )
        profile = config.agent("demo-bot")
        self.assertEqual(
            profile.agent_import_path, "swecraft_runner.harbor_agents:SwecraftPi"
        )
        self.assertEqual(
            profile.run_request_overrides(),
            {"agent_import_path": "swecraft_runner.harbor_agents:SwecraftPi"},
        )
        self.assertEqual(profile.agent, "pi")
        self.assertEqual(profile.agent_kwargs, {"thinking": "high"})
        with self.assertRaisesRegex(CoordinatorError, "module.path:ClassName"):
            load(
                MINIMAL
                + "agent_import_path: swecraft_runner.harbor_agents.SwecraftPi\n"
            )

    def test_long_wakes_need_a_budget_and_a_harness(self) -> None:
        config = load(MINIMAL)
        self.assertEqual(config.wake_mode, "single-task")
        self.assertFalse(config.long_wake)
        self.assertEqual(
            config.wake_environment(1800), {"SWECRAFT_WAKE_MODE": "single-task"}
        )
        pi = "agent: pi\nagent_import_path: swecraft_runner.harbor_agents:SwecraftPi\n"
        config = load(
            MINIMAL
            + pi
            + "wake_mode: long-wake\ntimeout_seconds: 604800\n"
            + "wake_budget: {turns: 1000, usd: 40}\n"
        )
        self.assertTrue(config.long_wake)
        self.assertEqual(config.wake_budget.describe(), "1,000 turns or $40")
        self.assertEqual(
            config.wake_environment(604800),
            {
                "SWECRAFT_WAKE_MODE": "long-wake",
                "SWECRAFT_WAKE_TURNS": "1000",
                "SWECRAFT_WAKE_SECONDS": "0",
                "SWECRAFT_WAKE_USD": "40.0",
                "SWECRAFT_WAKE_FINALIZE_AT": "0.97",
                "SWECRAFT_WAKE_HARD_STOP_AT": "1.05",
                "SWECRAFT_WAKE_DEADLINE_SECONDS": "603900",
            },
        )
        # The deadline keeps a margin under the runner's timeout, never more
        # than a tenth of a short one.
        self.assertEqual(wake_deadline(1800), 1620)
        self.assertEqual(wake_deadline(604800), 603900)
        self.assertEqual(
            load(
                MINIMAL
                + pi
                + "wake_mode: long-wake\nwake_budget: {seconds: 7200}\n"
                + "timeout_seconds: 8000\n"
            ).wake_budget.describe(),
            "2 hours",
        )
        with self.assertRaisesRegex(CoordinatorError, "requires a wake_budget"):
            load(MINIMAL + pi + "wake_mode: long-wake\n")
        with self.assertRaisesRegex(CoordinatorError, "missing on: demo-bot"):
            load(MINIMAL + "wake_mode: long-wake\nwake_budget: {turns: 10}\n")
        # Every agent must run on the enforcing harness, not just the default.
        mixed = MINIMAL.replace(
            "  - name: demo-bot\n    repo: hub/demo\n",
            "  - name: demo-bot\n    repo: hub/demo\n"
            "    agent_import_path: swecraft_runner.harbor_agents:SwecraftPi\n"
            "  - name: other-bot\n    repo: hub/other\n",
        )
        with self.assertRaisesRegex(CoordinatorError, "missing on: other-bot"):
            load(mixed + "wake_mode: long-wake\nwake_budget: {turns: 10}\n")
        with self.assertRaisesRegex(CoordinatorError, "requires wake_mode: long-wake"):
            load(MINIMAL + "wake_budget: {turns: 10}\n")
        with self.assertRaisesRegex(CoordinatorError, "timeout_seconds must cover"):
            load(MINIMAL + pi + "wake_mode: long-wake\nwake_budget: {seconds: 7200}\n")
        with self.assertRaisesRegex(CoordinatorError, "finalize_at"):
            load(
                MINIMAL
                + pi
                + "wake_mode: long-wake\n"
                + "wake_budget: {turns: 10, finalize_at: 1.2, hard_stop_at: 1.1}\n"
            )
        with self.assertRaisesRegex(CoordinatorError, "unknown wake_budget keys"):
            load(MINIMAL + "wake_budget: {tokens: 10}\n")
        with self.assertRaisesRegex(CoordinatorError, "wake_mode must be one of"):
            load(MINIMAL + "wake_mode: forever\n")

    def test_agents_can_mix_harnesses(self) -> None:
        # One agent on pi, the other on stock claude-code, in one society.
        config = load(
            """
model: anthropic/claude-opus-5
agents:
  - name: pi-bot
    repo: hub/a
    agent: pi
    agent_import_path: swecraft_runner.harbor_agents:SwecraftPi
  - name: cc-bot
    repo: hub/b
"""
        )
        self.assertEqual(
            config.agent("pi-bot").run_request_overrides(),
            {"agent_import_path": "swecraft_runner.harbor_agents:SwecraftPi"},
        )
        self.assertEqual(config.agent("cc-bot").run_request_overrides(), {})
        self.assertEqual(config.agent("cc-bot").agent, "claude-code")
        with self.assertRaisesRegex(CoordinatorError, "module.path:ClassName"):
            load(MINIMAL + "    agent_import_path: nope\n")

    def test_unknown_agent_lookup_fails(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "unknown agent"):
            load(MINIMAL).agent("missing")

    def test_model_pricing_is_keyed_by_exact_harbor_model(self) -> None:
        model = "openai/@cf/moonshotai/kimi-k2.7-code"
        config = load(
            f'''\
model: "{model}"
model_pricing:
  "{model}":
    input_per_million: 0.95
    cached_input_per_million: 0.19
    output_per_million: 4.0
agents:
  - name: demo-bot
    repo: hub/demo
'''
        )

        pricing = config.pricing_for(model)
        assert pricing is not None
        self.assertEqual(pricing.input_per_million, 0.95)
        self.assertEqual(pricing.cached_input_per_million, 0.19)
        self.assertEqual(pricing.output_per_million, 4.0)
        self.assertIsNone(config.pricing_for("openai/another-model"))

    def test_invalid_model_pricing_is_rejected(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "model_pricing must be"):
            load(MINIMAL + "model_pricing: []\n")
        with self.assertRaisesRegex(CoordinatorError, "missing pricing keys"):
            load(MINIMAL + "model_pricing:\n  test/model:\n    input_per_million: 1\n")
        with self.assertRaisesRegex(CoordinatorError, "finite non-negative"):
            load(
                MINIMAL
                + "model_pricing:\n"
                + "  test/model:\n"
                + "    input_per_million: -1\n"
                + "    output_per_million: 2\n"
            )

    def test_secret_indirection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "secret"
            secret.write_text("value\n", encoding="utf-8")
            config = load(MINIMAL + f"agent_env:\n  API_KEY: file:{secret}\n")
            self.assertEqual(resolve_agent_env(config), {"API_KEY": "value"})

    def test_per_agent_agent_env_layers_over_the_global_map(self) -> None:
        config = load(
            """
model: test/model
agent_env:
  TOKEN: shared-token
  SHARED: shared-value
agents:
  - name: first-bot
    repo: hub/first
  - name: second-bot
    repo: hub/second
    agent_env:
      TOKEN: second-token
"""
        )
        first, second = config.agents
        self.assertEqual(
            resolve_agent_env(config, first),
            {"TOKEN": "shared-token", "SHARED": "shared-value"},
        )
        self.assertEqual(
            resolve_agent_env(config, second),
            {"TOKEN": "second-token", "SHARED": "shared-value"},
        )
        # Without a profile the global map is unchanged.
        self.assertEqual(
            resolve_agent_env(config),
            {"TOKEN": "shared-token", "SHARED": "shared-value"},
        )

    def test_per_agent_agent_env_can_add_a_key_absent_globally(self) -> None:
        config = load(
            """
model: test/model
agent_env:
  SHARED: shared-value
agents:
  - name: demo-bot
    repo: hub/demo
    agent_env:
      EXTRA: only-this-agent
"""
        )
        self.assertEqual(
            resolve_agent_env(config, config.agents[0]),
            {"SHARED": "shared-value", "EXTRA": "only-this-agent"},
        )

    def test_invalid_per_agent_agent_env_is_rejected(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "agent environment variable"):
            load(MINIMAL + "    agent_env:\n      'bad name': x\n")

    def test_agent_kwargs_layer_and_reject_credentials(self) -> None:
        config = load(
            """
model: openai/@cf/moonshotai/kimi-k2.7-code
agent: kimi-cli
agent_kwargs:
  base_url: https://global.invalid/v1
  version: 1.2.3
agents:
  - name: first-bot
    repo: hub/first
  - name: second-bot
    repo: hub/second
    agent_kwargs:
      base_url: https://second.invalid/v1
"""
        )
        first, second = config.agents
        self.assertEqual(first.agent_kwargs, config.agent_kwargs)
        self.assertEqual(
            second.agent_kwargs,
            {
                "base_url": "https://second.invalid/v1",
                "version": "1.2.3",
            },
        )
        with self.assertRaisesRegex(CoordinatorError, "must use agent_env"):
            load(MINIMAL + "agent_kwargs:\n  api_key: do-not-store\n")

    def test_agent_defaults_and_overrides_per_agent(self) -> None:
        config = load(MINIMAL)
        self.assertEqual(config.agent("demo-bot").agent, "claude-code")
        config = load(
            """
model: test/model
agent: codex
agents:
  - name: first-bot
    repo: hub/first
  - name: second-bot
    repo: hub/second
    agent: claude-code
"""
        )
        first, second = config.agents
        self.assertEqual(first.agent, "codex")
        self.assertEqual(second.agent, "claude-code")

    def test_empty_agent_is_rejected(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "agent must not be empty"):
            load(MINIMAL + "agent: ''\n")

    def test_missing_admin_token_only_fails_when_needed(self) -> None:
        config = load(MINIMAL)
        with self.assertRaisesRegex(CoordinatorError, "admin_token"):
            config.resolved_admin_token()
        config = load(MINIMAL + "admin_token: literal-token\n")
        self.assertEqual(config.resolved_admin_token(), "literal-token")

    def test_api_defaults_to_loopback(self) -> None:
        config = load(MINIMAL)
        self.assertEqual(config.api_host, "127.0.0.1")
        self.assertEqual(config.api_port, 3_420)

    def test_api_settings_and_token_indirection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "api_token"
            secret.write_text("api-secret\n", encoding="utf-8")
            config = load(
                MINIMAL
                + f"api_token: file:{secret}\napi_host: 0.0.0.0\napi_port: 9999\n"
            )
            self.assertEqual(config.api_host, "0.0.0.0")
            self.assertEqual(config.api_port, 9999)
            self.assertEqual(config.resolved_api_token(), "api-secret")

    def test_missing_api_token_only_fails_when_serving(self) -> None:
        config = load(MINIMAL)
        self.assertEqual(config.api_token_source, "")
        with self.assertRaisesRegex(CoordinatorError, "api_token"):
            config.resolved_api_token()


class RepositoryLayoutTests(unittest.TestCase):
    def test_explicit_home_and_write_repos(self) -> None:
        config = load(
            "model: test/model\nagents:\n  - name: avery-bot\n"
            "    home_repo: avery-bot/home\n"
            "    write_repos: [hub/requests, hub/urllib3]\n"
        )
        profile = config.agent("avery-bot")
        self.assertEqual(profile.home_repo, "avery-bot/home")
        self.assertEqual(profile.write_repos, ("hub/requests", "hub/urllib3"))
        self.assertTrue(profile.dedicated_home)
        self.assertEqual(profile.work_repos, ("hub/requests", "hub/urllib3"))
        self.assertEqual(
            profile.grant_repos, ("avery-bot/home", "hub/requests", "hub/urllib3")
        )

    def test_legacy_repo_and_repos_normalize_to_a_maintained_home(self) -> None:
        config = load(
            "model: test/model\nagents:\n  - name: demo-bot\n    repo: hub/demo\n"
            "    repos: [hub/lib]\n"
        )
        profile = config.agent("demo-bot")
        self.assertEqual(profile.home_repo, "hub/demo")
        self.assertEqual(profile.write_repos, ("hub/demo", "hub/lib"))
        self.assertFalse(profile.dedicated_home)
        self.assertEqual(profile.work_repos, ("hub/lib",))
        self.assertEqual(profile.grant_repos, ("hub/demo", "hub/lib"))

    def test_the_two_forms_do_not_mix(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "not both"):
            load(
                "model: test/model\nagents:\n  - name: demo-bot\n    repo: hub/demo\n"
                "    write_repos: [hub/demo]\n"
            )

    def test_invalid_layouts_are_rejected(self) -> None:
        cases = {
            "write_repos must name": (
                "  - name: a-bot\n    home_repo: a-bot/home\n    write_repos: []\n"
            ),
            "owner/name": (
                "  - name: a-bot\n    home_repo: a-bot/home\n    write_repos: [nope]\n"
            ),
            "must be distinct": (
                "  - name: a-bot\n    home_repo: a-bot/home\n"
                "    write_repos: [hub/x, hub/x]\n"
            ),
            "must not repeat repo": (
                "  - name: a-bot\n    repo: hub/x\n    repos: [hub/x]\n"
            ),
            "distinct names": (
                "  - name: a-bot\n    home_repo: a-bot/home\n"
                "    write_repos: [hub/x, other/x]\n"
            ),
            "must be a list": (
                "  - name: a-bot\n    home_repo: a-bot/home\n    write_repos: hub/x\n"
            ),
        }
        for message, agents in cases.items():
            with self.subTest(message):
                with self.assertRaisesRegex(CoordinatorError, message):
                    load("model: test/model\nagents:\n" + agents)

    def test_homes_are_distinct_across_agents(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "distinct"):
            load(
                "model: test/model\nagents:\n  - name: a-bot\n    repo: hub/x\n"
                "  - name: b-bot\n    repo: hub/x\n"
            )


if __name__ == "__main__":
    unittest.main()
