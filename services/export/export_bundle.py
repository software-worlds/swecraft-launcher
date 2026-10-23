#!/usr/bin/env python3
"""
Export one society checkpoint as a sim-browser bundle (the protocol is sim-browser's
`services/sim-browser/src/lib/bundle/protocol.ts`; see docs/reference/export.md).

    python3 services/export/export_bundle.py \\
        --evidence <analysis dir> --checkpoint <checkpoint dir> \\
        --world <layout.json> --id my-run --label "my run" \\
        --out bundles

The inputs, all read-only:

- the analysis directory the hub's trace analyzer writes: `evidence.db`, and
  the forge it unpacked under `forge/` (Forgejo's database and the bare
  repositories);
- the checkpoint, for each run's receipt (`result.json`), transcript
  (`trajectory.json`, and for kimi-cli the session log `session/**/wire.jsonl`),
  prompt (`input/prompt.md`), final response and changed files, and the
  coordinator's agent-to-repository bindings;
- a world fixture, for the layout the map draws.

A session's steps are its trajectory's, every step and text whole, with the
metrics each records. kimi-cli's trajectory records neither the prompt nor
any time; its session log's are used. The evidence's tool calls stand in only
for a session whose checkpoint holds no readable trajectory, and the
session's file says which of the two its steps come from.

What the agents changed in each package is read with `git` from the forge's
bare repositories, from the seed commit to the head (`repo_code.py`); every
branch, the files of each branch's head and their contents, and the agents'
charters and memory branches are read with them (`repo_git.py`).

Every file holds the fields the protocol lists, built one by one, and every
text passes the redactor. The checkpoint's token and credential files are
never opened, and Forgejo's database is read only from the columns named in
`ForgeDb`. The run's files are written beside the old ones and moved into
place together; files other tools keep in the run's directory (the
evaluation page's `evaluation.json`, the factsheet cards under
`audit/`) are left as they are. Then the run's entry in `index.json` is
added or updated.

Requires Python 3.12 or later; standard library only.
"""

import argparse
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zlib
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from events import Sessions, epoch, from_forge, from_ledger, stamp
from redact import Redactor, known_secrets
import repo_git
from repo_code import count, repo_code

INDEX_SCHEMA = "sim-browser/index-v1"
BUNDLE_SCHEMA = "sim-browser/bundle-v1"

# Every text is kept whole. A session's steps come from its trajectory, which
# holds every text whole. Only a session whose checkpoint holds no readable
# trajectory takes its tool calls from the analyzer's evidence, which keeps an
# output's first 3,000 and last 1,000 characters and cuts an input at 20,000;
# those texts are marked truncated. An event's summary is one line of its
# text, which the session's file keeps whole.
SUMMARY_LIMIT = 140

# The analyzer's mark in an output it shortened (`transcripts.trim`).
_TRIMMED = re.compile(r"\n\[\.\.\. \d+ characters trimmed \.\.\.\]\n")
_TRIMMED_AT = 3_000
# The analyzer cuts a call's input at this length without a mark (`transcripts.TEXT_LIMIT`).
_INPUT_CUT = 20_000

# A step's author, as the trajectory's `source` names it.
ROLES = ("system", "user", "agent")
# The argument holding a call's command or code, in this order: a shell
# command (`command`, `cmd`), Codex's code-mode JavaScript (`input`), the
# arguments kimi-cli could not parse (`raw`), a patch (`patch`).
INPUT_ARGUMENTS = ("command", "cmd", "input", "raw", "patch")
# The arguments naming the file a call works on, as the analyzer reads them (`transcripts._describe`).
PATH_ARGUMENTS = ("file_path", "path", "filename", "notebook_path")
# The message kimi-cli's trajectory gives a step whose message has no text.
KIMI_NO_TEXT = "(tool use)"
# How the export's report counts the sessions whose steps came from each source.
TRANSCRIPT_COUNTS = {
    "trajectory": "sessions from a trajectory",
    "evidence": "sessions from the evidence",
    None: "sessions without a transcript",
}

OUTCOMES = {"worked", "idle", "blocked"}
VERDICTS = {"approve": "approved", "request_changes": "changes requested", "comment": "commented", "pending": "pending"}
EVENT_WORDS = {
    "reopen": "reopened",
    "close": "closed",
    "merge_pull": "merged",
    "label": "changed the labels",
    "lock": "locked the conversation",
    "unlock": "unlocked the conversation",
    "review_request": "requested a review",
    "dismiss_review": "dismissed a review",
}
RECEIPT_FIELDS = ("image", "requestedRef", "resolvedSha", "inputTokens", "outputTokens", "cacheTokens")
# A tool's kind by its name, as the analyzer's table gives it (`transcripts.kind_of`).
# kimi-cli's file and web tools, which that table does not name, take the kinds
# of the same tools of other harnesses; Codex's code-mode `exec`, whose
# JavaScript the analyzer reads as shell text, is a shell call.
KINDS = {
    **dict.fromkeys(
        ("bash", "exec_command", "shell", "local_shell", "run_shell_command", "run_terminal_cmd", "terminal", "write_stdin", "exec"),
        "shell",
    ),
    **dict.fromkeys(
        ("edit", "multiedit", "apply_patch", "str_replace", "str_replace_editor", "replace", "edit_file", "notebookedit", "strreplacefile"),
        "edit",
    ),
    **dict.fromkeys(("write", "write_file", "create_file", "create", "writefile"), "write"),
    **dict.fromkeys(("read", "read_file", "view", "grep", "glob", "ls", "list_dir", "find", "readfile"), "read"),
    **dict.fromkeys(("webfetch", "websearch", "web_search", "fetch", "fetch_url", "browser", "fetchurl", "searchweb"), "web"),
}
_RUN_ID = re.compile(r"^(\d{8}T\d{6}Z)-")
_COMMIT_LINK = re.compile(r'<a href="[^"]*/commit/([0-9a-f]{7,64})"[^>]*>(.*?)</a>', re.S)


# ------------------------------------------------------------------ inputs


