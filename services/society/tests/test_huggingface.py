"""Private directory-native Hugging Face transport without network calls."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_orchestrator import SocietyFixture

from swecraft_society.errors import SocietyError
from swecraft_society.huggingface import (
    DownloadedCheckpoint,
    HfLocation,
    HuggingFaceTransport,
    parse_hf_uri,
    record_load_provenance,
)
from swecraft_society.inventory import verify_checkpoint
from swecraft_society.orchestrator import checkpoint


class FakeApi:
    def __init__(
        self, *, private: bool = True, missing_remote: set[str] | None = None
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.private = private
        self.missing_remote = missing_remote or set()
        self.remote_files: list[str] | None = None

    def repo_info(self, **kwargs):
        self.calls.append(("info", kwargs))
        siblings = None
        if kwargs.get("revision") == "b" * 40 and self.remote_files is not None:
            siblings = [
                SimpleNamespace(rfilename=path)
                for path in self.remote_files
                if path not in self.missing_remote
            ]
        return SimpleNamespace(sha="a" * 40, private=self.private, siblings=siblings)

    def create_repo(self, **kwargs):
        self.calls.append(("create", kwargs))

    def upload_folder(self, **kwargs):
        self.calls.append(("upload", kwargs))
        folder = Path(kwargs["folder_path"])
        self.remote_files = sorted(
            path.relative_to(folder).as_posix()
            for path in folder.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        return SimpleNamespace(oid="b" * 40)

    def create_tag(self, repo_id, **kwargs):
        self.calls.append(("tag", {"repo_id": repo_id, **kwargs}))


class HuggingFaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.materialized = self.root / "dataset"
        self.materialized.mkdir()
        self.api = FakeApi()

    def test_uri_is_dataset_root_with_optional_commit_tag_or_branch(self) -> None:
        self.assertEqual(
            parse_hf_uri("hf://software-worlds/experiment-3"),
            HfLocation(repo_id="software-worlds/experiment-3"),
        )
        self.assertEqual(
            parse_hf_uri("hf://software-worlds/experiment-3@checkpoint-1").revision,
            "checkpoint-1",
        )
        self.assertEqual(
            parse_hf_uri("hf://software-worlds/experiment-3@refs/heads/trial").revision,
            "refs/heads/trial",
        )
        for invalid in (
            "https://huggingface.co/datasets/a/b",
            "hf://only-owner",
            "hf://owner/repo/archive.tar.gz",
            "hf://owner/repo@",
            "hf://owner/repo@one@two",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SocietyError):
                parse_hf_uri(invalid)

    def test_fetch_resolves_latest_then_downloads_the_exact_commit(self) -> None:
        downloads = []

        def download(repo_id, **kwargs):
            downloads.append((repo_id, kwargs))
            return str(self.materialized)

        transport = HuggingFaceTransport(api=self.api, download=download)
        result = transport.fetch(
            HfLocation("software-worlds/experiment-3"),
            cache_dir=self.root / "cache",
        )
        self.assertEqual(result.resolved_revision, "a" * 40)
        self.assertEqual(downloads[0][1]["revision"], "a" * 40)
        self.assertEqual(downloads[0][1]["repo_type"], "dataset")
        self.assertNotEqual(result.path, self.materialized.resolve())

    def test_fetch_dereferences_an_hf_cache_snapshot_before_real_verification(
        self,
    ) -> None:
        fixture = SocietyFixture(self)
        source = checkpoint(fixture.options(), forge_runtime=fixture.forge)
        cache = self.root / "cache"
        snapshot = (
            cache / "datasets--software-worlds--experiment-3/snapshots" / ("a" * 40)
        )
        blobs = cache / "datasets--software-worlds--experiment-3/blobs"
        blobs.mkdir(parents=True)
        for index, path in enumerate(sorted(source.rglob("*"))):
            relative = path.relative_to(source)
            target = snapshot / relative
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            blob = blobs / str(index)
            shutil.copy2(path, blob)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.path.relpath(blob, target.parent))

        transport = HuggingFaceTransport(
            api=self.api,
            download=lambda *_args, **_kwargs: str(snapshot),
        )
        result = transport.fetch(
            HfLocation("software-worlds/experiment-3"), cache_dir=cache
        )

        manifest = verify_checkpoint(result.path)
        self.assertEqual(manifest.world_id, "checkpoint-world")
        self.assertTrue((snapshot / "manifest.json").is_symlink())
        self.assertNotEqual(result.path, snapshot.resolve())
        self.assertFalse(any(path.is_symlink() for path in result.path.rglob("*")))

    def test_publish_mirrors_root_and_tags_the_checkpoint_id(self) -> None:
        transport = HuggingFaceTransport(api=self.api)
        manifest = SimpleNamespace(checkpoint_id="experiment-20260801T000000Z")
        with patch(
            "swecraft_society.huggingface.verify_checkpoint", return_value=manifest
        ):
            result = transport.publish(
                self.materialized,
                repo_id="software-worlds/experiment-3",
                private=True,
            )
        self.assertEqual(result.resolved_revision, "b" * 40)
        calls = dict(self.api.calls)
        self.assertTrue(calls["create"]["private"])
        self.assertNotIn("path_in_repo", calls["upload"])
        self.assertEqual(calls["upload"]["delete_patterns"], "*")
        self.assertEqual(calls["tag"]["tag"], manifest.checkpoint_id)
        self.assertEqual(calls["tag"]["revision"], "b" * 40)

    def test_publish_refuses_silent_remote_file_loss_before_tagging(self) -> None:
        missing = "runs/run-1/harbor/.git/config"
        path = self.materialized / missing
        path.parent.mkdir(parents=True)
        path.write_text("git metadata\n")
        api = FakeApi(missing_remote={missing})
        manifest = SimpleNamespace(checkpoint_id="checkpoint-1")

        with (
            patch(
                "swecraft_society.huggingface.verify_checkpoint", return_value=manifest
            ),
            self.assertRaisesRegex(SocietyError, "missing 1 checkpoint file"),
        ):
            HuggingFaceTransport(api=api).publish(
                self.materialized,
                repo_id="software-worlds/experiment-3",
                private=True,
            )

        self.assertNotIn("tag", [name for name, _ in api.calls])

    def test_publish_refuses_public_or_unconfirmed_dataset_visibility(self) -> None:
        manifest = SimpleNamespace(checkpoint_id="checkpoint-1")
        with patch(
            "swecraft_society.huggingface.verify_checkpoint", return_value=manifest
        ):
            with self.assertRaisesRegex(SocietyError, "public dataset"):
                HuggingFaceTransport(api=FakeApi(private=False)).publish(
                    self.materialized,
                    repo_id="software-worlds/public",
                    private=True,
                )
            with self.assertRaisesRegex(SocietyError, "only be published privately"):
                HuggingFaceTransport(api=self.api).publish(
                    self.materialized,
                    repo_id="software-worlds/public",
                    private=False,
                )

    def test_load_provenance_is_written_beside_the_loaded_copy(self) -> None:
        dest = self.root / "loaded/checkpoint-1"
        dest.mkdir(parents=True)
        downloaded = DownloadedCheckpoint(
            path=self.materialized,
            location=HfLocation("software-worlds/experiment-3", "main"),
            resolved_revision="c" * 40,
        )
        path = record_load_provenance(dest, downloaded)
        self.assertEqual(path, self.root / "loaded/checkpoint-1.load.json")
        payload = json.loads(path.read_text())
        self.assertEqual(payload["source"], "hf://software-worlds/experiment-3@main")
        self.assertEqual(payload["resolved_revision"], "c" * 40)
        self.assertFalse((dest / "checkpoint-1.load.json").exists())


if __name__ == "__main__":
    unittest.main()
