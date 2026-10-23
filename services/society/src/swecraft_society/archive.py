"""One-file transport for bundle directories."""

from __future__ import annotations

import os
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from swecraft_society.errors import SocietyError
from swecraft_society.layout import MANIFEST_NAME


def pack_archive(root: Path, output: Path) -> None:
    """Write a checkpoint directory as one tar.gz, atomically."""

    root = root.resolve()
    output = output.resolve()
    if output.is_relative_to(root):
        raise SocietyError(
            "checkpoint archive output must be outside the checkpoint directory"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with (
            os.fdopen(fd, "wb") as stream,
            tarfile.open(fileobj=stream, mode="w:gz") as tar,
        ):
            for path in sorted(root.rglob("*")):
                # rglob already enumerates every descendant; a recursive
                # add would emit nested members twice.
                tar.add(
                    path,
                    arcname=path.relative_to(root).as_posix(),
                    recursive=False,
                )
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def extract_archive(archive: Path, dest: Path) -> None:
    """Extract a bundle archive, refusing hostile member paths."""

    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, mode="r:*") as tar:
            tar.extractall(dest, filter="data")
    except tarfile.TarError as error:
        raise SocietyError(f"could not extract bundle {archive}: {error}") from None


@contextmanager
def opened_bundle(path: Path) -> Iterator[Path]:
    """Yield a bundle directory, extracting a tar.gz transport if needed."""

    if path.is_dir():
        if not (path / MANIFEST_NAME).is_file():
            raise SocietyError(f"{path} has no {MANIFEST_NAME}; not a bundle")
        yield path
        return
    if not path.is_file():
        raise SocietyError(f"bundle does not exist: {path}")
    with tempfile.TemporaryDirectory(prefix="swecraft-society-") as scratch:
        root = Path(scratch)
        extract_archive(path, root)
        if not (root / MANIFEST_NAME).is_file():
            raise SocietyError(f"{path} has no {MANIFEST_NAME}; not a bundle archive")
        yield root
