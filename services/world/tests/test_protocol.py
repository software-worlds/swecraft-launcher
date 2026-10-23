from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from swecraft_world import (
    PinSpec,
    RepositoryVisibility,
    WorldError,
    WorldLock,
    WorldLockRepository,
    WorldManifest,
    load_lock,
    load_manifest,
    validate_lock,
    write_lock_atomic,
)

PIN_COMMIT = "a" * 40
PIN_TREE = "b" * 40
BASELINE_COMMIT = "c" * 40
BASELINE_TREE = "d" * 40
SWECRAFT_COMMIT = "e" * 40


def manifest_payload(*repos: dict) -> dict:
    return {
        "schema_version": 1,
        "world_id": "experiment-2",
        "org": "hub",
        "repos": list(repos)
        or [
            {
                "name": "demo",
                "upstream": "https://example.test/demo.git",
                "pin": {"type": "tag", "value": "v1.0.0"},
            }
        ],
    }


def repo_payload(
    name: str = "demo",
    *,
    upstream: str | None = None,
    visibility: RepositoryVisibility = "public",
    pin_commit: str = PIN_COMMIT,
    pin_tree: str = PIN_TREE,
    transformed: bool = False,
    duplicate_pin_of: str | None = None,
) -> dict:
    return {
        "name": name,
        "upstream": upstream or f"https://example.test/{name}.git",
        "pin": {"type": "tag", "value": "v1.0.0"},
        "visibility": visibility,
        "pin_ref": "refs/tags/v1.0.0",
        "pin_commit": pin_commit,
        "pin_tree": pin_tree,
        "default_branch": "main",
        "seed_protocol": "relocate-github-actions-v1",
        "baseline_commit": BASELINE_COMMIT if transformed else pin_commit,
        "baseline_tree": BASELINE_TREE if transformed else pin_tree,
        "stripped_paths": [".github/workflows/ci.yml"] if transformed else [],
        "actions_enabled": True,
        **(
            {"duplicate_pin_of": duplicate_pin_of}
            if duplicate_pin_of is not None
            else {}
        ),
    }


def write_manifest(root: Path, payload: dict | None = None) -> Path:
    path = root / "world.yaml"
    path.write_text(
        yaml.safe_dump(payload or manifest_payload(), sort_keys=False),
        encoding="utf-8",
    )
    return path


def lock_payload(document, *repos: dict, status: str = "complete") -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "world_id": document.manifest.world_id,
        "seeded_at": "2026-07-28T12:00:00+00:00",
        "forge": "http://127.0.0.1:3400",
        "org": document.manifest.org,
        "manifest_path": str(document.path),
        "manifest_sha256": document.sha256,
        "hub_commit": SWECRAFT_COMMIT,
        "repos": list(repos) or [repo_payload()],
    }


