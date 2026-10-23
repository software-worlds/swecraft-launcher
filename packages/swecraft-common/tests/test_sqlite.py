"""Consistent SQLite snapshot primitive."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from swecraft_common.sqlite import snapshot_sqlite


class SqliteSnapshotTests(unittest.TestCase):
    def test_online_backup_copies_committed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            destination = root / "copy/snapshot.db"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE records (value TEXT)")
            connection.execute("INSERT INTO records VALUES ('captured')")
            connection.commit()
            snapshot_sqlite(source, destination)
            connection.close()
            copied = sqlite3.connect(destination)
            try:
                value = copied.execute("SELECT value FROM records").fetchone()[0]
            finally:
                copied.close()
            self.assertEqual(value, "captured")


if __name__ == "__main__":
    unittest.main()
