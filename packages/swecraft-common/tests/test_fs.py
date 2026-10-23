from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from swecraft_common import HubError
from swecraft_common.fs import copy_regular_tree, resolve_path


class ResolvePathTests(unittest.TestCase):
    def test_relative_paths_resolve_against_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(
                resolve_path(root, Path("state/db.sqlite")), root / "state/db.sqlite"
            )

    def test_absolute_paths_stay_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "root"
            absolute = base / "outside"
            self.assertEqual(resolve_path(root, absolute), absolute)


class CopyRegularTreeTests(unittest.TestCase):
    def test_copies_regular_files_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            nested = source / "nested/file.txt"
            nested.parent.mkdir()
            nested.write_text("payload\n")
            os.utime(nested, (1_700_000_000, 1_700_000_000))

            destination = root / "destination"
            copy_regular_tree(source, destination, label="fixture state")

            copied = destination / "nested/file.txt"
            self.assertEqual(copied.read_text(), "payload\n")
            self.assertEqual(int(copied.stat().st_mtime), int(nested.stat().st_mtime))

    def test_rejects_nested_symlinks_before_ignore_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / ".gitkeep").symlink_to("missing")

            with self.assertRaisesRegex(HubError, "fixture state contains a symlink"):
                copy_regular_tree(
                    source,
                    root / "destination",
                    label="fixture state",
                    ignore={".gitkeep"},
                )

    def test_pruned_subtree_is_not_walked_even_for_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "cell/sources/pkg").mkdir(parents=True)
            (source / "cell/keep.log").write_text("kept\n", encoding="utf-8")
            (source / "cell/sources/pkg/code.py").write_text("x\n", encoding="utf-8")
            (source / "cell/sources/pkg/link").symlink_to("missing")

            copy_regular_tree(
                source, root / "destination", label="records", prune={"sources"}
            )

            self.assertTrue((root / "destination/cell/keep.log").is_file())
            self.assertFalse((root / "destination/cell/sources").exists())

    def test_rejects_a_symlink_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_source = root / "real-source"
            real_source.mkdir()
            source = root / "source"
            source.symlink_to(real_source, target_is_directory=True)

            with self.assertRaisesRegex(HubError, "fixture state contains a symlink"):
                copy_regular_tree(source, root / "destination", label="fixture state")

    def test_ignores_matching_regular_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / ".gitkeep").write_text("keep\n")
            (source / "data.txt").write_text("data\n")

            destination = root / "destination"
            copy_regular_tree(
                source, destination, label="fixture state", ignore={".gitkeep"}
            )

            self.assertFalse((destination / ".gitkeep").exists())
            self.assertEqual((destination / "data.txt").read_text(), "data\n")

    def test_ignoring_a_directory_prunes_its_whole_subtree(self) -> None:
        """Naming a directory must not still copy everything inside it.

        The Forge runner's act cache lives at `runner/.cache/` and reaches
        thousands of files on a CI-writing society. Matching only leaf names
        would copy every one of them, which is what made Hugging Face reject
        published checkpoints -- it refuses any path under a `.cache/` folder.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "runner" / ".cache" / "act" / "deep").mkdir(parents=True)
            (source / "runner" / ".cache" / "act" / "deep" / "blob.json").write_text(
                "junk\n"
            )
            (source / "runner" / ".cache" / "top.txt").write_text("junk\n")
            (source / "runner" / "config.yml").write_text("real\n")
            (source / "runner" / ".runner").write_text("real\n")

            destination = root / "destination"
            copy_regular_tree(
                source, destination, label="fixture state", ignore={".cache"}
            )

            self.assertFalse((destination / "runner" / ".cache").exists())
            config = destination / "runner" / "config.yml"
            self.assertEqual(config.read_text(), "real\n")
            self.assertEqual((destination / "runner" / ".runner").read_text(), "real\n")

    def test_ignoring_a_relative_directory_path_prunes_its_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "keep" / "nested").mkdir(parents=True)
            (source / "drop" / "nested").mkdir(parents=True)
            (source / "keep" / "nested" / "a.txt").write_text("a\n")
            (source / "drop" / "nested" / "b.txt").write_text("b\n")

            destination = root / "destination"
            copy_regular_tree(
                source, destination, label="fixture state", ignore={"drop/nested"}
            )

            kept = destination / "keep" / "nested" / "a.txt"
            self.assertEqual(kept.read_text(), "a\n")
            self.assertFalse((destination / "drop" / "nested").exists())

    def test_empty_trees_do_not_create_the_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "empty").mkdir(parents=True)

            destination = root / "destination"
            copy_regular_tree(source, destination, label="fixture state")

            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
