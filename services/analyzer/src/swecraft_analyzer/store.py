"""The evidence store: one SQLite file per analysed checkpoint.

Every table is a projection of something the checkpoint already holds, keyed
so a finding can point back at its source: a wake by ``wake_id``/``run_id``
and ``step``, a pull request by ``repo``/``number``, a forge comment by its
``id``. Analysts query it with ``sqlite3`` or pandas; detectors read it with
plain SQL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE agents (agent TEXT NOT NULL, repo TEXT NOT NULL,
    PRIMARY KEY (agent, repo));
CREATE TABLE repos (repo TEXT PRIMARY KEY, default_branch TEXT, seed TEXT,
    head TEXT, is_world INTEGER);
CREATE TABLE wakes (wake_id INTEGER, run_id TEXT, agent TEXT, day TEXT,
    started TEXT, finished TEXT, status TEXT, outcome TEXT, summary TEXT,
    cost_usd REAL, error TEXT, harness TEXT, model TEXT, transcript TEXT,
    tool_calls INTEGER, response TEXT);
CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, wake_id INTEGER, run_id TEXT,
    agent TEXT, step INTEGER, call INTEGER, tool TEXT, kind TEXT, text TEXT,
    path TEXT, output TEXT);
CREATE TABLE forge_actions (id INTEGER PRIMARY KEY, wake_id INTEGER,
    run_id TEXT, agent TEXT, ts TEXT, repo TEXT, action TEXT, number INTEGER,
    metadata TEXT, cmd TEXT);
CREATE TABLE pulls (repo TEXT, number INTEGER, title TEXT, body TEXT,
    author TEXT, state TEXT, merged INTEGER, merger TEXT, merged_at TEXT,
    created_at TEXT, closed_at TEXT, base_branch TEXT, head_branch TEXT,
    merge_base TEXT, head_commit TEXT, merged_commit TEXT, additions INTEGER,
    deletions INTEGER, files INTEGER, diff_error TEXT, opened_wake_id INTEGER,
    opened_run_id TEXT, PRIMARY KEY (repo, number));
CREATE TABLE pull_files (repo TEXT, number INTEGER, path TEXT,
    additions INTEGER, deletions INTEGER, category TEXT);
CREATE TABLE pull_patches (repo TEXT, number INTEGER, patch TEXT,
    truncated INTEGER, PRIMARY KEY (repo, number));
CREATE TABLE issues (repo TEXT, number INTEGER, title TEXT, body TEXT,
    author TEXT, state TEXT, created_at TEXT, closed_at TEXT,
    PRIMARY KEY (repo, number));
CREATE TABLE comments (id INTEGER PRIMARY KEY, repo TEXT, number INTEGER,
    is_pull INTEGER, type INTEGER, kind TEXT, author TEXT, body TEXT,
    created_at TEXT, updated_at TEXT, review_id INTEGER, force_push INTEGER);
CREATE TABLE reviews (id INTEGER PRIMARY KEY, repo TEXT, number INTEGER,
    reviewer TEXT, pr_author TEXT, kind TEXT, body TEXT, created_at TEXT);
CREATE TABLE content_edits (id INTEGER PRIMARY KEY, repo TEXT, number INTEGER,
    comment_id INTEGER, editor TEXT, content_author TEXT, edited_at TEXT,
    is_first INTEGER, is_deleted INTEGER);
CREATE TABLE releases (repo TEXT, tag TEXT, title TEXT, author TEXT,
    sha TEXT, created_at TEXT, is_draft INTEGER, is_tag INTEGER);
CREATE TABLE ci_runs (id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT,
    event TEXT, status INTEGER, trigger_user TEXT, commit_sha TEXT,
    created_at TEXT, schedule_id INTEGER);
CREATE TABLE ci_schedules (id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT,
    created_at TEXT, content TEXT);
CREATE TABLE future_tags (repo TEXT, tag TEXT, "commit" TEXT);
CREATE TABLE future_commits (repo TEXT, sha TEXT, subject TEXT);
CREATE TABLE future_lines (repo TEXT, line TEXT, "commit" TEXT, path TEXT);
CREATE INDEX tool_calls_wake ON tool_calls (wake_id);
CREATE INDEX tool_calls_agent ON tool_calls (agent, kind);
CREATE INDEX forge_actions_number ON forge_actions (repo, number);
CREATE INDEX pull_files_pr ON pull_files (repo, number);
"""


def create(path: Path) -> sqlite3.Connection:
    """A fresh store at ``path``; an existing file is replaced."""

    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    return connection


def open_store(path: Path) -> sqlite3.Connection:
    """Open an existing store for reading, rows addressable by column name."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _clean(value: Any) -> Any:
    """Text SQLite can store: lone surrogates (from decoded JSON escapes in a
    transcript) become replacement characters."""

    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return value


def insert(
    connection: sqlite3.Connection, table: str, rows: Iterable[Mapping[str, Any]]
) -> int:
    """Insert dict rows; the first row's keys name the columns."""

    count = 0
    statement = None
    columns: tuple[str, ...] = ()
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        if statement is None:
            columns = tuple(row)
            statement = (
                f"INSERT INTO {table} ({', '.join(f'"{c}"' for c in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})"
            )
        batch.append(tuple(_clean(row.get(column)) for column in columns))
        if len(batch) >= 1000:
            connection.executemany(statement, batch)
            count += len(batch)
            batch.clear()
    if statement is not None and batch:
        connection.executemany(statement, batch)
        count += len(batch)
    return count
