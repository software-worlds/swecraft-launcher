import io
import json
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path

import httpx

from swecraft_evaluator.environment import load_world
from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.harness import (
    callgrind_benchmark,
    callgrind_benchmarks,
    callgrind_collected,
    codspeed_benchmarks,
)
from swecraft_evaluator.manifest import load_manifest
from swecraft_evaluator.runner import (
    CONTAINER_MARGIN_SECONDS,
    RunnerSettings,
    _budget,
    run_evaluation,
)

MANIFEST = """
world_lock: world.lock.json
subjects:
  attrs:
    install:
      - argv: [pip, install, --no-deps, /eval/sources/attrs]
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

WORLD = {
    "org": "hub",
    "repos": [{"name": "attrs", "baseline_commit": "a" * 40, "default_branch": "main"}],
}

STUB_DOCKER = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
mount = sys.argv[sys.argv.index("--volume") + 1]
workdir = Path(mount.split(":", 1)[0])
plan = json.loads((workdir / "plan.json").read_text())
gate = not (workdir / "sources" / "attrs" / "gate-must-fail").exists()
tasks = {}
if gate:
    tasks = {
        task["name"]: {"walltime": {"samples": [1.0, 1.1, 0.9]}, "error": ""}
        for task in plan["tasks"]
    }
(workdir / "out").mkdir(exist_ok=True)
(workdir / "out" / "outcome.json").write_text(
    json.dumps({"gate": gate, "tasks": tasks, "error": ""})
)
"""


