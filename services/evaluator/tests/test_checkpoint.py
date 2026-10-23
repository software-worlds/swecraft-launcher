import tempfile
import unittest
from pathlib import Path

from swecraft_evaluator.checkpoint import EvaluatorCheckpoint
from swecraft_evaluator.errors import EvaluatorError


class EvaluatorCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.records = self.root / "records"
        self.state = self.root / "state.json"
        self.stage = self.root / "stage"
        self.adapter = EvaluatorCheckpoint(
            records_dir=self.records, state_path=self.state
        )

    def capture(self) -> tuple[str, ...]:
        return self.adapter.capture(
            state_destination=self.stage / "state/evaluator",
            results_destination=self.stage / "evaluations",
        )

    def test_complete_records_and_state_are_captured(self) -> None:
        record = self.records / "20260810T000000Z-checks-abc123"
        (record / "society-general").mkdir(parents=True)
        (record / "record.json").write_text('{"id": "r"}\n', encoding="utf-8")
        (record / "society-general/container.log").write_text("ok\n", encoding="utf-8")
        self.state.write_text('{"releases": {}}\n', encoding="utf-8")

        skipped = self.capture()

        self.assertEqual(skipped, ())
        captured = self.stage / "evaluations/20260810T000000Z-checks-abc123"
        self.assertEqual(
            (captured / "record.json").read_text(encoding="utf-8"), '{"id": "r"}\n'
        )
        self.assertTrue((captured / "society-general/container.log").is_file())
        self.assertEqual(
            (self.stage / "state/evaluator/state.json").read_text(encoding="utf-8"),
            '{"releases": {}}\n',
        )

    def test_injected_sources_are_pruned_from_the_capture(self) -> None:
        record = self.records / "20260810T000000Z-checks-abc123"
        (record / "cell/sources/requests/tests/certs").mkdir(parents=True)
        (record / "record.json").write_text('{"id": "r"}\n', encoding="utf-8")
        (record / "cell/container.log").write_text("ok\n", encoding="utf-8")
        (record / "cell/sources/requests/setup.py").write_text("", encoding="utf-8")
        # Upstream test fixtures carry symlinks; pruned means never seen.
        (record / "cell/sources/requests/tests/certs/ca").symlink_to("../ca.pem")

        skipped = self.capture()

        self.assertEqual(skipped, ())
        captured = self.stage / "evaluations/20260810T000000Z-checks-abc123"
        self.assertTrue((captured / "cell/container.log").is_file())
        self.assertFalse((captured / "cell/sources").exists())

    def test_record_directory_without_record_json_is_skipped(self) -> None:
        complete = self.records / "complete"
        complete.mkdir(parents=True)
        (complete / "record.json").write_text("{}\n", encoding="utf-8")
        inflight = self.records / "inflight"
        inflight.mkdir()
        (inflight / "container.log").write_text("running\n", encoding="utf-8")

        skipped = self.capture()

        self.assertEqual(skipped, ("inflight",))
        self.assertTrue((self.stage / "evaluations/complete/record.json").is_file())
        self.assertFalse((self.stage / "evaluations/inflight").exists())

    def test_missing_records_and_state_capture_nothing(self) -> None:
        skipped = self.capture()

        self.assertEqual(skipped, ())
        self.assertFalse((self.stage / "evaluations").exists())
        self.assertFalse((self.stage / "state/evaluator").exists())

    def test_symlinked_record_entry_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.records.mkdir()
        (self.records / "linked").symlink_to(outside)

        with self.assertRaisesRegex(EvaluatorError, "symlink"):
            self.capture()

    def test_symlinked_state_is_refused(self) -> None:
        real = self.root / "real-state.json"
        real.write_text("{}\n", encoding="utf-8")
        self.state.symlink_to(real)

        with self.assertRaisesRegex(EvaluatorError, "symlink"):
            self.capture()


if __name__ == "__main__":
    unittest.main()
