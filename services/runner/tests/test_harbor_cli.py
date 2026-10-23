"""The swecraft-harbor launcher's scrub switch, exercised without Harbor."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from swecraft_runner import harbor_cli
from swecraft_runner.runner import DEFAULT_HARBOR_COMMAND


def make_trial_class() -> type:
    class FakeTrial:
        # How often Harbor's own scrub ran.
        scrubs: int = 0

        def __init__(self, env: dict[str, str]) -> None:
            self.extra_env = env

        def _scrub_jobs_dir(self) -> str:
            type(self).scrubs += 1
            return "scrubbed"

    return FakeTrial


class ScrubSettingTests(unittest.TestCase):
    def test_unset_and_blank_mean_off(self) -> None:
        self.assertEqual(harbor_cli.scrub_setting({}), "off")
        self.assertEqual(harbor_cli.scrub_setting({harbor_cli.SCRUB_ENV: " "}), "off")

    def test_values_are_case_insensitive(self) -> None:
        for raw, setting in (("ON", "on"), ("Off", "off"), (" on ", "on")):
            self.assertEqual(
                harbor_cli.scrub_setting({harbor_cli.SCRUB_ENV: raw}), setting
            )

    def test_unknown_value_is_refused(self) -> None:
        for raw in ("floor", "1", "true", "nope"):
            with self.assertRaises(ValueError) as caught:
                harbor_cli.scrub_setting({harbor_cli.SCRUB_ENV: raw})
            self.assertIn(harbor_cli.SCRUB_ENV, str(caught.exception))


class ScrubSwitchTests(unittest.TestCase):
    def test_off_makes_the_scrub_a_no_op_and_leaves_the_env_alone(self) -> None:
        trial_class = make_trial_class()
        self.assertTrue(harbor_cli.apply_scrub_switch(trial_class, "off"))
        env = {"SWECRAFT_FORGE_TOKEN": "t" * 40, "CLAUDE_FORCE_OAUTH": "1"}
        trial = trial_class(dict(env))
        self.assertIsNone(trial._scrub_jobs_dir())
        self.assertEqual(trial_class.scrubs, 0)
        self.assertEqual(trial.extra_env, env)

    def test_on_leaves_the_stock_scrub_untouched(self) -> None:
        trial_class = make_trial_class()
        original = trial_class._scrub_jobs_dir
        self.assertFalse(harbor_cli.apply_scrub_switch(trial_class, "on"))
        self.assertIs(trial_class._scrub_jobs_dir, original)
        self.assertEqual(trial_class({})._scrub_jobs_dir(), "scrubbed")
        self.assertEqual(trial_class.scrubs, 1)

    def test_applies_once(self) -> None:
        trial_class = make_trial_class()
        self.assertTrue(harbor_cli.apply_scrub_switch(trial_class, "off"))
        replaced = trial_class._scrub_jobs_dir
        self.assertFalse(harbor_cli.apply_scrub_switch(trial_class, "off"))
        self.assertIs(trial_class._scrub_jobs_dir, replaced)

    def test_default_setting_is_off(self) -> None:
        self.assertEqual(harbor_cli.DEFAULT_SCRUB_SETTING, "off")
        self.assertEqual(harbor_cli.scrub_setting({}), harbor_cli.DEFAULT_SCRUB_SETTING)


class LauncherTests(unittest.TestCase):
    def test_the_default_command_runs_the_declared_entry_point(self) -> None:
        pyproject = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )

        scripts = pyproject["project"]["scripts"]
        self.assertEqual(
            scripts[DEFAULT_HARBOR_COMMAND[-1]], "swecraft_runner.harbor_cli:main"
        )
        self.assertTrue(callable(harbor_cli.main))
