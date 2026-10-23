import math
import tempfile
import unittest
from pathlib import Path

from swecraft_evaluator.manifest import load_manifest
from swecraft_evaluator.report import (
    ASSUMED_FLOOR,
    MIN_EFFECT,
    ambient_subjects,
    build_report,
    classify_spec,
    exposed_tasks,
    noise_floors,
    reading_from_record,
    threshold,
)

MANIFEST = """
world_lock: world.lock.json
subjects:
  alpha: {install: [{argv: [pip, install, /eval/sources/alpha]}]}
  beta: {install: [{argv: [pip, install, /eval/sources/beta]}]}
  runner: {install: [{argv: [pip, install, /eval/sources/runner]}]}
evaluators:
  cons:
    image: eval-cons:1
    subjects: [alpha, beta, runner]
    gate: {argv: [pytest]}
    tasks:
      a: {command: {argv: [python, -m, pytest, a.py]}, metrics: [codspeed-cpu-sim]}
      b: {command: {argv: [python, -m, pytest, b.py]}, metrics: [codspeed-cpu-sim]}
      c: {command: {argv: [python, -m, pytest, c.py]}, metrics: [codspeed-cpu-sim]}
evaluations:
  sweep: {tasks: [cons@a, cons@b, cons@c], metric: codspeed-cpu-sim}
  downstream-alpha: {tasks: [cons@a], metric: codspeed-cpu-sim}
  downstream-runner: {tasks: [cons@a, cons@b, cons@c], metric: codspeed-cpu-sim}
"""


def record(
    record_id: str,
    evaluation: str,
    spec: str,
    digest: str,
    instructions: dict[str, int | None],
    *,
    started: str,
    gate: bool = True,
) -> dict:
    tasks = {}
    for name, count in instructions.items():
        if count is None:
            tasks[name] = {"error": "RuntimeError: exited with 3"}
        else:
            tasks[name] = {"codspeed-cpu-sim": {"instructions": count}, "error": ""}
    return {
        "id": record_id,
        "evaluation": evaluation,
        "environment_spec": spec,
        "environment_digest": digest,
        "environment": {"alpha": "a" * 8, "beta": "b" * 8, "runner": "c" * 8},
        "started": started,
        "finished": started[:11] + "01:00:00+00:00",
        "evaluators": {"cons": {"gate": gate, "error": "", "tasks": tasks}},
    }


def sweep(record_id, spec, digest, a, b, c, *, started, gate=True):
    return record(
        record_id,
        "sweep",
        spec,
        digest,
        {"a": a, "b": b, "c": c},
        started=started,
        gate=gate,
    )


