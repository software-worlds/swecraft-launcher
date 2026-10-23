import tempfile
import unittest
from pathlib import Path

from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.manifest import load_manifest

MINIMAL = """
world_lock: world.lock.json
subjects:
  attrs:
    install:
      - argv: [pip, install, /eval/sources/attrs]
evaluators:
  ray:
    image: eval-ray:1
    subjects: [attrs]
    gate: {argv: [pytest, -q, tests/gate.py]}
    tasks:
      conda-yaml:
        command: {argv: [python, bench.py]}
        metrics: [walltime]
evaluations:
  society-general:
    tasks: [ray@conda-yaml]
    metric: walltime
"""


def write_manifest(text: str) -> Path:
    directory = Path(tempfile.mkdtemp())
    path = directory / "evaluation.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class ManifestTests(unittest.TestCase):
    def test_minimal_manifest_parses(self) -> None:
        manifest = load_manifest(write_manifest(MINIMAL))
        self.assertEqual(list(manifest.evaluators), ["ray"])
        self.assertTrue(manifest.world_lock.is_absolute())
        evaluator, task_name, task = manifest.task("ray@conda-yaml")
        self.assertEqual((evaluator, task_name), ("ray", "conda-yaml"))
        self.assertEqual(task.metrics, ["walltime"])
        evaluation = manifest.evaluations["society-general"]
        self.assertEqual(evaluation.aggregates, ["geomean"])

    def test_unknown_subject_rejected(self) -> None:
        broken = MINIMAL.replace("subjects: [attrs]", "subjects: [missing]")
        with self.assertRaises(EvaluatorError):
            load_manifest(write_manifest(broken))

    def test_dangling_task_reference_rejected(self) -> None:
        broken = MINIMAL.replace("[ray@conda-yaml]", "[ray@absent]")
        with self.assertRaises(EvaluatorError):
            load_manifest(write_manifest(broken))

    def test_evaluation_metric_must_be_measured(self) -> None:
        broken = MINIMAL.replace("metric: walltime", "metric: codspeed-walltime")
        with self.assertRaises(EvaluatorError):
            load_manifest(write_manifest(broken))

    def test_unknown_metric_rejected(self) -> None:
        broken = MINIMAL.replace("[walltime]", "[perf]")
        with self.assertRaises(EvaluatorError):
            load_manifest(write_manifest(broken))

    def test_codspeed_metrics_require_direct_pytest_argv(self) -> None:
        broken = MINIMAL.replace("[python, bench.py]", "[python, bench.py]")
        broken = broken.replace("[walltime]", "[codspeed-walltime]")
        broken = broken.replace("metric: walltime", "metric: codspeed-walltime")
        with self.assertRaises(EvaluatorError):
            load_manifest(write_manifest(broken))

    def test_codspeed_metrics_accept_python_m_pytest_argv(self) -> None:
        text = MINIMAL.replace("[python, bench.py]", "[python, -m, pytest, bench.py]")
        text = text.replace("[walltime]", "[codspeed-cpu-sim]")
        text = text.replace("metric: walltime", "metric: codspeed-cpu-sim")
        manifest = load_manifest(write_manifest(text))
        self.assertEqual(
            manifest.evaluators["ray"].tasks["conda-yaml"].metrics,
            ["codspeed-cpu-sim"],
        )

    def test_codspeed_metrics_reject_hand_written_instrument_flags(self) -> None:
        text = MINIMAL.replace(
            "[python, bench.py]",
            "[python, -m, pytest, bench.py, --codspeed]",
        )
        text = text.replace("[walltime]", "[codspeed-walltime]")
        text = text.replace("metric: walltime", "metric: codspeed-walltime")
        with self.assertRaises(EvaluatorError):
            load_manifest(write_manifest(text))

    def test_unknown_task_lookup_fails(self) -> None:
        manifest = load_manifest(write_manifest(MINIMAL))
        with self.assertRaises(EvaluatorError):
            manifest.task("ray@absent")


if __name__ == "__main__":
    unittest.main()


class ExposureTests(unittest.TestCase):
    def test_exposed_tasks_must_belong_to_the_evaluation(self) -> None:
        import tempfile
        from pathlib import Path

        text = """
world_lock: world.lock.json
subjects:
  attrs: {install: [{argv: [pip, install, /eval/sources/attrs]}]}
evaluators:
  ray:
    image: eval-ray:1
    subjects: [attrs]
    gate: {argv: [pytest]}
    tasks:
      fast: {command: {argv: [python, -m, pytest, a.py]}, metrics: [codspeed-cpu-sim]}
      slow: {command: {argv: [python, -m, pytest, b.py]}, metrics: [codspeed-cpu-sim]}
evaluations:
  sweep: {tasks: [ray@fast], metric: codspeed-cpu-sim, exposed: [ray@slow]}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.yaml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(EvaluatorError, "exposed task ray@slow"):
                load_manifest(path)


class ShippedManifestTests(unittest.TestCase):
    """The manifests the repository ships parse against the live schema."""

    ROOT = Path(__file__).resolve().parents[3]

    def test_template_and_example_parse(self) -> None:
        for relative in (
            "templates/experiment/evaluation.yaml",
            "examples/evaluation.yaml",
            "experiments/demo/evaluation.yaml",
        ):
            with self.subTest(relative):
                manifest = load_manifest(self.ROOT / relative)
                self.assertTrue(manifest.evaluations)
