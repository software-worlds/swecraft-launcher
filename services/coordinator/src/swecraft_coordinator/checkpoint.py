"""Coordinator-owned checkpoint inspection, capture, and path resolution."""

from __future__ import annotations

import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from swecraft_common.fs import copy_regular_tree, resolve_path
from swecraft_common.sqlite import snapshot_sqlite
from swecraft_runner.checkpoint import RunNotTerminalError, export_run
from swecraft_runner.runner import RunnerError

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import CoordinatorConfig

# Run exports are independent read-only projections, so they parallelize
# safely; the bound keeps gzip work from starving the host.
_EXPORT_WORKERS = 8


@dataclass(frozen=True)
class CoordinatorCheckpoint:
    """Read-only checkpoint view of one configured coordinator."""

    config: CoordinatorConfig
    database: Path
    runs_dir: Path
    credentials_dir: Path
    api_token_path: Path | None

    @classmethod
    def load(cls, config_path: Path, *, root: Path) -> CoordinatorCheckpoint:
        config = CoordinatorConfig.load(config_path)
        return cls(
            config=config,
            database=resolve_path(root, config.database),
            runs_dir=resolve_path(root, config.runs_dir),
            credentials_dir=resolve_path(root, config.credentials_dir),
            api_token_path=_local_token_path(root, config.api_token_source),
        )

    @property
    def agent_image(self) -> str:
        return self.config.image

    @property
    def forge_url(self) -> str:
        return self.config.forge_url

    def assert_quiescent(self) -> None:
        """Refuse capture while any coordinator wake is active."""

        if not self.database.is_file():
            return
        try:
            connection = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise CoordinatorError(
                f"could not open coordinator database {self.database}: {error}"
            ) from None
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            required = {"agent_state", "wakes"}
            if not tables.intersection(required):
                return
            if not required.issubset(tables):
                raise CoordinatorError(
                    f"coordinator ledger schema is incomplete: {self.database}"
                )
            active = connection.execute(
                "SELECT agent FROM agent_state WHERE active_started IS NOT NULL "
                "UNION SELECT agent FROM wakes WHERE status = 'running'"
            ).fetchall()
        except sqlite3.Error as error:
            raise CoordinatorError(
                f"could not inspect coordinator database {self.database}: {error}"
            ) from None
        finally:
            connection.close()
        if active:
            names = ", ".join(sorted({str(row[0]) for row in active}))
            raise CoordinatorError(
                f"agents have active wakes: {names}; finish or recover them "
                "before checkpointing"
            )

    def capture(
        self,
        *,
        state_destination: Path,
        runs_destination: Path,
        include_harbor: bool = False,
    ) -> tuple[str, ...]:
        """Snapshot state and terminal runs; return skipped partial run IDs.

        Run traces are always captured — they are the irreplaceable half
        of the record; only the raw Harbor archive is optional.
        Exports are independent projections and run on a bounded pool.
        """

        if self.database.is_file():
            snapshot_sqlite(self.database, state_destination / "coordinator.db")
        if self.api_token_path is not None:
            _copy_required_file(
                self.api_token_path, state_destination / "api_token", "Hub API token"
            )
        if self.credentials_dir.is_dir():
            copy_regular_tree(
                self.credentials_dir,
                state_destination / "credentials",
                label="coordinator state",
            )
        skipped: list[str] = []
        exportable: list[Path] = []
        if self.runs_dir.is_dir():
            for run in sorted(self.runs_dir.iterdir()):
                if run.is_symlink():
                    raise CoordinatorError(
                        f"coordinator run directory contains a symlink: {run}"
                    )
                if not run.is_dir():
                    skipped.append(run.name)
                    continue
                exportable.append(run)
        # Terminal runs are immutable and the archive bytes deterministic,
        # so the packed harbor tree is cached per run id beside the runs
        # tree (never inside it): repeat captures link cached bytes instead
        # of re-gzipping every run — the dominant CPU cost of a boundary.
        harbor_cache = self.runs_dir.parent / "harbor-cache" if include_harbor else None
        workers = max(1, min(_EXPORT_WORKERS, os.cpu_count() or 1))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="run-export"
        ) as pool:
            outcomes = pool.map(
                lambda run: _export_one(
                    run,
                    runs_destination / run.name,
                    include_harbor=include_harbor,
                    harbor_cache=harbor_cache,
                ),
                exportable,
            )
        for name in outcomes:
            if name is not None:
                skipped.append(name)
        if harbor_cache is not None:
            _prune_harbor_cache(harbor_cache, {run.name for run in exportable})
        return tuple(sorted(skipped))


def _export_one(
    run: Path,
    destination: Path,
    *,
    include_harbor: bool,
    harbor_cache: Path | None = None,
) -> str | None:
    """Export one run; return its name when skipped as non-terminal."""

    try:
        export_run(
            run,
            destination,
            include_harbor=include_harbor,
            harbor_cache=harbor_cache,
        )
    except RunNotTerminalError:
        return run.name
    except RunnerError as error:
        raise CoordinatorError(
            f"could not checkpoint coordinator run {run.name}: {error}"
        ) from None
    return None


def _prune_harbor_cache(cache: Path, live_run_ids: set[str]) -> None:
    """Drop cache entries for runs retention has swept; keep the cache
    bounded by the same policy as the runs tree it mirrors. Best-effort:
    a capture never fails on cache housekeeping."""

    if not cache.is_dir():
        return
    suffix = ".harbor.tar.gz"
    for entry in cache.iterdir():
        # A crashed fill (killed between the hardlink and the atomic
        # replace) leaves a .{run_id}.XXXX.tmp behind. Prune runs only
        # after the export pool has drained and captures cannot overlap
        # (lifecycle lock), so any .tmp still present here is dead.
        if entry.name.endswith(".tmp"):
            try:
                entry.unlink()
            except OSError:
                pass
            continue
        if not entry.name.endswith(suffix):
            continue
        if entry.name[: -len(suffix)] not in live_run_ids:
            try:
                entry.unlink()
            except OSError:
                continue


def _local_token_path(root: Path, source: str) -> Path | None:
    if not source.startswith("file:"):
        return None
    raw = source.removeprefix("file:")
    if not raw:
        raise CoordinatorError("api_token file source is empty")
    return resolve_path(root, Path(raw))


def _copy_required_file(source: Path, destination: Path, label: str) -> None:
    if source.is_symlink() or not source.is_file():
        raise CoordinatorError(f"configured {label} is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
