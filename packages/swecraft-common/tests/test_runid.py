from __future__ import annotations

import unittest
from datetime import UTC, datetime

from swecraft_common.runid import is_run_id, started_at


class RunIdTests(unittest.TestCase):
    def test_accepts_the_runner_grammar(self) -> None:
        self.assertTrue(is_run_id("20260722T010203Z-0123abcd"))

    def test_rejects_near_misses(self) -> None:
        for value in (
            "20260722T010203Z-0123ABCD",  # uppercase hex
            "20260722T010203Z-0123abc",  # short suffix
            "not-a-run-id",
            "../20260722T010203Z-0123abcd",  # traversal must not fullmatch
        ):
            self.assertFalse(is_run_id(value), value)

    def test_started_at_reads_the_embedded_timestamp(self) -> None:
        self.assertEqual(
            started_at("20260722T010203Z-0123abcd"),
            datetime(2026, 7, 22, 1, 2, 3, tzinfo=UTC),
        )
        self.assertIsNone(started_at("nope"))


if __name__ == "__main__":
    unittest.main()