class ManifestTests(unittest.TestCase):
    def test_loads_explicit_tag_and_commit_pins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_manifest(
                root,
                manifest_payload(
                    {
                        "name": "tagged",
                        "upstream": "https://example.test/tagged.git",
                        "pin": {"type": "tag", "value": "v1.0.0"},
                    },
                    {
                        "name": "committed",
                        "upstream": "https://example.test/committed.git",
                        "pin": {"type": "commit", "value": PIN_COMMIT},
                    },
                ),
            )
            document = load_manifest(path)

        self.assertEqual(document.manifest.repos[0].pin.ref, "refs/tags/v1.0.0")
        self.assertEqual(document.manifest.repos[1].pin.ref, PIN_COMMIT)
        self.assertEqual(len(document.sha256), 64)

    def test_repository_visibility_defaults_public_and_accepts_private(self) -> None:
        manifest = WorldManifest.model_validate(
            manifest_payload(
                {
                    "name": "public",
                    "upstream": "https://example.test/public.git",
                    "pin": {"type": "tag", "value": "v1.0.0"},
                },
                {
                    "name": "private",
                    "upstream": "https://example.test/private.git",
                    "pin": {"type": "tag", "value": "v1.0.0"},
                    "visibility": "private",
                },
            )
        )
        self.assertEqual(manifest.repos[0].visibility, "public")
        self.assertEqual(manifest.repos[1].visibility, "private")

        payload = manifest_payload()
        payload["repos"][0]["visibility"] = "internal"
        with self.assertRaisesRegex(ValidationError, "private"):
            WorldManifest.model_validate(payload)

    def test_rejects_unknown_keys_and_ambiguous_pins(self) -> None:
        payload = manifest_payload()
        payload["surprise"] = True
        with self.assertRaises(ValidationError):
            WorldManifest.model_validate(payload)
        with self.assertRaisesRegex(ValidationError, "full lowercase"):
            PinSpec(type="commit", value="abc")
        with self.assertRaisesRegex(ValidationError, "not a refs/tags"):
            PinSpec(type="tag", value="refs/tags/v1")

    def test_rejects_http_upstreams_with_embedded_credentials(self) -> None:
        payload = manifest_payload()
        payload["repos"][0]["upstream"] = "https://user:secret@example.test/demo.git"
        with self.assertRaisesRegex(ValidationError, "must not contain credentials"):
            WorldManifest.model_validate(payload)

    def test_rejects_option_like_upstreams_and_branches(self) -> None:
        for field, value in (
            ("upstream", "--upload-pack=helper"),
            ("default_branch", "--orphan"),
        ):
            payload = manifest_payload()
            payload["repos"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValidationError, "command-line option"):
                    WorldManifest.model_validate(payload)

    def test_duplicate_relationships_are_explicit_and_ordered(self) -> None:
        primary = {
            "name": "primary",
            "upstream": "https://example.test/primary.git",
            "pin": {"type": "tag", "value": "v1"},
        }
        duplicate = {
            "name": "duplicate",
            "upstream": "https://example.test/duplicate.git",
            "pin": {"type": "tag", "value": "v1"},
            "duplicate_pin_of": "primary",
        }
        manifest = WorldManifest.model_validate(manifest_payload(primary, duplicate))
        self.assertEqual(manifest.repos[1].duplicate_pin_of, "primary")

        with self.assertRaisesRegex(ValidationError, "primary must appear first"):
            WorldManifest.model_validate(manifest_payload(duplicate, primary))
        duplicate["duplicate_pin_of"] = "missing"
        with self.assertRaisesRegex(ValidationError, "unknown repository"):
            WorldManifest.model_validate(manifest_payload(primary, duplicate))

    def test_load_errors_are_world_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            malformed = root / "world.yaml"
            malformed.write_text("repos: [", encoding="utf-8")
            with self.assertRaisesRegex(WorldError, "invalid world manifest"):
                load_manifest(malformed)
            with self.assertRaisesRegex(WorldError, "could not read world manifest"):
                load_manifest(root / "missing.yaml")


class LockModelTests(unittest.TestCase):
    def test_baseline_shape_tracks_the_seed_transformation(self) -> None:
        untouched = WorldLockRepository.model_validate(repo_payload())
        transformed = WorldLockRepository.model_validate(repo_payload(transformed=True))
        self.assertEqual(untouched.baseline_commit, untouched.pin_commit)
        self.assertNotEqual(transformed.baseline_commit, transformed.pin_commit)

        wrong = repo_payload()
        wrong["baseline_commit"] = BASELINE_COMMIT
        with self.assertRaisesRegex(ValidationError, "exactly when"):
            WorldLockRepository.model_validate(wrong)

    def test_old_lock_entries_default_to_public_visibility(self) -> None:
        payload = repo_payload()
        payload.pop("visibility")
        self.assertEqual(
            WorldLockRepository.model_validate(payload).visibility,
            "public",
        )

    def test_stripped_paths_are_safe_sorted_workflow_paths(self) -> None:
        for paths in (
            [".github/workflows/z.yml", ".github/workflows/a.yml"],
            ["../secret"],
            ["src/code.py"],
        ):
            payload = repo_payload(transformed=True)
            payload["stripped_paths"] = paths
            with self.subTest(paths=paths):
                with self.assertRaises(ValidationError):
                    WorldLockRepository.model_validate(payload)

    def test_duplicate_pin_commits_need_one_declared_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = load_manifest(write_manifest(Path(directory)))
            payload = lock_payload(
                document,
                repo_payload("one"),
                repo_payload("two"),
            )
            with self.assertRaisesRegex(ValidationError, "ambiguous duplicate"):
                WorldLock.model_validate(payload)

    def test_explicit_duplicate_pin_group_binds_to_declared_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = load_manifest(
                write_manifest(
                    root,
                    manifest_payload(
                        {
                            "name": "one",
                            "upstream": "https://example.test/one.git",
                            "pin": {"type": "tag", "value": "v1.0.0"},
                        },
                        {
                            "name": "two",
                            "upstream": "https://example.test/two.git",
                            "pin": {"type": "tag", "value": "v1.0.0"},
                            "duplicate_pin_of": "one",
                        },
                    ),
                )
            )
            lock = WorldLock.model_validate(
                lock_payload(
                    document,
                    repo_payload("one"),
                    repo_payload("two", duplicate_pin_of="one"),
                )
            )
        self.assertEqual(validate_lock(document, lock), lock)

    def test_lock_requires_one_timezone_aware_creation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = load_manifest(write_manifest(Path(directory)))
            payload = lock_payload(document)
            payload["adopted_at"] = payload["seeded_at"]
            with self.assertRaisesRegex(ValidationError, "exactly one"):
                WorldLock.model_validate(payload)
            payload.pop("seeded_at")
            payload["adopted_at"] = "2026-07-28T12:00:00"
            with self.assertRaisesRegex(ValidationError, "timezone"):
                WorldLock.model_validate(payload)


