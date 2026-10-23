from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from swecraft_common.lifecycle import LifecycleBusy

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.state import StateStore


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = StateStore(Path(self._directory.name) / "state.db")

    def test_new_agent_is_due_immediately(self) -> None:
        self.store.ensure_agent("demo-bot")
        self.assertTrue(self.store.is_due("demo-bot"))

    def test_claim_logs_and_waits_behind_checkpoint_lifecycle(self) -> None:
        with (
            patch(
                "swecraft_coordinator.state.society_lifecycle_lock",
                side_effect=[LifecycleBusy("busy"), nullcontext()],
            ),
            self.assertLogs("swecraft_coordinator.state", level="INFO") as logs,
        ):
            self.store.claim("demo-bot", stale_after_seconds=3600)
        self.assertIn("waiting to claim demo-bot", logs.output[0])
        status = self.store.status("demo-bot")
        assert status is not None
        self.assertIsNotNone(status.active_started)

    def test_claim_blocks_second_claim_until_finished(self) -> None:
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.assertFalse(self.store.is_due("demo-bot"))
        with self.assertRaisesRegex(CoordinatorError, "active wake"):
            self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.finish(
            "demo-bot",
            wake,
            run_id="run-1",
            outcome="worked",
            summary="done",
            cost_usd=0.5,
            next_due_seconds=600,
        )
        status = self.store.status("demo-bot")
        assert status is not None
        self.assertIsNone(status.active_started)
        self.assertEqual(status.last_outcome, "worked")
        self.assertEqual(status.consecutive_failures, 0)
        self.assertFalse(self.store.is_due("demo-bot"))
        due = datetime.fromisoformat(status.next_due)
        self.assertGreater(due, datetime.now(UTC))

    def test_stale_claim_is_taken_over_and_marked_interrupted(self) -> None:
        self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.claim("demo-bot", stale_after_seconds=0)
        with sqlite3.connect(self.store._path) as connection:
            statuses = [
                row[0]
                for row in connection.execute("SELECT status FROM wakes ORDER BY id")
            ]
        self.assertEqual(statuses, ["interrupted", "running"])
        status = self.store.status("demo-bot")
        assert status is not None
        self.assertTrue(status.last_uncertain)
        self.assertIn("interrupted", status.last_error or "")

    def test_superseded_wake_cannot_mutate_scheduler_state(self) -> None:
        first = self.store.claim("demo-bot", stale_after_seconds=3600)
        second = self.store.claim("demo-bot", stale_after_seconds=0)
        fenced = self.store.finish(
            "demo-bot",
            first,
            run_id="run-old",
            outcome="worked",
            summary="stale",
            cost_usd=0.1,
            next_due_seconds=600,
        )
        self.assertFalse(fenced)
        status = self.store.status("demo-bot")
        assert status is not None
        # The newer wake still owns the claim; the stale completion changed
        # nothing about scheduling.
        self.assertEqual(status.active_wake_id, second)
        self.assertIsNotNone(status.active_started)
        self.assertNotEqual(status.last_run_id, "run-old")
        with sqlite3.connect(self.store._path) as connection:
            row = connection.execute(
                "SELECT status FROM wakes WHERE id = ?", (first,)
            ).fetchone()
        self.assertEqual(row[0], "superseded")
        self.assertTrue(
            self.store.finish(
                "demo-bot",
                second,
                run_id="run-new",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                next_due_seconds=600,
            )
        )

    def test_run_wakes_counts_every_wake_started(self) -> None:
        self.assertEqual(self.store.run_wakes("demo-bot"), 0)
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.assertEqual(self.store.run_wakes("demo-bot"), 1)
        self.store.fail(
            "demo-bot",
            wake,
            error="boom",
            run_id=None,
            cost_usd=0.1,
            next_due_seconds=60,
        )
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.finish(
            "demo-bot",
            wake,
            run_id="run-2",
            outcome="idle",
            summary="quiet",
            cost_usd=0.0,
            next_due_seconds=60,
        )
        # A stale claim taken over counts too: the interrupted wake happened.
        self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.claim("demo-bot", stale_after_seconds=0)
        self.assertEqual(self.store.run_wakes("demo-bot"), 4)
        self.assertEqual(self.store.run_wakes("other-bot"), 0)

    def test_wake_treatments_are_recorded_beside_the_wake(self) -> None:
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.assertIsNone(self.store.treatment(wake))
        self.store.mark_treatment(wake, "collaboration", "2 of 10 reached out")
        self.assertEqual(
            self.store.treatment(wake), ("collaboration", "2 of 10 reached out")
        )
        self.store.mark_treatment(wake, "collaboration", "3 of 10 reached out")
        self.assertEqual(self.store.treatment(wake)[1], "3 of 10 reached out")

    def test_ledger_allocation_round_trips(self) -> None:
        self.assertEqual(self.store.ledger_allocation(), (None, {}))
        self.store.set_ledger_allocation("a" * 40, {"demo-bot": 80, "other-bot": 120})
        self.assertEqual(
            self.store.ledger_allocation(),
            ("a" * 40, {"demo-bot": 80, "other-bot": 120}),
        )
        self.store.set_ledger_allocation("b" * 40, {"demo-bot": 90, "other-bot": 110})
        self.assertEqual(self.store.ledger_allocation()[0], "b" * 40)

    def test_failure_marks_uncertain_and_success_clears_it(self) -> None:
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.fail(
            "demo-bot",
            wake,
            error="memory conflict after pushing a branch",
            run_id="run-1",
            cost_usd=0.2,
            next_due_seconds=60,
        )
        status = self.store.status("demo-bot")
        assert status is not None
        self.assertTrue(status.last_uncertain)
        self.assertIn("memory conflict", status.last_error or "")
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.finish(
            "demo-bot",
            wake,
            run_id="run-2",
            outcome="worked",
            summary="reconciled",
            cost_usd=0.1,
            next_due_seconds=600,
        )
        status = self.store.status("demo-bot")
        assert status is not None
        self.assertFalse(status.last_uncertain)
        self.assertIsNone(status.last_error)

    def test_bootstrap_flag(self) -> None:
        self.assertFalse(self.store.is_bootstrapped("demo-bot"))
        self.store.mark_bootstrapped("demo-bot")
        self.assertTrue(self.store.is_bootstrapped("demo-bot"))

    def test_recover_releases_a_dead_claim_without_charging_a_failure(self) -> None:
        self.assertFalse(self.store.recover("demo-bot"))
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.assertTrue(self.store.recover("demo-bot"))
        status = self.store.status("demo-bot")
        assert status is not None
        self.assertIsNone(status.active_started)
        self.assertEqual(status.consecutive_failures, 0)
        self.assertTrue(status.last_uncertain)
        self.assertIn("operator recovery", status.last_error or "")
        with sqlite3.connect(self.store._path) as connection:
            row = connection.execute(
                "SELECT status FROM wakes WHERE id = ?", (wake,)
            ).fetchone()
        self.assertEqual(row[0], "cancelled")
        self.assertFalse(self.store.recover("demo-bot"))

    def test_failures_back_off_and_accumulate(self) -> None:
        wake = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.fail(
            "demo-bot",
            wake,
            error="boom",
            run_id=None,
            cost_usd=0.0,
            next_due_seconds=300,
        )
        status = self.store.status("demo-bot")
        assert status is not None
        self.assertEqual(status.consecutive_failures, 1)
        self.assertFalse(self.store.is_due("demo-bot"))

    def test_day_usage_sums_wakes_and_cost(self) -> None:
        first = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.finish(
            "demo-bot",
            first,
            run_id="run-1",
            outcome="idle",
            summary=None,
            cost_usd=0.25,
            next_due_seconds=0,
        )
        second = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.fail(
            "demo-bot",
            second,
            error="boom",
            run_id=None,
            cost_usd=0.1,
            next_due_seconds=60,
        )
        wakes, cost = self.store.day_usage("demo-bot")
        self.assertEqual(wakes, 2)
        self.assertAlmostEqual(cost, 0.35)
        self.assertEqual(self.store.day_usage("other-bot"), (0, 0.0))

    def test_list_wakes_is_newest_first_and_filterable(self) -> None:
        first = self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.finish(
            "demo-bot",
            first,
            run_id="run-1",
            outcome="worked",
            summary="one",
            cost_usd=0.25,
            next_due_seconds=0,
        )
        self.store.claim("demo-bot", stale_after_seconds=3600)
        self.store.claim("docs-bot", stale_after_seconds=3600)

        everything = self.store.list_wakes()
        self.assertEqual([wake.id for wake in everything], [3, 2, 1])
        self.assertEqual(
            [wake.agent for wake in self.store.list_wakes(agent="demo-bot")],
            ["demo-bot", "demo-bot"],
        )
        self.assertEqual(
            [wake.id for wake in self.store.list_wakes(status="completed")], [1]
        )
        self.assertEqual([wake.id for wake in self.store.list_wakes(limit=1)], [3])

        completed = self.store.list_wakes(status="completed")[0]
        self.assertEqual(completed.run_id, "run-1")
        self.assertEqual(completed.outcome, "worked")
        self.assertEqual(completed.summary, "one")
        self.assertAlmostEqual(completed.cost_usd, 0.25)
        self.assertIsNotNone(completed.finished)

    def test_list_wakes_on_an_empty_store(self) -> None:
        self.assertEqual(self.store.list_wakes(), [])
        self.assertEqual(self.store.known_agents(), [])

    def test_known_agents_includes_agents_without_wakes(self) -> None:
        self.store.ensure_agent("docs-bot")
        self.store.claim("demo-bot", stale_after_seconds=3600)
        self.assertEqual(self.store.known_agents(), ["demo-bot", "docs-bot"])


if __name__ == "__main__":
    unittest.main()


class TimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.store = StateStore(Path(self._directory.name) / "state.db")

    def test_completed_timeline_lists_finished_wakes_oldest_first(self) -> None:
        for agent, outcome, cost in (
            ("a-bot", "worked", 0.5),
            ("b-bot", "idle", 0.25),
            ("a-bot", "worked", 1.0),
        ):
            wake = self.store.claim(agent, stale_after_seconds=3600)
            self.store.finish(
                agent,
                wake,
                run_id=f"run-{wake}",
                outcome=outcome,
                summary="done",
                cost_usd=cost,
                next_due_seconds=600,
            )
        # A wake that is still running has no finish time and is left out.
        self.store.claim("c-bot", stale_after_seconds=3600)
        timeline = self.store.completed_timeline()
        self.assertEqual(
            [(outcome, cost) for _, outcome, cost in timeline],
            [("worked", 0.5), ("idle", 0.25), ("worked", 1.0)],
        )
        finished = [row[0] for row in timeline]
        self.assertEqual(finished, sorted(finished))
