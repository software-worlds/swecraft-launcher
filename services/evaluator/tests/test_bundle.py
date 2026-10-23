import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from swecraft_evaluator.bundle import (
    cell_id_from_array_index,
    collect_bundle_results,
    export_evaluation_bundle,
    run_bundle_cell,
)
from swecraft_evaluator.environment import load_world
from swecraft_evaluator.errors import EvaluatorError
from swecraft_evaluator.executor import ApptainerExecutor
from swecraft_evaluator.manifest import load_manifest
from swecraft_evaluator.runner import RunnerSettings

MANIFEST = """
world_lock: world.lock.json
subjects:
  attrs:
    install:
      - argv: [pip, install, --no-deps, /eval/sources/attrs]
evaluators:
  ray:
    image: eval-ray.sif
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

STUB_APPTAINER = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
if sys.argv[1] != "exec":
    raise SystemExit(2)
if "--net" not in sys.argv or sys.argv[sys.argv.index("--network") + 1] != "none":
    raise SystemExit(3)
if "--writable-tmpfs" not in sys.argv:
    raise SystemExit(4)
mount = sys.argv[sys.argv.index("--bind") + 1]
workdir = Path(mount.split(":", 1)[0])
plan = json.loads((workdir / "plan.json").read_text())
tasks = {
    task["name"]: {"walltime": {"samples": [1.0, 1.1]}, "error": ""}
    for task in plan["tasks"]
}
(workdir / "out").mkdir(exist_ok=True)
(workdir / "out" / "outcome.json").write_text(
    json.dumps({"gate": True, "tasks": tasks, "error": ""})
)
"""


def repo_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in {
            "attrs/pyproject.toml": b"[project]\n",
            "attrs/.swecraft/forge/admin_token": b"secret-token\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp())
        (self.base / "evaluation.yaml").write_text(MANIFEST, encoding="utf-8")
        (self.base / "world.lock.json").write_text(json.dumps(WORLD), encoding="utf-8")
        self.manifest = load_manifest(self.base / "evaluation.yaml")
        self.world = load_world(self.manifest.world_lock)
        self.bundle = self.base / "bundle"
        self.records = self.base / "records"
        self.stub = self.base / "apptainer-stub"
        self.stub.write_text(STUB_APPTAINER, encoding="utf-8")
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=repo_archive())

        self.settings = RunnerSettings(
            forge_url="http://forge.invalid",
            forge_token="secret",
            records_dir=self.records,
            transport=httpx.MockTransport(handler),
        )

    def export_bundle(self) -> dict:
        exported = export_evaluation_bundle(
            self.manifest,
            self.world,
            "society-general",
            {"attrs": "a" * 40},
            "world@seed",
            self.settings,
            self.bundle,
        )
        return exported.payload

    def test_export_writes_sanitized_cell_bundle_and_slurm_adapter(self) -> None:
        payload = self.export_bundle()
        self.assertEqual(payload["sanitization"]["forge_token"], "excluded")
        cell_id = payload["expected_cells"][0]["id"]
        workdir = self.bundle / "cells" / cell_id / "work"
        self.assertTrue((workdir / "harness.py").is_file())
        self.assertTrue((workdir / "plan.json").is_file())
        self.assertTrue((workdir / "sources/attrs/pyproject.toml").is_file())
        self.assertFalse((workdir / "sources/attrs/.swecraft").exists())
        self.assertEqual(cell_id_from_array_index(self.bundle, 1), cell_id)
        slurm = self.bundle / "slurm/swecraft-evaluator-worker.sbatch"
        script = slurm.read_text(encoding="utf-8")
        self.assertIn("#SBATCH --array=1-1", script)
        # sbatch copies the script into slurmd's spool, so the default must
        # be the exported bundle's absolute path, not a BASH_SOURCE lookup.
        self.assertIn(f"SWECRAFT_EVALUATOR_BUNDLE:-{self.bundle.resolve()}", script)
        self.assertNotIn("BASH_SOURCE", script)
        self.assertIn("#SBATCH --time=", script)

    def test_collector_waits_until_all_cells_are_terminal(self) -> None:
        self.export_bundle()
        with self.assertRaisesRegex(EvaluatorError, "waiting for 1 cell"):
            collect_bundle_results(self.bundle, self.records)

    def test_worker_result_collects_into_record_json_once(self) -> None:
        payload = self.export_bundle()
        cell_id = payload["expected_cells"][0]["id"]
        with mock.patch.dict(
            os.environ, {"SWECRAFT_EVALUATOR_WORKER_CLASS": "slurm-a"}
        ):
            result = run_bundle_cell(
                self.bundle,
                cell_id,
                None,
                ApptainerExecutor((str(self.stub),)),
            )
        self.assertTrue(result["terminal"])
        self.assertEqual(result["worker"]["class"], "apptainer:eval-ray.sif:slurm-a")
        self.assertFalse((self.bundle / "results" / cell_id / "work/sources").exists())

        record = collect_bundle_results(self.bundle, self.records)
        ray = record.payload["evaluators"]["ray"]
        self.assertTrue(ray["gate"])
        self.assertEqual(
            ray["tasks"]["conda-yaml"]["walltime"]["samples"],
            [1.0, 1.1],
        )
        self.assertEqual(ray["worker"]["class"], "apptainer:eval-ray.sif:slurm-a")
        self.assertTrue(record.path.is_file())
        self.assertFalse((record.path.parent / "ray/sources").exists())

        again = collect_bundle_results(self.bundle, self.records)
        self.assertEqual(again.path, record.path)
        self.assertEqual(again.payload["id"], record.payload["id"])


if __name__ == "__main__":
    unittest.main()
