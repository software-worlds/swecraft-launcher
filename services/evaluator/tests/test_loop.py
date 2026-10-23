import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swecraft_evaluator.environment import (
    Forge,
    environment_digest,
    load_world,
    seed_environment,
)
from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.loop import LoopSettings, reconcile
from swecraft_evaluator.manifest import load_manifest
from swecraft_evaluator.runner import RunnerSettings, RunRecord

MANIFEST = """
world_lock: world.lock.json
subjects:
  attrs:
    install:
      - argv: [pip, install, /eval/sources/attrs]
evaluators:
  ray:
    image: eval-ray:1
    subjects: [attrs]
    gate: {argv: [pytest]}
    tasks:
      bench: {command: {argv: [python, a.py]}, metrics: [walltime]}
evaluations:
  society-general:
    tasks: [ray@bench]
    metric: walltime
  downstream-attrs:
    tasks: [ray@bench]
    metric: walltime
"""

WORLD = {
    "org": "hub",
    "repos": [{"name": "attrs", "baseline_commit": "a" * 40, "default_branch": "main"}],
}


class StubForge(Forge):
    def __init__(self) -> None:
        super().__init__("http://forge.invalid", "token")
        self.heads = {"attrs": "b" * 40}
        self.release_tags: list[str] = []

    def branch_head(self, org: str, repo: str, branch: str) -> str:
        return self.heads[repo]

    def tag_commit(self, org: str, repo: str, tag: str) -> str:
        return "c" * 40

    def releases(
        self, org: str, repo: str, *, limit: int = 10, page: int = 1
    ) -> list[str]:
        start = (page - 1) * limit
        return list(self.release_tags[start : start + limit])


class LoopTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp())
        (base / "evaluation.yaml").write_text(MANIFEST, encoding="utf-8")
        (base / "world.lock.json").write_text(json.dumps(WORLD), encoding="utf-8")
        self.manifest = load_manifest(base / "evaluation.yaml")
        self.world = load_world(self.manifest.world_lock)
        self.forge = StubForge()
        self.runner = RunnerSettings(
            forge_url="http://forge.invalid",
            forge_token="token",
            records_dir=base / "records",
        )
        self.settings = LoopSettings(
            state_path=base / "state.json",
            cadence_evaluation="society-general",
        )
        self.executed: list[tuple[str, str]] = []

        def fake_run(manifest, world, evaluation, environment, spec, settings):
            self.executed.append((evaluation, spec))
            digest = environment_digest(environment)
            record_id = f"{len(self.executed):04d}-{evaluation}-{digest}"
            payload = {
                "id": record_id,
                "evaluation": evaluation,
                "environment_spec": spec,
                "environment": dict(environment),
                "environment_digest": digest,
                "evaluators": {
                    "ray": {
                        "gate": True,
                        "tasks": {
                            "bench": {
                                "walltime": {"samples": [1.0, 1.0]},
                                "error": "",
                            }
                        },
                        "error": "",
                        "worker": {
                            "class": "docker:eval-ray:loop-host",
                            "fingerprint": "loop-host",
                        },
                    }
                },
            }
            record_dir = settings.records_dir / record_id
            record_dir.mkdir(parents=True)
            path = record_dir / "record.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return RunRecord(path=path, payload=payload)

        self.patcher = mock.patch(
            "swecraft_evaluator.loop.run_evaluation", side_effect=fake_run
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def reconcile(self) -> list[dict]:
        return reconcile(
            self.manifest,
            self.world,
            self.forge,
            self.runner,
            self.settings,
            report=lambda _line: None,
        )

    def test_first_pass_measures_seed_then_latest(self) -> None:
        scores = self.reconcile()
        self.assertEqual(
            self.executed,
            [
                ("society-general", "world@seed"),
                ("society-general", "world@latest"),
            ],
        )
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["aggregates"]["geomean"], 1.0)

    def test_sweep_not_repeated_within_interval(self) -> None:
        self.reconcile()
        self.executed.clear()
        self.reconcile()
        self.assertEqual(self.executed, [])

    def test_empty_downstream_evaluation_routes_and_advances(self) -> None:
        # An explicit `downstream-<repo>: tasks: []` is the manifest's way
        # of saying a release has nothing to measure; the lane still runs
        # it (instant empty record) and the cursor advances normally.
        base = self.settings.state_path.parent
        manifest_text = (base / "evaluation.yaml").read_text(encoding="utf-8")
        manifest_text += (
            "  downstream-attrs-none:\n    tasks: []\n    metric: walltime\n"
        )
        (base / "evaluation.yaml").write_text(manifest_text, encoding="utf-8")
        manifest = load_manifest(base / "evaluation.yaml")
        self.assertEqual(manifest.evaluations["downstream-attrs-none"].tasks, [])

    def test_release_cursors_initialize_without_evaluating(self) -> None:
        self.forge.release_tags = ["v1.0.0"]
        self.reconcile()
        specs = [spec for _, spec in self.executed]
        self.assertNotIn("world@seed+attrs:v1.0.0", specs)
        state = json.loads(self.settings.state_path.read_text())
        self.assertEqual(state["releases"], {"attrs": "v1.0.0"})

    def test_new_release_runs_downstream_evaluation(self) -> None:
        self.forge.release_tags = ["v1.0.0"]
        self.reconcile()
        self.executed.clear()
        self.forge.release_tags = ["v1.1.0", "v1.0.0"]
        self.reconcile()
        self.assertIn(("downstream-attrs", "world@seed+attrs:v1.1.0"), self.executed)
        state = json.loads(self.settings.state_path.read_text())
        self.assertEqual(state["releases"], {"attrs": "v1.1.0"})

    def test_seed_reference_reused_across_passes(self) -> None:
        self.forge.release_tags = ["v1.0.0"]
        self.reconcile()
        self.executed.clear()
        self.forge.release_tags = ["v1.1.0", "v1.0.0"]
        self.reconcile()
        seed_runs = [
            entry
            for entry in self.executed
            if entry == ("downstream-attrs", "world@seed")
        ]
        # downstream-attrs has no seed record yet: measured once, then reused.
        self.assertEqual(len(seed_runs), 1)
        self.executed.clear()
        self.forge.release_tags = ["v1.2.0", "v1.1.0"]
        self.reconcile()
        self.assertEqual(
            self.executed,
            [("downstream-attrs", "world@seed+attrs:v1.2.0")],
        )

    def test_legacy_seed_reference_refreshes_before_latest_score(self) -> None:
        seed_digest = environment_digest(seed_environment(self.world))
        legacy_id = f"0000-society-general-{seed_digest}"
        record_dir = self.runner.records_dir / legacy_id
        record_dir.mkdir(parents=True)
        (record_dir / "record.json").write_text(
            json.dumps(
                {
                    "id": legacy_id,
                    "evaluation": "society-general",
                    "environment_spec": "world@seed",
                    "environment_digest": seed_digest,
                    "evaluators": {
                        "ray": {
                            "gate": True,
                            "tasks": {
                                "bench": {
                                    "walltime": {"samples": [1.0, 1.0]},
                                    "error": "",
                                }
                            },
                            "error": "",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        scores = self.reconcile()
        self.assertEqual(
            self.executed,
            [
                ("society-general", "world@seed"),
                ("society-general", "world@latest"),
            ],
        )
        self.assertNotEqual(scores[0]["reference"], legacy_id)
        self.assertEqual(scores[0]["scored_tasks"], 1)


class ReleaseDurabilityTests(LoopTests):
    def bootstrap(self) -> None:
        self.forge.release_tags = ["v1.0.0"]
        self.reconcile()
        self.executed.clear()

    def test_all_unseen_tags_run_oldest_first(self) -> None:
        self.bootstrap()
        self.forge.release_tags = ["v1.2.0", "v1.1.0", "v1.0.0"]
        self.reconcile()
        self.assertEqual(
            [spec for _, spec in self.executed if "seed+" in spec],
            ["world@seed+attrs:v1.1.0", "world@seed+attrs:v1.2.0"],
        )
        state = json.loads(self.settings.state_path.read_text())
        self.assertEqual(state["releases"], {"attrs": "v1.2.0"})

    def test_failed_release_keeps_cursor_and_retries(self) -> None:
        self.bootstrap()
        self.forge.release_tags = ["v1.1.0", "v1.0.0"]

        def failing_run(*args, **kwargs):
            raise EvaluatorError("container lost")

        with mock.patch(
            "swecraft_evaluator.loop.run_evaluation", side_effect=failing_run
        ):
            self.reconcile()
        state = json.loads(self.settings.state_path.read_text())
        self.assertEqual(state["releases"], {"attrs": "v1.0.0"})
        self.executed.clear()
        self.reconcile()
        self.assertIn(("downstream-attrs", "world@seed+attrs:v1.1.0"), self.executed)
        state = json.loads(self.settings.state_path.read_text())
        self.assertEqual(state["releases"], {"attrs": "v1.1.0"})


class ReviewFindingTests(LoopTests):
    def test_first_release_after_empty_bootstrap_is_evaluated(self) -> None:
        # A zero-release first scan still initializes; the first future
        # release must be measured, not swallowed as bootstrap history.
        self.reconcile()
        self.executed.clear()
        self.forge.release_tags = ["v1.0.0"]
        self.reconcile()
        self.assertIn(("downstream-attrs", "world@seed+attrs:v1.0.0"), self.executed)
        state = json.loads(self.settings.state_path.read_text())
        self.assertTrue(state["releases_initialized"])
        self.assertEqual(state["releases"], {"attrs": "v1.0.0"})

    def test_cursor_older_than_one_page_pages_back_to_it(self) -> None:
        # 12 releases land while the loop is down: the cursor sits beyond
        # the first page of 10, and every tag in the gap must still run.
        self.forge.release_tags = ["v1"]
        self.reconcile()
        self.executed.clear()
        self.forge.release_tags = [f"v{n}" for n in range(13, 0, -1)]
        self.reconcile()
        release_specs = [spec for _, spec in self.executed if "seed+" in spec]
        self.assertEqual(
            release_specs,
            [f"world@seed+attrs:v{n}" for n in range(2, 14)],
        )
        state = json.loads(self.settings.state_path.read_text())
        self.assertEqual(state["releases"], {"attrs": "v13"})


if __name__ == "__main__":
    unittest.main()
