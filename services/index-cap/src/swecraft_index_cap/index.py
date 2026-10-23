"""PyPI's simple index as it stood at a cutoff.

A world is seeded at pinned releases, but an agent's `pip install` resolves
against the package index of the day it runs. From the moment an upstream
publishes a later release, every environment in the world contains it: in
one of our runs a requests maintainer benchmarked against urllib3 2.8.0, which
upstream published three weeks after the world's pin. This module filters one
project's index page down to the files that existed at the cutoff, using the
upload time PyPI reports for every file (PEP 700), so a resolver sees the index
the world was pinned against.

Only the listing is filtered. File URLs are left pointing at the upstream
file host, so installs download exactly what PyPI serves.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

JSON_TYPE = "application/vnd.pypi.simple.v1+json"
HTML_TYPE = "application/vnd.pypi.simple.v1+html"

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NORMALIZE = re.compile(r"[-_.]+")
_SDIST_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".tgz", ".egg")


class IndexCapError(ValueError):
    """A request this index cannot serve; the message is safe to show."""


def parse_cutoff(text: str) -> datetime:
    """A cutoff from a URL segment: a date, or an RFC 3339 timestamp.

    A bare date means the first instant of that day in UTC, so nothing
    uploaded on the day itself is visible. That is the conservative reading,
    and it keeps a date and its midnight timestamp interchangeable.
    """

    value = text.strip()
    try:
        if _DATE.fullmatch(value):
            return datetime.fromisoformat(value).replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IndexCapError(
            f"cutoff {text!r} is neither a date (2026-08-16) nor an RFC 3339 "
            "timestamp (2026-08-16T00:00:00Z)"
        ) from error
    if parsed.tzinfo is None:
        raise IndexCapError(f"cutoff {text!r} needs a time zone, e.g. a trailing Z")
    return parsed.astimezone(UTC)


def normalize(project: str) -> str:
    """PEP 503 project-name normalization."""

    return _NORMALIZE.sub("-", project).lower()


def _uploaded(file: dict) -> datetime | None:
    raw = file.get("upload-time")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _file_version(filename: str) -> str | None:
    """The version a distribution file carries in its name, lower-cased."""

    name = filename.lower()
    if name.endswith(".whl"):
        parts = name[: -len(".whl")].split("-")
        return parts[1] if len(parts) >= 5 else None
    for suffix in _SDIST_SUFFIXES:
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return stem.rsplit("-", 1)[1] if "-" in stem else None
    return None


@dataclass(frozen=True)
class CappedPage:
    """One project's index page with the files uploaded after the cutoff removed."""

    page: dict
    kept: int
    dropped: int

    def json(self) -> bytes:
        return json.dumps(self.page, separators=(",", ":")).encode()

    def html(self) -> bytes:
        """The same page as PEP 503 HTML, for installers that predate PEP 691."""

        name = html.escape(str(self.page.get("name", "")))
        lines = [
            "<!DOCTYPE html>",
            "<html>",
            "  <head>",
            '    <meta name="pypi:repository-version" content="1.1">',
            f"    <title>Links for {name}</title>",
            "  </head>",
            "  <body>",
            f"    <h1>Links for {name}</h1>",
        ]
        for file in self.page["files"]:
            url = str(file["url"])
            hashes = file.get("hashes") or {}
            if "sha256" in hashes:
                url += f"#sha256={hashes['sha256']}"
            elif hashes:
                algorithm, digest = next(iter(hashes.items()))
                url += f"#{algorithm}={digest}"
            attributes = [f'href="{html.escape(url, quote=True)}"']
            requires = file.get("requires-python")
            if requires:
                attributes.append(
                    f'data-requires-python="{html.escape(str(requires), quote=True)}"'
                )
            yanked = file.get("yanked")
            if yanked:
                reason = yanked if isinstance(yanked, str) else ""
                attributes.append(f'data-yanked="{html.escape(reason, quote=True)}"')
            metadata = file.get("core-metadata", file.get("dist-info-metadata"))
            if metadata:
                value = "true"
                if isinstance(metadata, dict) and "sha256" in metadata:
                    value = f"sha256={metadata['sha256']}"
                attributes.append(f'data-core-metadata="{value}"')
                attributes.append(f'data-dist-info-metadata="{value}"')
            label = html.escape(str(file["filename"]))
            lines.append(f"    <a {' '.join(attributes)}>{label}</a><br />")
        lines += ["  </body>", "</html>", ""]
        return "\n".join(lines).encode()


def cap_page(upstream: dict, cutoff: datetime) -> CappedPage:
    """Filter an upstream PEP 691 page to the files uploaded before the cutoff.

    A file without a readable upload time is dropped: the index exists to
    withhold what cannot be shown to predate the cutoff, not to guess.
    """

    files = upstream.get("files")
    if not isinstance(files, list):
        raise IndexCapError("the upstream page has no file list")
    kept = [
        file
        for file in files
        if isinstance(file, dict)
        and (uploaded := _uploaded(file)) is not None
        and uploaded < cutoff
    ]
    page = {key: value for key, value in upstream.items() if key != "files"}
    page["files"] = kept
    if isinstance(upstream.get("versions"), list):
        present = {
            version
            for file in kept
            if (version := _file_version(str(file.get("filename", "")))) is not None
        }
        page["versions"] = [
            version
            for version in upstream["versions"]
            if str(version).lower() in present
        ]
    return CappedPage(page=page, kept=len(kept), dropped=len(files) - len(kept))
