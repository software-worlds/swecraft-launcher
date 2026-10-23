import subprocess
import tempfile
import unittest
from pathlib import Path

import swecraft_evaluator.harness as harness


class TimeoutLogTest(unittest.TestCase):
    def test_timeout_preserves_captured_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            original = harness.OUT
            harness.OUT = out
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    harness._run(
                        {
                            "argv": ["sh", "-c", "echo banner; sleep 5"],
                            "cwd": directory,
                            "timeout_seconds": 0.3,
                        },
                        "slow",
                        check=True,
                    )
            finally:
                harness.OUT = original
            stdout = (out / "slow.stdout.log").read_text(encoding="utf-8")
            self.assertIn("banner", stdout)
            self.assertTrue((out / "slow.stderr.log").exists())


class CodspeedCommandTest(unittest.TestCase):
    def test_codspeed_command_loads_plugins_and_prepends_eval_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = harness.ROOT
            harness.ROOT = root
            try:
                command = {
                    "argv": ["python", "-m", "pytest", "bench.py", "-q"],
                    "env": {
                        "PYTHONPATH": "/extra",
                        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    },
                    "cwd": directory,
                    "timeout_seconds": 30,
                }
                transformed = harness._pytest_codspeed_command(
                    command, ["--codspeed"], {"CODSPEED_ENV": "1"}
                )
                self.assertEqual(
                    transformed["argv"],
                    [
                        "python",
                        "-m",
                        "pytest",
                        "bench.py",
                        "-q",
                        "-p",
                        "no:codspeed",
                        "-p",
                        "pytest_codspeed.plugin",
                        "-p",
                        harness.MIN_TIME_PLUGIN,
                        "--codspeed",
                    ],
                )
                self.assertEqual(transformed["env"]["CODSPEED_ENV"], "1")
                self.assertTrue(transformed["env"]["PYTHONPATH"].startswith(f"{root}:"))
                self.assertTrue((root / f"{harness.MIN_TIME_PLUGIN}.py").exists())
            finally:
                harness.ROOT = original

    def test_codspeed_command_inserts_pytest_flags_before_double_dash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = harness.ROOT
            harness.ROOT = Path(directory)
            try:
                transformed = harness._pytest_codspeed_command(
                    {
                        "argv": ["pytest", "bench.py", "--", "--subject-flag"],
                        "cwd": directory,
                        "timeout_seconds": 30,
                    },
                    ["--codspeed"],
                )
            finally:
                harness.ROOT = original
        separator = transformed["argv"].index("--")
        self.assertIn("--codspeed", transformed["argv"][:separator])
        self.assertEqual(transformed["argv"][separator:], ["--", "--subject-flag"])


class ThreadCapDefaultsTest(unittest.TestCase):
    def test_caps_apply_as_defaults_and_recipes_still_override(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            for name in harness.THREAD_CAP_DEFAULTS:
                os.environ.pop(name, None)
            os.environ["OMP_NUM_THREADS"] = "16"  # a recipe/operator choice
            for name, value in harness.THREAD_CAP_DEFAULTS.items():
                os.environ.setdefault(name, value)
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "16")
            self.assertEqual(os.environ["OPENBLAS_NUM_THREADS"], "4")
            self.assertEqual(os.environ["MKL_NUM_THREADS"], "4")

    def test_per_command_env_wins_over_process_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            original = harness.OUT
            harness.OUT = out
            try:
                import os
                from unittest.mock import patch

                with patch.dict(os.environ, {"OPENBLAS_NUM_THREADS": "4"}):
                    harness._run(
                        {
                            "argv": ["sh", "-c", "echo $OPENBLAS_NUM_THREADS"],
                            "cwd": directory,
                            "env": {"OPENBLAS_NUM_THREADS": "32"},
                            "timeout_seconds": 10,
                        },
                        "override",
                        check=True,
                    )
            finally:
                harness.OUT = original
            stdout = (out / "override.stdout.log").read_text(encoding="utf-8")
            self.assertEqual(stdout.strip(), "32")


if __name__ == "__main__":
    unittest.main()
