import json
import tempfile
import unittest
from pathlib import Path

from swecraft_evaluator.environment import (
    Forge,
    environment_digest,
    load_world,
    parse_environment,
    release_environment,
    seed_environment,
)
from swecraft_evaluator.errors import EvaluatorError


def write_world() -> Path:
    directory = Path(tempfile.mkdtemp())
    path = directory / "world.lock.json"
    path.write_text(
        json.dumps(
            {
                "org": "hub",
                "repos": [
                    {
                        "name": "attrs",
                        "baseline_commit": "a" * 40,
                        "default_branch": "main",
                    },
                    {
                        "name": "pyyaml",
                        "baseline_commit": "b" * 40,
                        "default_branch": "main",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class StubForge(Forge):
    def __init__(self) -> None:
        super().__init__("http://forge.invalid", "token")
        self.heads = {"attrs": "c" * 40, "pyyaml": "d" * 40}
        self.tags = {("attrs", "v1.2.3"): "e" * 40}

    def branch_head(self, org: str, repo: str, branch: str) -> str:
        return self.heads[repo]

    def tag_commit(self, org: str, repo: str, tag: str) -> str:
        try:
            return self.tags[(repo, tag)]
        except KeyError:
            raise EvaluatorError(f"unknown tag {repo}:{tag}") from None


class EnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = load_world(write_world())
        self.forge = StubForge()

    def test_seed_uses_baselines(self) -> None:
        self.assertEqual(
            seed_environment(self.world),
            {"attrs": "a" * 40, "pyyaml": "b" * 40},
        )

    def test_latest_reads_branch_heads(self) -> None:
        self.assertEqual(
            parse_environment("world@latest", self.world, self.forge),
            {"attrs": "c" * 40, "pyyaml": "d" * 40},
        )

    def test_release_moves_only_one_repo(self) -> None:
        environment = release_environment(self.world, self.forge, "attrs", "v1.2.3")
        self.assertEqual(environment["attrs"], "e" * 40)
        self.assertEqual(environment["pyyaml"], "b" * 40)

    def test_release_shortcut_parses(self) -> None:
        environment = parse_environment(
            "world@seed+attrs:v1.2.3", self.world, self.forge
        )
        self.assertEqual(environment["attrs"], "e" * 40)

    def test_release_of_unknown_repo_fails(self) -> None:
        with self.assertRaises(EvaluatorError):
            release_environment(self.world, self.forge, "absent", "v1")

    def test_malformed_shortcuts_fail(self) -> None:
        for spec in ("world@head", "world@seed+attrs", "seed"):
            with self.assertRaises(EvaluatorError):
                parse_environment(spec, self.world, self.forge)

    def test_latest_without_forge_fails(self) -> None:
        with self.assertRaises(EvaluatorError):
            parse_environment("world@latest", self.world, None)

    def test_digest_is_order_independent(self) -> None:
        one = environment_digest({"a": "1", "b": "2"})
        two = environment_digest({"b": "2", "a": "1"})
        self.assertEqual(one, two)
        self.assertEqual(len(one), 12)
        self.assertNotEqual(one, environment_digest({"a": "1", "b": "3"}))


if __name__ == "__main__":
    unittest.main()
