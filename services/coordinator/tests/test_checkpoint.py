"""Coordinator owns checkpoint interpretation of its config and state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.checkpoint import CoordinatorCheckpoint
from swecraft_coordinator.state import StateStore


class CoordinatorCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "coordinator.yaml"
        self.config.write_text(
            "forge_url: http://127.0.0.1:3400\n"
            "model: test/model\n"
            "image: agent:1\n"
            "api_token: file:.swecraft/api_token\n"
            "agents:\n  - name: demo\n    repo: hub/demo\n"
        )
        hub = self.root / ".swecraft"
        (hub / "coordinator/credentials").mkdir(parents=True)
        (hub / "api_token").write_text("api\n")
        (hub / "coordinator/credentials/demo.token").write_text("agent\n")
        (hub / "runs/run-1/output").mkdir(parents=True)
        (hub / "runs/run-1/output/result.json").write_text(
            '{"run_id": "run-1", "status": "completed"}\n'
        )
        (hub / "runs/run-1/harbor").mkdir()
        (hub / "runs/run-1/harbor/result.json").write_text("{}\n")
        self.state = StateStore(hub / "coordinator.db")
        self.state.ensure_agent("demo")

    def test_capture_uses_authoritative_coordinator_configuration(self) -> None:
        source = CoordinatorCheckpoint.load(self.config, root=self.root)
        stage = self.root / "stage"
        source.assert_quiescent()
        skipped = source.capture(
            state_destination=stage / "state",
            runs_destination=stage / "runs",
            include_harbor=True,
        )
        self.assertEqual(skipped, ())
        self.assertEqual(source.agent_image, "agent:1")
        self.assertTrue((stage / "state/coordinator.db").is_file())
        self.assertEqual((stage / "state/api_token").read_text(), "api\n")
        self.assertEqual(
            (stage / "state/credentials/demo.token").read_text(), "agent\n"
        )
        self.assertTrue((stage / "runs/run-1/output/result.json").is_file())
        self.assertTrue((stage / "runs/run-1/harbor.tar.gz").is_file())
        self.assertTrue((hub := self.root / ".swecraft/runs/run-1/harbor").is_dir())
        self.assertFalse((hub.parent / "harbor.tar.gz").exists())

    def test_repeat_captures_reuse_the_harbor_cache_and_prune_it(self) -> None:
        source = CoordinatorCheckpoint.load(self.config, root=self.root)
        first = self.root / "first"
        source.capture(
            state_destination=first / "state",
            runs_destination=first / "runs",
            include_harbor=True,
        )
        cache = self.root / ".swecraft/harbor-cache/run-1.harbor.tar.gz"
        self.assertTrue(cache.is_file())
        self.assertEqual(
            cache.read_bytes(), (first / "runs/run-1/harbor.tar.gz").read_bytes()
        )

        # Second capture must not re-pack: identical bytes come from cache.
        from unittest.mock import patch

        second = self.root / "second"
        with patch(
            "swecraft_runner.checkpoint._pack_harbor",
            side_effect=AssertionError("cache hit must not re-pack"),
        ):
            source.capture(
                state_destination=second / "state",
                runs_destination=second / "runs",
                include_harbor=True,
            )
        self.assertEqual(
            (second / "runs/run-1/harbor.tar.gz").read_bytes(), cache.read_bytes()
        )

        # A cache entry for a swept run is pruned on the next capture,
        # and so is .tmp litter from a fill crashed mid-publish.
        stale = self.root / ".swecraft/harbor-cache/run-0.harbor.tar.gz"
        stale.write_bytes(b"x")
        litter = self.root / ".swecraft/harbor-cache/.run-1.k3j2h1.tmp"
        litter.write_bytes(b"partial")
        third = self.root / "third"
        source.capture(
            state_destination=third / "state",
            runs_destination=third / "runs",
            include_harbor=True,
        )
        self.assertFalse(stale.exists())
        self.assertFalse(litter.exists())
        self.assertTrue(cache.is_file())

    def test_capture_skips_partial_run_beside_terminal_run(self) -> None:
        partial = self.root / ".swecraft/runs/run-partial"
        (partial / "output").mkdir(parents=True)
        (partial / "output/result.json").write_text('{"status": "running"}\n')
        (self.root / ".swecraft/runs/operator-note.txt").write_text("not a run\n")
        source = CoordinatorCheckpoint.load(self.config, root=self.root)
        stage = self.root / "stage"

        skipped = source.capture(
            state_destination=stage / "state",
            runs_destination=stage / "runs",
        )

        self.assertEqual(skipped, ("operator-note.txt", "run-partial"))
        self.assertTrue((stage / "runs/run-1/output/result.json").is_file())
        self.assertFalse((stage / "runs/run-partial").exists())

    def test_active_wake_is_refused_by_the_state_owner(self) -> None:
        self.state.claim("demo", stale_after_seconds=3_600)
        source = CoordinatorCheckpoint.load(self.config, root=self.root)
        with self.assertRaisesRegex(CoordinatorError, "active wakes"):
            source.assert_quiescent()


if __name__ == "__main__":
    unittest.main()
