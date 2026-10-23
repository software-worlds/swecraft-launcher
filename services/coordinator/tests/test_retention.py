from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from swecraft_coordinator.retention import slim_run, sweep


def build_run_dir(root: Path, run_id: str) -> Path:
    run_dir = root / run_id
    for relative in (
        "input/repository/.git",
        "input/memory/.git",
        "input/repos/lib/.git",
        "input/prior-session/subagents",
        "harbor/tasks/task",
        "harbor/jobs/j/t/artifacts/app/repository",
        "harbor/jobs/j/t/artifacts/app/memory",
    ):
        (run_dir / relative).mkdir(parents=True)
        (run_dir / relative / "blob.bin").write_bytes(b"x" * 1024)
    (run_dir / "input" / "prompt.md").write_text("prompt\n", encoding="utf-8")
    (run_dir / "input" / "request.json").write_text("{}", encoding="utf-8")
    output = run_dir / "output"
    output.mkdir(parents=True)
    (output / "response.md").write_text("done\n", encoding="utf-8")
    (output / "result.json").write_text("{}", encoding="utf-8")
    session = output / "session"
    session.mkdir()
    (session / "thread.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "harbor" / "stdout.log").write_text("log\n", encoding="utf-8")
    return run_dir


class SlimRunTests(unittest.TestCase):
    def test_removes_repository_copies_and_keeps_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = build_run_dir(Path(directory), "20260722T010000Z-aaaaaaaa")

            freed = slim_run(run_dir)

            self.assertGreaterEqual(freed, 7 * 1024)
            self.assertFalse((run_dir / "input" / "repository").exists())
            self.assertFalse((run_dir / "input" / "memory").exists())
            self.assertFalse((run_dir / "input" / "repos").exists())
            self.assertFalse((run_dir / "input" / "prior-session").exists())
            self.assertFalse((run_dir / "harbor" / "tasks").exists())
            self.assertFalse(
                (run_dir / "harbor/jobs/j/t/artifacts/app/repository").exists()
            )
            self.assertFalse(
                (run_dir / "harbor/jobs/j/t/artifacts/app/memory").exists()
            )
            self.assertTrue((run_dir / "input" / "prompt.md").is_file())
            self.assertTrue((run_dir / "input" / "request.json").is_file())
            self.assertTrue((run_dir / "output" / "response.md").is_file())
            self.assertTrue((run_dir / "output" / "result.json").is_file())
            self.assertTrue((run_dir / "output" / "session" / "thread.jsonl").is_file())
            self.assertTrue((run_dir / "harbor" / "stdout.log").is_file())

    def test_missing_run_dir_is_a_noop(self) -> None:
        self.assertEqual(slim_run(Path("/nonexistent/run")), 0)

    def test_slim_participates_in_the_society_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = build_run_dir(Path(directory), "20260722T010000Z-aaaaaaaa")
            with patch("swecraft_coordinator.retention.society_lifecycle_lock") as lock:
                slim_run(run_dir)
            lock.assert_called_once_with(run_dir, timeout_seconds=-1)


class SweepTests(unittest.TestCase):
    def test_removes_only_expired_run_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = build_run_dir(root, "20260101T000000Z-aaaaaaaa")
            recent = build_run_dir(root, "20260721T000000Z-bbbbbbbb")
            unrelated = root / "not-a-run"
            unrelated.mkdir()

            removed = sweep(root, 14, now=datetime(2026, 7, 22, tzinfo=UTC))

            self.assertEqual(removed, ["20260101T000000Z-aaaaaaaa"])
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())

    def test_sweep_participates_in_the_society_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("swecraft_coordinator.retention.society_lifecycle_lock") as lock:
                sweep(root, 14)
            lock.assert_called_once_with(root, timeout_seconds=-1)

    def test_zero_retention_disables_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_run_dir(root, "20200101T000000Z-cccccccc")
            self.assertEqual(sweep(root, 0), [])


if __name__ == "__main__":
    unittest.main()
