"""Pull requests, comments, reviews and releases out of an unpacked forge.

Reads Forgejo's own SQLite database and bare repositories as the checkpoint
captured them, so a forge that no longer runs can still be analysed. The
comment and review type numbers are Forgejo's (inherited from Gitea); a type
this module does not name is kept as ``type-<n>``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

PATCH_LIMIT = 1_000_000

COMMENT_KINDS = {
    0: "comment",
    1: "reopen",
    2: "close",
    3: "issue_ref",
    4: "commit_ref",
    5: "comment_ref",
    6: "pull_ref",
    7: "label",
    10: "change_title",
    11: "delete_branch",
    21: "code",
    22: "review",
    23: "lock",
    24: "unlock",
    25: "change_target_branch",
    27: "review_request",
    28: "merge_pull",
    29: "pull_push",
    32: "dismiss_review",
}
REVIEW_KINDS = {
    0: "pending",
    1: "approve",
    2: "comment",
    3: "request_changes",
    4: "request",
}

_TEST = re.compile(
    r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$"
)
_BENCH = re.compile(
    r"(^|/)(bench|benches|benchmark|benchmarks|asv_bench|perf)(/|$)|"
    r"(^|/)bench_[^/]*\.py$|_bench[^/]*\.py$|(^|/)test_bench[^/]*\.py$|asv\.conf\.json$"
)
_CI = re.compile(
    r"(^|/)\.(forgejo|github|gitea)/workflows/|\.gitlab-ci\.yml$|Jenkinsfile$"
)
_CONFIG = re.compile(
    r"(^|/)(pyproject\.toml|setup\.cfg|setup\.py|tox\.ini|pytest\.ini|noxfile\.py|"
    r"Makefile|MANIFEST\.in|\.pre-commit-config\.yaml|requirements[^/]*\.(txt|in))$"
)
_DOCS = re.compile(r"(^|/)docs?/|\.(md|rst|txt)$")


def category(path: str) -> str:
    """Coarse role of a changed file: goal, ci, bench, test, config, docs, src."""

    if PurePosixPath(path).name == "GOAL.md":
        return "goal"
    if _CI.search(path):
        return "ci"
    if _BENCH.search(path):
        return "bench"
    if _TEST.search(path):
        return "test"
    if _CONFIG.search(path):
        return "config"
    if _DOCS.search(path):
        return "docs"
    return "src"


def _iso(unix: Any) -> str | None:
    if not unix:
        return None
    return datetime.fromtimestamp(int(unix), UTC).isoformat().replace("+00:00", "Z")


class Forge:
    """One unpacked Forgejo data volume (``data/forgejo.db`` + ``git/``)."""

    def __init__(self, root: Path) -> None:
        database = root / "data" / "forgejo.db"
        if not database.is_file():
            raise FileNotFoundError(f"no Forgejo database under {root}")
        # A read-write handle on our own unpacked copy lets SQLite fold the
        # captured write-ahead log in; the checkpoint itself is never opened.
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.git_root = root / "git" / "repositories"
        self.users = {
            row["id"]: row["lower_name"]
            for row in self.db.execute("SELECT id, lower_name FROM user")
        }
        self.repos = {
            row["id"]: f"{row['owner_name']}/{row['lower_name']}"
            for row in self.db.execute(
                "SELECT id, owner_name, lower_name FROM repository"
            )
        }
        self.issues = {row["id"]: row for row in self.db.execute("SELECT * FROM issue")}

    def close(self) -> None:
        self.db.close()

    def _has(self, table: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    def _user(self, user_id: Any) -> str:
        return self.users.get(user_id, f"user-{user_id}")

    def _issue_ref(self, issue_id: Any) -> tuple[str, int | None, int]:
        issue = self.issues.get(issue_id)
        if issue is None:
            return "", None, 0
        return self.repos.get(issue["repo_id"], ""), issue["index"], issue["is_pull"]

    def repositories(self) -> Iterator[dict[str, Any]]:
        for row in self.db.execute(
            "SELECT owner_name, lower_name, default_branch FROM repository"
        ):
            yield {
                "repo": f"{row['owner_name']}/{row['lower_name']}",
                "default_branch": row["default_branch"],
            }

    def git(self, repo: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.git_root / f"{repo}.git"), *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )

    def pulls(self) -> Iterator[dict[str, Any]]:
        for row in self.db.execute("SELECT * FROM pull_request ORDER BY id"):
            issue = self.issues.get(row["issue_id"])
            if issue is None:
                continue
            repo = self.repos.get(row["base_repo_id"], "")
            head = self.git(
                repo, "rev-parse", "--verify", "-q", f"refs/pull/{issue['index']}/head"
            )
            yield {
                "repo": repo,
                "number": issue["index"],
                "title": issue["name"],
                "body": issue["content"],
                "author": self._user(issue["poster_id"]),
                "state": "merged"
                if row["has_merged"]
                else ("closed" if issue["is_closed"] else "open"),
                "merged": int(bool(row["has_merged"])),
                "merger": self._user(row["merger_id"]) if row["merger_id"] else None,
                "merged_at": _iso(row["merged_unix"]),
                "created_at": _iso(issue["created_unix"]),
                "closed_at": _iso(issue["closed_unix"]),
                "base_branch": row["base_branch"],
                "head_branch": row["head_branch"],
                "merge_base": row["merge_base"],
                "head_commit": head.stdout.strip() or None,
                "merged_commit": row["merged_commit_id"] or None,
            }

    def diff(
        self, repo: str, base: str | None, head: str | None
    ) -> tuple[list[dict[str, Any]], str, str | None]:
        """(files, patch, error) of ``base..head`` in one repository."""

        if not base or not head:
            return [], "", "merge base or head commit unknown"
        numstat = self.git(repo, "diff", "--numstat", "-M", base, head)
        if numstat.returncode:
            return [], "", numstat.stderr.strip()[:500] or "git diff failed"
        files = []
        for line in numstat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            if " => " in path:
                path = re.sub(r"\{[^{}]* => ([^{}]*)\}", r"\1", path)
                path = path.split(" => ")[-1]
            files.append(
                {
                    "path": path,
                    "additions": int(added) if added.isdigit() else None,
                    "deletions": int(deleted) if deleted.isdigit() else None,
                    "category": category(path),
                }
            )
        patch = self.git(repo, "diff", "-M", "--unified=3", base, head)
        return files, patch.stdout, None

    def issues_only(self) -> Iterator[dict[str, Any]]:
        for issue in self.issues.values():
            if issue["is_pull"]:
                continue
            yield {
                "repo": self.repos.get(issue["repo_id"], ""),
                "number": issue["index"],
                "title": issue["name"],
                "body": issue["content"],
                "author": self._user(issue["poster_id"]),
                "state": "closed" if issue["is_closed"] else "open",
                "created_at": _iso(issue["created_unix"]),
                "closed_at": _iso(issue["closed_unix"]),
            }

    def comments(self) -> Iterator[dict[str, Any]]:
        for row in self.db.execute("SELECT * FROM comment ORDER BY id"):
            repo, number, is_pull = self._issue_ref(row["issue_id"])
            force_push = None
            if row["type"] == 29:
                try:
                    force_push = int(
                        bool(json.loads(row["content"] or "{}").get("is_force_push"))
                    )
                except ValueError:
                    force_push = None
            yield {
                "id": row["id"],
                "repo": repo,
                "number": number,
                "is_pull": is_pull,
                "type": row["type"],
                "kind": COMMENT_KINDS.get(row["type"], f"type-{row['type']}"),
                "author": self._user(row["poster_id"]),
                "body": row["content"],
                "created_at": _iso(row["created_unix"]),
                "updated_at": _iso(row["updated_unix"]),
                "review_id": row["review_id"] or None,
                "force_push": force_push,
            }

    def reviews(self) -> Iterator[dict[str, Any]]:
        for row in self.db.execute("SELECT * FROM review ORDER BY id"):
            repo, number, _ = self._issue_ref(row["issue_id"])
            issue = self.issues.get(row["issue_id"])
            yield {
                "id": row["id"],
                "repo": repo,
                "number": number,
                "reviewer": self._user(row["reviewer_id"]),
                "pr_author": self._user(issue["poster_id"])
                if issue is not None
                else None,
                "kind": REVIEW_KINDS.get(row["type"], f"type-{row['type']}"),
                "body": row["content"],
                "created_at": _iso(row["created_unix"]),
            }

    def content_edits(self) -> Iterator[dict[str, Any]]:
        if not self._has("issue_content_history"):
            return
        posters = {
            row["id"]: row["poster_id"]
            for row in self.db.execute("SELECT id, poster_id FROM comment")
        }
        for row in self.db.execute("SELECT * FROM issue_content_history ORDER BY id"):
            repo, number, _ = self._issue_ref(row["issue_id"])
            issue = self.issues.get(row["issue_id"])
            if row["comment_id"]:
                author_id = posters.get(row["comment_id"])
            else:
                author_id = issue["poster_id"] if issue is not None else None
            yield {
                "id": row["id"],
                "repo": repo,
                "number": number,
                "comment_id": row["comment_id"] or None,
                "editor": self._user(row["poster_id"]),
                "content_author": self._user(author_id) if author_id else None,
                "edited_at": _iso(row["edited_unix"]),
                "is_first": int(bool(row["is_first_created"])),
                "is_deleted": int(bool(row["is_deleted"])),
            }

    def releases(self) -> Iterator[dict[str, Any]]:
        for row in self.db.execute("SELECT * FROM release ORDER BY id"):
            yield {
                "repo": self.repos.get(row["repo_id"], ""),
                "tag": row["tag_name"],
                "title": row["title"],
                "author": self._user(row["publisher_id"]),
                "sha": row["sha1"],
                "created_at": _iso(row["created_unix"]),
                "is_draft": int(bool(row["is_draft"])),
                "is_tag": int(bool(row["is_tag"])),
            }

    def ci_runs(self) -> Iterator[dict[str, Any]]:
        if not self._has("action_run"):
            return
        for row in self.db.execute("SELECT * FROM action_run ORDER BY id"):
            yield {
                "id": row["id"],
                "repo": self.repos.get(row["repo_id"], ""),
                "workflow": row["workflow_id"],
                "event": row["event"],
                "status": row["status"],
                "trigger_user": self._user(row["trigger_user_id"]),
                "commit_sha": row["commit_sha"],
                "created_at": _iso(row["created"]),
                "schedule_id": row["schedule_id"] or None,
            }

    def ci_schedules(self) -> Iterator[dict[str, Any]]:
        if not self._has("action_schedule"):
            return
        for row in self.db.execute("SELECT * FROM action_schedule ORDER BY id"):
            content = row["content"]
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            yield {
                "id": row["id"],
                "repo": self.repos.get(row["repo_id"], ""),
                "workflow": row["workflow_id"],
                "created_at": _iso(row["created"]),
                "content": content,
            }
