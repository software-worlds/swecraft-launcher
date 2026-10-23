from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swecraft_common.forge import ForgeError

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.budget import (
    agent_cap,
    budget_variables,
    parse_ledger,
    refresh_ledger,
    render_ledger,
)
from swecraft_coordinator.config import (
    AgentProfile,
    CoordinatorConfig,
    LedgerFile,
    PromptPolicy,
    RunBudget,
)
from swecraft_coordinator.state import StateStore


def agent(name: str) -> AgentProfile:
    return AgentProfile(
        name=name,
        home_repo=f"hub/{name}",
        write_repos=(f"hub/{name}", "hub/society"),
        image="image:1",
        model="test/model",
        interval_seconds=600,
        timeout_seconds=300,
    )


def make_config(root: Path, **overrides) -> CoordinatorConfig:
    values = dict(
        forge_url="http://127.0.0.1:3400",
        push_url="http://forge.internal:3000",
        model="test/model",
        agents=(agent("demo-bot"), agent("other-bot")),
        agent_env_sources={},
        runs_dir=root / "runs",
        database=root / "state.db",
        credentials_dir=root / "credentials",
        budget=RunBudget(wakes=100, ledger=LedgerFile("hub/society", "budget.yaml")),
    )
    values.update(overrides)
    return CoordinatorConfig(**values)


