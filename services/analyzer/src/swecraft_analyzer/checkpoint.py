"""Read one society checkpoint directory; never start or touch a service.

The layout is the one ``swecraft-society checkpoint`` writes (see
docs/reference/society-checkpoints.md): ``manifest.json``, ``apparatus/``,
``state/coordinator/coordinator.db``, ``state/forge/forgejo-data.tar``,
``runs/<run-id>/output/`` and ``evaluations/``. The checkpoint is only read;
the one thing unpacked, the forge volume, goes into the analysis directory.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AnalyzerError(RuntimeError):
    """A checkpoint or analysis directory that cannot be used as asked."""


# Forgejo's SQLite database (with its write-ahead log) and the bare
# repositories are what the collector reads; avatars, queues, indexers and
# the CI log store stay in the tar.
_FORGE_MEMBERS = ("data/forgejo.db", "git/repositories/")


@dataclass(frozen=True)
class Checkpoint:
    root: Path
    manifest: dict[str, Any]

    @classmethod
    def open(cls, root: Path) -> Checkpoint:
        manifest = root / "manifest.json"
        if not manifest.is_file():
            raise AnalyzerError(f"not a society checkpoint (no manifest.json): {root}")
        return cls(root=root.resolve(), manifest=json.loads(manifest.read_text()))

    @property
    def checkpoint_id(self) -> str:
        return str(self.manifest.get("checkpoint_id") or self.root.name)

    @property
    def forge_tar(self) -> Path:
        return self.root / "state" / "forge" / "forgejo-data.tar"

    @property
    def forge_url(self) -> str:
        return str((self.manifest.get("forge") or {}).get("source_url") or "")

    def heads(self) -> list[dict[str, str]]:
        return [
            {
                "repo": str(head.get("name") or ""),
                "seed": str(head.get("baseline_commit") or ""),
                "head": str(head.get("current_head") or ""),
            }
            for head in self.manifest.get("heads") or []
        ]

    def apparatus_file(self, name: str) -> str | None:
        path = self.root / "apparatus" / name
        return (
            path.read_text(encoding="utf-8", errors="replace")
            if path.is_file()
            else None
        )

    def run_outputs(self) -> dict[str, Path]:
        runs = self.root / "runs"
        if not runs.is_dir():
            return {}
        return {
            path.name: path / "output"
            for path in sorted(runs.iterdir())
            if path.is_dir()
        }

    def coordinator_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """(wakes, agent→repo rows) from the captured coordinator ledger."""

        database = self.root / "state" / "coordinator" / "coordinator.db"
        if not database.is_file():
            return [], []
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            wakes = (
                [
                    dict(row)
                    for row in connection.execute("SELECT * FROM wakes ORDER BY id")
                ]
                if "wakes" in tables
                else []
            )
            repos = (
                [
                    dict(row)
                    for row in connection.execute("SELECT agent, repo FROM agent_repos")
                ]
                if "agent_repos" in tables
                else []
            )
        finally:
            connection.close()
        return wakes, repos


def extract_forge(tar_path: Path, destination: Path) -> Path:
    """Unpack the forge database and repositories once per tar file.

    A marker records the tar's size and mtime; a second collect over the same
    checkpoint reuses the unpacked copy instead of reading the tar again.
    """

    stat = tar_path.stat()
    key = {"tar": str(tar_path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    marker = destination / ".extracted.json"
    if marker.is_file() and json.loads(marker.read_text()) == key:
        return destination
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(tar_path) as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.removeprefix("./").startswith(_FORGE_MEMBERS)
        ]
        archive.extractall(destination, members=members, filter="data")
    marker.write_text(json.dumps(key))
    return destination
