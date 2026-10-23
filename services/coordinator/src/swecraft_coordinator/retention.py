"""Run-directory retention: slim successful runs, sweep old ones.

A run directory can hold repository and memory checkouts, the staged work
repositories, a staged prior session bundle, Harbor's compiled task, and
collected post-turn artifacts.
Once a successful job has published its results, those input copies and
worktrees are no longer needed; the run keeps its decision record, response,
change metadata, and canonical output session bundle.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from swecraft_common.lifecycle import society_lifecycle_lock
from swecraft_common.runid import started_at

logger = logging.getLogger(__name__)

_SLIM_TARGETS = (
    "input/repository",
    "input/memory",
    "input/repos",
    "input/prior-session",
    "harbor/tasks",
)
_COLLECTED_GLOBS = (
    "harbor/jobs/*/*/artifacts/*/repository",
    "harbor/jobs/*/*/artifacts/*/memory",
)


def slim_run(run_dir: Path) -> int:
    """Delete repository copies without racing checkpoint trace capture."""

    if not run_dir.is_dir():
        return 0
    with society_lifecycle_lock(run_dir, timeout_seconds=-1):
        return _slim_run(run_dir)


def _slim_run(run_dir: Path) -> int:
    if not run_dir.is_dir():
        return 0
    targets = [run_dir / relative for relative in _SLIM_TARGETS]
    for pattern in _COLLECTED_GLOBS:
        targets.extend(run_dir.glob(pattern))
    freed = 0
    for target in targets:
        if not target.is_dir() or target.is_symlink():
            continue
        freed += _tree_size(target)
        shutil.rmtree(target, ignore_errors=True)
    return freed


def sweep(runs_dir: Path, max_age_days: int, now: datetime | None = None) -> list[str]:
    """Delete old runs without racing checkpoint trace capture."""

    if max_age_days <= 0 or not runs_dir.is_dir():
        return []
    with society_lifecycle_lock(runs_dir, timeout_seconds=-1):
        return _sweep(runs_dir, max_age_days, now)


def _sweep(runs_dir: Path, max_age_days: int, now: datetime | None = None) -> list[str]:
    """Delete whole run directories older than the retention window."""

    if max_age_days <= 0 or not runs_dir.is_dir():
        return []
    cutoff = (now or datetime.now(UTC)) - timedelta(days=max_age_days)
    removed = []
    for entry in sorted(runs_dir.iterdir()):
        started = started_at(entry.name)
        if started is None or not entry.is_dir() or entry.is_symlink():
            continue
        if started < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def _tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total
