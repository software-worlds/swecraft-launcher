from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from swecraft_common.forge import ForgeError

from swecraft_coordinator.config import (
    AgentProfile,
    CoordinatorConfig,
    LedgerFile,
    RunBudget,
)
from swecraft_coordinator.scheduler import (
    _refresh_ledger,
    _within_budget,
    serve_forever,
)
from swecraft_coordinator.state import StateStore
from swecraft_coordinator.wake import WakeReport


def make_config(root: Path, **overrides) -> CoordinatorConfig:
    values = dict(
        forge_url="http://127.0.0.1:3400",
        push_url="http://forge.internal:3000",
        model="test/model",
        agents=(
            AgentProfile(
                name="demo-bot",
                home_repo="hub/demo",
                write_repos=("hub/demo",),
                image="image:1",
                model="test/model",
                interval_seconds=600,
                timeout_seconds=300,
            ),
        ),
        agent_env_sources={},
        runs_dir=root / "runs",
        database=root / "state.db",
        credentials_dir=root / "credentials",
    )
    values.update(overrides)
    return CoordinatorConfig(**values)


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def test_serve_wakes_due_agents_once_per_interval(self) -> None:
        config = make_config(self.root)
        state = StateStore(config.database)
        stop = threading.Event()
        woken = []

        def wake(config_, profile, state_):
            wake_id = state_.claim(profile.name, stale_after_seconds=3600)
            state_.finish(
                profile.name,
                wake_id,
                run_id="run-1",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                next_due_seconds=profile.interval_seconds,
            )
            woken.append(profile.name)
            stop.set()
            return WakeReport(
                agent=profile.name,
                run_id="run-1",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                memory_commit=None,
            )

        serve_forever(config, state, stop, wake=wake)
        self.assertEqual(woken, ["demo-bot"])
        self.assertFalse(state.is_due("demo-bot"))

    def test_workers_bound_the_wakes_in_flight(self) -> None:
        # Three due agents, two workers: two must genuinely overlap, and the
        # third must wait for a slot rather than starting alongside them.
        agents = tuple(
            AgentProfile(
                name=f"bot-{index}",
                home_repo=f"hub/repo-{index}",
                write_repos=(f"hub/repo-{index}",),
                image="image:1",
                model="test/model",
                interval_seconds=600,
                timeout_seconds=300,
            )
            for index in range(3)
        )
        config = make_config(self.root, agents=agents, workers=2)
        state = StateStore(config.database)
        stop = threading.Event()
        started = threading.Semaphore(0)
        release = threading.Event()
        lock = threading.Lock()
        live = 0
        peak = 0
        order: list[str] = []

        def wake(config_, profile, state_):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
                order.append(profile.name)
            started.release()
            release.wait(5)
            wake_id = state_.claim(profile.name, stale_after_seconds=3600)
            state_.finish(
                profile.name,
                wake_id,
                run_id="run-1",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                next_due_seconds=profile.interval_seconds,
            )
            with lock:
                live -= 1
            return WakeReport(
                agent=profile.name,
                run_id="run-1",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                memory_commit=None,
            )

        def drive() -> None:
            self.assertTrue(started.acquire(timeout=10))
            self.assertTrue(started.acquire(timeout=10))
            # The third agent is due, but both workers are occupied.
            self.assertFalse(started.acquire(timeout=0.5))
            release.set()
            self.assertTrue(started.acquire(timeout=10))
            stop.set()

        driver = threading.Thread(target=drive)
        driver.start()
        serve_forever(config, state, stop, wake=wake)
        driver.join(15)
        self.assertEqual(peak, 2)
        self.assertEqual(sorted(order), ["bot-0", "bot-1", "bot-2"])

    def test_contended_slot_goes_to_the_oldest_due_agent(self) -> None:
        # alpha precedes beta in the roster, but beta became due first;
        # the single slot must go by waiting time, not YAML position.
        agents = tuple(
            AgentProfile(
                name=name,
                home_repo=f"hub/{name}",
                write_repos=(f"hub/{name}",),
                image="image:1",
                model="test/model",
                interval_seconds=600,
                timeout_seconds=300,
            )
            for name in ("alpha", "beta")
        )
        config = make_config(self.root, agents=agents, workers=1)
        state = StateStore(config.database)
        for name, seconds_overdue in (("alpha", 60), ("beta", 600)):
            state.ensure_agent(name)
            wake_id = state.claim(name, stale_after_seconds=3600)
            state.finish(
                name,
                wake_id,
                run_id="seed",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                next_due_seconds=-seconds_overdue,
            )
        stop = threading.Event()
        woken: list[str] = []

        def wake(config_, profile, state_):
            wake_id = state_.claim(profile.name, stale_after_seconds=3600)
            state_.finish(
                profile.name,
                wake_id,
                run_id="run-1",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                next_due_seconds=profile.interval_seconds,
            )
            woken.append(profile.name)
            stop.set()
            return WakeReport(
                agent=profile.name,
                run_id="run-1",
                outcome="idle",
                summary=None,
                cost_usd=0.0,
                memory_commit=None,
            )

        serve_forever(config, state, stop, wake=wake)
        self.assertEqual(woken, ["beta"])

    def test_serial_unless_configured_otherwise(self) -> None:
        self.assertEqual(make_config(self.root).workers, 1)

    def test_budget_caps_pause_the_agent(self) -> None:
        config = make_config(self.root, daily_wake_limit=1, daily_cost_limit_usd=1.0)
        state = StateStore(config.database)
        profile = config.agents[0]
        self.assertTrue(_within_budget(config, state, profile))
        wake_id = state.claim(profile.name, stale_after_seconds=3600)
        state.finish(
            profile.name,
            wake_id,
            run_id="run-1",
            outcome="worked",
            summary=None,
            cost_usd=0.2,
            next_due_seconds=0,
        )
        self.assertFalse(_within_budget(config, state, profile))
        config = make_config(self.root, daily_wake_limit=0, daily_cost_limit_usd=0.1)
        self.assertFalse(_within_budget(config, state, profile))
        config = make_config(self.root)
        self.assertTrue(_within_budget(config, state, profile))

    def test_run_budget_stops_the_agent_for_good(self) -> None:
        config = make_config(self.root, budget=RunBudget(wakes=2))
        state = StateStore(config.database)
        profile = config.agents[0]
        noticed: dict[str, str] = {}
        for _ in range(2):
            self.assertTrue(_within_budget(config, state, profile, noticed))
            wake_id = state.claim(profile.name, stale_after_seconds=3600)
            state.fail(
                profile.name,
                wake_id,
                error="boom",
                run_id=None,
                cost_usd=0.0,
                next_due_seconds=0,
            )
        # Failed wakes count; the notice is given once for the run.
        with self.assertLogs("swecraft_coordinator.scheduler", level="WARNING") as logs:
            self.assertFalse(_within_budget(config, state, profile, noticed))
        self.assertIn("used its run budget of 2 wakes", logs.output[0])
        self.assertEqual(noticed[profile.name], "run")
        with self.assertNoLogs("swecraft_coordinator.scheduler", level="WARNING"):
            self.assertFalse(_within_budget(config, state, profile, noticed))
        # No budget: the same history is not a cap.
        self.assertTrue(_within_budget(make_config(self.root), state, profile))

    def test_ledger_allocation_is_the_cap_and_is_read_as_an_agent(self) -> None:
        config = make_config(
            self.root,
            budget=RunBudget(wakes=5, ledger=LedgerFile("hub/society", "budget.yaml")),
        )
        state = StateStore(config.database)
        profile = config.agents[0]
        state.set_ledger_allocation("a" * 40, {profile.name: 1})
        state.claim(profile.name, stale_after_seconds=3600)
        self.assertFalse(_within_budget(config, state, profile, {}))
        # A refresh reads the board with an agent's token; without credentials
        # it says so once and leaves the allocation alone.
        noticed: dict[str, str] = {}
        with self.assertLogs("swecraft_coordinator.scheduler", level="WARNING"):
            _refresh_ledger(config, state, noticed)
        with self.assertNoLogs("swecraft_coordinator.scheduler", level="WARNING"):
            _refresh_ledger(config, state, noticed)
        config.credentials_dir.mkdir(parents=True)
        config.credentials_path(profile.name).write_text(
            "bot-token\n", encoding="utf-8"
        )
        forge = MagicMock()
        forge.get_file.return_value = ("b" * 40, f"{profile.name}: 3\n")
        with patch(
            "swecraft_coordinator.scheduler.ForgeClient", return_value=forge
        ) as client:
            _refresh_ledger(config, state, noticed)
        client.assert_called_once_with(config.forge_url, "bot-token")
        forge.get_file.assert_called_once_with("hub/society", "budget.yaml")
        self.assertTrue(_within_budget(config, state, profile, {}))
        # A zero line stops the agent; it is not an absent cap.
        state.set_ledger_allocation("c" * 40, {profile.name: 0})
        self.assertFalse(_within_budget(config, state, profile, {}))
        # A stale first token does not block the read: the next one is tried.
        state.set_ledger_allocation("d" * 40, {profile.name: 5})
        config.credentials_path("second-bot").write_text("second\n", encoding="utf-8")
        two = make_config(
            self.root,
            agents=(
                profile,
                replace(profile, name="second-bot", home_repo="hub/second"),
            ),
            budget=config.budget,
        )
        stale, fresh = MagicMock(), MagicMock()
        stale.get_file.side_effect = ForgeError("bad token", status_code=401)
        fresh.get_file.return_value = ("e" * 40, f"{profile.name}: 4\nsecond-bot: 5\n")
        with patch(
            "swecraft_coordinator.scheduler.ForgeClient", side_effect=[stale, fresh]
        ):
            _refresh_ledger(two, state, {})
        self.assertEqual(state.ledger_allocation()[1][profile.name], 4)

    def test_boundary_checkpoint_drains_then_runs_at_the_step(self) -> None:
        import time
        from unittest.mock import patch

        marker = self.root / "checkpointed"
        config = make_config(
            self.root,
            checkpoint_every_wakes=1,
            checkpoint_command=("/bin/sh", "-c", f"touch {marker}"),
        )
        state = StateStore(config.database)
        stop = threading.Event()

        def wake(config_, profile, state_):
            wake_id = state_.claim(profile.name, stale_after_seconds=3600)
            state_.finish(
                profile.name,
                wake_id,
                run_id="run-1",
                outcome="worked",
                summary=None,
                cost_usd=0.0,
                next_due_seconds=600,
            )
            return WakeReport(
                agent=profile.name,
                run_id="run-1",
                outcome="worked",
                summary=None,
                cost_usd=0.0,
                memory_commit=None,
            )

        with patch("swecraft_coordinator.scheduler.POLL_SECONDS", 0.05):
            server = threading.Thread(
                target=serve_forever, args=(config, state, stop), kwargs={"wake": wake}
            )
            server.start()
            try:
                deadline = time.monotonic() + 10
                while not marker.is_file() and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                stop.set()
                server.join(timeout=10)
        self.assertTrue(marker.is_file())
        self.assertEqual(state.checkpoint_marker(), 1)


if __name__ == "__main__":
    unittest.main()
