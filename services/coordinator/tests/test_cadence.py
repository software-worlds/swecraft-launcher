"""Boundary-checkpoint cadence: due logic, invocation, marker, pruning."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import swecraft_coordinator.cadence as cadence_module
from swecraft_coordinator.cadence import (
    checkpoint_due,
    checkpoint_request_path,
    evaluation_due,
    run_boundary_checkpoint,
    run_boundary_evaluation,
    warn_if_publish_misconfigured,
)
from swecraft_coordinator.config import AgentProfile, CoordinatorConfig
from swecraft_coordinator.state import StateStore


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
        runs_dir=root / "runs",
        database=root / "state.db",
        credentials_dir=root / "credentials",
    )
    values.update(overrides)
    return CoordinatorConfig(**values)


class CadenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = StateStore(self.root / "state.db")

    def finish_wake(self, run_id: str) -> None:
        wake_id = self.state.claim("demo-bot", stale_after_seconds=3600)
        self.state.finish(
            "demo-bot",
            wake_id,
            run_id=run_id,
            outcome="worked",
            summary=None,
            cost_usd=0.0,
            next_due_seconds=600,
        )

    def test_due_only_after_the_configured_step_count(self) -> None:
        config = make_config(self.root, checkpoint_every_wakes=2)
        self.assertFalse(checkpoint_due(config, self.state))
        self.finish_wake("run-1")
        self.assertFalse(checkpoint_due(config, self.state))
        self.finish_wake("run-2")
        self.assertTrue(checkpoint_due(config, self.state))

    def test_zero_cadence_disables_the_checkpoint(self) -> None:
        config = make_config(self.root, checkpoint_every_wakes=0)
        self.finish_wake("run-1")
        self.assertFalse(checkpoint_due(config, self.state))

    def test_success_prunes_where_society_actually_writes(self) -> None:
        marker = self.root / "invoked"
        # Retention must follow Society's own output_dir, not a second
        # coordinator-owned setting that could silently diverge.
        checkpoints = self.root / "custom-checkpoints"
        (self.root / ".swecraft").mkdir(parents=True, exist_ok=True)
        (self.root / ".swecraft/checkpoint.yaml").write_text(
            "checkpoint:\n  output_dir: custom-checkpoints\n"
        )
        for name in ("demo-20260801T000000Z", "demo-20260802T000000Z"):
            (checkpoints / name).mkdir(parents=True)
            (checkpoints / name / "manifest.json").write_text("{}\n")
        (checkpoints / "not-a-checkpoint").mkdir()
        (checkpoints / ".staging").mkdir()
        config = make_config(
            self.root,
            checkpoint_every_wakes=1,
            checkpoint_keep=1,
            checkpoint_command=("/bin/sh", "-c", f"touch {marker}"),
        )
        self.finish_wake("run-1")
        self.assertTrue(run_boundary_checkpoint(config, self.state, root=self.root))
        self.assertTrue(marker.is_file())
        self.assertEqual(self.state.checkpoint_marker(), 1)
        self.assertFalse(checkpoint_due(config, self.state))
        self.assertFalse((checkpoints / "demo-20260801T000000Z").exists())
        self.assertTrue((checkpoints / "demo-20260802T000000Z").exists())
        self.assertTrue((checkpoints / "not-a-checkpoint").exists())
        self.assertTrue((checkpoints / ".staging").exists())

    def test_marker_is_recorded_before_the_command_runs(self) -> None:
        # The checkpoint copies the coordinator database, so the captured
        # state must already carry its own boundary marker.
        observed = self.root / "observed"
        database = self.root / "state.db"
        config = make_config(
            self.root,
            checkpoint_every_wakes=1,
            checkpoint_command=(
                "/bin/sh",
                "-c",
                f"sqlite3 {database} "
                "'SELECT wake_count FROM checkpoint_marker' "
                f"> {observed}",
            ),
        )
        self.finish_wake("run-1")
        self.assertTrue(run_boundary_checkpoint(config, self.state, root=self.root))
        self.assertEqual(observed.read_text().strip(), "1")

    def test_failure_skips_the_boundary_instead_of_hot_looping(self) -> None:
        config = make_config(
            self.root,
            checkpoint_every_wakes=1,
            checkpoint_command=("/bin/sh", "-c", "echo broken >&2; exit 3"),
        )
        self.finish_wake("run-1")
        self.assertFalse(run_boundary_checkpoint(config, self.state))
        # The marker still advanced: the next attempt comes a full cadence
        # later rather than every poll.
        self.assertEqual(self.state.checkpoint_marker(), 1)
        self.assertFalse(checkpoint_due(config, self.state))
        self.finish_wake("run-2")
        self.assertTrue(checkpoint_due(config, self.state))

    def test_missing_command_is_a_skipped_boundary(self) -> None:
        config = make_config(
            self.root,
            checkpoint_every_wakes=1,
            checkpoint_command=(str(self.root / "does-not-exist"),),
        )
        self.finish_wake("run-1")
        self.assertFalse(run_boundary_checkpoint(config, self.state))
        self.assertEqual(self.state.checkpoint_marker(), 1)

    def test_operator_request_makes_a_boundary_due_off_cadence(self) -> None:
        # `checkpoint-now` works even with the cadence disabled, and the
        # request is consumed by the attempt — success or failure — so a
        # broken pipeline cannot hot-loop on one request.
        config = make_config(
            self.root,
            checkpoint_every_wakes=0,
            checkpoint_command=("/bin/sh", "-c", "exit 0"),
        )
        self.finish_wake("run-1")
        self.assertFalse(checkpoint_due(config, self.state))
        checkpoint_request_path(config).write_text("requested\n")
        self.assertTrue(checkpoint_due(config, self.state))
        self.assertTrue(run_boundary_checkpoint(config, self.state, root=self.root))
        self.assertFalse(checkpoint_request_path(config).exists())
        self.assertFalse(checkpoint_due(config, self.state))

    def test_failed_request_does_not_steal_the_scheduled_boundary(self) -> None:
        # A failed off-cadence request restores the previous marker: it
        # must not push the next *scheduled* boundary out by a cadence it
        # never consumed. (A failed scheduled boundary still advances the
        # marker — that skip-loudly behavior is covered above.)
        config = make_config(
            self.root,
            checkpoint_every_wakes=2,
            checkpoint_command=("/bin/sh", "-c", "exit 3"),
        )
        self.finish_wake("run-1")
        checkpoint_request_path(config).write_text("requested\n")
        self.assertFalse(run_boundary_checkpoint(config, self.state, root=self.root))
        self.assertEqual(self.state.checkpoint_marker(), 0)
        self.assertFalse(checkpoint_request_path(config).exists())
        self.finish_wake("run-2")
        self.assertTrue(checkpoint_due(config, self.state))

    def test_active_publish_skips_the_next_and_shields_pruning(self) -> None:
        # While a detached publish is still reading an old checkpoint, a
        # new boundary neither starts a second upload nor prunes the
        # directory out from under the running one.
        class StillRunning:
            def poll(self) -> None:
                return None

        checkpoints = self.root / ".swecraft/checkpoints"
        old = checkpoints / "demo-20260801T000000Z"
        new = checkpoints / "demo-20260802T000000Z"
        for path in (old, new):
            path.mkdir(parents=True)
            (path / "manifest.json").write_text("{}\n")
        (self.root / ".swecraft/checkpoint.yaml").write_text(
            "checkpoint:\n  output_dir: .swecraft/checkpoints\n"
        )
        cadence_module._publish_active = StillRunning()
        cadence_module._publish_target = old
        self.addCleanup(setattr, cadence_module, "_publish_active", None)
        self.addCleanup(setattr, cadence_module, "_publish_target", None)
        published = self.root / "published"
        config = make_config(
            self.root,
            checkpoint_every_wakes=1,
            checkpoint_keep=1,
            checkpoint_command=("/bin/sh", "-c", f"echo 'checkpoint: {new}'"),
            checkpoint_publish=True,
            publish_command=("/bin/sh", "-c", f'printf %s "$0" > {published}'),
        )
        self.finish_wake("run-1")
        with self.assertLogs("swecraft_coordinator.cadence", level="WARNING") as logs:
            self.assertTrue(run_boundary_checkpoint(config, self.state, root=self.root))
        self.assertTrue(any("still running" in message for message in logs.output))
        self.assertFalse(published.exists())
        self.assertTrue(old.is_dir())
        self.assertTrue(new.is_dir())

    def test_unconfigured_publish_lane_warns_at_startup(self) -> None:
        # A dead publish lane must be visible when serve starts, not hours
        # later in the first detached publisher's failure log.
        (self.root / ".swecraft").mkdir()
        (self.root / ".swecraft/checkpoint.yaml").write_text("schema_version: 1\n")
        config = make_config(self.root, checkpoint_publish=True)
        with self.assertLogs("swecraft_coordinator.cadence", level="WARNING") as logs:
            warn_if_publish_misconfigured(config, root=self.root)
        self.assertTrue(any("huggingface.repo_id" in line for line in logs.output))

        (self.root / ".swecraft/checkpoint.yaml").write_text(
            "huggingface:\n  repo_id: owner/dataset\n"
        )
        with self.assertNoLogs("swecraft_coordinator.cadence", level="WARNING"):
            warn_if_publish_misconfigured(config, root=self.root)
        # A custom publish_command is the operator's own contract.
        custom = make_config(
            self.root, checkpoint_publish=True, publish_command=("my-publisher",)
        )
        (self.root / ".swecraft/checkpoint.yaml").unlink()
        with self.assertNoLogs("swecraft_coordinator.cadence", level="WARNING"):
            warn_if_publish_misconfigured(custom, root=self.root)

    def test_publish_runs_detached_with_the_reported_checkpoint_path(self) -> None:
        # Publication is out-of-band: the boundary returns as soon as the
        # capture command does, and the upload command receives the path
        # the checkpoint reported on stdout as its final argument.
        cadence_module._publish_active = None
        self.addCleanup(setattr, cadence_module, "_publish_active", None)
        published = self.root / "published"
        config = make_config(
            self.root,
            checkpoint_every_wakes=1,
            checkpoint_command=(
                "/bin/sh",
                "-c",
                "echo 'checkpoint: /tmp/ckpt/demo-20260801T000000Z'",
            ),
            checkpoint_publish=True,
            publish_command=("/bin/sh", "-c", f'printf %s "$0" > {published}'),
        )
        self.finish_wake("run-1")
        self.assertTrue(run_boundary_checkpoint(config, self.state, root=self.root))
        deadline = time.monotonic() + 10
        while not published.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(published.read_text(), "/tmp/ckpt/demo-20260801T000000Z")


if __name__ == "__main__":
    unittest.main()


class EvaluationCadenceTests(CadenceTests):
    def test_due_only_after_completed_wakes(self) -> None:
        config = make_config(
            self.root, evaluate_every_wakes=2, evaluate_command=("/usr/bin/true",)
        )
        self.assertFalse(evaluation_due(config, self.state))
        self.finish_wake("run-1")
        self.assertFalse(evaluation_due(config, self.state))
        self.finish_wake("run-2")
        self.assertTrue(evaluation_due(config, self.state))

    def test_zero_cadence_disables_the_evaluation(self) -> None:
        config = make_config(self.root, evaluate_command=("/usr/bin/true",))
        self.finish_wake("run-1")
        self.assertFalse(evaluation_due(config, self.state))

    def test_success_records_the_marker(self) -> None:
        config = make_config(
            self.root, evaluate_every_wakes=1, evaluate_command=("/usr/bin/true",)
        )
        self.finish_wake("run-1")
        self.assertTrue(run_boundary_evaluation(config, self.state))
        self.assertEqual(self.state.evaluation_marker(), 1)
        self.assertFalse(evaluation_due(config, self.state))

    def test_failure_skips_the_boundary_instead_of_hot_looping(self) -> None:
        config = make_config(
            self.root,
            evaluate_every_wakes=1,
            evaluate_command=("/bin/sh", "-c", "echo broken >&2; exit 3"),
        )
        self.finish_wake("run-1")
        self.assertFalse(run_boundary_evaluation(config, self.state))
        self.assertEqual(self.state.evaluation_marker(), 1)
        self.assertFalse(evaluation_due(config, self.state))
        self.finish_wake("run-2")
        self.assertTrue(evaluation_due(config, self.state))
