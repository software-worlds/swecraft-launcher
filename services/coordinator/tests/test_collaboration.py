from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from swecraft_coordinator.collaboration import (
    TREATMENT,
    Rate,
    below_rate_due,
    cadence_due,
    collaboration_nudge,
    cross_repository_rate,
    prompt_variables,
    wake_reached_out,
)
from swecraft_coordinator.config import (
    AgentProfile,
    BelowRate,
    CollaborationNudge,
    CoordinatorConfig,
    Nudges,
    PromptPolicy,
)
from swecraft_coordinator.state import StateStore


def agent(name: str, *repos: str) -> AgentProfile:
    return AgentProfile(
        name=name,
        home_repo=repos[0],
        write_repos=tuple(repos),
        image="image:1",
        model="test/model",
        interval_seconds=600,
        timeout_seconds=300,
    )


def below(target: float = 0.5, window: int = 4, every_wakes: int = 0) -> Nudges:
    return Nudges(
        collaboration=CollaborationNudge(
            below_rate=BelowRate(target=target, window=window), every_wakes=every_wakes
        )
    )


def make_config(root: Path, **overrides) -> CoordinatorConfig:
    values = dict(
        forge_url="http://127.0.0.1:3400",
        push_url="http://forge.internal:3000",
        model="test/model",
        agents=(agent("demo-bot", "hub/demo"), agent("lib-bot", "hub/lib")),
        agent_env_sources={},
        runs_dir=root / "runs",
        database=root / "state.db",
        credentials_dir=root / "credentials",
        nudges=below(),
    )
    values.update(overrides)
    return CoordinatorConfig(**values)


