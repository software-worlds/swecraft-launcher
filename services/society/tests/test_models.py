"""Checkpoint manifest invariants and path safety."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from pydantic import ValidationError

from swecraft_society.models import (
    ApparatusInfo,
    CheckpointManifest,
    ContributorRecord,
    FileEntry,
    ForgeInfo,
    ImageIdentity,
    RepoHead,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "c" * 64
IMAGE_ID = "sha256:" + "d" * 64


def entry(path: str) -> FileEntry:
    return FileEntry(path=path, sha256=DIGEST, size=1)


def manifest(**overrides: object) -> CheckpointManifest:
    payload: dict[str, object] = {
        "checkpoint_id": "demo-20260730T000000Z",
        "created_at": datetime(2026, 7, 30, tzinfo=UTC),
        "hub_commit": SHA_A,
        "apparatus": ApparatusInfo(
            sha256=DIGEST,
            git_ref="exp/demo",
            git_commit=SHA_A,
            dirty=True,
        ),
        "world_id": "demo",
        "world_lock_sha256": DIGEST,
        "agent_image": ImageIdentity(
            reference="hub-agent:1", image_id=IMAGE_ID, platform="linux/arm64"
        ),
        "forge": ForgeInfo(
            source_url="http://127.0.0.1:3400",
            compose_project="swecraft-forge",
            image="codeberg.org/forgejo/forgejo@sha256:abc",
            volume="forgejo-data",
        ),
        "heads": (RepoHead(name="demo", baseline_commit=SHA_A, current_head=SHA_B),),
        "contributors": (
            ContributorRecord(
                name="forge", files=(entry("state/forge/forgejo-data.tar"),)
            ),
        ),
    }
    payload.update(overrides)
    return CheckpointManifest.model_validate(payload)


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_round_trips_through_json(self) -> None:
        document = manifest()
        payload = document.model_dump(mode="json", exclude_none=True)
        self.assertEqual(CheckpointManifest.model_validate(payload), document)
        self.assertTrue(document.apparatus.dirty)

    def test_created_at_requires_timezone(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(created_at=datetime(2026, 7, 30))

    def test_duplicate_head_names_are_refused(self) -> None:
        head = RepoHead(name="demo", baseline_commit=SHA_A, current_head=SHA_B)
        with self.assertRaises(ValidationError):
            manifest(heads=(head, head))

    def test_duplicate_contributor_names_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(
                contributors=(
                    ContributorRecord(name="forge", files=(entry("state/a"),)),
                    ContributorRecord(name="forge", files=(entry("state/b"),)),
                )
            )

    def test_one_path_cannot_be_claimed_twice(self) -> None:
        with self.assertRaises(ValidationError):
            manifest(
                contributors=(
                    ContributorRecord(name="forge", files=(entry("shared"),)),
                    ContributorRecord(name="runs", files=(entry("shared"),)),
                )
            )

    def test_reserved_root_files_cannot_be_claimed(self) -> None:
        for reserved in ("manifest.json", "README.md"):
            with self.assertRaises(ValidationError):
                manifest(
                    contributors=(
                        ContributorRecord(name="forge", files=(entry(reserved),)),
                    )
                )

    def test_contributor_files_must_be_sorted_and_unique(self) -> None:
        with self.assertRaises(ValidationError):
            ContributorRecord(name="forge", files=(entry("b"), entry("a")))
        with self.assertRaises(ValidationError):
            ContributorRecord(name="forge", files=(entry("a"), entry("a")))

    def test_unsafe_paths_are_refused(self) -> None:
        for path in ("", "/etc/passwd", "a/../b", ".", "a//b", "a\\b"):
            with self.assertRaises(ValidationError):
                entry(path)

    def test_image_id_and_source_url_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            ImageIdentity(
                reference="hub-agent:1", image_id="latest", platform="linux/arm64"
            )
        with self.assertRaises(ValidationError):
            ForgeInfo(
                source_url="forge.local",
                compose_project="swecraft-forge",
                image="forgejo:15",
                volume="forgejo-data",
            )


if __name__ == "__main__":
    unittest.main()
