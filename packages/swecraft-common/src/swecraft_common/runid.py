"""The run-id grammar, as read back from disk.

The runner names run directories ``<ISO-basic UTC stamp>-<8 hex chars>``.
This is the reader-side contract for anything that resolves a recorded run
id to a directory or an age; the runner remains the only writer.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

RUN_ID = re.compile(r"(\d{8}T\d{6}Z)-[0-9a-f]{8}")


# For readers that take run ids from untrusted input (a request path) and
# must validate them before they reach the filesystem.
def is_run_id(value: str) -> bool:
    return bool(RUN_ID.fullmatch(value))


def started_at(run_id: str) -> datetime | None:
    """The UTC start time embedded in a run id, or None for a non-run-id."""

    match = RUN_ID.fullmatch(run_id)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