def read_only(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """A read-only handle; `immutable` for a file on a read-only mount, so no journal is touched."""

    if not path.is_file():
        raise SystemExit(f"no database at {path}")
    flags = "mode=ro&immutable=1" if immutable else "mode=ro"
    connection = sqlite3.connect(f"file:{path}?{flags}", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def has_table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Names:
    """Forge repository names (`hub/pytest`) to world names (`pytest`)."""

    def __init__(self, org: str, world: Iterable[str]) -> None:
        self.org, self.world = org, set(world)

    def __call__(self, forge_name: str | None) -> str | None:
        """The world's name for a forge repository, or None when it is not one of the world's."""

        if not forge_name:
            return None
        owner, _, name = forge_name.rpartition("/")
        if owner and owner != self.org:
            return None
        return name if name in self.world else None


# The evidence store's forge tables, by the columns the bundle uses.
EVIDENCE_QUERIES = {
    "pulls": "SELECT repo, number, title, body, author, state, merged, merger, merged_at, created_at, closed_at,"
    " base_branch, head_branch, additions, deletions, opened_run_id FROM pulls ORDER BY repo, number",
    "issues": "SELECT repo, number, title, body, author, state, created_at, closed_at FROM issues ORDER BY repo, number",
    "comments": "SELECT id, repo, number, is_pull, type, kind, author, body, created_at, review_id, force_push FROM comments ORDER BY id",
    "reviews": "SELECT id, repo, number, reviewer, kind, body, created_at FROM reviews ORDER BY id",
    "releases": "SELECT repo, tag, title, author, sha, created_at, is_draft, is_tag FROM releases",
    "pull_files": "SELECT repo, number, path, additions, deletions FROM pull_files",
    "pull_patches": "SELECT repo, number, patch, truncated FROM pull_patches",
    "repos": "SELECT repo, seed, head FROM repos",
    "forge_actions": "SELECT run_id, agent, ts, repo, action, number, metadata FROM forge_actions ORDER BY id",
}


class ForgeDb:
    """Forgejo's database, read from these columns only:

    repository: id, owner_name, lower_name, description, default_branch
    issue: id, repo_id, index, is_pull
    comment: id, type, ref_issue_id, old_title, new_title, old_ref, new_ref
    release: repo_id, tag_name, note
    """

    # Comment types whose timeline words need columns the evidence store does
    # not copy: references (3, 5, 6), a title change (10), a deleted branch
    # (11) and a new base branch (25).
    DETAIL_TYPES = (3, 5, 6, 10, 11, 25)

    def __init__(self, path: Path) -> None:
        db = read_only(path)
        try:
            self.repos = {
                row["id"]: dict(row)
                for row in db.execute("SELECT id, owner_name, lower_name, description, default_branch FROM repository")
            }
            issues = {
                row["id"]: (self.name(row["repo_id"]), row["index"], bool(row["is_pull"]))
                for row in db.execute('SELECT id, repo_id, "index", is_pull FROM issue')
            }
            marks = ",".join("?" for _ in self.DETAIL_TYPES)
            self.details = {}
            for row in db.execute(
                f"SELECT id, ref_issue_id, old_title, new_title, old_ref, new_ref FROM comment WHERE type IN ({marks})",
                self.DETAIL_TYPES,
            ):
                ref = issues.get(row["ref_issue_id"])
                self.details[row["id"]] = {**dict(row), "ref": ref if ref and ref[0] else None}
            self.notes = {
                (self.name(row["repo_id"]), row["tag_name"]): row["note"] or None
                for row in db.execute("SELECT repo_id, tag_name, note FROM release")
            }
        finally:
            db.close()

    def name(self, repo_id: Any) -> str | None:
        repo = self.repos.get(repo_id)
        return f"{repo['owner_name']}/{repo['lower_name']}" if repo else None

    def repository(self, forge_name: str) -> dict[str, Any] | None:
        return next((repo for repo_id, repo in self.repos.items() if self.name(repo_id) == forge_name), None)


def commit_subjects(git_dir: Path, shas: list[str]) -> dict[str, str]:
    """sha → the commit's subject line, for the commits a bare repository holds."""

    if not shas or not git_dir.is_dir():
        return {}
    done = subprocess.run(
        ["git", "-C", str(git_dir), "cat-file", "--batch"],
        input=("\n".join(shas) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    subjects, data, at = {}, done.stdout, 0
    while at < len(data):
        end = data.find(b"\n", at)
        if end < 0:
            break
        header = data[at:end].decode(errors="replace").split()
        at = end + 1
        if len(header) != 3:
            # "<sha> missing": the repository does not hold it.
            continue
        sha, kind, size = header[0], header[1], int(header[2])
        body = data[at : at + size]
        at += size + 1
        if kind == "commit":
            message = body.split(b"\n\n", 1)[1] if b"\n\n" in body else b""
            subjects[sha] = message.decode(errors="replace").split("\n", 1)[0].strip()
    return subjects


def pushed_commits(comment: dict[str, Any]) -> tuple[list[str], bool]:
    """(the commit ids a push record lists, oldest first; whether it was forced)."""

    try:
        pushed = json.loads(comment["body"] or "{}")
    except ValueError:
        pushed = {}
    if not isinstance(pushed, dict):
        pushed = {}
    shas = [sha for sha in pushed.get("commit_ids") or [] if isinstance(sha, str)]
    return shas, bool(pushed.get("is_force_push") or comment["force_push"])


def receipt_of(output: Path) -> dict[str, Any]:
    """The allow-listed fields of a run's `result.json`."""

    raw = read_json(output / "result.json")
    if not isinstance(raw, dict):
        return {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}

    def number(value: Any) -> int | float | None:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def text(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    return {
        "status": text(raw.get("status")),
        "harness": text(raw.get("agent")),
        "model": text(raw.get("model")),
        # Only to find the agent of a run the ledger did not book; never written.
        "repository": text(raw.get("repository")),
        # The session's error when the ledger has none for it, written scrubbed.
        "error": text(raw.get("error")),
        "image": text(raw.get("image")),
        "requestedRef": text(raw.get("requested_ref")),
        "resolvedSha": text(raw.get("resolved_sha")),
        "inputTokens": number(usage.get("input_tokens")),
        "outputTokens": number(usage.get("output_tokens")),
        "cacheTokens": number(usage.get("cache_tokens")),
    }


# A `timestamp` field in a harness's session log: epoch seconds or
# milliseconds, or an ISO-8601 string.
_LOG_TIME = re.compile(r'"timestamp"\s*:\s*(?:(\d{10,13}(?:\.\d+)?)(?![\d.])|"([^"]{10,40})")')


def log_time(value: Any) -> str | None:
    """A session log's time (epoch seconds or milliseconds, or an ISO-8601 string) to the second; None when unreadable."""

    if isinstance(value, str):
        return stamp(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_step_time(output: Path) -> str | None:
    """When the run's transcript recorded its last step.

    The trajectory's step times when it has them. kimi-cli's trajectory has
    none; its session log (`session/**/*.jsonl`) records each message's time,
    so the latest time there stands in.
    """

    trajectory = read_json(output / "trajectory.json")
    steps = trajectory.get("steps") if isinstance(trajectory, dict) else None
    times = [stamp(step.get("timestamp")) for step in steps or [] if isinstance(step, dict)]
    latest = max((t for t in times if t), default=None)
    if latest is not None or not (output / "session").is_dir():
        return latest
    for path in sorted((output / "session").rglob("*.jsonl")):
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                for number, text in _LOG_TIME.findall(line):
                    value = log_time(float(number)) if number else stamp(text)
                    if value is not None and (latest is None or value > latest):
                        latest = value
    return latest


def bindings(checkpoint: Path, society: list[str]) -> tuple[dict[str, list[str]], str]:
    """(agent → the repositories it may write to, its own first; where they came from).

    The coordinator's `agent_repos` table when the checkpoint has one, else
    the roster's naming rule: the agent of repository `x` is `x-bot`. Of an
    agent's bound repositories, its own is the one its name is made from.
    """

    db_path = checkpoint / "state" / "coordinator" / "coordinator.db"
    rows: list[tuple[str, str]] = []
    if db_path.is_file():
        db = read_only(db_path, immutable=True)
        try:
            if has_table(db, "agent_repos"):
                rows = [(str(r["agent"]), str(r["repo"]).rsplit("/", 1)[-1]) for r in db.execute("SELECT agent, repo FROM agent_repos")]
        finally:
            db.close()
    if not rows:
        return {f"{repo}-bot": [repo] for repo in society}, "the <repo>-bot naming rule"
    bound: dict[str, list[str]] = defaultdict(list)
    for agent, repo in rows:
        if repo in society and repo not in bound[agent]:
            bound[agent].append(repo)
    for agent, repos in bound.items():
        own = agent.removesuffix("-bot")
        if own in repos:
            repos.insert(0, repos.pop(repos.index(own)))
    return dict(bound), "the coordinator's agent_repos table"


def world_of(fixture: dict[str, Any]) -> dict[str, Any]:
    """The protocol's `BundleWorld` from a fixture's `world` block."""

    raw = fixture.get("world")
    if not isinstance(raw, dict) or not isinstance(raw.get("repos"), list):
        raise SystemExit("the world fixture has no world.repos")
    return {
        "id": str(raw["id"]),
        "label": str(raw.get("label") or raw["id"]),
        "org": str(raw.get("org") or "hub"),
        "repos": [
            {
                "name": str(repo["name"]),
                "upstream": repo["upstream"] if isinstance(repo.get("upstream"), str) else None,
                "hex": [int(repo["hex"][0]), int(repo["hex"][1])],
                "languages": {str(k): int(v) for k, v in (repo.get("languages") or {}).items()},
                "heldOut": repo.get("held_out") is True,
                "landmark": repo.get("landmark") or None,
            }
            for repo in raw["repos"]
        ],
        "deps": [[str(a), str(b)] for a, b in raw.get("deps") or []],
        "heldOutReach": {str(k): [str(x) for x in v] for k, v in (raw.get("held_out_reach") or {}).items()},
    }


# -------------------------------------------------------------------- text

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SKIP_LINE = re.compile(r"^(?:#{1,6}(?:\s|$)|```|~~~|(?:[-*_]\s*){3,}$)")
_MARKER = re.compile(r"^(?:[-*+>]\s+|\d+[.)]\s+)+")


def shorten(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters, at a space when one is near the cut."""

    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit - 30:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "…"


def first_line(text: str | None, limit: int = SUMMARY_LIMIT) -> str | None:
    """The first paragraph of prose on one line, cut at `limit`.

    Headings, code fences, rules and markup comments are skipped. A paragraph
    wrapped over several lines is joined; it ends at a blank line, a skipped
    line or the next list item.
    """

    if not text:
        return None
    words: list[str] = []
    for raw in _HTML_COMMENT.sub("", text).splitlines():
        line = raw.strip()
        if not line or _SKIP_LINE.match(line) or (words and _MARKER.match(line)):
            if words:
                break
            continue
        words.extend(_MARKER.sub("", line).split())
        if sum(len(word) + 1 for word in words) > limit:
            break
    return shorten(" ".join(words), limit) if words else None


def event_words(comment: dict[str, Any], detail: dict[str, Any] | None, names: Names, redactor: Redactor) -> str:
    """What a forge record in a thread's timeline says happened, in words."""

    kind, detail = comment["kind"], detail or {}
    if kind in ("issue_ref", "pull_ref", "comment_ref"):
        if not detail.get("ref"):
            return "referenced from another thread"
        repo, number, pull = detail["ref"]
        where = f"{names(repo) or repo}#{number}"
        if kind == "comment_ref":
            return f"referenced in a comment on {where}"
        return f"referenced from {'pull request' if pull else 'issue'} {where}"
    if kind == "commit_ref":
        link = _COMMIT_LINK.search(comment["body"] or "")
        if link is None:
            return "referenced in a commit"
        subject = redactor.line(html.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip())
        return f"referenced in commit {link.group(1)[:7]}" + (f": {subject}" if subject else "")
    if kind == "change_title":
        old, new = redactor.line(detail.get("old_title")), redactor.line(detail.get("new_title"))
        return f"changed the title from “{old}” to “{new}”" if old and new else "changed the title"
    if kind == "delete_branch":
        return f"deleted the branch {detail['old_ref']}" if detail.get("old_ref") else "deleted the branch"
    if kind == "change_target_branch":
        if detail.get("old_ref") and detail.get("new_ref"):
            return f"changed the base branch from {detail['old_ref']} to {detail['new_ref']}"
        return "changed the base branch"
    return EVENT_WORDS.get(kind, kind.replace("_", " "))


# ------------------------------------------------------------------ output


def file_name(name: str) -> str:
    """A URL path segment, as `encodeURIComponent` writes it."""

    return quote(name, safe="-_.!~*'()")


def dump(path: Path, value: Any) -> int:
    """Write compact JSON; the file's size in bytes."""

    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(data)
    return len(data)


def git_head(path: Path) -> str | None:
    done = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def most_common(values: Iterable[str | None]) -> str | None:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else None


def tool_kind(tool: str) -> str:
    """A tool's kind by its name: shell, edit, write, read, web or other."""

    return KINDS.get(tool.lower(), "other")


def call_kind(tool: str, kind: str | None) -> str | None:
    """An evidence row's kind: the analyzer's, unless it said "other" for a tool `KINDS` names."""

    if kind == "other":
        return tool_kind(tool)
    return kind or None


# ------------------------------------------------------------- transcripts


def read_trajectory(output: Path) -> dict[str, Any] | None:
    """The run's ATIF trajectory, or None when the checkpoint holds none the exporter can read.

    Bytes that are not UTF-8 read as U+FFFD, as the analyzer reads them.
    """

    try:
        trajectory = json.loads((output / "trajectory.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return trajectory if isinstance(trajectory, dict) and isinstance(trajectory.get("steps"), list) else None


def content_text(value: Any, notes: Counter[str]) -> str | None:
    """A message's or a result's text: the string, or the texts of a list of content parts, joined.

    A part that is not text (an image) has no place in the protocol's `Text`
    and is counted in `notes`.
    """

    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    texts = [part["text"] for part in value if isinstance(part, dict) and isinstance(part.get("text"), str)]
    if len(texts) < len(value):
        notes["content parts that are not text"] += len(value) - len(texts)
    return "\n".join(texts)


def trajectory_call(call: dict[str, Any], output: str | None, redactor: Redactor) -> dict[str, Any]:
    """One tool call: its command or code as `input`, each other argument by name, and its output, all whole."""

    tool = str(call.get("function_name") or call.get("name") or "?")
    arguments = call.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {"input": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}
    key = next((name for name in INPUT_ARGUMENTS if isinstance(arguments.get(name), str)), None)
    path = next((arguments[name] for name in PATH_ARGUMENTS if isinstance(arguments.get(name), str) and arguments[name]), None)
    return {
        "tool": tool,
        "kind": tool_kind(tool),
        "input": redactor.text(arguments[key]) if key else None,
        "arguments": [
            {
                "name": str(name),
                "value": redactor.text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)),
            }
            for name, value in arguments.items()
            if name != key
        ],
        "path": redactor.line(path),
        "output": redactor.text(output),
    }


def converter(trajectory: dict[str, Any]) -> str | None:
    """The harness whose converter wrote a trajectory (`agent.name`)."""

    agent = trajectory.get("agent")
    return agent["name"] if isinstance(agent, dict) and isinstance(agent.get("name"), str) else None


def trajectory_steps(trajectory: dict[str, Any], redactor: Redactor, notes: Counter[str]) -> list[dict[str, Any]]:
    """A session's steps from its ATIF trajectory, in the trajectory's order, every text whole.

    A step keeps the number the analyzer gives it (`transcripts.from_atif`):
    its `step_id`, else its position in the list, so the evidence's and the
    factsheet's references to steps still hold; a trajectory whose numbers do
    not increase is numbered by position. Each call takes the result naming
    its id; calls that share an id take the results naming it in order.
    Anything the protocol cannot hold is counted in `notes`.
    """

    kimi = converter(trajectory) == "kimi-cli"
    entries = [(index, step) for index, step in enumerate(trajectory["steps"]) if isinstance(step, dict)]
    if len(entries) < len(trajectory["steps"]):
        notes["steps that are not objects"] += len(trajectory["steps"]) - len(entries)
    numbers = [step["step_id"] if isinstance(step.get("step_id"), int) else index + 1 for index, step in entries]
    if any(later <= earlier for earlier, later in zip(numbers, numbers[1:])):
        notes["trajectories numbered by position"] += 1
        numbers = list(range(1, len(entries) + 1))
    steps = []
    for number, (_, step) in zip(numbers, entries):
        role = step.get("source")
        if role not in ROLES:
            notes["steps without a known source, taken as the agent's"] += 1
            role = "agent"
        message = content_text(step.get("message"), notes)
        if kimi and message == KIMI_NO_TEXT:
            message = None
        reasoning = step.get("reasoning_content")
        results: defaultdict[str, deque[str | None]] = defaultdict(deque)
        observation = step.get("observation")
        for result in (observation.get("results") if isinstance(observation, dict) else None) or []:
            if isinstance(result, dict):
                results[str(result.get("source_call_id") or "")].append(content_text(result.get("content"), notes))
        calls = []
        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict):
                notes["tool calls that are not objects"] += 1
                continue
            waiting = results.get(str(call.get("tool_call_id") or ""))
            calls.append(trajectory_call(call, waiting.popleft() if waiting else None, redactor))
        left = sum(len(waiting) for waiting in results.values())
        if left:
            notes["results that name no call"] += left
        model = step.get("model_name")
        steps.append(
            {
                "step": number,
                "role": role,
                "at": stamp(step.get("timestamp")),
                "model": model if isinstance(model, str) and model else None,
                "message": redactor.text(message) if message else None,
                "reasoning": redactor.text(reasoning) if isinstance(reasoning, str) and reasoning else None,
                "calls": calls,
                "metrics": metric_values(step["metrics"], redactor, notes) if isinstance(step.get("metrics"), dict) else None,
            }
        )
    return steps


def metric_values(value: Any, redactor: Redactor, notes: Counter[str]) -> Any:
    """A step's metrics as the trajectory records them, their texts redacted.

    A number JSON cannot hold (NaN, an infinity) becomes null and is counted in `notes`.
    """

    if isinstance(value, dict):
        return {str(name): metric_values(item, redactor, notes) for name, item in value.items()}
    if isinstance(value, list):
        return [metric_values(item, redactor, notes) for item in value]
    if isinstance(value, str):
        return redactor.scrub(value)[0]
    if isinstance(value, float) and not math.isfinite(value):
        notes["metric values that are not finite numbers"] += 1
        return None
    return value


def kimi_session_log(output: Path, notes: Counter[str]) -> tuple[str | None, list[str | None]]:
    """(the user's input, the time each step began) from the session log of a kimi-cli run.

    A run keeps a log (`session/**/wire.jsonl`) for its own session and one for
    each subagent it starts; its own begins first. The log records the user's
    input when the turn begins (`TurnBegin`) and a time when each step begins
    (`StepBegin`); a subagent's records inside it (`SubagentEvent`) are not the
    session's steps.
    """

    logs = []
    for path in sorted((output / "session").rglob("wire.jsonl")):
        records = []
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("message"), dict):
                    records.append(record)
        # When the log begins, in seconds: kimi-cli writes epoch seconds, some logs milliseconds.
        numbers = [t for t in (r.get("timestamp") for r in records) if isinstance(t, (int, float)) and not isinstance(t, bool)]
        begins = [t / (1000 if t > 1e11 else 1) for t in numbers]
        if records:
            logs.append((min(begins, default=math.inf), str(path), records))
    if not logs:
        return None, []
    records = min(logs)[2]
    user_input, times = None, []
    for record in records:
        message = record["message"]
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        if message.get("type") == "TurnBegin" and user_input is None:
            user_input = content_text(payload.get("user_input"), notes)
        elif message.get("type") == "StepBegin":
            times.append(log_time(record.get("timestamp")))
    return user_input or None, times


def public(record: dict[str, Any]) -> dict[str, Any]:
    """A record without the keys kept beside it for the detail files (those starting with `_`)."""

    return {k: v for k, v in record.items() if not k.startswith("_")}


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event[key] for key in ("t", "session", "agent", "repo", "action", "ref", "tag", "summary")}


class Export:
    """One export: `load` reads the inputs, `core`, `threads`, `code` and `sessions` build the files."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.redactor = Redactor(self.operator_secrets())
        self.skipped: Counter[str] = Counter()
        self.sizes: dict[str, list[int]] = defaultdict(list)
        self.code_counts: Counter[str] = Counter()
        # Where the sessions' steps came from, and what the protocol could not hold.
        self.transcripts: Counter[str] = Counter()
        # kimi-cli sessions whose session log has another number of steps than the trajectory: their steps have no times.
        self.untimed: list[str] = []

    def operator_secrets(self) -> list[str]:
        """The values of `--known-secrets`, a file the operator prepares outside the checkpoint."""

        given = getattr(self.args, "known_secrets", None)
        if not given:
            return []
        path, checkpoint = Path(given), Path(self.args.checkpoint)
        # Checkpoint files can be links into a store elsewhere, so both the
        # path as given and the path it resolves to are compared.
        paths = {Path(os.path.abspath(path)), path.resolve()}
        roots = {Path(os.path.abspath(checkpoint)), checkpoint.resolve()}
        if any(p.is_relative_to(root) for p in paths for root in roots):
            raise SystemExit("--known-secrets must name a file the operator prepares, outside the checkpoint")
        return known_secrets(path)

    def run(self) -> dict[str, Any]:
        self.load()
        out = Path(self.args.out)
        out.mkdir(parents=True, exist_ok=True)
        building = out / f".{self.args.id}.building"
        if building.exists():
            shutil.rmtree(building)
        (building / "threads").mkdir(parents=True)
        (building / "sessions").mkdir()
        (building / "code").mkdir()
        # The sessions' files first: each counts its session's tool calls for `core.json`.
        for session in self.session_records:
            self.sizes["sessions"].append(dump(building / "sessions" / f"{file_name(session['id'])}.json", self.session(session)))
        self.evidence.close()
        core = self.core()
        self.sizes["core"].append(dump(building / "core.json", core))
        for repo in core["repos"]:
            name = file_name(repo["name"])
            self.sizes["threads"].append(dump(building / "threads" / f"{name}.json", self.threads(repo["name"])))
            self.sizes["code"].append(dump(building / "code" / f"{name}.json", self.code(repo)))
        self.git_report = repo_git.build(building, core, Path(self.args.evidence), self.checkpoint, self.redactor)
        swap(building, out / self.args.id)
        update_index(out / "index.json", core, self.index_entry(core), self.args.group)
        return self.report(core)

    # ---------------------------------------------------------------- load

    def load(self) -> None:
        args = self.args
        evidence_dir, self.checkpoint = Path(args.evidence), Path(args.checkpoint)
        fixture = read_json(Path(args.world))
        if not isinstance(fixture, dict):
            raise SystemExit(f"cannot read the world fixture {args.world}")
        self.fixture = fixture
        self.world = world_of(fixture)
        manifest = read_json(self.checkpoint / "manifest.json")
        if not isinstance(manifest, dict):
            raise SystemExit(f"not a society checkpoint (no manifest.json): {self.checkpoint}")
        self.manifest = manifest
        org = self.world["org"]
        society = [repo["name"] for repo in self.world["repos"] if not repo["heldOut"]]
        self.names = Names(org, society)

        self.forge = ForgeDb(evidence_dir / "forge" / "data" / "forgejo.db")
        self.git_root = evidence_dir / "forge" / "git" / "repositories" / org
        self.evidence = read_only(evidence_dir / "evidence.db")
        self.records: dict[str, list[dict[str, Any]]] = {}
        for table, sql in EVIDENCE_QUERIES.items():
            rows = []
            for row in self.evidence.execute(sql):
                item = dict(row)
                item["repo"] = self.names(item["repo"])
                if item["repo"] is None:
                    self.skipped[f"{table} rows outside the world"] += 1
                    continue
                rows.append(item)
            self.records[table] = rows
        for release in self.records["releases"]:
            release["notes"] = self.forge.notes.get((f"{org}/{release['repo']}", release["tag"]))
        # Forgejo stores a review's text again as a comment. When a review is
        # submitted twice, the review keeps the later text and the earlier one
        # survives only as a comment, so only a comment repeating its review's
        # text is left out of the timeline.
        review_texts = {review["id"]: review["body"] or "" for review in self.records["reviews"]}
        self.echoes = {
            comment["id"]
            for comment in self.records["comments"]
            if comment["kind"] == "review" and review_texts.get(comment["review_id"]) == (comment["body"] or "")
        }

        self.society = [repo for repo in society if self.forge.repository(f"{org}/{repo}") is not None]
        self.bound, self.bound_from = bindings(self.checkpoint, self.society)
        self.home = {agent: repos[0] for agent, repos in self.bound.items() if repos}
        wakes = [
            dict(row)
            for row in self.evidence.execute(
                "SELECT wake_id, run_id, agent, started, finished, status, outcome, summary, cost_usd, error,"
                " harness, model, tool_calls, response FROM wakes ORDER BY run_id"
            )
        ]
        self.session_records = self.read_sessions(wakes)
        self.running = Sessions((s["id"], s["agent"], s["started"], s["finished"]) for s in self.session_records)

        start = min((s["started"] for s in self.session_records), default=None)
        if self.records["forge_actions"]:
            self.events_from = "ledger"
            self.events = from_ledger(self.records["forge_actions"], self.ledger_texts())
        else:
            self.events_from = "forge"
            self.events, skipped = from_forge(self.records, set(self.home), self.running, start)
            self.skipped.update(skipped)
        # An action is an agent's, and belongs to a session the bundle lists.
        self.listed = {s["id"] for s in self.session_records}
        kept = []
        for event in self.events:
            if event["agent"] not in self.home:
                self.skipped[f"{event['action']} by a user who is not an agent"] += 1
                continue
            if event["session"] not in self.listed:
                event["session"] = None
            event["text"] = self.redactor.text(event["text"])
            event["summary"] = first_line(event["text"]["text"]) if event["text"] else None
            kept.append(event)
        self.events = kept
        self.by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in self.events:
            if event["session"]:
                self.by_session[event["session"]].append(event)

    def read_sessions(self, wakes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Session records, with what a detail file needs kept beside them under `_` keys."""

        owner_of = {repo: agent for agent, repo in self.home.items()}
        # A run the ledger did not book is a bootstrap only when there is a ledger.
        ledgered = any(wake["wake_id"] is not None for wake in wakes)
        records = []
        for wake in wakes:
            run_id = str(wake["run_id"])
            output = self.checkpoint / "runs" / run_id / "output"
            receipt = receipt_of(output)
            agent = wake["agent"]
            if not agent and receipt.get("repository"):
                agent = owner_of.get(receipt["repository"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
            if agent not in self.home:
                self.skipped["sessions of an agent the bindings do not name"] += 1
                continue
            started, finished = stamp(wake["started"]), stamp(wake["finished"])
            if started is None:
                match = _RUN_ID.match(run_id)
                if match:
                    started = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
                finished = finished or last_step_time(output)
            if started is None:
                self.skipped["sessions without a start time"] += 1
                continue
            response = wake["response"]
            if response is None and (output / "response.md").is_file():
                response = (output / "response.md").read_text(encoding="utf-8", errors="replace")
            records.append(
                {
                    "id": run_id,
                    "agent": agent,
                    "repo": self.home[agent],
                    "started": started,
                    "finished": finished,
                    "status": wake["status"] or receipt.get("status") or "unknown",
                    "outcome": wake["outcome"] if wake["outcome"] in OUTCOMES else None,
                    "bootstrap": ledgered and wake["wake_id"] is None,
                    "summary": self.redactor.line(wake["summary"]),
                    "costUsd": None if wake["cost_usd"] is None else round(wake["cost_usd"], 4),
                    "toolCalls": int(wake["tool_calls"] or 0),
                    "actions": 0,
                    "_harness": wake["harness"] or receipt.get("harness"),
                    "_model": wake["model"] or receipt.get("model"),
                    "_error": wake["error"] or receipt.get("error"),
                    "_response": response,
                    "_receipt": receipt,
                    "_output": output,
                }
            )
        records.sort(key=lambda s: (s["started"], s["id"]))
        return records

    def ledger_texts(self) -> dict[tuple[str, str, Any], str | None]:
        """What each ledger action carried, keyed as `events.from_ledger` looks it up."""

        texts: dict[tuple[str, str, Any], str | None] = {}
        for comment in self.records["comments"]:
            texts[("comment", comment["repo"], comment["id"])] = comment["body"]
        for issue in self.records["issues"]:
            texts[("issue", issue["repo"], issue["number"])] = issue["body"]
        for pull in self.records["pulls"]:
            texts[("pull", pull["repo"], pull["number"])] = pull["body"]
        for review in self.records["reviews"]:
            texts[("review", review["repo"], review["id"])] = review["body"]
        for release in self.records["releases"]:
            texts[("release", release["repo"], release["tag"])] = release["notes"]
        return texts

    # ---------------------------------------------------------------- core

    def core(self) -> dict[str, Any]:
        for session in self.session_records:
            session["actions"] = len(self.by_session.get(session["id"], []))
        self.thread_records = self.read_threads()
        self.release_records = self.read_releases()
        return {
            "schema": BUNDLE_SCHEMA,
            "id": self.args.id,
            "label": self.args.label,
            "source": {
                "checkpoint": str(self.manifest.get("checkpoint_id") or self.checkpoint.name),
                "captured": stamp(self.manifest.get("created_at")),
                "hubCommit": self.manifest["hub_commit"] if isinstance(self.manifest.get("hub_commit"), str) else None,
                "exporter": git_head(Path(__file__).resolve().parent),
                "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "events": self.events_from,
            },
            "run": self.run_summary(),
            "world": self.world,
            "agents": self.agents(),
            "sessions": [public(s) for s in self.session_records],
            "events": [public_event(e) for e in self.events],
            "repos": self.repos(),
            "threads": [public(t) for t in self.thread_records],
            "releases": [public(r) for r in self.release_records],
        }

    def run_summary(self) -> dict[str, Any]:
        sessions = self.session_records
        finished = [s["finished"] for s in sessions if s["finished"]]
        seconds = sum(epoch(s["finished"]) - epoch(s["started"]) for s in sessions if s["finished"])
        costs = [s["costUsd"] for s in sessions if s["costUsd"] is not None]
        return {
            "started": min((s["started"] for s in sessions), default=None),
            "finished": max(finished, default=None),
            "sessions": len(sessions),
            "hours": round(seconds / 3600, 2),
            "costUsd": round(sum(costs), 2) if costs else None,
        }

    def agents(self) -> list[dict[str, Any]]:
        avatar = self.avatars()
        mine: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for session in self.session_records:
            mine[session["agent"]].append(session)
        order = {repo: i for i, repo in enumerate(r["name"] for r in self.world["repos"])}
        out = []
        for name in sorted(self.home, key=lambda agent: (order.get(self.home[agent], len(order)), agent)):
            sessions = mine.get(name, [])
            costs = [s["costUsd"] for s in sessions if s["costUsd"] is not None]
            out.append(
                {
                    "name": name,
                    "repo": self.home[name],
                    "writeRepos": self.bound[name],
                    "avatar": avatar(name),
                    "harness": most_common(s["_harness"] for s in sessions),
                    "model": most_common(s["_model"] for s in sessions),
                    "sessions": len(sessions),
                    "worked": sum(1 for s in sessions if s["outcome"] == "worked"),
                    "costUsd": round(sum(costs), 2) if costs else None,
                }
            )
        return out

    def avatars(self) -> Callable[[str], str | None]:
        """The fixture's portrait for an agent it names, else one picked by a hash of the name."""

        run = self.fixture.get("run") if isinstance(self.fixture.get("run"), dict) else {}
        chosen = {a["name"]: a["avatar"] for a in run.get("agents") or [] if isinstance(a, dict) and isinstance(a.get("avatar"), str)}
        portraits = sorted(path.stem for path in Path(self.args.avatars).glob("*.png"))

        def pick(name: str) -> str | None:
            if name in chosen:
                return chosen[name]
            return portraits[zlib.crc32(name.encode()) % len(portraits)] if portraits else None

        return pick

    def read_threads(self) -> list[dict[str, Any]]:
        comments = Counter(
            (c["repo"], c["number"])
            for c in self.records["comments"]
            if c["kind"] in ("comment", "code") or (c["kind"] == "review" and c["id"] not in self.echoes)
        )
        threads = []
        for pull in self.records["pulls"]:
            threads.append(self.thread_summary(pull, True, comments, pull["opened_run_id"]))
        for issue in self.records["issues"]:
            threads.append(self.thread_summary(issue, False, comments, None))
        threads.sort(key=lambda t: (t["repo"], t["number"]))
        return threads

    def thread_summary(self, record: dict[str, Any], pull: bool, comments: Counter, opened: str | None) -> dict[str, Any]:
        created = stamp(record["created_at"])
        state = "open" if record["state"] == "open" else "closed"
        session = opened or (self.running.at(record["author"], created) if created else None)
        return {
            "repo": record["repo"],
            "number": record["number"],
            "pull": pull,
            "title": self.redactor.line(record["title"]) or f"#{record['number']}",
            "state": state,
            "merged": bool(record.get("merged")),
            "author": record["author"],
            "created": created,
            "closed": stamp(record["closed_at"]) if state == "closed" else None,
            "comments": comments[(record["repo"], record["number"])],
            # As an event's: the run that opened it may be one the bundle leaves out.
            "session": session if session in self.listed else None,
            "_record": record,
        }

    def read_releases(self) -> list[dict[str, Any]]:
        """The releases kept as events: published by an agent during the run."""

        kept = {(e["repo"], e["tag"]): e for e in self.events if e["action"] == "release.create"}
        releases = []
        for release in self.records["releases"]:
            event = kept.get((release["repo"], release["tag"]))
            if event is None:
                continue
            releases.append(
                {
                    "repo": release["repo"],
                    "tag": release["tag"],
                    "title": self.redactor.line(release["title"]),
                    "author": release["author"],
                    "created": event["t"],
                    "session": event["session"],
                    "_sha": release["sha"] or None,
                    "_notes": release["notes"],
                }
            )
        releases.sort(key=lambda r: (r["created"], r["repo"], r["tag"]))
        return releases

    def repos(self) -> list[dict[str, Any]]:
        seeds = {row["repo"]: row for row in self.records["repos"]}
        out = []
        for name in self.society:
            forge = self.forge.repository(f"{self.world['org']}/{name}") or {}
            threads = [t for t in self.thread_records if t["repo"] == name]
            issues = [t for t in threads if not t["pull"]]
            pulls = [t for t in threads if t["pull"]]
            out.append(
                {
                    "name": name,
                    "description": self.redactor.line(forge.get("description")),
                    "defaultBranch": forge.get("default_branch") or "main",
                    "seed": (seeds.get(name) or {}).get("seed") or None,
                    "head": (seeds.get(name) or {}).get("head") or None,
                    "issues": {
                        "open": sum(1 for t in issues if t["state"] == "open"),
                        "closed": sum(1 for t in issues if t["state"] == "closed"),
                    },
                    "pulls": {
                        "open": sum(1 for t in pulls if t["state"] == "open"),
                        "merged": sum(1 for t in pulls if t["merged"]),
                        "closed": sum(1 for t in pulls if t["state"] == "closed" and not t["merged"]),
                    },
                    "releases": sum(1 for r in self.release_records if r["repo"] == name),
                }
            )
        return out

    # ------------------------------------------------------------- threads

    def threads(self, repo: str) -> dict[str, Any]:
        """`threads/<repo>.json`."""

        on_repo = lambda rows: [row for row in rows if row["repo"] == repo]
        comments, reviews, files = defaultdict(list), defaultdict(list), defaultdict(list)
        for comment in on_repo(self.records["comments"]):
            comments[comment["number"]].append(comment)
        for review in on_repo(self.records["reviews"]):
            reviews[review["number"]].append(review)
        for row in on_repo(self.records["pull_files"]):
            files[row["number"]].append({"path": row["path"], "additions": row["additions"], "deletions": row["deletions"]})
        patches = {row["number"]: row for row in on_repo(self.records["pull_patches"])}
        shas = sorted({sha for c in on_repo(self.records["comments"]) if c["kind"] == "pull_push" for sha in pushed_commits(c)[0]})
        subjects = commit_subjects(self.git_root / f"{repo}.git", shas)

        threads = []
        for summary in (t for t in self.thread_records if t["repo"] == repo):
            record, number = summary["_record"], summary["number"]
            pull = record if summary["pull"] else None
            merged = bool(pull and pull["merged"])
            patch = patches.get(number)
            threads.append(
                {
                    **public(summary),
                    "body": self.redactor.text(record["body"]) if record["body"] else None,
                    "merger": pull["merger"] if merged else None,
                    "mergedAt": stamp(pull["merged_at"]) if merged else None,
                    "base": pull["base_branch"] if pull else None,
                    "head": pull["head_branch"] if pull else None,
                    "additions": pull["additions"] if pull else None,
                    "deletions": pull["deletions"] if pull else None,
                    "timeline": self.timeline(comments[number], reviews[number], subjects),
                    "files": files[number],
                    "patch": self.redactor.text(patch["patch"], trimmed=bool(patch["truncated"]))
                    if patch and patch["patch"]
                    else None,
                }
            )
        releases = [
            {
                **public(release),
                "sha": release["_sha"],
                "notes": self.redactor.text(release["_notes"]) if release["_notes"] else None,
            }
            for release in self.release_records
            if release["repo"] == repo
        ]
        return {"schema": BUNDLE_SCHEMA, "repo": repo, "threads": threads, "releases": releases}

    def timeline(self, comments: list[dict[str, Any]], reviews: list[dict[str, Any]], subjects: dict[str, str]) -> list[dict[str, Any]]:
        """A thread's comments, reviews, pushes and the forge's own records, in order of time."""

        entries: list[tuple[str, int, dict[str, Any]]] = []
        for review in reviews:
            at, verdict = stamp(review["created_at"]), VERDICTS.get(review["kind"])
            if at is None or verdict is None:
                continue
            body = self.redactor.text(review["body"]) if review["body"] else None
            entries.append((at, 0, {"type": "review", "id": review["id"], "author": review["reviewer"], "at": at, "verdict": verdict, "body": body}))
        for comment in comments:
            at, kind = stamp(comment["created_at"]), comment["kind"]
            if at is None or comment["id"] in self.echoes:
                continue
            if kind in ("comment", "code", "review"):
                body = self.redactor.text(comment["body"] or "")
                entry = {"type": "comment", "id": comment["id"], "author": comment["author"], "at": at, "body": body}
            elif kind == "pull_push":
                shas, force = pushed_commits(comment)
                commits = [{"sha": sha, "subject": self.redactor.line(subjects.get(sha))} for sha in shas]
                entry = {"type": "push", "author": comment["author"], "at": at, "force": force, "commits": commits}
            else:
                what = event_words(comment, self.forge.details.get(comment["id"]), self.names, self.redactor)
                entry = {"type": "event", "author": comment["author"], "at": at, "what": what}
            entries.append((at, comment["id"], entry))
        entries.sort(key=lambda item: item[:2])
        return [entry for *_, entry in entries]

    # ---------------------------------------------------------------- code

    def code(self, repo: dict[str, Any]) -> dict[str, Any]:
        """`code/<repo>.json`: the files the agents changed from the seed commit to the head."""

        body = repo_code(self.git_root / f"{repo['name']}.git", repo["seed"], repo["head"], self.redactor)
        count(body, self.code_counts)
        return {"schema": BUNDLE_SCHEMA, "repo": repo["name"], **body}

    # ------------------------------------------------------------ sessions

    def session(self, record: dict[str, Any]) -> dict[str, Any]:
        """`sessions/<id>.json`. The session's tool calls, as `core.json` lists them, are counted from its steps."""

        output = record["_output"]
        trajectory = read_trajectory(output)
        logged_prompt = None
        if trajectory is not None:
            steps, transcript = trajectory_steps(trajectory, self.redactor, self.transcripts), "trajectory"
            if converter(trajectory) == "kimi-cli":
                # kimi-cli's trajectory records neither times nor the prompt; its session log
                # does. The log's step times are the trajectory's steps' only when it has as many.
                logged_prompt, times = kimi_session_log(output, self.transcripts)
                if len(times) == len(steps):
                    for step, at in zip(steps, times):
                        step["at"] = step["at"] or at
                    self.transcripts["steps timed from the session log"] += sum(1 for at in times if at)
                else:
                    self.untimed.append(record["id"])
        else:
            if (output / "trajectory.json").exists():
                self.transcripts["trajectories the exporter cannot read"] += 1
            steps = self.evidence_steps(record["id"])
            transcript = "evidence" if steps else None
        self.transcripts[TRANSCRIPT_COUNTS[transcript]] += 1
        self.transcripts["steps"] += len(steps)
        record["toolCalls"] = sum(len(step["calls"]) for step in steps)
        # The prompt the run started with, kept beside the transcript when no step
        # holds it (Codex and Claude Code record it as a user step): the user input
        # kimi-cli's session log records, else the run's `input/prompt.md`.
        prompt = None
        if not any(step["role"] == "user" for step in steps):
            if logged_prompt:
                prompt = logged_prompt
                self.transcripts["prompts from the session log"] += 1
            elif (output.parent / "input" / "prompt.md").is_file():
                prompt = (output.parent / "input" / "prompt.md").read_text(encoding="utf-8", errors="replace")
                self.transcripts["prompts from input/prompt.md"] += 1
        changes = read_json(output / "changes.json")
        receipt = record["_receipt"]
        return {
            **{k: v for k, v in public(record).items() if k != "actions"},
            "schema": BUNDLE_SCHEMA,
            "harness": record["_harness"],
            "model": record["_model"],
            "error": self.redactor.line(record["_error"]),
            "response": self.redactor.text(record["_response"]) if record["_response"] else None,
            "actions": [{**public_event(e), "text": e["text"]} for e in self.by_session.get(record["id"], [])],
            "transcript": transcript,
            "prompt": self.redactor.text(prompt),
            "steps": steps,
            "changes": {
                kind: [self.redactor.line(str(path)) or "" for path in changes.get(kind) or [] if isinstance(path, str)]
                for kind in ("added", "modified", "deleted")
            }
            if isinstance(changes, dict)
            else None,
            "receipt": {field: receipt.get(field) for field in RECEIPT_FIELDS} if receipt else None,
        }

    def evidence_steps(self, run_id: str) -> list[dict[str, Any]]:
        """A session's tool calls as the analyzer's evidence records them, one step per step number.

        The evidence holds a row for each call and nothing else, so a step without
        a call is missing, and its texts arrive shortened (`_TRIMMED`, `_INPUT_CUT`),
        marked truncated.
        """

        steps: list[dict[str, Any]] = []
        for row in self.evidence.execute(
            "SELECT step, call, tool, kind, text, path, output FROM tool_calls WHERE run_id = ? ORDER BY step, call",
            (run_id,),
        ):
            if not steps or steps[-1]["step"] != row["step"]:
                steps.append(
                    {
                        "step": row["step"], "role": "agent", "at": None, "model": None, "message": None, "reasoning": None,
                        "calls": [], "metrics": None,
                    }
                )
            output = row["output"] or None
            steps[-1]["calls"].append(
                {
                    "tool": row["tool"],
                    "kind": call_kind(row["tool"], row["kind"]),
                    "input": self.redactor.text(row["text"], trimmed=len(row["text"]) >= _INPUT_CUT) if row["text"] else None,
                    "arguments": [],
                    "path": self.redactor.line(row["path"]),
                    "output": self.redactor.text(output, trimmed=_TRIMMED.match(output, _TRIMMED_AT) is not None)
                    if output
                    else None,
                }
            )
        return steps

    # -------------------------------------------------------------- index

    def index_entry(self, core: dict[str, Any]) -> dict[str, Any]:
        receipts = [s["_receipt"] for s in self.session_records]
        tokens = sum((r.get("inputTokens") or 0) + (r.get("outputTokens") or 0) for r in receipts)
        # As Table 7 counts them: the completed sessions the coordinator scheduled.
        scheduled = [s for s in self.session_records if s["status"] == "completed" and not s["bootstrap"]]
        seconds = sum(epoch(s["finished"]) - epoch(s["started"]) for s in scheduled if s["finished"])
        return {
            "id": core["id"],
            "label": core["label"],
            "group": self.args.group,
            "model": most_common(s["_model"] for s in self.session_records) or "unknown",
            "harness": most_common(s["_harness"] for s in self.session_records) or "unknown",
            "agents": str(len(core["agents"])),
            "setting": self.args.setting,
            "sessions": len(scheduled),
            "hours": round(seconds / 3600, 2),
            "tokensB": round(tokens / 1e9, 3),
            "sCarry": None,
            "bundle": core["id"],
        }

    def report(self, core: dict[str, Any]) -> dict[str, Any]:
        events = core["events"]
        return {
            "events": dict(Counter(e["action"] for e in events)),
            "events from": self.events_from,
            "linked to a session": sum(1 for e in events if e["session"]),
            "unlinked": dict(Counter(e["action"] for e in events if not e["session"])),
            "releases kept": len(core["releases"]),
            "agents from": self.bound_from,
            "sessions": len(core["sessions"]),
            "bootstrap sessions": sum(1 for s in core["sessions"] if s["bootstrap"]),
            "code": dict(self.code_counts),
            "git": self.git_report["git"],
            "git bytes": self.git_report["bytes"],
            "git withheld": self.git_report["withheld"],
            "git unscanned": self.git_report["unscanned"],
            "transcripts": dict(self.transcripts),
            "kimi-cli sessions without step times": self.untimed,
            "redacted": dict(self.redactor.counts),
            "left out": dict(self.skipped),
            "bytes": {kind: {"files": len(v), "total": sum(v), "largest": max(v)} for kind, v in self.sizes.items()},
        }


# What the exporter writes in a run's directory. Anything else there belongs
# to other tools and stays.
OWNED = ("core.json", "threads", "sessions", "code", *repo_git.OWNED)


def swap(building: Path, target: Path) -> None:
    """Move the exporter's files from the finished `building` directory into `target`, replacing the old ones."""

    target.mkdir(parents=True, exist_ok=True)
    old = target.with_name(f".{target.name}.old")
    if old.exists():
        shutil.rmtree(old)
    old.mkdir()
    for name in OWNED:
        if (target / name).exists():
            (target / name).rename(old / name)
        (building / name).rename(target / name)
    shutil.rmtree(old)
    building.rmdir()


def update_index(path: Path, core: dict[str, Any], entry: dict[str, Any], group: str | None) -> None:
    """Add or update the run's entry in `index.json`, keeping every other entry.

    What the catalog states and a checkpoint does not (the group, the
    setting, the speedup) is kept from an existing entry unless given.
    """

    index = read_json(path) if path.is_file() else None
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        index = {"schema": INDEX_SCHEMA, "groups": [], "runs": []}
    previous = next((r for r in index["runs"] if r.get("id") == entry["id"]), {})
    world = core["world"]
    entry = {
        **entry,
        "group": group or previous.get("group") or world["id"],
        "setting": entry["setting"] or previous.get("setting"),
        "sCarry": previous.get("sCarry"),
    }
    if not any(g.get("id") == entry["group"] for g in index["groups"]):
        index["groups"].append({"id": entry["group"], "label": world["label"], "reference": None, "world": world["label"]})
    # A run exported again keeps its place in the list; a new one goes last.
    runs = index["runs"]
    at = next((i for i, run in enumerate(runs) if run.get("id") == entry["id"]), None)
    if at is None:
        runs.append(entry)
    else:
        runs[at] = entry
    dump(path, index)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Export one society checkpoint as a sim-browser bundle.")
    parser.add_argument("--evidence", required=True, help="the analyzer's directory: evidence.db and forge/")
    parser.add_argument("--checkpoint", required=True, help="the society checkpoint directory")
    parser.add_argument("--world", required=True, help="a fixture whose world block lays the repositories out")
    parser.add_argument("--id", required=True, help="the run's id, also its bundle directory")
    parser.add_argument("--label", required=True, help="the run's name in lists")
    parser.add_argument("--out", required=True, help="the bundles directory, for example bundles")
    parser.add_argument("--group", help="the catalog group to list the run under (default: kept, else the world's id)")
    parser.add_argument("--setting", help="the run's setting, as the catalog states it")
    parser.add_argument("--avatars", default=str(root / "public" / "avatars"), help="the portraits to assign from")
    parser.add_argument(
        "--known-secrets",
        help="a file the operator prepares, outside the checkpoint, with one secret value per line;"
        " every exact occurrence is redacted",
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", args.id, re.I):
        parser.error("--id must be a short name of letters, digits, '.', '_' and '-'")
    print(json.dumps(Export(args).run(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
