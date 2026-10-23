"""Shared society lifecycle lock path and exclusion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swecraft_common.lifecycle import (
    LifecycleBusy,
    lifecycle_lock_path,
    society_lifecycle_lock,
)


class LifecycleTests(unittest.TestCase):
    def test_state_stores_under_one_hub_root_share_the_lock(self) -> None:
        root = Path("/tmp/example/.swecraft").resolve()
        self.assertEqual(
            lifecycle_lock_path(root / "coordinator.db"),
            lifecycle_lock_path(root / "evaluator/evaluator.db"),
        )
        self.assertEqual(
            lifecycle_lock_path(root / "coordinator.db"),
            root / "society.lifecycle.lock",
        )

    def test_loaded_service_state_uses_one_shared_lock(self) -> None:
        root = Path("/tmp/loaded/state").resolve()
        self.assertEqual(
            lifecycle_lock_path(root / "coordinator/coordinator.db"),
            lifecycle_lock_path(root / "evaluator/evaluator.db"),
        )
        self.assertEqual(
            lifecycle_lock_path(root / "coordinator/coordinator.db"),
            root / "society.lifecycle.lock",
        )

    def test_loaded_state_nested_under_hub_prefers_the_nearest_root(self) -> None:
        outer = Path("/tmp/original/.swecraft").resolve()
        state = outer / "loaded/checkpoint/state"
        coordinator = lifecycle_lock_path(state / "coordinator/coordinator.db")
        evaluator = lifecycle_lock_path(state / "evaluator/evaluator.db")
        self.assertEqual(coordinator, state / "society.lifecycle.lock")
        self.assertEqual(evaluator, coordinator)
        self.assertNotEqual(coordinator, outer / "society.lifecycle.lock")

    def test_lock_excludes_another_owner_and_releases(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        state = Path(scratch.name) / ".swecraft/coordinator.db"
        with society_lifecycle_lock(state):
            with self.assertRaises(LifecycleBusy):
                with society_lifecycle_lock(state):
                    pass
        with society_lifecycle_lock(state) as lock_path:
            self.assertEqual(
                lock_path,
                Path(scratch.name).resolve() / ".swecraft/society.lifecycle.lock",
            )


if __name__ == "__main__":
    unittest.main()
