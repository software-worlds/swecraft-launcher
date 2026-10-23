"""Archive transport: round-trip fidelity and hostile-member refusal."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from swecraft_society.archive import extract_archive, opened_bundle, pack_archive
from swecraft_society.errors import SocietyError
from swecraft_society.layout import MANIFEST_NAME


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.bundle = self.scratch / "bundle"
        (self.bundle / "apparatus").mkdir(parents=True)
        (self.bundle / MANIFEST_NAME).write_text("{}\n")
        (self.bundle / "apparatus/world.yaml").write_text("world_id: demo\n")

    def test_pack_then_extract_round_trips(self) -> None:
        archive = self.scratch / "bundle.society.tar.gz"
        pack_archive(self.bundle, archive)
        dest = self.scratch / "restored"
        extract_archive(archive, dest)
        self.assertEqual(
            sorted(path.relative_to(dest).as_posix() for path in dest.rglob("*")),
            ["apparatus", "apparatus/world.yaml", MANIFEST_NAME],
        )
        self.assertEqual(
            (dest / "apparatus/world.yaml").read_text(), "world_id: demo\n"
        )

    def test_archive_output_inside_checkpoint_is_refused(self) -> None:
        output = self.bundle / "bundle.tar.gz"
        with self.assertRaisesRegex(SocietyError, "outside the checkpoint"):
            pack_archive(self.bundle, output)
        self.assertFalse(output.exists())
        self.assertFalse(any(self.bundle.glob(".*bundle.tar.gz.*.tmp")))

    def test_archive_members_are_unique(self) -> None:
        archive = self.scratch / "bundle.society.tar.gz"
        pack_archive(self.bundle, archive)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertEqual(sorted(names), sorted(set(names)))

    def test_opened_bundle_accepts_directory_and_archive(self) -> None:
        with opened_bundle(self.bundle) as root:
            self.assertEqual(root, self.bundle)
        archive = self.scratch / "bundle.society.tar.gz"
        pack_archive(self.bundle, archive)
        with opened_bundle(archive) as root:
            self.assertTrue((root / MANIFEST_NAME).is_file())
            extracted = root
        self.assertFalse(extracted.exists())

    def test_opened_bundle_refuses_non_bundles(self) -> None:
        with self.assertRaises(SocietyError):
            with opened_bundle(self.scratch / "absent"):
                pass
        empty = self.scratch / "empty"
        empty.mkdir()
        with self.assertRaises(SocietyError):
            with opened_bundle(empty):
                pass

    def test_hostile_member_paths_are_refused(self) -> None:
        hostile = self.scratch / "hostile.tar.gz"
        with tarfile.open(hostile, "w:gz") as tar:
            payload = b"pwned"
            info = tarfile.TarInfo(name="../escape.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with self.assertRaises(SocietyError):
            extract_archive(hostile, self.scratch / "out")
        self.assertFalse((self.scratch.parent / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