class FakeForge:
    """The board as the coordinator reads it: one file, or an error."""

    def __init__(self, found: tuple[str, str] | None = None) -> None:
        self.found = found
        self.error: ForgeError | None = None
        self.reads = 0

    def get_file(self, repo: str, path: str, *, ref: str | None = None):
        self.reads += 1
        assert (repo, path) == ("hub/society", "budget.yaml")
        if self.error is not None:
            raise self.error
        return self.found


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.config = make_config(Path(self._directory.name))

    def test_exactly_the_roster_with_whole_numbers_under_the_total(self) -> None:
        self.assertEqual(
            parse_ledger("demo-bot: 150\nother-bot: 50\n", self.config),
            {"demo-bot": 150, "other-bot": 50},
        )
        self.assertEqual(
            parse_ledger("# moved\nother-bot: 0\ndemo-bot: 200\n", self.config),
            {"demo-bot": 200, "other-bot": 0},
        )
        for text, why in (
            (
                "demo-bot: 101\nother-bot: 100\n",
                "allots 201 wakes but the society has 200",
            ),
            ("demo-bot: 100\n", "missing: other-bot"),
            ("demo-bot: 100\nother-bot: 100\nthird: 0\n", "not on it: third"),
            ("demo-bot: -1\nother-bot: 100\n", "non-negative whole number"),
            ("demo-bot: 1.5\nother-bot: 100\n", "non-negative whole number"),
            ("demo-bot: yes\nother-bot: 100\n", "non-negative whole number"),
            ("- demo-bot\n", "mapping of agent to wakes"),
            ("demo-bot: [\n", "not valid YAML"),
        ):
            with self.assertRaisesRegex(CoordinatorError, why):
                parse_ledger(text, self.config)

    def test_spent_wakes_cannot_be_given(self) -> None:
        # demo-bot started 100 of its 100: it has nothing left to give, and a
        # file that lowers it hands its spent wakes to other-bot.
        used = {"demo-bot": 100, "other-bot": 10}
        with self.assertRaisesRegex(
            CoordinatorError,
            "demo-bot has already started 100 wakes and cannot go below",
        ):
            parse_ledger("demo-bot: 0\nother-bot: 200\n", self.config, used)
        # other-bot, 10 started, may give up to 90.
        self.assertEqual(
            parse_ledger("demo-bot: 190\nother-bot: 10\n", self.config, used),
            {"demo-bot": 190, "other-bot": 10},
        )
        # Giving everything away is a zero line, not an uncapped one.
        self.assertEqual(
            parse_ledger(
                "demo-bot: 200\nother-bot: 0\n", self.config, {"other-bot": 0}
            ),
            {"demo-bot": 200, "other-bot": 0},
        )


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = make_config(self.root)
        self.state = StateStore(self.config.database)

    def test_applies_a_new_version_once_and_caps_from_it(self) -> None:
        self.assertEqual(agent_cap(self.config, self.state, "demo-bot"), 100)
        forge = FakeForge(("a" * 40, "demo-bot: 150\nother-bot: 50\n"))
        noticed: dict[str, str] = {}
        with self.assertLogs("swecraft_coordinator.budget", level="INFO") as logs:
            self.assertTrue(refresh_ledger(self.config, self.state, forge, noticed))
        self.assertIn("demo-bot 100 -> 150, other-bot 100 -> 50", logs.output[0])
        self.assertEqual(agent_cap(self.config, self.state, "demo-bot"), 150)
        self.assertEqual(agent_cap(self.config, self.state, "other-bot"), 50)
        # The same version is not re-applied.
        self.assertFalse(refresh_ledger(self.config, self.state, forge, noticed))
        self.assertEqual(noticed, {})

    def test_bad_versions_keep_the_last_good_one_and_say_so_once(self) -> None:
        noticed: dict[str, str] = {}
        forge = FakeForge(None)
        with self.assertLogs("swecraft_coordinator.budget", level="WARNING") as logs:
            self.assertFalse(refresh_ledger(self.config, self.state, forge, noticed))
        self.assertIn("is missing", logs.output[0])
        with self.assertNoLogs("swecraft_coordinator.budget", level="WARNING"):
            self.assertFalse(refresh_ledger(self.config, self.state, forge, noticed))
        forge.found = ("a" * 40, "demo-bot: 150\nother-bot: 50\n")
        self.assertTrue(refresh_ledger(self.config, self.state, forge, noticed))
        forge.found = ("b" * 40, "demo-bot: 500\nother-bot: 50\n")
        with self.assertLogs("swecraft_coordinator.budget", level="WARNING") as logs:
            self.assertFalse(refresh_ledger(self.config, self.state, forge, noticed))
        self.assertIn("@bbbbbbbb not applied: allots 550 wakes", logs.output[0])
        self.assertEqual(agent_cap(self.config, self.state, "demo-bot"), 150)
        with self.assertNoLogs("swecraft_coordinator.budget", level="WARNING"):
            self.assertFalse(refresh_ledger(self.config, self.state, forge, noticed))
        forge.error = ForgeError("forge down")
        with self.assertLogs("swecraft_coordinator.budget", level="WARNING") as logs:
            self.assertFalse(refresh_ledger(self.config, self.state, forge, noticed))
        self.assertIn("could not be read", logs.output[0])
        self.assertEqual(agent_cap(self.config, self.state, "demo-bot"), 150)

    def test_refresh_refuses_to_move_spent_wakes(self) -> None:
        for _ in range(3):
            wake = self.state.claim("demo-bot", stale_after_seconds=3600)
            self.state.fail(
                "demo-bot",
                wake,
                error="x",
                run_id=None,
                cost_usd=0.0,
                next_due_seconds=0,
            )
        forge = FakeForge(("a" * 40, "demo-bot: 2\nother-bot: 198\n"))
        noticed: dict[str, str] = {}
        with self.assertLogs("swecraft_coordinator.budget", level="WARNING") as logs:
            self.assertFalse(refresh_ledger(self.config, self.state, forge, noticed))
        self.assertIn("already started 3 wakes", logs.output[0])
        self.assertEqual(agent_cap(self.config, self.state, "demo-bot"), 100)
        forge.found = ("b" * 40, "demo-bot: 3\nother-bot: 197\n")
        self.assertTrue(refresh_ledger(self.config, self.state, forge, noticed))
        self.assertEqual(agent_cap(self.config, self.state, "demo-bot"), 3)

    def test_a_zero_line_is_a_cap_of_zero(self) -> None:
        self.state.set_ledger_allocation("a" * 40, {"demo-bot": 0, "other-bot": 200})
        self.assertEqual(agent_cap(self.config, self.state, "demo-bot"), 0)
        variables = budget_variables(self.config, self.state, self.config.agents[0])
        self.assertTrue(variables["BUDGETED"])
        self.assertEqual(variables["BUDGET_WAKES"], 0)
        uncapped = make_config(Path(self._directory.name), budget=RunBudget())
        self.assertIsNone(agent_cap(uncapped, self.state, "demo-bot"))
        self.assertFalse(
            budget_variables(uncapped, self.state, uncapped.agents[0])["BUDGETED"]
        )

    def test_without_a_ledger_nothing_is_read(self) -> None:
        config = make_config(self.root, budget=RunBudget(wakes=100))
        forge = FakeForge(("a" * 40, "demo-bot: 1\nother-bot: 1\n"))
        self.assertFalse(refresh_ledger(config, self.state, forge))
        self.assertEqual(forge.reads, 0)
        self.assertEqual(agent_cap(config, self.state, "demo-bot"), 100)

    def test_render_and_prompt_variables_follow_the_applied_allocation(self) -> None:
        # The seed is the initial split, whatever an old state database holds.
        self.assertEqual(
            render_ledger(self.config).splitlines()[-2:],
            ["demo-bot: 100", "other-bot: 100"],
        )
        self.state.set_ledger_allocation("a" * 40, {"demo-bot": 150, "other-bot": 50})
        self.assertIn("demo-bot: 100", render_ledger(self.config))
        wake = self.state.claim("demo-bot", stale_after_seconds=3600)
        self.assertEqual(
            budget_variables(self.config, self.state, self.config.agents[0]),
            {
                "BUDGETED": True,
                "BUDGET_WAKES": 150,
                "WAKES_USED": 1,
                "WAKES_LEFT": 149,
                "LEDGER_REPO": "hub/society",
                "LEDGER_PATH": "budget.yaml",
            },
        )
        silent = make_config(self.root, prompt=PromptPolicy(budget="silent"))
        self.assertFalse(
            budget_variables(silent, self.state, silent.agents[0])["BUDGETED"]
        )
        self.assertIsInstance(wake, int)


if __name__ == "__main__":
    unittest.main()
