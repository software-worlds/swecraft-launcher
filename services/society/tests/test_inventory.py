"""Byte and semantic verification of a complete checkpoint fixture."""

from __future__ import annotations

import hashlib
import subprocess
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import yaml
from swecraft_world.models import PinSpec, WorldLock, WorldLockRepository

from swecraft_society.errors import SocietyError
from swecraft_society.inventory import (
    collect_entries,
    hash_file,
    inventory_digest,
    verify_checkpoint,
    write_manifest,
)
from swecraft_society.layout import CARD_NAME, MANIFEST_NAME
from swecraft_society.models import (
    ApparatusInfo,
    CheckpointManifest,
    ContributorRecord,
    ForgeInfo,
    ImageIdentity,
    RepoHead,
)

SHA_A = "a" * 40
IMAGE_ID = "sha256:" + "d" * 64


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


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.root = self.scratch / "checkpoint"
        self.root.mkdir()
        self.source = self.scratch / "source"
        self.source.mkdir()
        git("init", "-b", "main", cwd=self.source)
        (self.source / "demo.py").write_text("answer = 42\n")
        git("add", "demo.py", cwd=self.source)
        git("commit", "-m", "seed", cwd=self.source)
        self.head = git("rev-parse", "HEAD", cwd=self.source)
        self.tree = git("rev-parse", "HEAD^{tree}", cwd=self.source)

        self._write_apparatus()
        self._write_forge_volume()
        (self.root / "runs/r1").mkdir(parents=True)
        (self.root / "runs/r1/receipt.json").write_text("{}\n")
        self.manifest = self._build_manifest()
        write_manifest(self.root, self.manifest)

    def _write_apparatus(self) -> None:
        apparatus = self.root / "apparatus"
        apparatus.mkdir()
        world = {
            "schema_version": 1,
            "world_id": "demo",
            "org": "hub",
            "repos": [
                {
                    "name": "demo",
                    "upstream": self.source.as_uri(),
                    "pin": {"type": "commit", "value": self.head},
                    "default_branch": "main",
                    "seed_protocol": "relocate-github-actions-v1",
                }
            ],
        }
        world_raw = yaml.safe_dump(world, sort_keys=False)
        (apparatus / "world.yaml").write_text(world_raw)
        pin = PinSpec(type="commit", value=self.head)
        lock = WorldLock(
            status="complete",
            world_id="demo",
            seeded_at=datetime(2026, 7, 1, tzinfo=UTC),
            forge="http://127.0.0.1:3400",
            org="hub",
            manifest_path="world.yaml",
            manifest_sha256=hashlib.sha256(world_raw.encode()).hexdigest(),
            hub_commit=SHA_A,
            repos=(
                WorldLockRepository(
                    name="demo",
                    upstream=self.source.as_uri(),
                    pin=pin,
                    pin_ref=self.head,
                    pin_commit=self.head,
                    pin_tree=self.tree,
                    default_branch="main",
                    seed_protocol="relocate-github-actions-v1",
                    baseline_commit=self.head,
                    baseline_tree=self.tree,
                    stripped_paths=(),
                    actions_enabled=True,
                ),
            ),
        )
        (apparatus / "world.lock.json").write_text(
            lock.model_dump_json(exclude_none=True)
        )
        evaluation = {
            "schema_version": 1,
            "suite_id": "demo-suite",
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
                    "source_url": "https://example.test/consumer.git",
                    "pin": {"type": "commit", "value": self.head},
                    "image": {"reference": "swecraft-eval-consumer:1"},
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
        (apparatus / "evaluation.yaml").write_text(
            yaml.safe_dump(evaluation, sort_keys=False)
        )
        (apparatus / "coordinator.yaml").write_text(
            "model: test/model\nagents:\n  - name: demo-bot\n    repo: hub/demo\n"
        )
        (apparatus / "charters").mkdir()
        (apparatus / "charters/demo-bot.md").write_text("# Demo\n")
        (apparatus / "images/agent").mkdir(parents=True)
        (apparatus / "images/agent/Dockerfile").write_text("FROM scratch\n")

    def _write_forge_volume(self) -> None:
        volume = self.scratch / "volume"
        bare = volume / "git/repositories/hub/demo.git"
        bare.parent.mkdir(parents=True)
        git("init", "--bare", "-b", "main", str(bare), cwd=self.source)
        git("push", str(bare), "main", cwd=self.source)
        output = self.root / "state/forge/forgejo-data.tar"
        output.parent.mkdir(parents=True)
        with tarfile.open(output, "w") as archive:
            archive.add(volume, arcname=".")

    def _build_manifest(self, **updates: object) -> CheckpointManifest:
        apparatus = collect_entries(self.root, "apparatus")
        payload: dict[str, object] = {
            "checkpoint_id": "demo-20260730T000000Z",
            "created_at": datetime(2026, 7, 30, tzinfo=UTC),
            "hub_commit": SHA_A,
            "apparatus": ApparatusInfo(
                sha256=inventory_digest(apparatus),
                git_ref="exp/demo",
                git_commit=SHA_A,
                dirty=True,
            ),
            "world_id": "demo",
            "world_lock_sha256": hash_file(self.root / "apparatus/world.lock.json")[0],
            "agent_image": ImageIdentity(
                reference="hub-agent:1", image_id=IMAGE_ID, platform="linux/arm64"
            ),
            "forge": ForgeInfo(
                source_url="http://127.0.0.1:3400",
                compose_project="swecraft-forge",
                image="forgejo@sha256:fixture",
                volume="forgejo-data",
            ),
            "heads": (
                RepoHead(
                    name="demo",
                    baseline_commit=self.head,
                    current_head=self.head,
                ),
            ),
            "contributors": (
                ContributorRecord(name="apparatus", files=apparatus),
                ContributorRecord(
                    name="forge", files=collect_entries(self.root, "state/forge")
                ),
                ContributorRecord(
                    name="runs", files=collect_entries(self.root, "runs")
                ),
            ),
        }
        payload.update(updates)
        return CheckpointManifest.model_validate(payload)

    def test_write_then_verify_round_trips(self) -> None:
        self.assertEqual(verify_checkpoint(self.root), self.manifest)
        self.assertIn(
            "private checkpoint contains society-local Forge credentials",
            (self.root / CARD_NAME).read_text(),
        )

    def test_tampered_missing_and_unlisted_files_are_detected(self) -> None:
        (self.root / "runs/r1/receipt.json").write_text("tampered\n")
        with self.assertRaises(SocietyError):
            verify_checkpoint(self.root)
        self.setUp()
        (self.root / "runs/r1/receipt.json").unlink()
        with self.assertRaises(SocietyError):
            verify_checkpoint(self.root)
        self.setUp()
        (self.root / "runs/r1/extra.bin").write_text("stowaway")
        with self.assertRaises(SocietyError):
            verify_checkpoint(self.root)

    def test_semantic_world_and_head_relabelling_is_detected(self) -> None:
        write_manifest(self.root, self._build_manifest(world_id="other"))
        with self.assertRaisesRegex(SocietyError, "manifest world"):
            verify_checkpoint(self.root)
        wrong = RepoHead(name="demo", baseline_commit=SHA_A, current_head=self.head)
        write_manifest(self.root, self._build_manifest(heads=(wrong,)))
        with self.assertRaisesRegex(SocietyError, "baseline"):
            verify_checkpoint(self.root)
        wrong = RepoHead(name="demo", baseline_commit=self.head, current_head=SHA_A)
        write_manifest(self.root, self._build_manifest(heads=(wrong,)))
        with self.assertRaisesRegex(SocietyError, "frozen Forge volume"):
            verify_checkpoint(self.root)

    def test_edited_evaluation_manifest_fails_the_inventory(self) -> None:
        # The evaluation files are opaque to the society; tampering is caught
        # by the byte inventory, not by parsing.
        (self.root / "apparatus/evaluation.yaml").write_text("invalid: true\n")
        with self.assertRaisesRegex(SocietyError, "does not match its manifest"):
            verify_checkpoint(self.root)

    def test_generated_card_is_required_and_checked(self) -> None:
        (self.root / CARD_NAME).write_text("misleading card\n")
        with self.assertRaisesRegex(SocietyError, "README.md"):
            verify_checkpoint(self.root)
        (self.root / CARD_NAME).unlink()
        with self.assertRaisesRegex(SocietyError, "README.md"):
            verify_checkpoint(self.root)

    def test_symlinks_are_refused_when_collecting_and_verifying(self) -> None:
        escape = self.root / "runs/r1/escape"
        escape.symlink_to(self.root / "apparatus/world.yaml")
        with self.assertRaises(SocietyError):
            collect_entries(self.root, "runs")
        with self.assertRaises(SocietyError):
            verify_checkpoint(self.root)

    def test_manifest_json_is_stable(self) -> None:
        first = (self.root / MANIFEST_NAME).read_bytes()
        write_manifest(self.root, self.manifest)
        self.assertEqual(first, (self.root / MANIFEST_NAME).read_bytes())


if __name__ == "__main__":
    unittest.main()
