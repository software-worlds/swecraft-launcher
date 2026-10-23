import math
import tempfile
import unittest
from pathlib import Path

from swecraft_evaluator.manifest import load_manifest
from swecraft_evaluator.score import (
    latest_reference,
    score_record,
    task_sample,
)

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
      fast: {command: {argv: [python, a.py]}, metrics: [walltime]}
      slow:
        command: {argv: [python, -m, pytest, b.py]}
        metrics: [walltime, codspeed-walltime, codspeed-cpu-sim]
evaluations:
  society-general:
    tasks: [ray@fast, ray@slow]
    metric: walltime
    aggregates: [geomean, average]
  hot-path:
    tasks: [ray@slow]
    metric: codspeed-walltime
  cpu-sim:
    tasks: [ray@slow]
    metric: codspeed-cpu-sim
"""


def record(
    record_id: str,
    digest: str,
    tasks: dict,
    *,
    gate: bool = True,
    evaluation: str = "society-general",
    worker_class: str | None = None,
) -> dict:
    evaluator = {"gate": gate, "tasks": tasks, "error": ""}
    if worker_class is not None:
        evaluator["worker"] = {"class": worker_class, "fingerprint": worker_class}
    return {
        "id": record_id,
        "evaluation": evaluation,
        "environment_digest": digest,
        "environment_spec": "world@seed",
        "evaluators": {"ray": evaluator},
    }


def walltime(*samples: float) -> dict:
    return {"walltime": {"samples": list(samples)}, "error": ""}


def codspeed_walltime(seconds: float) -> dict:
    return {
        "codspeed-walltime": {
            "seconds": seconds,
            "benchmarks": {"bench.py::test": {"min_ns": seconds * 1e9}},
        },
        "error": "",
    }


def codspeed_cpu_sim(instructions: int) -> dict:
    return {
        "codspeed-cpu-sim": {
            "instructions": instructions,
            "benchmarks": {"bench.py::test": {"instructions": instructions}},
        },
        "error": "",
    }


class ScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp())
        (base / "evaluation.yaml").write_text(MANIFEST, encoding="utf-8")
        (base / "world.lock.json").write_text(
            '{"org": "hub", "repos": [{"name": "attrs", '
            '"baseline_commit": "a", "default_branch": "main"}]}',
            encoding="utf-8",
        )
        self.manifest = load_manifest(base / "evaluation.yaml")

    def test_task_sample_means_walltime(self) -> None:
        row = record("r", "d", {"fast": walltime(1.0, 2.0, 3.0)})
        self.assertEqual(task_sample(row, "ray@fast", "walltime"), 2.0)

    def test_task_sample_reads_codspeed_walltime_seconds(self) -> None:
        row = record("r", "d", {"slow": codspeed_walltime(0.5)})
        self.assertEqual(task_sample(row, "ray@slow", "codspeed-walltime"), 0.5)

    def test_task_sample_reads_codspeed_cpu_sim_instructions(self) -> None:
        row = record("r", "d", {"slow": codspeed_cpu_sim(123)})
        self.assertEqual(task_sample(row, "ray@slow", "codspeed-cpu-sim"), 123.0)

    def test_failed_gate_and_task_errors_yield_none(self) -> None:
        failed_gate = record("r", "d", {"fast": walltime(1.0)}, gate=False)
        self.assertIsNone(task_sample(failed_gate, "ray@fast", "walltime"))
        errored = record(
            "r", "d", {"fast": {"walltime": {"samples": [1.0]}, "error": "boom"}}
        )
        self.assertIsNone(task_sample(errored, "ray@fast", "walltime"))

    def test_speedups_carry_sample_cvs(self) -> None:
        reference = record("seed", "s", {"fast": walltime(1.0, 2.0, 3.0)})
        candidate = record("cand", "c", {"fast": walltime(2.0, 2.0)})
        score = score_record(self.manifest, candidate, reference)
        entry = score["tasks"]["ray@fast"]
        self.assertAlmostEqual(entry["seed_cv_percent"], 50.0)
        self.assertEqual(entry["current_cv_percent"], 0.0)

    def test_score_record_aggregates_speedups(self) -> None:
        reference = record("seed", "s", {"fast": walltime(2.0), "slow": walltime(3.0)})
        candidate = record("cand", "c", {"fast": walltime(1.0), "slow": walltime(3.0)})
        score = score_record(self.manifest, candidate, reference)
        self.assertEqual(score["tasks"]["ray@fast"]["speedup"], 2.0)
        self.assertEqual(score["tasks"]["ray@slow"]["speedup"], 1.0)
        self.assertEqual(score["scored_tasks"], 2)
        self.assertAlmostEqual(score["aggregates"]["geomean"], math.sqrt(2.0))
        self.assertAlmostEqual(score["aggregates"]["average"], 1.5)

    def test_unscoreable_tasks_stay_visible_as_null(self) -> None:
        reference = record("seed", "s", {"fast": walltime(2.0)})
        candidate = record("cand", "c", {"fast": walltime(1.0)}, gate=False)
        score = score_record(self.manifest, candidate, reference)
        self.assertIsNone(score["tasks"]["ray@fast"]["speedup"])
        self.assertIsNone(score["aggregates"]["geomean"])
        self.assertEqual(score["scored_tasks"], 0)

    def test_mismatched_worker_classes_do_not_score(self) -> None:
        reference = record(
            "seed",
            "s",
            {"fast": walltime(2.0)},
            worker_class="docker:eval:host-a",
        )
        candidate = record(
            "cand",
            "c",
            {"fast": walltime(1.0)},
            worker_class="apptainer:eval:partition-b",
        )
        score = score_record(self.manifest, candidate, reference)
        entry = score["tasks"]["ray@fast"]
        self.assertIsNone(entry["speedup"])
        self.assertFalse(entry["worker_class_match"])
        self.assertEqual(entry["seed_worker_class"], "docker:eval:host-a")
        self.assertEqual(entry["current_worker_class"], "apptainer:eval:partition-b")
        self.assertIsNone(score["aggregates"]["geomean"])

    def test_matching_worker_classes_score(self) -> None:
        reference = record(
            "seed",
            "s",
            {"fast": walltime(2.0)},
            worker_class="apptainer:eval:partition-a",
        )
        candidate = record(
            "cand",
            "c",
            {"fast": walltime(1.0)},
            worker_class="apptainer:eval:partition-a",
        )
        score = score_record(self.manifest, candidate, reference)
        self.assertEqual(score["tasks"]["ray@fast"]["speedup"], 2.0)
        self.assertTrue(score["tasks"]["ray@fast"]["worker_class_match"])

    def test_latest_reference_picks_newest_matching(self) -> None:
        tasks = {"fast": walltime(1.0), "slow": walltime(1.0)}
        records = [
            record("1-old", "seed-digest", tasks),
            record("2-new", "seed-digest", tasks),
            record("3-other", "other-digest", tasks),
            record("4-other-eval", "seed-digest", tasks, evaluation="downstream-x"),
        ]
        found = latest_reference(
            self.manifest, records, "society-general", "seed-digest"
        )
        self.assertEqual(found["id"], "2-new")
        self.assertIsNone(
            latest_reference(self.manifest, records, "absent", "seed-digest")
        )

    def test_latest_reference_can_require_worker_provenance(self) -> None:
        tasks = {"fast": walltime(1.0), "slow": walltime(1.0)}
        legacy = record("1-legacy", "seed-digest", tasks)
        current = record(
            "2-current",
            "seed-digest",
            tasks,
            worker_class="docker:eval-ray:host-a",
        )
        self.assertIsNone(
            latest_reference(
                self.manifest,
                [legacy],
                "society-general",
                "seed-digest",
                require_worker=True,
            )
        )
        found = latest_reference(
            self.manifest,
            [legacy, current],
            "society-general",
            "seed-digest",
            require_worker=True,
        )
        self.assertEqual(found["id"], "2-current")

    def test_latest_reference_skips_legacy_for_worker_candidate(self) -> None:
        tasks = {"fast": walltime(1.0), "slow": walltime(1.0)}
        legacy = record("1-legacy", "seed-digest", tasks)
        candidate = record(
            "candidate",
            "candidate-digest",
            tasks,
            worker_class="docker:eval-ray:host-a",
        )
        self.assertIsNone(
            latest_reference(
                self.manifest,
                [legacy],
                "society-general",
                "seed-digest",
                candidate=candidate,
            )
        )
        self.assertEqual(
            latest_reference(
                self.manifest,
                [legacy],
                "society-general",
                "seed-digest",
            )["id"],
            "1-legacy",
        )

    def test_latest_reference_rejects_legacy_metric_records(self) -> None:
        legacy = record(
            "1-legacy",
            "seed-digest",
            {"slow": {"codspeed": {"seconds": 0.5}, "error": ""}},
            evaluation="hot-path",
        )
        self.assertIsNone(
            latest_reference(self.manifest, [legacy], "hot-path", "seed-digest")
        )
        current = record(
            "2-current",
            "seed-digest",
            {"slow": codspeed_walltime(0.5)},
            evaluation="hot-path",
        )
        found = latest_reference(
            self.manifest, [legacy, current], "hot-path", "seed-digest"
        )
        self.assertEqual(found["id"], "2-current")


if __name__ == "__main__":
    unittest.main()
