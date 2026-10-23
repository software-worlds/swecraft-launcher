"""One local-host lock that excludes society claims during checkpoint capture."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from swecraft_common import HubError

LOCK_NAME = "society.lifecycle.lock"


class LifecycleBusy(HubError):
    """Raised when another lifecycle operation owns the society lock."""


def lifecycle_lock_path(state_path: Path) -> Path:
    """Resolve one lock under a live ``.swecraft`` or loaded ``state`` root.

    Normal services keep databases under one ``.swecraft`` directory. A resumed
    checkpoint keeps them under sibling ``state/coordinator`` and
    ``state/evaluator`` directories; both must still derive one shared lock.
    Other custom stores get a lock beside the store.
    """

    resolved = state_path.expanduser().resolve()
    parents = (resolved.parent, *resolved.parents)
    for parent in parents:
        if parent.name in (".swecraft", "state"):
            return parent / LOCK_NAME
    return resolved.parent / f".{LOCK_NAME}"


@contextmanager
def society_lifecycle_lock(
    state_path: Path, *, timeout_seconds: float = 0
) -> Iterator[Path]:
    """Acquire the lock shared by checkpoint and service claim transactions."""

    path = lifecycle_lock_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path, timeout=timeout_seconds)
    try:
        lock.acquire()
    except Timeout:
        raise LifecycleBusy(f"society lifecycle is busy ({path})") from None
    try:
        yield path
    finally:
        lock.release()
