"""SQLite snapshot primitive shared by service-owned checkpoint adapters."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from swecraft_common import HubError


class SqliteSnapshotError(HubError):
    """Raised when a consistent SQLite backup cannot be created."""


def snapshot_sqlite(source: Path, destination: Path) -> None:
    """Copy a live database through SQLite's online backup API."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            target_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
        finally:
            source_connection.close()
    except sqlite3.Error as error:
        destination.unlink(missing_ok=True)
        raise SqliteSnapshotError(
            f"could not snapshot SQLite database {source}: {error}"
        ) from None
