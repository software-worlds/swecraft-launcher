"""
A run's git data and its agents' details: what the forge's repository
browser and the agent page read.

From the forge's bare repositories, read with `git`:

    git/<repo>.json                  every branch, with its head commit (RepoBranches)
    git/<repo>/trees/<commit>.json   every file of one head commit (Tree)
    git/<repo>/log.json              the commits since the society started (RepoLog)
    git/<repo>/commits/<id>.json     one commit: its message and its changes (CommitDetail)
    blobs/<id>.json                  one file's content by git's object id, written once per run (Blob)
    blobs/<id>.bin                   a binary file's bytes, unmodified

and from the checkpoint's apparatus:

    agents/<name>.json               the agent's charter, and where its memory is (AgentDetail)

The repositories are the world's, named as in the world (`attrs`), and each
dedicated home an agent has, named `<owner>/<name>`; a home's files are named
with `~` in place of `/` (`git/avery-bot~home.json`), as no forge name holds
`~` and a URL path never decodes it. An agent's memory is the
`agent/memory` branch of its home: the `home_repo` of its profile in the
coordinator's configuration (`apparatus/coordinator.yaml`), or, when the
profile names none, the repository it maintains.

Git runs as in repo_code.py: without the user's or the system's
configuration, never running an external diff or a text conversion, and only
reading. A content with a NUL byte in its first 8,000 bytes is binary: its
bytes are written unmodified, unless the redactor finds a known secret (as
exact bytes) or a secret's pattern (over the bytes read as latin-1) in them
or in what they expand to when they are compressed (xz, gzip, bzip2, zip);
then they are withheld and counted. A compressed content that cannot be read
whole is exported, and the report lists it as unscanned. Every other content,
a link's target included, is a text that passes the redactor whole. A tree
entry whose path is not a plain relative path is refused and counted, and so
is an agent or a home whose name could not be a file's.

A world repository's history is every commit reachable from a branch or a
pull request's head and not from its seed (`core.repos[].seed`): the
imported upstream tags reach upstream's own releases and branches, which are
left out, and a repository whose seed the forge does not hold has no log. A
dedicated home is made during the run, so its history is every commit
reachable from its branches and pull requests. A commit's files are its diff
against its first parent, with repo_code.py's options and no length limit; a
commit without parents is compared with the empty tree. Messages and diffs
pass the redactor whole.

The exporter writes these with the rest of a run. For a run already
exported, this module writes `git/`, `blobs/` and `agents/` alone, from the
run's core.json, and leaves every other file as it is:

    python3.14 scripts/export/repo_git.py --evidence <analysis dir> --checkpoint <checkpoint dir> --out bundles/<run>
"""

import argparse
import bz2
import gzip
import io
import json
import lzma
import os
import re
import shutil
import sys
import zipfile
import zlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from redact import Redactor, known_secrets
from repo_code import _BINARY_SPAN, blobs, changed, git, has_commit, lines_of, objects, patches

MEMORY_BRANCH = "agent/memory"
# What this module writes in a run's directory.
OWNED = ("git", "blobs", "agents")
# Blobs or commits read from git per call.
BATCH = 500

# An agent's, an owner's or a repository's name, safe to make a file name from.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_name(name: Any) -> bool:
    return isinstance(name, str) and _NAME.match(name) is not None and ".." not in name