RECORDS = [
    # Two seeds: b differs by 0.5%, so b's floor is known and nonzero while
    # a and c reproduce bit for bit. The first seed is partial (c failed)
    # and must not be chosen as the reference.
    sweep(
        "20260801T000000Z-sweep-seed",
        "world@seed",
        "seed",
        1000,
        2000,
        None,
        started="2026-08-01T00:00:00+00:00",
    ),
    sweep(
        "20260801T000500Z-sweep-seed",
        "world@seed",
        "seed",
        1000,
        2010,
        3000,
        started="2026-08-01T00:05:00+00:00",
    ),
    sweep(
        "20260801T001000Z-sweep-seed",
        "world@seed",
        "seed",
        1000,
        2000,
        3000,
        started="2026-08-01T00:10:00+00:00",
    ),
    # Cadence sweeps of the world's heads.
    sweep(
        "20260802T000000Z-sweep-h1",
        "world@latest",
        "h1",
        1000,
        2000,
        3000,
        started="2026-08-02T00:00:00+00:00",
    ),
    sweep(
        "20260803T000000Z-sweep-h2",
        "world@latest",
        "h2",
        800,
        2000,
        3050,
        started="2026-08-03T00:00:00+00:00",
    ),
    # A trajectory cutoff named by a script, and a sweep whose gate failed.
    sweep(
        "20260803T120000Z-sweep-w250",
        "world@worked250",
        "w250",
        900,
        2000,
        3000,
        started="2026-08-03T12:00:00+00:00",
    ),
    sweep(
        "20260804T000000Z-sweep-h3",
        "world@latest",
        "h3",
        1,
        1,
        1,
        started="2026-08-04T00:00:00+00:00",
        gate=False,
    ),
    # alpha's release chain runs on its own battery; beta has none, so its
    # release is measured on the sweep battery where every task is a control.
    record(
        "20260802T060000Z-downstream-alpha-r1",
        "downstream-alpha",
        "world@seed+alpha:v1",
        "r1",
        {"a": 900},
        started="2026-08-02T06:00:00+00:00",
    ),
    record(
        "20260802T120000Z-downstream-alpha-r2",
        "downstream-alpha",
        "world@seed+alpha:v2",
        "r2",
        {"a": 800},
        started="2026-08-02T12:00:00+00:00",
    ),
    record(
        "20260801T000000Z-downstream-alpha-seed",
        "downstream-alpha",
        "world@seed",
        "seed",
        {"a": 1000},
        started="2026-08-01T00:20:00+00:00",
    ),
    sweep(
        "20260802T180000Z-sweep-b1",
        "world@seed+beta:v1",
        "b1",
        1000,
        2000,
        3000,
        started="2026-08-02T18:00:00+00:00",
    ),
    # An evaluation the manifest no longer names.
    sweep(
        "20260802T190000Z-mystery-x",
        "world@latest",
        "x",
        1,
        1,
        1,
        started="2026-08-02T19:00:00+00:00",
    ),
]
RECORDS[-1]["evaluation"] = "mystery"


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "evaluation.yaml"
        path.write_text(MANIFEST, encoding="utf-8")
        self.manifest = load_manifest(path)
        self.report = build_report(self.manifest, RECORDS)

    def test_specs_classify(self) -> None:
        self.assertEqual(classify_spec("world@seed"), ("seed", None, None, None))
        self.assertEqual(classify_spec("world@latest"), ("latest", None, None, None))
        self.assertEqual(
            classify_spec("world@seed+alpha:v1.2"), ("release", "alpha", "v1.2", None)
        )
        self.assertEqual(classify_spec("world@worked250"), ("other", None, None, 250))
        self.assertEqual(classify_spec("world@backfill-75"), ("other", None, None, 75))
        self.assertEqual(classify_spec("world@cutoff_300"), ("other", None, None, 300))
        self.assertEqual(
            classify_spec("world@seed+broken"), ("other", None, None, None)
        )
        self.assertEqual(classify_spec("anything"), ("other", None, None, None))

    def test_reading_reduces_a_record_and_names_what_failed(self) -> None:
        reading = reading_from_record(self.manifest, RECORDS[0])
        assert reading is not None
        self.assertEqual(reading.kind, "seed")
        self.assertEqual(
            reading.samples, {"cons@a": 1000.0, "cons@b": 2000.0, "cons@c": None}
        )
        self.assertFalse(reading.complete)
        self.assertEqual(reading.measured, 2)
        self.assertEqual(reading.errors, ("cons@c: RuntimeError: exited with 3",))
        self.assertEqual(reading.duration_seconds, 3600.0)
        failed = reading_from_record(self.manifest, RECORDS[6])
        assert failed is not None
        self.assertEqual(failed.errors, ("cons: gate failed",))
        self.assertEqual(failed.measured, 0)
        # gate: false is a finding about the world, not an instrument gap.
        self.assertEqual(failed.gated_tasks, ("cons@a", "cons@b", "cons@c"))
        self.assertEqual(failed.missing_tasks, ())
        self.assertEqual(reading.gated_tasks, ())
        self.assertEqual(reading.missing_tasks, ("cons@c",))
        self.assertIsNone(reading_from_record(self.manifest, RECORDS[-1]))

    def test_reference_is_the_newest_complete_seed(self) -> None:
        summary = self.report["evaluations"]["sweep"]
        self.assertEqual(summary["reference"], "20260801T001000Z-sweep-seed")
        self.assertTrue(summary["reference_complete"])
        self.assertEqual(
            summary["readings"], {"seed": 3, "latest": 3, "release": 1, "other": 1}
        )

    def test_exposure_ignores_the_ambient_battery(self) -> None:
        self.assertEqual(ambient_subjects(self.manifest, "sweep"), ["runner"])
        self.assertEqual(exposed_tasks(self.manifest, "sweep"), ["cons@a"])
        summary = self.report["evaluations"]["sweep"]
        self.assertEqual(summary["exposed_tasks"], ["cons@a"])
        self.assertEqual(summary["ambient_subjects"], ["runner"])
        self.assertEqual(summary["exposure_source"], "derived")

    def test_an_explicit_exposed_list_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.yaml"
            path.write_text(
                MANIFEST.replace(
                    "metric: codspeed-cpu-sim}\n  downstream-alpha",
                    "metric: codspeed-cpu-sim, exposed: [cons@b, cons@a]}\n"
                    "  downstream-alpha",
                ),
                encoding="utf-8",
            )
            manifest = load_manifest(path)
        report = build_report(manifest, RECORDS)
        summary = report["evaluations"]["sweep"]
        # Listed order does not matter; the battery's order is kept.
        self.assertEqual(summary["exposed_tasks"], ["cons@a", "cons@b"])
        self.assertEqual(summary["exposure_source"], "manifest")
        second = report["series"]["sweep"][1]
        self.assertAlmostEqual(
            second["aggregates"]["geomean_exposed"], (1.25 * 1.0) ** 0.5
        )
        self.assertEqual(
            self.report["evaluations"]["downstream-alpha"]["downstream_of"], "alpha"
        )

    def test_floors_come_from_replicate_pairs(self) -> None:
        tasks = self.report["tasks"]
        self.assertAlmostEqual(tasks["cons@b"]["floor"], math.log(2010 / 2000))
        self.assertEqual(tasks["cons@b"]["pairs"], 3)  # three seeds, all pairs
        self.assertFalse(tasks["cons@b"]["assumed"])
        self.assertEqual(tasks["cons@a"]["floor"], 0.0)
        self.assertEqual(tasks["cons@a"]["threshold"], MIN_EFFECT)
        self.assertAlmostEqual(tasks["cons@b"]["threshold"], 2 * math.log(2010 / 2000))
        # A task with no replicate is assumed noisy rather than assumed clean.
        self.assertEqual(threshold(None), 2 * ASSUMED_FLOOR)
        floors = noise_floors([])
        self.assertEqual(floors, {})

    def test_series_orders_sweeps_and_flags_movers(self) -> None:
        series = self.report["series"]["sweep"]
        self.assertEqual(
            [point["spec"] for point in series],
            ["world@latest", "world@latest", "world@worked250", "world@latest"],
        )
        first, second, cutoff, failed = series
        self.assertAlmostEqual(first["aggregates"]["geomean"], 1.0)
        self.assertEqual(first["movers_up"], [])
        self.assertAlmostEqual(second["speedups"]["cons@a"], 1.25)
        self.assertAlmostEqual(second["aggregates"]["geomean_exposed"], 1.25)
        self.assertEqual([m["ref"] for m in second["movers_up"]], ["cons@a"])
        self.assertEqual([m["ref"] for m in second["movers_down"]], ["cons@c"])
        self.assertEqual(cutoff["step"], 250)
        self.assertEqual(cutoff["kind"], "other")
        self.assertFalse(failed["complete"])
        self.assertIsNone(failed["aggregates"]["geomean"])
        # Adoption semantics: the red-gated consumer's tasks score 1.0 (the
        # world is declined, not adopted), so the aggregate stays on a fixed
        # composition — here every task is gated, so exactly 1.0.
        self.assertEqual(failed["gated_tasks"], ["cons@a", "cons@b", "cons@c"])
        self.assertEqual(failed["missing_tasks"], [])
        self.assertAlmostEqual(failed["aggregates"]["geomean_adoption"], 1.0)
        # On a complete reading the adoption basis is the plain geomean.
        self.assertAlmostEqual(
            second["aggregates"]["geomean_adoption"],
            second["aggregates"]["geomean"],
        )
        self.assertEqual(second["gated_tasks"], [])
        self.assertEqual(failed["measured"], 0)

    def test_release_chain_marginals_and_controls(self) -> None:
        alpha = self.report["releases"]["alpha"]
        self.assertEqual([entry["tag"] for entry in alpha], ["v1", "v2"])
        v1, v2 = alpha
        self.assertAlmostEqual(v1["speedups"]["cons@a"], 1000 / 900)
        self.assertAlmostEqual(v1["marginal"]["cons@a"], 1000 / 900)
        self.assertAlmostEqual(v2["speedups"]["cons@a"], 1.25)
        self.assertAlmostEqual(v2["marginal"]["cons@a"], 1.25 / (1000 / 900))
        self.assertAlmostEqual(v2["aggregates"]["geomean"], 1.25)
        self.assertEqual(v2["battery_tasks"], 1)
        self.assertEqual(v2["battery"], ["cons@a"])
        self.assertAlmostEqual(
            v2["aggregates"]["marginal_geomean"], 1.25 / (1000 / 900)
        )
        self.assertIsNone(v2["aggregates"]["control_leak"])
        self.assertEqual(
            v2["moved"],
            [
                {
                    "ref": "cons@a",
                    "speedup": 1.25,
                    "marginal": 1.25 / (1000 / 900),
                    "control": False,
                }
            ],
        )
        beta = self.report["releases"]["beta"]
        self.assertEqual(len(beta), 1)
        self.assertIsNone(beta[0]["aggregates"]["geomean"])
        self.assertEqual(beta[0]["aggregates"]["controls"], 3)
        self.assertEqual(beta[0]["battery_tasks"], 0)
        self.assertEqual(beta[0]["battery"], [])
        self.assertAlmostEqual(beta[0]["aggregates"]["control_leak"], 1.0)
        self.assertEqual(beta[0]["moved"], [])

    def test_owner_is_the_largest_real_marginal_step(self) -> None:
        owners = self.report["owners"]
        self.assertEqual(owners["cons@a"]["tag"], "v2")
        self.assertEqual(owners["cons@a"]["repo"], "alpha")
        self.assertIsNone(owners["cons@b"])
        self.assertIsNone(owners["cons@c"])

    def test_additivity_compares_the_sweep_with_the_release_product(self) -> None:
        additivity = self.report["additivity"]["sweep"]
        # The newest *complete* sweep, not the gate-failed one after it.
        self.assertEqual(additivity["reading"], "20260803T000000Z-sweep-h2")
        tasks = additivity["tasks"]
        self.assertAlmostEqual(tasks["cons@a"]["predicted"], 1.25)
        self.assertAlmostEqual(tasks["cons@a"]["residual"], 1.0)
        self.assertAlmostEqual(tasks["cons@c"]["observed"], 3000 / 3050)
        self.assertAlmostEqual(tasks["cons@c"]["predicted"], 1.0)
        self.assertEqual(set(tasks["cons@a"]["contributions"]), {"alpha", "beta"})
        self.assertLess(additivity["residual"], 1.0)

    def test_health_counts_and_unknown_evaluations(self) -> None:
        health = self.report["health"]
        self.assertEqual(health["readings"], 11)
        self.assertEqual(health["partial"], 2)
        self.assertEqual(health["unknown_evaluation_records"], 1)
        self.assertEqual(health["unknown_evaluations"], ["mystery"])
        self.assertEqual(health["by_kind"]["release"], 3)
        self.assertEqual(health["error_count"], 2)
        readings = self.report["readings"]
        self.assertEqual(len(readings), 11)
        self.assertEqual(readings[0]["id"], "20260801T000000Z-sweep-seed")
        self.assertEqual(readings[0]["tasks"], 3)

    def test_empty_records_still_report(self) -> None:
        report = build_report(self.manifest, [])
        self.assertEqual(
            report["series"],
            {"sweep": [], "downstream-alpha": [], "downstream-runner": []},
        )
        self.assertEqual(report["releases"], {})
        self.assertEqual(report["additivity"], {})
        self.assertEqual(report["health"]["readings"], 0)
        self.assertIsNone(report["evaluations"]["sweep"]["reference"])


if __name__ == "__main__":
    unittest.main()