def repo_archive(*extra_files: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in ("attrs/pyproject.toml", *extra_files):
            data = b"[project]\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp())
        (base / "evaluation.yaml").write_text(MANIFEST, encoding="utf-8")
        (base / "world.lock.json").write_text(json.dumps(WORLD), encoding="utf-8")
        self.manifest = load_manifest(base / "evaluation.yaml")
        self.world = load_world(self.manifest.world_lock)
        stub = base / "docker-stub"
        stub.write_text(STUB_DOCKER, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.requested: list[str] = []
        self.archive = repo_archive()

        def handler(request: httpx.Request) -> httpx.Response:
            self.requested.append(str(request.url))
            return httpx.Response(200, content=self.archive)

        self.settings = RunnerSettings(
            forge_url="http://forge.invalid",
            forge_token="secret",
            records_dir=base / "records",
            docker_command=(str(stub),),
            transport=httpx.MockTransport(handler),
        )

    def run_it(self) -> dict:
        record = run_evaluation(
            self.manifest,
            self.world,
            "society-general",
            {"attrs": "a" * 40},
            "world@seed",
            self.settings,
        )
        return record.payload

    def test_record_carries_measurements(self) -> None:
        payload = self.run_it()
        ray = payload["evaluators"]["ray"]
        self.assertTrue(ray["gate"])
        self.assertEqual(
            ray["tasks"]["conda-yaml"]["walltime"]["samples"], [1.0, 1.1, 0.9]
        )
        self.assertEqual(payload["environment"], {"attrs": "a" * 40})
        self.assertEqual(len(payload["environment_digest"]), 12)
        self.assertIn("a" * 40 + ".tar.gz", self.requested[0])
        record_path = self.settings.records_dir / payload["id"] / "record.json"
        self.assertEqual(json.loads(record_path.read_text())["id"], payload["id"])

    def test_sources_are_removed_after_the_run(self) -> None:
        payload = self.run_it()
        workdir = self.settings.records_dir / payload["id"] / "ray"
        self.assertFalse((workdir / "sources").exists())
        self.assertTrue((workdir / "plan.json").exists())

    def test_failed_gate_voids_tasks(self) -> None:
        self.archive = repo_archive("attrs/gate-must-fail")
        payload = self.run_it()
        ray = payload["evaluators"]["ray"]
        self.assertFalse(ray["gate"])
        self.assertEqual(ray["tasks"], {})

    def test_missing_subject_commit_fails(self) -> None:
        with self.assertRaises(EvaluatorError):
            run_evaluation(
                self.manifest,
                self.world,
                "society-general",
                {},
                "world@seed",
                self.settings,
            )

    def test_lost_container_fails_one_evaluator_not_the_record(self) -> None:
        self.settings = RunnerSettings(
            forge_url=self.settings.forge_url,
            forge_token=self.settings.forge_token,
            records_dir=self.settings.records_dir,
            docker_command=("/bin/false",),
            transport=self.settings.transport,
        )
        payload = self.run_it()
        ray = payload["evaluators"]["ray"]
        self.assertIsNone(ray["gate"])
        self.assertTrue(ray["error"])
        record_path = self.settings.records_dir / payload["id"] / "record.json"
        self.assertTrue(record_path.exists())

    def test_unknown_evaluation_fails(self) -> None:
        with self.assertRaises(EvaluatorError):
            run_evaluation(
                self.manifest,
                self.world,
                "absent",
                {"attrs": "a" * 40},
                "world@seed",
                self.settings,
            )


class CodspeedParserTests(unittest.TestCase):
    def test_full_stats_are_keyed_by_benchmark_uri(self) -> None:
        text = json.dumps(
            {
                "benchmarks": [
                    {
                        "uri": "bench.py::a",
                        "stats": {"min_ns": 2_000_000.0, "mean_ns": 3_000_000.0},
                    },
                    {"name": "b", "stats": {"min_ns": 500_000.0, "rounds": 20}},
                ]
            }
        )
        self.assertEqual(
            codspeed_benchmarks(text),
            {
                "bench.py::a": {"min_ns": 2_000_000.0, "mean_ns": 3_000_000.0},
                "b": {"min_ns": 500_000.0, "rounds": 20},
            },
        )


class CallgrindParserTests(unittest.TestCase):
    def test_collected_total_allows_commas(self) -> None:
        stderr = """
==1== Events    : Ir
==1== Collected : 24,461,333,705
"""
        self.assertEqual(callgrind_collected(stderr), 24_461_333_705)

    def test_benchmark_dump_uses_trigger_and_ir_totals(self) -> None:
        text = """
desc: Trigger: Client Request: tests/test_benchmarks.py::test_cutting[param]
events: Dr Ir Dw
summary: 0 0 0
totals: 7 12345 9
"""
        self.assertEqual(
            callgrind_benchmark(text),
            ("tests/test_benchmarks.py::test_cutting[param]", 12345),
        )

    def test_metadata_and_program_termination_dumps_are_ignored(self) -> None:
        self.assertIsNone(
            callgrind_benchmark(
                "desc: Trigger: Client Request: Metadata: pytest-codspeed 5.0.3\n"
                "events: Ir\nsummary: 0\ntotals: 10\n"
            )
        )
        self.assertIsNone(
            callgrind_benchmark(
                "desc: Trigger: Program termination\n"
                "events: Ir\nsummary: 0\ntotals: 10\n"
            )
        )

    def test_profile_parts_are_summed_by_uri(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "task.callgrind"
            (Path(directory) / "task.callgrind.1").write_text(
                "desc: Trigger: Client Request: bench.py::test_a\n"
                "events: Ir\nsummary: 0\ntotals: 10\n",
                encoding="utf-8",
            )
            (Path(directory) / "task.callgrind.2").write_text(
                "desc: Trigger: Client Request: bench.py::test_a\n"
                "events: Ir\nsummary: 0\ntotals: 5\n",
                encoding="utf-8",
            )
            (Path(directory) / "task.callgrind.3").write_text(
                "desc: Trigger: Client Request: bench.py::test_b\n"
                "events: Ir\nsummary: 0\ntotals: 2\n",
                encoding="utf-8",
            )
            self.assertEqual(
                callgrind_benchmarks(profile),
                {
                    "bench.py::test_a": {"instructions": 15},
                    "bench.py::test_b": {"instructions": 2},
                },
            )


class BudgetTests(unittest.TestCase):
    def test_each_metric_adds_one_task_command_run(self) -> None:
        plan = {
            "setup": [],
            "install": [],
            "gate": {"timeout_seconds": 5},
            "tasks": [
                {
                    "command": {"timeout_seconds": 10},
                    "walltime_repeats": 3,
                    "metrics": ["walltime", "codspeed-walltime", "codspeed-cpu-sim"],
                }
            ],
        }
        self.assertEqual(_budget(plan), 5 + 50 + CONTAINER_MARGIN_SECONDS)


if __name__ == "__main__":
    unittest.main()
