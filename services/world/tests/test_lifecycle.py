from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swecraft_world import WorldError
from swecraft_world.lifecycle import world_mutation_lock


class WorldMutationLockTests(unittest.TestCase):
    def test_second_mutation_fails_and_release_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "world.lock.json"
            with world_mutation_lock(lock_path):
                with self.assertRaisesRegex(WorldError, "already using"):
                    with world_mutation_lock(lock_path):
                        self.fail("the second mutation must not acquire the lock")

            with world_mutation_lock(lock_path):
                pass


if __name__ == "__main__":
    unittest.main()
