"""Serialize world mutations that share one provenance lock."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from swecraft_world.errors import WorldError


@contextmanager
def world_mutation_lock(lock_path: Path) -> Iterator[None]:
    """Hold the operation lock beside ``world.lock.json`` without waiting.

    Atomic JSON replacement protects readers from torn files, but it cannot
    serialize the Forge mutations that produce those files. A second seed or
    adopt against the same lock therefore fails immediately instead of racing
    repository creation, replacement, or journal progress.
    """

    path = lock_path.with_name(f".{lock_path.name}.operation.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise WorldError(
            f"another world mutation is already using {lock_path}"
        ) from None
    try:
        yield
    finally:
        lock.release()