def ledger(root: Path, run_id: str, rows: list[dict]) -> None:
    output = root / "runs" / run_id / "output"
    output.mkdir(parents=True, exist_ok=True)
    (output / "forge-actions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


class CollaborationNudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = make_config(self.root)
        self.state = StateStore(self.config.database)
        self.profile = self.config.agents[0]

    def complete(self, run_id: str, rows: list[dict] | None = None) -> None:
        wake = self.state.claim("demo-bot", stale_after_seconds=3600)
        self.state.finish(
            "demo-bot",
            wake,
            run_id=run_id,
            outcome="worked",
            summary=None,
            cost_usd=0.0,
            next_due_seconds=0,
        )
        if rows is not None:
            ledger(self.root, run_id, rows)

    def release(self, wake: int) -> None:
        """Let go of a claimed wake as failed: it leaves the window untouched."""

        self.state.fail(
            "demo-bot",
            wake,
            error="released",
            run_id=None,
            cost_usd=0.0,
            next_due_seconds=0,
        )

    def test_a_wake_reached_out_when_it_acted_on_another_repository(self) -> None:
        own = frozenset({"hub/demo"})
        ledger(self.root, "r1", [{"repo": "hub/demo", "action": "pr.create"}])
        self.assertFalse(wake_reached_out(self.root / "runs" / "r1", own))
        ledger(self.root, "r2", [{"repo": "hub/lib", "action": "issue.comment"}])
        self.assertTrue(wake_reached_out(self.root / "runs" / "r2", own))
        # Housekeeping elsewhere is not communication, nor is a reaction (the
        # cheapest way to satisfy a stated rate); a missing ledger is nothing.
        ledger(self.root, "r3", [{"repo": "hub/lib", "action": "ci.rerun"}, "garbage"])
        self.assertFalse(wake_reached_out(self.root / "runs" / "r3", own))
        ledger(self.root, "r5", [{"repo": "hub/lib", "action": "thread.react"}])
        self.assertFalse(wake_reached_out(self.root / "runs" / "r5", own))
        self.assertFalse(wake_reached_out(self.root / "runs" / "r4", own))

    def test_rate_needs_a_full_window_of_completed_wakes(self) -> None:
        self.complete("r1", [{"repo": "hub/lib", "action": "issue.create"}])
        self.complete("r2", [])
        self.complete("r3", [])
        self.assertIsNone(cross_repository_rate(self.config, self.state, self.profile))
        self.complete("r4", [])
        self.assertEqual(
            cross_repository_rate(self.config, self.state, self.profile), Rate(1, 4)
        )
        # The window slides: r1 leaves after one more completed wake.
        self.complete("r5", [])
        self.assertEqual(
            cross_repository_rate(self.config, self.state, self.profile), Rate(0, 4)
        )
        # A failed wake does not count as a completed one.
        wake = self.state.claim("demo-bot", stale_after_seconds=3600)
        self.state.fail(
            "demo-bot", wake, error="x", run_id="r6", cost_usd=0.0, next_due_seconds=0
        )
        self.assertEqual(
            cross_repository_rate(self.config, self.state, self.profile), Rate(0, 4)
        )

    def test_due_below_target_and_never_under_isolation_or_without_a_target(
        self,
    ) -> None:
        for run_id, rows in (
            ("r1", [{"repo": "hub/lib", "action": "pr.review"}]),
            ("r2", []),
            ("r3", []),
            ("r4", []),
        ):
            self.complete(run_id, rows)
        self.assertEqual(
            below_rate_due(self.config, self.state, self.profile), Rate(1, 4)
        )
        lower = make_config(self.root, nudges=below(target=0.25))
        self.assertIsNone(below_rate_due(lower, self.state, self.profile))
        off = make_config(self.root, nudges=Nudges())
        self.assertIsNone(below_rate_due(off, self.state, self.profile))
        isolated = replace(self.config, prompt=PromptPolicy(collaboration="isolated"))
        self.assertIsNone(below_rate_due(isolated, self.state, self.profile))

    def test_cadence_counts_the_wake_being_prepared(self) -> None:
        every = make_config(
            self.root, nudges=Nudges(collaboration=CollaborationNudge(every_wakes=2))
        )
        self.assertFalse(cadence_due(self.config, self.state, self.profile))
        first = self.state.claim("demo-bot", stale_after_seconds=3600)
        self.assertFalse(cadence_due(every, self.state, self.profile))
        self.release(first)
        second = self.state.claim("demo-bot", stale_after_seconds=3600)
        self.assertTrue(cadence_due(every, self.state, self.profile))
        self.release(second)

    def test_prompt_variables(self) -> None:
        off = prompt_variables(None, self.config)
        self.assertFalse(off["COLLABORATION_CHECK"])
        self.assertFalse(off["COLLABORATION_RATE"])
        # The cadence alone: the check without a rate to state.
        cadence = prompt_variables(None, self.config, cadence=True)
        self.assertTrue(cadence["COLLABORATION_CHECK"])
        self.assertFalse(cadence["COLLABORATION_RATE"])
        # The record: the check with the rate stated.
        self.assertEqual(
            prompt_variables(Rate(1, 4), self.config),
            {
                "COLLABORATION_CHECK": True,
                "COLLABORATION_RATE": True,
                "COLLABORATION_WINDOW": 4,
                "COLLABORATION_REACHED": 1,
                "COLLABORATION_TARGET": 2,
            },
        )
        # The shown target is the smallest count that satisfies the rate, so
        # a fractional target never reads as already met on a triggered wake.
        fractional = make_config(self.root, nudges=below(target=0.45, window=10))
        self.assertEqual(
            prompt_variables(Rate(4, 10), fractional)["COLLABORATION_TARGET"], 5
        )
        self.assertEqual(
            prompt_variables(Rate(1, 10), self.config)["COLLABORATION_TARGET"], 5
        )

    def test_the_check_is_decided_and_recorded_per_wake(self) -> None:
        for run_id in ("r1", "r2", "r3", "r4"):
            self.complete(run_id, [])
        # The record alone.
        wake = self.state.claim("demo-bot", stale_after_seconds=3600)
        variables = collaboration_nudge(
            self.config, self.state, self.profile, wake_id=wake
        )
        self.assertTrue(variables["COLLABORATION_CHECK"])
        self.assertTrue(variables["COLLABORATION_RATE"])
        self.assertEqual(self.state.treatment(wake), (TREATMENT, "0 of 4 reached out"))
        self.release(wake)
        # The cadence alone records its cadence; both at once record the rate.
        every = make_config(
            self.root, nudges=Nudges(collaboration=CollaborationNudge(every_wakes=1))
        )
        wake = self.state.claim("demo-bot", stale_after_seconds=3600)
        variables = collaboration_nudge(every, self.state, self.profile, wake_id=wake)
        self.assertTrue(variables["COLLABORATION_CHECK"])
        self.assertFalse(variables["COLLABORATION_RATE"])
        self.assertEqual(self.state.treatment(wake), (TREATMENT, "every 1 wakes"))
        self.release(wake)
        both = make_config(self.root, nudges=below(every_wakes=1))
        wake = self.state.claim("demo-bot", stale_after_seconds=3600)
        variables = collaboration_nudge(both, self.state, self.profile, wake_id=wake)
        self.assertTrue(variables["COLLABORATION_RATE"])
        self.assertEqual(self.state.treatment(wake), (TREATMENT, "0 of 4 reached out"))
        self.release(wake)
        # Nothing configured: nothing marked.
        quiet = make_config(self.root, nudges=Nudges())
        wake = self.state.claim("demo-bot", stale_after_seconds=3600)
        collaboration_nudge(quiet, self.state, self.profile, wake_id=wake)
        self.assertIsNone(self.state.treatment(wake))


if __name__ == "__main__":
    unittest.main()