class BindingAndIoTests(unittest.TestCase):
    def test_complete_lock_round_trips_and_binds_to_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = load_manifest(write_manifest(root))
            lock = WorldLock.model_validate(lock_payload(document))
            lock_path = root / "world.lock.json"

            write_lock_atomic(lock_path, lock)
            loaded = load_lock(lock_path)

            self.assertEqual(validate_lock(document, loaded), lock)
            self.assertTrue(lock_path.read_text(encoding="utf-8").endswith("\n"))
            self.assertEqual(list(root.glob(".world.lock.json.*.tmp")), [])

    def test_partial_lock_must_be_a_manifest_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = {
                "name": "first",
                "upstream": "https://example.test/first.git",
                "pin": {"type": "tag", "value": "v1.0.0"},
            }
            second = {
                "name": "second",
                "upstream": "https://example.test/second.git",
                "pin": {"type": "tag", "value": "v1.0.0"},
            }
            document = load_manifest(
                write_manifest(root, manifest_payload(first, second))
            )
            partial = WorldLock.model_validate(
                lock_payload(
                    document,
                    repo_payload("first"),
                    status="partial",
                )
            )
            self.assertEqual(
                validate_lock(document, partial, require_complete=False), partial
            )
            with self.assertRaisesRegex(WorldError, "partial"):
                validate_lock(document, partial)

            wrong = WorldLock.model_validate(
                lock_payload(
                    document,
                    repo_payload("second"),
                    status="partial",
                )
            )
            with self.assertRaisesRegex(WorldError, "manifest prefix"):
                validate_lock(document, wrong, require_complete=False)

            full_partial = WorldLock.model_validate(
                lock_payload(
                    document,
                    repo_payload("first"),
                    repo_payload("second", pin_commit="f" * 40, pin_tree="1" * 40),
                    status="partial",
                )
            )
            with self.assertRaisesRegex(WorldError, "already contains every"):
                validate_lock(document, full_partial, require_complete=False)

    def test_binding_rejects_digest_and_intent_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = load_manifest(write_manifest(root))
            payload = lock_payload(document)
            payload["manifest_sha256"] = "f" * 64
            drifted_digest = WorldLock.model_validate(payload)
            with self.assertRaisesRegex(WorldError, "manifest digest"):
                validate_lock(document, drifted_digest)

            payload = lock_payload(document)
            payload["repos"][0]["upstream"] = "https://other.test/demo.git"
            drifted_intent = WorldLock.model_validate(payload)
            with self.assertRaisesRegex(WorldError, "different upstream"):
                validate_lock(document, drifted_intent)

            payload = lock_payload(document)
            payload["repos"][0]["visibility"] = "private"
            drifted_visibility = WorldLock.model_validate(payload)
            with self.assertRaisesRegex(WorldError, "different visibility"):
                validate_lock(document, drifted_visibility)

    def test_failed_atomic_replace_preserves_the_previous_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = load_manifest(write_manifest(root))
            lock = WorldLock.model_validate(lock_payload(document))
            path = root / "world.lock.json"
            write_lock_atomic(path, lock)
            before = path.read_bytes()

            with patch("swecraft_world.io.os.replace", side_effect=OSError("disk")):
                with self.assertRaisesRegex(OSError, "disk"):
                    write_lock_atomic(path, lock)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(root.glob(".world.lock.json.*.tmp")), [])

    def test_invalid_json_is_a_world_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.lock.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(WorldError, "invalid world lock"):
                load_lock(path)


if __name__ == "__main__":
    unittest.main()