def safe_path(path: str) -> bool:
    """A plain relative path: no leading or trailing `/`, no empty, `.` or `..` part, no control character."""

    if not path or path.startswith("/") or path.endswith("/") or _CONTROL.search(path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def repo_file(key: str) -> str:
    """A repository's name in its file names: an `<owner>/<name>` home's `/` becomes `~`, which no forge name holds."""

    from export_bundle import file_name

    return file_name(key.replace("/", "~"))


def browser_order(path: str) -> bytes:
    """The order the browser checks paths in: JavaScript's, by UTF-16 code units."""

    return path.encode("utf-16-be", errors="surrogatepass")


def stamp(seconds: str) -> str:
    return datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ homes


def homes(core: dict[str, Any], checkpoint: Path) -> dict[str, str]:
    """agent → the key of its home.

    The coordinator's agent profile names the home as `home_repo`; without
    one (experiments 6 to 8), the home is the repository the agent
    maintains. A home in the world's organisation is keyed by its name
    (`attrs`); one outside it is dedicated and keyed `<owner>/<name>`.
    """

    home = {agent["name"]: agent["repo"] for agent in core["agents"]}
    org = core["world"]["org"]
    for name, home_repo in profile_homes(checkpoint / "apparatus" / "coordinator.yaml").items():
        if name in home:
            owner, _, repo = home_repo.partition("/")
            home[name] = repo if owner == org else home_repo
    return home


_KEY_VALUE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def scalar(raw: str) -> str:
    """A YAML scalar as written on one line: its quotes removed, or a trailing comment dropped."""

    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split(" #", 1)[0].strip()


def flow_pairs(body: str) -> dict[str, str]:
    """The top-level keys and raw values of a one-line flow mapping, `{a: b, c: {d: e}}`."""

    parts, depth, quote, current = [], 0, "", ""
    for char in body:
        if quote:
            current += char
            quote = "" if char == quote else quote
            continue
        if char in "'\"":
            quote = char
        elif char in "{[":
            depth += 1
            if depth == 1:
                continue
        elif char in "}]":
            depth -= 1
            if depth == 0:
                break
        elif char == "," and depth == 1:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return {m.group(1): m.group(2) for part in parts if (m := _KEY_VALUE.match(part.strip()))}


def profile_homes(path: Path) -> dict[str, str]:
    """agent → `home_repo`, for the coordinator's agent profiles that name a home.

    Only the `agents:` list is read, and from each entry only `name` and
    `home_repo`, in either YAML form the hub's configurations use: a flow
    mapping on one line (`- {name: avery-bot, home_repo: avery-bot/home}`) or
    a block mapping (`- name: avery-bot`, then `home_repo:` at the same
    column). No other field, the agent's environment among them, is kept.
    """

    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    entry: dict[str, str] | None = None
    column, inside = -1, False

    def keep() -> None:
        if entry and entry.get("name") and entry.get("home_repo"):
            found[scalar(entry["name"])] = scalar(entry["home_repo"])

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 and not stripped.startswith("-"):
            keep()
            entry, inside = None, re.match(r"^agents\s*:", line) is not None
            continue
        if not inside:
            continue
        if stripped == "-" or stripped.startswith("- "):
            keep()
            body = stripped[1:].strip()
            column = indent + len(stripped) - len(body)
            if body.startswith("{"):
                entry = flow_pairs(body)
            else:
                entry = {m.group(1): m.group(2)} if (m := _KEY_VALUE.match(body)) else {}
        elif entry is not None and indent == column and (m := _KEY_VALUE.match(stripped)):
            entry.setdefault(m.group(1), m.group(2))
    keep()
    return found


# ---------------------------------------------------------------- reading

# The refs whose history is the society's: every branch and every pull request's head
# (`refs/pull/<n>/head`). The imported upstream tags are left out: they reach upstream
# commits the seed does not, such as upstream's releases after the pinned commit.
HISTORY_REFS = ("--branches", "--glob=refs/pull/*")


def branches(git_dir: Path, default: str | None, redactor: Redactor) -> tuple[str, list[dict[str, Any]]]:
    """(the default branch, every branch: the default first, then by head date, newest first)."""

    fields = "%(refname)%00%(objecttype)%00%(objectname)%00%(committerdate:unix)%00%(subject)%00%(authorname)"
    out = git(git_dir, "for-each-ref", f"--format={fields}", "refs/heads") or b""
    found = []
    for line in out.split(b"\n"):
        parts = line.split(b"\0")
        if len(parts) != 6 or parts[1] != b"commit":
            continue
        ref, _, head, seconds, subject, author = (part.decode("utf-8", errors="replace") for part in parts)
        found.append(
            {
                "name": ref.removeprefix("refs/heads/"),
                "head": head,
                "date": stamp(seconds),
                "subject": redactor.line(subject),
                "author": redactor.line(author),
            }
        )
    names = {b["name"] for b in found}
    if default not in names:
        current = (git(git_dir, "symbolic-ref", "--short", "HEAD") or b"").decode().strip()
        default = current if current in names else (max(found, key=lambda b: b["date"])["name"] if found else default or "main")
    # Newest first; branches with the same date in name order.
    others = sorted(sorted((b for b in found if b["name"] != default), key=lambda b: b["name"]), key=lambda b: b["date"], reverse=True)
    first = [b for b in found if b["name"] == default]
    return default, first + others


def history(git_dir: Path, seed: str | None) -> list[dict[str, Any]] | None:
    """The commits since the society started: reachable from a branch or a pull request and not from `seed`.

    Newest first by committer date, each commit before its parents (git's
    `--date-order`). Without a seed, as for a dedicated home made during the
    run, every commit is the society's. None when git cannot list them.
    """

    fields = "%H%x1f%P%x1f%an%x1f%ae%x1f%ct%x1f%at%x1f%s"
    out = git(git_dir, "log", "--no-show-signature", *HISTORY_REFS, "--date-order", "-z", f"--format={fields}", *([f"^{seed}"] if seed else []))
    if out is None:
        return None
    commits = []
    for record in out.split(b"\0"):
        # The subject comes last, so a separator inside it stays there.
        parts = record.decode("utf-8", errors="replace").split("\x1f", 6)
        if len(parts) != 7:
            continue
        sha, parents, author, email, committed, authored, subject = parts
        commits.append(
            {
                "id": sha,
                "parents": parents.split(),
                "author": author or None,
                "authorEmail": email or None,
                "date": stamp(committed),
                "authored": stamp(authored),
                "subject": subject or None,
            }
        )
    return commits


def messages(git_dir: Path, ids: list[str]) -> dict[str, str]:
    """id → the commit's whole message: its object after the blank line that ends the headers."""

    found = {}
    for start in range(0, len(ids), BATCH):
        for sha, body in objects(git_dir, ids[start : start + BATCH], b"commit").items():
            found[sha] = body.partition(b"\n\n")[2].decode("utf-8", errors="replace")
    return found


def empty_tree(git_dir: Path) -> str:
    """The id of the empty tree in the repository's object format, which a commit without parents is compared with."""

    return (git(git_dir, "hash-object", "-t", "tree", os.devnull) or b"").decode().strip()


# ------------------------------------------------------ binary contents

# What a compressed content may expand to, in all, when it is searched for secrets. The rest is
# not read, so that a small file that expands without bound cannot exhaust memory.
EXPANDED_LIMIT = 64 * 1024 * 1024
# How many compressed layers inside one another are opened.
EXPANDED_DEPTH = 4
# Compressed formats by their first bytes: those the standard library reads, and some it does not.
COMPRESSED = ((b"\xfd7zXZ\x00", "xz"), (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"PK\x03\x04", "zip"))
NOT_READ = ((b"\x28\xb5\x2f\xfd", "zstd"), (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "rar"), (b"\x04\x22\x4d\x18", "lz4"), (b"LZIP", "lzip"))
_STREAMS = {"xz": lzma.LZMAFile, "gzip": lambda data: gzip.GzipFile(fileobj=data), "bzip2": bz2.BZ2File}
# What reading a damaged stream or archive raises; NotImplementedError is a zip member's unknown method.
_DAMAGED = (OSError, EOFError, ValueError, lzma.LZMAError, zlib.error, zipfile.BadZipFile, NotImplementedError)


def expanded(content: bytes) -> tuple[list[bytes], list[str]]:
    """What to search in a binary content, and why any of it could not be read.

    The content itself, then what each compressed layer in it expands to
    (xz, gzip, bzip2, and each member of a zip archive), opened up to
    EXPANDED_DEPTH layers deep and up to EXPANDED_LIMIT bytes in all. A layer
    that is encrypted, damaged, past the limit or in a format the standard
    library does not read is named in the second list; what was read of it is
    searched all the same.
    """

    parts, unread, room = [content], [], EXPANDED_LIMIT
    pending = [(content, 0)]
    while pending:
        data, depth = pending.pop()
        kind = next((name for magic, name in COMPRESSED if data.startswith(magic)), None)
        if kind is None:
            other = next((name for magic, name in NOT_READ if data.startswith(magic)), None)
            if other is not None:
                unread.append(f"{other}, a format the standard library does not read")
            continue
        if depth == EXPANDED_DEPTH:
            unread.append(f"{kind} nested more than {EXPANDED_DEPTH} layers deep")
            continue
        for inner, problem in layer(kind, data, room):
            room -= len(inner)
            if problem is not None:
                unread.append(f"{kind}: {problem}")
            if inner:
                parts.append(inner)
                pending.append((inner, depth + 1))
    return parts, unread


def layer(kind: str, data: bytes, room: int) -> list[tuple[bytes, str | None]]:
    """(the bytes, why they are incomplete or None) of each part of one compressed layer, at most `room` bytes in all."""

    if kind != "zip":
        try:
            with _STREAMS[kind](io.BytesIO(data)) as stream:
                inner = stream.read(room + 1)
        except _DAMAGED:
            return [(b"", "damaged")]
        return [(inner[:room], "past the expanded size limit")] if len(inner) > room else [(inner, None)]
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except _DAMAGED:
        return [(b"", "damaged")]
    parts: list[tuple[bytes, str | None]] = []
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                parts.append((b"", "a member is encrypted"))
                continue
            try:
                with archive.open(info) as member:
                    inner = member.read(room + 1)
            except _DAMAGED:
                parts.append((b"", "a member is damaged or compressed with a method the standard library does not read"))
                continue
            if len(inner) > room:
                parts.append((inner[:room], "past the expanded size limit"))
                break
            room -= len(inner)
            parts.append((inner, None))
    return parts


class Store:
    """The git data of one run: trees per repository, blobs once for the run."""

    def __init__(self, root: Path, redactor: Redactor) -> None:
        from export_bundle import BUNDLE_SCHEMA, file_name

        self.root, self.redactor = root, redactor
        self.schema, self.file_name = BUNDLE_SCHEMA, file_name
        self.counts: Counter[str] = Counter()
        self.sizes: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.written: set[str] = set()
        # A blob's first repository and path, for the report's lists of binary contents.
        self.where: dict[str, tuple[str, str]] = {}
        self.unscanned: list[dict[str, Any]] = []
        self.withheld: list[dict[str, Any]] = []
        self.paths: dict[str, str] = {}
        self.commits: set[str] = set()
        (root / "git").mkdir(parents=True)
        (root / "blobs").mkdir()
        (root / "agents").mkdir()

    def dump(self, kind: str, path: Path, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path.write_bytes(data)
        self.sizes[kind].append((len(data), len(gzip.compress(data, mtime=0))))

    def path(self, raw: bytes) -> str | None:
        """A tree entry's path as written, or None when it is refused."""

        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if path not in self.paths:
            self.paths[path] = self.redactor.line(path) or ""
        kept = self.paths[path]
        return kept if safe_path(kept) else None

    def tree(self, git_dir: Path, commit: str) -> list[dict[str, Any]] | None:
        """Every file of a commit, in the browser's path order; None when git cannot list it."""

        out = git(git_dir, "ls-tree", "-r", "-l", "-z", commit)
        if out is None:
            return None
        entries, seen = [], set()
        for item in out.split(b"\0"):
            if not item:
                continue
            meta, raw = item.split(b"\t", 1)
            mode, _, oid, size = meta.decode().split()
            path = self.path(raw)
            if path is None or path in seen:
                self.counts["paths refused"] += 1
                continue
            seen.add(path)
            if mode == "160000":
                entries.append({"path": path, "kind": "submodule", "mode": mode, "size": None, "blob": None})
                self.counts["submodules"] += 1
            else:
                kind = "link" if mode == "120000" else "file"
                entries.append({"path": path, "kind": kind, "mode": mode, "size": int(size), "blob": oid})
                self.counts[f"{kind}s"] += 1
        entries.sort(key=lambda entry: browser_order(entry["path"]))
        return entries

    def repository(self, key: str, git_dir: Path, default: str | None, *, seed: str | None, log: bool) -> dict[str, str]:
        """Write a repository's branches, its trees and the blobs they name that are not written yet; branch → head.

        With `log`, also its history since `seed` (since its start when `seed` is None).
        """

        default, found = branches(git_dir, default, self.redactor)
        name = repo_file(key)
        kept, needed = [], set()
        for branch in found:
            head = branch["head"]
            tree_path = self.root / "git" / name / "trees" / f"{head}.json"
            if not tree_path.exists():
                entries = self.tree(git_dir, head)
                if entries is None:
                    self.counts["branches unreadable"] += 1
                    continue
                tree_path.parent.mkdir(parents=True, exist_ok=True)
                self.dump("trees", tree_path, {"schema": self.schema, "repo": key, "commit": head, "entries": entries})
                self.commits.add(head)
                for entry in entries:
                    if entry["blob"]:
                        needed.add(entry["blob"])
                        self.where.setdefault(entry["blob"], (key, entry["path"]))
            kept.append(branch)
        if kept and kept[0]["name"] != default:
            # The default branch could not be read: the newest readable branch is shown first.
            default = kept[0]["name"]
        self.dump("branches", self.root / "git" / f"{name}.json", {"schema": self.schema, "repo": key, "defaultBranch": default, "branches": kept})
        self.counts["repositories"] += 1
        self.counts["branches"] += len(kept)
        self.blobs(git_dir, sorted(needed - self.written))
        if log:
            self.history(key, git_dir, seed, kept)
        return {branch["name"]: branch["head"] for branch in kept}

    def history(self, key: str, git_dir: Path, seed: str | None, kept: list[dict[str, Any]]) -> None:
        """Write `git/<repo>/log.json` and a file per commit in `git/<repo>/commits/`."""

        commits = history(git_dir, seed)
        if commits is None:
            self.counts["logs git cannot list"] += 1
            return
        folder = self.root / "git" / repo_file(key)
        (folder / "commits").mkdir(parents=True, exist_ok=True)
        heads: dict[str, list[str]] = defaultdict(list)
        for branch in kept:
            heads[branch["head"]].append(branch["name"])
        texts = messages(git_dir, [c["id"] for c in commits])
        empty = empty_tree(git_dir)
        summaries = []
        for commit in commits:
            people = {
                "author": self.redactor.line(commit["author"]),
                "authorEmail": self.redactor.line(commit["authorEmail"]),
            }
            summaries.append(
                {
                    "id": commit["id"],
                    "parents": commit["parents"],
                    **people,
                    "date": commit["date"],
                    "authored": commit["authored"],
                    "subject": self.redactor.line(commit["subject"]),
                    "branches": heads.get(commit["id"], []),
                }
            )
            detail = {
                "schema": self.schema,
                "repo": key,
                "id": commit["id"],
                "parents": commit["parents"],
                **people,
                "date": commit["date"],
                "authored": commit["authored"],
                "message": self.redactor.text(texts[commit["id"]]),
                "files": self.commit_files(git_dir, commit, empty),
            }
            self.dump("commits", folder / "commits" / f"{commit['id']}.json", detail)
        self.dump("logs", folder / "log.json", {"schema": self.schema, "repo": key, "seed": seed, "commits": summaries})
        self.counts["logs"] += 1
        self.counts["commits"] += len(commits)

    def commit_files(self, git_dir: Path, commit: dict[str, Any], empty: str) -> list[dict[str, Any]]:
        """The files a commit changed against its first parent (against nothing without one), as repo_code.py diffs them."""

        base = commit["parents"][0] if commit["parents"] else empty
        entries = changed(git_dir, base, commit["id"])
        blocks = patches(git_dir, base, commit["id"], entries) if entries else []
        files = []
        for entry, block in zip(entries, blocks):
            path = self.path(entry["_header"])
            if path is None:
                self.counts["paths refused"] += 1
                continue
            binary, added, deleted, hunks = lines_of(block)
            files.append(
                {
                    "path": path,
                    "from": None if entry["from"] is None else self.path(entry["from"].encode()),
                    "status": entry["status"],
                    "additions": added,
                    "deletions": deleted,
                    "binary": binary,
                    "diff": None if binary or hunks is None else self.redactor.text(hunks),
                }
            )
        return files

    def blobs(self, git_dir: Path, ids: list[str]) -> None:
        for start in range(0, len(ids), BATCH):
            batch = ids[start : start + BATCH]
            contents = blobs(git_dir, batch)
            for oid in batch:
                content = contents.get(oid)
                if content is None:
                    self.counts["blobs missing"] += 1
                    continue
                binary = b"\0" in content[:_BINARY_SPAN]
                text = None if binary else self.redactor.text(content.decode("utf-8", errors="replace"))
                body = {"schema": self.schema, "id": oid, "size": len(content), "binary": binary, "text": text, "bytes": None}
                if binary:
                    body["bytes"] = self.binary(oid, content)
                self.dump("blobs", self.root / "blobs" / f"{oid}.json", body)
                self.written.add(oid)
                self.counts["binary blobs" if binary else "text blobs"] += 1

    def binary(self, oid: str, content: bytes) -> str:
        """Write a binary content's bytes unmodified as `blobs/<id>.bin`, unless a secret is found in them; `exported` or `withheld`.

        The bytes are searched, and so is what their compressed layers expand
        to. A content part of which could not be read is exported all the
        same, and listed in the report as unscanned.
        """

        repo, path = self.where.get(oid, (None, None))
        parts, unread = expanded(content)
        self.counts["binary blobs expanded"] += len(parts) > 1
        found = next((name for name in map(self.redactor.secret_in, parts) if name is not None), None)
        if found is not None:
            self.counts["binary blobs withheld"] += 1
            self.counts[f"binary blobs withheld, {found}"] += 1
            self.withheld.append({"repo": repo, "path": path, "id": oid, "found": found})
            return "withheld"
        if unread:
            self.counts["binary blobs unscanned"] += 1
            self.unscanned.append({"repo": repo, "path": path, "id": oid, "why": sorted(set(unread))})
        (self.root / "blobs" / f"{oid}.bin").write_bytes(content)
        self.sizes["binary bytes"].append((len(content), len(gzip.compress(content, mtime=0))))
        return "exported"

    def agent(self, name: str, home: str | None, heads: dict[str, dict[str, str]], checkpoint: Path) -> None:
        """`agents/<name>.json`: the charter, redacted and whole, and the memory branch of the agent's home."""

        charter = checkpoint / "apparatus" / "charters" / f"{name}.md"
        instructions = None
        if charter.is_file():
            instructions = self.redactor.text(charter.read_text(encoding="utf-8", errors="replace"))
            self.counts["agents with instructions"] += 1
        memory = None
        if home is not None:
            memory = {"repo": home, "branch": MEMORY_BRANCH, "head": heads.get(home, {}).get(MEMORY_BRANCH)}
            self.counts["agents with a memory branch"] += memory["head"] is not None
        detail = {"schema": self.schema, "name": name, "instructions": instructions, "memory": memory}
        self.dump("agents", self.root / "agents" / f"{self.file_name(name)}.json", detail)
        self.counts["agents"] += 1

    def report(self) -> dict[str, Any]:
        def place(item: dict[str, Any]) -> tuple[str, str, str]:
            return item["repo"] or "", item["path"] or "", item["id"]

        return {
            "git": {**dict(self.counts), "distinct commits": len(self.commits)},
            "bytes": {
                kind: {"files": len(v), "total": sum(s for s, _ in v), "gzipped": sum(g for _, g in v), "largest": max(s for s, _ in v)}
                for kind, v in self.sizes.items()
            },
            # Binary contents by repository and path: withheld, and exported with part of their compressed content unread.
            "withheld": sorted(self.withheld, key=place),
            "unscanned": sorted(self.unscanned, key=place),
        }


# ---------------------------------------------------------------- writing


def build(root: Path, core: dict[str, Any], evidence: Path, checkpoint: Path, redactor: Redactor) -> dict[str, Any]:
    """Write `git/`, `blobs/` and `agents/` under `root`, for the run `core` describes; what was written."""

    store = Store(root, redactor)
    forge = evidence / "forge" / "git" / "repositories"
    org = core["world"]["org"]
    home = homes(core, checkpoint)
    heads: dict[str, dict[str, str]] = {}
    for repo in core["repos"]:
        git_dir = forge / org / f"{repo['name']}.git"
        if not safe_name(repo["name"]) or not git_dir.is_dir():
            store.counts["repositories not in the forge"] += 1
            continue
        seed = repo.get("seed")
        if not (seed and has_commit(git_dir, seed)):
            # Without its seed, the history since the society started cannot be told from upstream's.
            store.counts["seeds not in the forge"] += 1
            seed = None
        heads[repo["name"]] = store.repository(repo["name"], git_dir, repo["defaultBranch"], seed=seed, log=seed is not None)
    world = {repo["name"] for repo in core["repos"]}
    refused = set()
    for key in sorted(set(home.values()) - world):
        owner, _, name = key.partition("/")
        if not (safe_name(owner) and safe_name(name)):
            refused.add(key)
            store.counts["homes refused"] += 1
            continue
        if not (forge / owner / f"{name}.git").is_dir():
            store.counts["homes not in the forge"] += 1
            continue
        # A dedicated home is made during the run: its whole history is the society's.
        heads[key] = store.repository(key, forge / owner / f"{name}.git", None, seed=None, log=True)
    for agent in core["agents"]:
        if not safe_name(agent["name"]):
            store.counts["agents refused"] += 1
            continue
        key = home.get(agent["name"])
        store.agent(agent["name"], None if key in refused else key, heads, checkpoint)
    # The forge's other repositories, which are neither the world's nor a home.
    for git_dir in sorted(forge.glob("*/*.git")) if forge.is_dir() else []:
        owner, name = git_dir.parent.name, git_dir.name.removesuffix(".git")
        if (name if owner == org else f"{owner}/{name}") not in heads:
            store.counts["repositories left out"] += 1
    return store.report()


def write(run: Path, evidence: Path, checkpoint: Path, redactor: Redactor) -> dict[str, Any]:
    """Write `git/`, `blobs/` and `agents/` in an exported run's directory, replacing them and nothing else.

    The files are built beside the old ones and moved into place together.
    """

    core = json.loads((run / "core.json").read_text(encoding="utf-8"))
    building, old = run / ".repo-git.building", run / ".repo-git.old"
    for leftover in (building, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    report = build(building, core, evidence, checkpoint, redactor)
    old.mkdir()
    for name in OWNED:
        if (run / name).exists():
            (run / name).rename(old / name)
        (building / name).rename(run / name)
    shutil.rmtree(old)
    building.rmdir()
    return report


def inside(path: Path, roots: list[Path]) -> bool:
    """Whether `path`, as given or as it resolves, lies in one of `roots`, as given or as they resolve."""

    paths = {Path(os.path.abspath(path)), path.resolve()}
    bases = {base for root in roots for base in (Path(os.path.abspath(root)), root.resolve())}
    return any(p.is_relative_to(base) for p in paths for base in bases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the git data and agents' details of an exported run, leaving its other files as they are.")
    parser.add_argument("--evidence", required=True, help="the analyzer's directory, whose forge/ holds the bare repositories")
    parser.add_argument("--checkpoint", required=True, help="the society checkpoint, whose apparatus/ holds the charters and the world specification")
    parser.add_argument("--out", required=True, help="the run's directory in the bundles directory, holding its core.json")
    parser.add_argument(
        "--known-secrets",
        help="a file the operator prepares, outside the checkpoint and the analyzer's directory, with one secret value"
        " per line; every exact occurrence is redacted",
    )
    args = parser.parse_args(argv)
    run, evidence, checkpoint = Path(args.out), Path(args.evidence), Path(args.checkpoint)
    if not (run / "core.json").is_file():
        parser.error(f"{run} holds no core.json: export the run first")
    known: list[str] = []
    if args.known_secrets:
        if inside(Path(args.known_secrets), [checkpoint, evidence]):
            parser.error("--known-secrets must name a file the operator prepares, outside the checkpoint and the analyzer's directory")
        known = known_secrets(Path(args.known_secrets))
    redactor = Redactor(known)
    report = write(run, evidence, checkpoint, redactor)
    print(json.dumps({**report, "redacted": dict(redactor.counts)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
