"""Checkpoint/load orchestration against real files and a fake Forge port."""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import yaml
from swecraft_common.lifecycle import LifecycleBusy, society_lifecycle_lock
from swecraft_coordinator.state import StateStore
from swecraft_world import PinSpec, WorldLock, WorldLockRepository, load_manifest
from swecraft_world.models import SEED_PROTOCOL

from swecraft_society.errors import SocietyError
from swecraft_society.inventory import verify_checkpoint
from swecraft_society.orchestrator import (
    CheckpointOptions,
    LoadOptions,
    _wait_until_actions_idle,
    checkpoint,
    load,
)

SWECRAFT_COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "b" * 64


def git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "checkpoint-test",
            "GIT_AUTHOR_EMAIL": "checkpoint@example.test",
            "GIT_COMMITTER_NAME": "checkpoint-test",
            "GIT_COMMITTER_EMAIL": "checkpoint@example.test",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return completed.stdout.strip()


class FakeForge:
    def __init__(self, volume_dir: Path, state_dir: Path) -> None:
        self.volume_dir = volume_dir
        self.state_dir = state_dir
        self.project = "swecraft-forge"
        self.image = "forgejo@sha256:fixture"
        self.running = ("forgejo", "dind", "runner")
        self.calls: list[tuple] = []
        self.fail_export = False
        self.on_status = None

    def running_services(self) -> tuple[str, ...]:
        self.calls.append(("status", self.project))
        if self.on_status is not None:
            self.on_status()
        return self.running

    def stop(self, services) -> None:
        self.calls.append(("stop", *services))

    def start(self, services) -> None:
        from datetime import UTC
        from datetime import datetime as _datetime

        self.started_at = _datetime.now(UTC)
        self.calls.append(("start", *services))

    def inspect_image(self, reference: str) -> tuple[str, str]:
        self.calls.append(("inspect", reference))
        return IMAGE_ID, "linux/arm64"

    def capture(self, destination: Path) -> None:
        self.calls.append(("capture", self.project, self.image))
        if self.fail_export:
            raise SocietyError("export failed")
        for path in sorted(self.state_dir.rglob("*")):
            if not path.is_file():
                continue
            target = destination / "host" / path.relative_to(self.state_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(destination / "forgejo-data.tar", "w") as archive:
            archive.add(self.volume_dir, arcname=".")


class SocietyFixture:
    def __init__(self, case: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "experiment"
        self.root.mkdir()
        self.source = Path(temporary.name) / "source"
        self.source.mkdir()
        git("init", "-b", "main", cwd=self.source)
        (self.source / "demo.py").write_text("seed = True\n")
        git("add", "demo.py", cwd=self.source)
        git("commit", "-m", "seed", cwd=self.source)
        self.baseline = git("rev-parse", "HEAD", cwd=self.source)
        self.tree = git("rev-parse", "HEAD^{tree}", cwd=self.source)

        self.volume = Path(temporary.name) / "volume"
        bare = self.volume / "git/repositories/hub/demo.git"
        bare.parent.mkdir(parents=True)
        git("init", "--bare", "-b", "main", str(bare), cwd=self.source)
        git("push", str(bare), "main", cwd=self.source)

        self.target = Path(temporary.name) / "target"
        self.target.mkdir()
        git("init", "-b", "main", cwd=self.target)
        (self.target / "bench.py").write_text("print('ok')\n")
        git("add", "bench.py", cwd=self.target)
        git("commit", "-m", "target", cwd=self.target)

        self._write_apparatus()
        self._write_runtime()
        self.compose_file = Path(temporary.name) / "forge/compose.yaml"
        (self.compose_file.parent / "state/runner").mkdir(parents=True)
        self.compose_file.write_text(
            "services:\n  forgejo:\n    image: forgejo@sha256:fixture\n"
        )
        (self.compose_file.parent / "state/admin_token").write_text("admin-token\n")
        (self.compose_file.parent / "state/admin_password").write_text("password\n")
        (self.compose_file.parent / "state/forgejo_secret_key").write_text("secret\n")
        (self.compose_file.parent / "state/forgejo_internal_token").write_text(
            "internal\n"
        )
        (self.compose_file.parent / "state/runner/.runner").write_text("registration\n")
        self.forge = FakeForge(self.volume, self.compose_file.parent / "state")

    def _write_apparatus(self) -> None:
        world = {
            "schema_version": 1,
            "world_id": "checkpoint-world",
            "org": "hub",
            "repos": [
                {
                    "name": "demo",
                    "upstream": self.source.as_uri(),
                    "pin": {"type": "commit", "value": self.baseline},
                }
            ],
        }
        (self.root / "world.yaml").write_text(yaml.safe_dump(world, sort_keys=False))
        world_document = load_manifest(self.root / "world.yaml")
        pin = PinSpec(type="commit", value=self.baseline)
        world_lock = WorldLock(
            status="complete",
            world_id="checkpoint-world",
            seeded_at=datetime(2026, 8, 1, tzinfo=UTC),
            forge="http://127.0.0.1:3400",
            org="hub",
            manifest_path="world.yaml",
            manifest_sha256=world_document.sha256,
            hub_commit=SWECRAFT_COMMIT,
            repos=(
                WorldLockRepository(
                    name="demo",
                    upstream=self.source.as_uri(),
                    pin=pin,
                    pin_ref=self.baseline,
                    pin_commit=self.baseline,
                    pin_tree=self.tree,
                    default_branch="main",
                    seed_protocol=SEED_PROTOCOL,
                    baseline_commit=self.baseline,
                    baseline_tree=self.tree,
                    stripped_paths=(),
                    actions_enabled=True,
                ),
            ),
        )
        (self.root / "world.lock.json").write_text(
            world_lock.model_dump_json(exclude_none=True)
        )
        evaluation = {
            "schema_version": 1,
            "suite_id": "checkpoint-suite",
            "world_manifest": "world.yaml",
            "world_lock": "world.lock.json",
            "subjects": [
                {
                    "name": "demo",
                    "world_repo": "demo",
                    "components": [
                        {
                            "name": "demo",
                            "inject": [{"argv": ["true"]}],
                            "modules": ["demo"],
                        }
                    ],
                }
            ],
            "targets": [
                {
                    "name": "consumer",
                    "source_url": self.target.as_uri(),
                    "pin": {"type": "commit", "value": self.baseline},
                    "image": {"reference": "hub-eval:1"},
                    "subjects": ["demo"],
                    "gate": {"argv": ["true"]},
                    "workloads": [
                        {
                            "name": "request",
                            "phases": [
                                {
                                    "name": "steady",
                                    "command": {"argv": ["true"]},
                                    "collectors": [
                                        {"collector": "walltime", "repeats": 2}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        (self.root / "evaluation.yaml").write_text(
            yaml.safe_dump(evaluation, sort_keys=False)
        )
        (self.root / "coordinator.yaml").write_text(
            "forge_url: http://127.0.0.1:3400\n"
            "api_token: file:.swecraft/api_token\n"
            "model: test/model\n"
            "image: hub-agent:1\n"
            "agents:\n  - name: demo-bot\n    repo: hub/demo\n"
        )
        (self.root / "charters").mkdir()
        (self.root / "charters/demo-bot.md").write_text("# Demo bot\n")
        (self.root / "images/agent").mkdir(parents=True)
        (self.root / "images/agent/Dockerfile").write_text("FROM scratch\n")

    def _write_runtime(self) -> None:
        hub = self.root / ".swecraft"
        (hub / "runs/run-1/input").mkdir(parents=True)
        (hub / "runs/run-1/output").mkdir()
        (hub / "runs/run-1/harbor").mkdir()
        # Unit fixtures have no live Forge API; the Docker acceptance exercises
        # the default Actions wait against Forgejo itself.
        (hub / "checkpoint.yaml").write_text(
            "schema_version: 1\ncheckpoint:\n  actions_wait_seconds: 0\n"
        )
        (hub / "api_token").write_text("hub-api-token\n")
        (hub / "runs/run-1/input/request.json").write_text(
            '{"agent": "claude-code", "model": "test/model"}\n'
        )
        (hub / "runs/run-1/input/prompt.md").write_text("Test wake.\n")
        (hub / "runs/run-1/output/result.json").write_text(
            '{"run_id": "run-1", "status": "completed"}\n'
        )
        (hub / "runs/run-1/output/response.md").write_text("Done.\n")
        (hub / "runs/run-1/harbor/result.json").write_text("{}\n")
        (hub / "coordinator/credentials").mkdir(parents=True)
        (hub / "coordinator/credentials/demo-bot.token").write_text("agent-token\n")
        self.coordinator_state = StateStore(hub / "coordinator.db")
        self.coordinator_state.ensure_agent("demo-bot")
        # The eval-1 record directory contains record.json, the completeness
        # proof: only such directories enter a checkpoint's evaluations/.
        (hub / "evaluator/records/eval-1").mkdir(parents=True)
        (hub / "evaluator/records/eval-1/record.json").write_text("{}\n")
        (hub / "evaluator/state.json").write_text("{}\n")

    def options(self, **updates: object) -> CheckpointOptions:
        values: dict[str, object] = {
            "root": self.root,
            "compose_file": self.compose_file,
        }
        values.update(updates)
        return CheckpointOptions(**values)  # type: ignore[arg-type]


class FakeActionsProbe:
    def __init__(self, responses: list[list[dict]]) -> None:
        self.responses = responses
        self.calls = 0

    def active_jobs(self) -> list[dict]:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ActionsWaitTests(unittest.TestCase):
    def test_idle_actions_continue_without_sleeping(self) -> None:
        probe = FakeActionsProbe([[]])
        clock = FakeClock()
        _wait_until_actions_idle(
            probe,
            timeout_seconds=30,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertEqual(probe.calls, 1)
        self.assertEqual(clock.sleeps, [])

    def test_active_actions_are_allowed_to_finish(self) -> None:
        probe = FakeActionsProbe([[{"id": 1}], [{"id": 1}], []])
        clock = FakeClock()
        _wait_until_actions_idle(
            probe,
            timeout_seconds=30,
            poll_seconds=5,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertEqual(probe.calls, 3)
        self.assertEqual(clock.sleeps, [5, 5])

    def test_checkpoint_is_skipped_when_actions_outlive_the_wait(self) -> None:
        probe = FakeActionsProbe([[{"id": 1}, {"id": 2}]])
        clock = FakeClock()
        with self.assertRaisesRegex(
            SocietyError, "checkpoint skipped: 2 Forgejo Actions job"
        ):
            _wait_until_actions_idle(
                probe,
                timeout_seconds=10,
                poll_seconds=5,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(clock.sleeps, [5, 5])

    def test_zero_wait_preserves_advisory_crash_consistent_capture(self) -> None:
        probe = FakeActionsProbe([[{"id": 1}]])
        clock = FakeClock()
        _wait_until_actions_idle(
            probe,
            timeout_seconds=0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        self.assertEqual(probe.calls, 1)
        self.assertEqual(clock.sleeps, [])


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = SocietyFixture(self)

    def test_checkpoint_uses_hub_defaults_and_captures_every_surface(self) -> None:
        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        manifest = verify_checkpoint(output)
        self.assertEqual(
            output.parent, self.fixture.root.resolve() / ".swecraft/checkpoints"
        )
        self.assertEqual(manifest.world_id, "checkpoint-world")
        self.assertEqual(manifest.heads[0].current_head, self.fixture.baseline)
        self.assertEqual(manifest.agent_image.image_id, IMAGE_ID)
        paths = {entry.path for entry in manifest.files}
        for required in (
            "apparatus/evaluation.yaml",
            "apparatus/charters/demo-bot.md",
            "apparatus/images/agent/Dockerfile",
            "state/forge/host/admin_token",
            "state/forge/host/runner/.runner",
            "state/coordinator/coordinator.db",
            "state/coordinator/api_token",
            "state/coordinator/credentials/demo-bot.token",
            "state/evaluator/state.json",
            "runs/run-1/output/result.json",
            "runs/run-1/output/response.md",
            "evaluations/eval-1/record.json",
        ):
            self.assertIn(required, paths)
        self.assertNotIn("apparatus/forge.compose.yaml", paths)
        # The bulky raw Harbor archive is a policy extra, off by default;
        # the normalized run evidence above is always captured.
        self.assertNotIn("runs/run-1/harbor.tar.gz", paths)
        self.assertEqual(manifest.omissions, ())
        # Complete checkpoints stay byte-compatible with pre-partial
        # readers: the omissions key appears only when something is omitted.
        payload = json.loads((output / "manifest.json").read_text())
        self.assertNotIn("omissions", payload)
        self.assertIn(("stop", "forgejo", "dind", "runner"), self.fixture.forge.calls)
        self.assertIn(("start", "forgejo", "dind", "runner"), self.fixture.forge.calls)

    def test_review_batches_are_captured_when_present(self) -> None:
        batch = self.fixture.root / ".swecraft/reviews/run-001"
        (batch / "reviews/peer").mkdir(parents=True)
        (batch / "reviews/peer/alice__bob.json").write_text('{"run_id": "run-001"}\n')
        (batch / "record.json").write_text('{"status": "completed"}\n')

        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        manifest = verify_checkpoint(output)
        paths = {entry.path for entry in manifest.files}
        self.assertIn("reviews/run-001/record.json", paths)
        self.assertIn("reviews/run-001/reviews/peer/alice__bob.json", paths)
        contributors = {record.name for record in manifest.contributors}
        self.assertIn("reviews", contributors)

    def test_checkpoint_can_also_emit_a_standalone_archive(self) -> None:
        archive = self.fixture.root / "checkpoint.tar.gz"
        output = checkpoint(
            self.fixture.options(archive=archive), forge_runtime=self.fixture.forge
        )
        self.assertTrue(output.is_dir())
        self.assertTrue(archive.is_file())
        loaded = load(
            LoadOptions(
                source=archive,
                root=self.fixture.root,
                dest=Path("archive-copy"),
            )
        )
        self.assertEqual(loaded.manifest.world_id, "checkpoint-world")
        self.assertTrue((loaded.dest / "state/forge/forgejo-data.tar").is_file())

    def test_configured_evaluator_paths_are_the_captured_state(self) -> None:
        custom = self.fixture.root / ".swecraft/custom-evaluator"
        custom.mkdir()
        (self.fixture.root / ".swecraft/evaluator/records").replace(custom / "results")
        (self.fixture.root / ".swecraft/evaluator/state.json").replace(
            custom / "state.json"
        )
        (self.fixture.root / ".swecraft/checkpoint.yaml").write_text(
            "schema_version: 1\n"
            "checkpoint:\n"
            "  actions_wait_seconds: 0\n"
            "  evaluator_records_dir: .swecraft/custom-evaluator/results\n"
            "  evaluator_state: .swecraft/custom-evaluator/state.json\n"
        )

        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        paths = {entry.path for entry in verify_checkpoint(output).files}
        self.assertIn("state/evaluator/state.json", paths)
        self.assertIn("evaluations/eval-1/record.json", paths)

    def test_checkpoint_refuses_a_disjoint_evaluator_lifecycle_lock(self) -> None:
        (self.fixture.root / ".swecraft/checkpoint.yaml").write_text(
            "schema_version: 1\n"
            "checkpoint:\n"
            "  actions_wait_seconds: 0\n"
            "  evaluator_state: evaluator-state/state.json\n"
        )
        with self.assertRaisesRegex(SocietyError, "must share one .swecraft root"):
            checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        self.assertEqual(self.fixture.forge.calls, [])

    def test_checkpoint_refuses_active_coordinator_wakes(self) -> None:
        self.fixture.coordinator_state.claim("demo-bot", stale_after_seconds=3_600)
        with self.assertRaisesRegex(SocietyError, "active wakes"):
            checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        self.assertEqual(self.fixture.forge.calls, [])

    def test_actions_timeout_skips_checkpoint_before_forge_is_stopped(self) -> None:
        coordinator = self.fixture.root / "coordinator.yaml"
        coordinator.write_text(
            coordinator.read_text().replace(
                "forge_url:", "admin_token: fixture-admin-token\nforge_url:"
            )
        )
        import swecraft_society.orchestrator as orchestrator_module

        with patch.object(
            orchestrator_module,
            "_wait_until_actions_idle",
            side_effect=SocietyError(
                "checkpoint skipped: 1 Forgejo Actions job remains active"
            ),
        ):
            with self.assertRaisesRegex(SocietyError, "checkpoint skipped"):
                checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        # The wait sits directly before the stop: CI outliving it must leave
        # the Forge untouched, and the abandoned stage must not leak.
        kinds = [call[0] for call in self.fixture.forge.calls]
        self.assertNotIn("stop", kinds)
        self.assertNotIn("capture", kinds)
        self.assertEqual(
            list((self.fixture.root / ".swecraft/checkpoints").iterdir()), []
        )

    def test_running_evaluation_never_gates_a_checkpoint(self) -> None:
        # The evaluator writes neither the Forge nor the coordinator ledger;
        # a record directory still missing record.json must not block the
        # step boundary and never enters the checkpoint.
        inflight_dir = self.fixture.root / ".swecraft/evaluator/records/eval-inflight"
        inflight_dir.mkdir(parents=True)
        (inflight_dir / "partial.json").write_text("{")

        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        manifest = verify_checkpoint(output)
        paths = {entry.path for entry in manifest.files}
        self.assertIn("evaluations/eval-1/record.json", paths)
        self.assertNotIn("evaluations/eval-inflight/partial.json", paths)
        self.assertIn("state/evaluator/state.json", paths)

    def test_forge_stop_covers_only_the_volume_export(self) -> None:
        # The static copies (image identity, apparatus, ledger, runs,
        # evaluations) happen while the Forge serves; the stop wraps
        # exactly the volume tar, and expensive verification runs only
        # after the society resumed.
        forge = self.fixture.forge
        import swecraft_society.orchestrator as orchestrator_module

        real_verify = orchestrator_module.verify_checkpoint

        def recording_verify(root):
            forge.calls.append(("verify",))
            return real_verify(root)

        with patch.object(orchestrator_module, "verify_checkpoint", recording_verify):
            output = checkpoint(self.fixture.options(), forge_runtime=forge)
        kinds = [call[0] for call in forge.calls]
        self.assertLess(kinds.index("inspect"), kinds.index("stop"))
        self.assertLess(kinds.index("stop"), kinds.index("capture"))
        self.assertLess(kinds.index("capture"), kinds.index("start"))
        self.assertLess(kinds.index("start"), kinds.index("verify"))
        # created_at means the frozen cut — the moment the Forge stopped —
        # not whenever finalization wrote the manifest.
        manifest = verify_checkpoint(output)
        self.assertLessEqual(manifest.created_at, forge.started_at)

    def test_checkpoint_refuses_a_same_named_repository_from_another_world(
        self,
    ) -> None:
        wrong_source = self.fixture.root.parent / "wrong-source"
        wrong_source.mkdir()
        git("init", "-b", "main", cwd=wrong_source)
        (wrong_source / "demo.py").write_text("wrong_world = True\n")
        git("add", "demo.py", cwd=wrong_source)
        git("commit", "-m", "unrelated seed", cwd=wrong_source)
        wrong_volume = self.fixture.root.parent / "wrong-volume"
        bare = wrong_volume / "git/repositories/hub/demo.git"
        bare.parent.mkdir(parents=True)
        git("init", "--bare", "-b", "main", str(bare), cwd=wrong_source)
        git("push", str(bare), "main", cwd=wrong_source)
        self.fixture.forge.volume_dir = wrong_volume

        with self.assertRaisesRegex(SocietyError, "locked baseline"):
            checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)

        self.assertIn(("start", "forgejo", "dind", "runner"), self.fixture.forge.calls)
        self.assertEqual(
            list((self.fixture.root / ".swecraft/checkpoints").iterdir()), []
        )

    def test_checkpoint_holds_the_claim_lock_for_capture(self) -> None:
        observed = []

        def try_claim_lock() -> None:
            try:
                with society_lifecycle_lock(
                    self.fixture.root / ".swecraft/coordinator.db"
                ):
                    pass
            except LifecycleBusy:
                observed.append("blocked")

        self.fixture.forge.on_status = try_claim_lock
        checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        self.assertEqual(observed, ["blocked"])

    def test_transient_lock_contention_does_not_consume_the_checkpoint(self) -> None:
        # An evaluator claim or retention sweep holds the lifecycle lock for
        # moments; the checkpoint must wait it out, not fail — a boundary
        # checkpoint's cadence marker is already spent by then.
        import threading

        database = self.fixture.root / ".swecraft/coordinator.db"
        release = threading.Event()
        held = threading.Event()

        def transient_holder() -> None:
            with society_lifecycle_lock(database, timeout_seconds=5):
                held.set()
                release.wait(timeout=10)

        holder = threading.Thread(target=transient_holder)
        holder.start()
        self.assertTrue(held.wait(timeout=5))
        timer = threading.Timer(0.5, release.set)
        timer.start()
        try:
            output = checkpoint(
                self.fixture.options(), forge_runtime=self.fixture.forge
            )
        finally:
            release.set()
            timer.cancel()
            holder.join(timeout=10)
        self.assertTrue((output / "manifest.json").is_file())

    def test_exhausted_lock_wait_still_refuses(self) -> None:
        import threading

        database = self.fixture.root / ".swecraft/coordinator.db"
        release = threading.Event()
        held = threading.Event()

        def stubborn_holder() -> None:
            with society_lifecycle_lock(database, timeout_seconds=5):
                held.set()
                release.wait(timeout=30)

        holder = threading.Thread(target=stubborn_holder)
        holder.start()
        self.assertTrue(held.wait(timeout=5))
        try:
            with patch("swecraft_society.orchestrator.LIFECYCLE_WAIT_SECONDS", 0.2):
                with self.assertRaisesRegex(SocietyError, "lifecycle"):
                    checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        finally:
            release.set()
            holder.join(timeout=10)

    def test_failure_restores_exact_original_services_and_no_output(self) -> None:
        self.fixture.forge.running = ["forgejo"]
        self.fixture.forge.fail_export = True
        with self.assertRaisesRegex(SocietyError, "export failed"):
            checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        self.assertIn(("stop", "forgejo"), self.fixture.forge.calls)
        self.assertIn(("start", "forgejo"), self.fixture.forge.calls)
        checkpoints = self.fixture.root / ".swecraft/checkpoints"
        self.assertEqual(list(checkpoints.iterdir()), [])

    def test_partial_coordinator_run_is_reported_and_excluded(self) -> None:
        partial = self.fixture.root / ".swecraft/runs/run-partial"
        partial.mkdir(parents=True)

        with self.assertLogs("swecraft_society.orchestrator", level="WARNING") as logs:
            output = checkpoint(
                self.fixture.options(), forge_runtime=self.fixture.forge
            )

        paths = {entry.path for entry in verify_checkpoint(output).files}
        self.assertIn("runs/run-1/output/result.json", paths)
        self.assertFalse(any(path.startswith("runs/run-partial/") for path in paths))
        self.assertTrue(any("run-partial" in message for message in logs.output))

    def test_harbor_include_packs_the_raw_archive(self) -> None:
        output = checkpoint(
            self.fixture.options(include_harbor=True), forge_runtime=self.fixture.forge
        )
        paths = {entry.path for entry in verify_checkpoint(output).files}
        self.assertIn("runs/run-1/harbor.tar.gz", paths)
        self.assertIn("runs/run-1/output/result.json", paths)

    def test_missing_evaluation_apparatus_degrades_to_a_partial_checkpoint(
        self,
    ) -> None:
        # A re-seeded world with no evaluation manifest
        # must still yield a recovery point. Capture proceeds, records the
        # omission, and the result verifies as a partial checkpoint.
        (self.fixture.root / "evaluation.yaml").unlink()
        with self.assertLogs("swecraft_society.orchestrator", level="WARNING") as logs:
            output = checkpoint(
                self.fixture.options(), forge_runtime=self.fixture.forge
            )
        self.assertTrue(
            any("without evaluation apparatus" in message for message in logs.output)
        )
        manifest = verify_checkpoint(output)
        self.assertEqual(manifest.omissions, ("apparatus/evaluation.yaml",))
        payload = json.loads((output / "manifest.json").read_text())
        self.assertIn("omissions", payload)
        paths = {entry.path for entry in manifest.files}
        self.assertNotIn("apparatus/evaluation.yaml", paths)
        self.assertIn("apparatus/world.lock.json", paths)
        self.assertIn("runs/run-1/output/result.json", paths)
        self.assertIn("state/forge/forgejo-data.tar", paths)

    def test_unparseable_evaluation_manifest_is_captured_as_bytes(self) -> None:
        # The society treats the evaluation manifest as an opaque hashed
        # artifact: content it cannot parse is still capturable by bytes.
        (self.fixture.root / "evaluation.yaml").write_text("not: [valid: yaml")
        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        manifest = verify_checkpoint(output)
        self.assertEqual(manifest.omissions, ())
        paths = {entry.path for entry in manifest.files}
        self.assertIn("apparatus/evaluation.yaml", paths)

    def test_missing_world_lock_still_refuses(self) -> None:
        # Recovery essentials never degrade: without the world lock the
        # checkpoint could not be verified or resumed.
        (self.fixture.root / "world.lock.json").unlink()
        with self.assertRaisesRegex(SocietyError, "world"):
            checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        self.assertEqual(self.fixture.forge.calls, [])

    def test_load_is_a_pure_verified_filesystem_materialization(self) -> None:
        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        before = list(self.fixture.forge.calls)

        with patch(
            "swecraft_society.orchestrator.verify_checkpoint", wraps=verify_checkpoint
        ) as verify:
            result = load(LoadOptions(source=output, root=self.fixture.root))

        verify.assert_called_once()
        self.assertEqual(
            result.dest.parent, (self.fixture.root / ".swecraft/loaded").resolve()
        )
        self.assertEqual(self.fixture.forge.calls, before)
        self.assertEqual(
            verify_checkpoint(result.dest).checkpoint_id,
            verify_checkpoint(output).checkpoint_id,
        )

    def test_load_refuses_destination_inside_source_without_mutating_it(self) -> None:
        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        with self.assertRaisesRegex(SocietyError, "outside the source checkpoint"):
            load(
                LoadOptions(
                    source=output,
                    root=self.fixture.root,
                    dest=output / "loaded",
                )
            )
        self.assertFalse((output / "loaded").exists())
        verify_checkpoint(output)

    def test_load_refuses_a_nonempty_destination_without_mutating_it(self) -> None:
        output = checkpoint(self.fixture.options(), forge_runtime=self.fixture.forge)
        dest = self.fixture.root / "occupied"
        dest.mkdir()
        marker = dest / "keep.txt"
        marker.write_text("keep\n")

        with self.assertRaisesRegex(SocietyError, "not empty"):
            load(LoadOptions(source=output, root=self.fixture.root, dest=dest))

        self.assertEqual(marker.read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main()
