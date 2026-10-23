"""
What the agents changed in a package: every file that differs between the
commit the world was seeded from and the head at the checkpoint, read from
the forge's bare repository with `git`.

For each changed file, `code/<repo>.json` holds its status, its line counts,
its unified diff and its whole text at head; beside them, the top-level
listing of the head commit's tree. Every diff and text passes the redactor
and is kept whole. A caller may still bound them (`file_limit` per text,
`repo_limit` for the repository's diffs and texts together, diffs first, in
path order); a file whose diff or text does not fit is then marked
`omitted`, and what was left out is null. The exporter sets no bound.

Git runs without the user's or the system's configuration, with prefixes,
context and rename detection stated, and never runs an external diff or a
text conversion. It only reads.

The exporter writes `code/` with the rest of a run. For a run already
exported, this module writes `code/` alone, from the repositories and
commits the run's `core.json` names, and leaves every other file as it is:

    python3.14 scripts/export/repo_code.py --evidence <analysis dir> --out bundles/<run>
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from redact import Redactor, known_secrets


# Git takes a blob holding a NUL byte in its first 8,000 bytes for binary.
_BINARY_SPAN = 8_000

_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
}
_DIFF = ("--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "--no-relative", "--unified=3")

STATUSES = {"A": "added", "M": "modified", "T": "modified", "D": "deleted", "R": "renamed"}
KINDS = {"tree": "dir", "blob": "file", "commit": "submodule"}

_HEADER = re.compile(rb"^diff --git ", re.M)
_BINARY = re.compile(rb"^(?:Binary files .* differ|GIT binary patch)$", re.M)
_HUNK = re.compile(rb"^@@ ", re.M)


def git(git_dir: Path, *args: str, stdin: bytes | None = None) -> bytes | None:
    """The output of `git` on the repository; None when it fails."""

    done = subprocess.run(["git", f"--git-dir={git_dir}", *args], input=stdin, capture_output=True, env=_ENV, check=False)
    return done.stdout if done.returncode == 0 else None


def has_commit(git_dir: Path, sha: str | None) -> bool:
    return bool(sha) and git(git_dir, "cat-file", "-e", f"{sha}^{{commit}}") is not None


def changed(git_dir: Path, seed: str, head: str) -> list[dict[str, Any]]:
    """The raw diff: per changed file its status, paths, mode and blob at head, in git's order."""

    raw = git(git_dir, "diff", *_DIFF, "--raw", "-z", "--no-abbrev", seed, head)
    parts = (raw or b"").split(b"\0")
    entries, at = [], 0
    while at < len(parts) and parts[at].startswith(b":"):
        _, mode, _, blob, status = parts[at][1:].decode().split(" ")
        letter = status[0]
        if letter in ("R", "C"):
            before, path = parts[at + 1], parts[at + 2]
            at += 3
        else:
            before, path = None, parts[at + 1]
            at += 2
        entries.append(
            {
                "path": path.decode(errors="replace"),
                "from": before.decode(errors="replace") if before is not None and letter == "R" else None,
                # A copy is reported as what it is at head: a file added.
                "status": STATUSES.get(letter, "added" if letter == "C" else "modified"),
                "_mode": mode,
                "_blob": None if letter == "D" else blob,
                "_header": path,
                # A type change (a file that became a link, say) is two blocks in the patch.
                "_blocks": 2 if letter == "T" else 1,
            }
        )
    return entries


def patches(git_dir: Path, seed: str, head: str, entries: list[dict[str, Any]]) -> list[bytes]:
    """Each entry's block of the unified diff.

    Git writes the raw and the patch formats from the same list of changes
    and in its order, so the n-th block belongs to the n-th entry, except
    that git writes a type change as two blocks, the old file's deletion and
    the new one's creation, which stay together as the entry's block. Each
    block's header is checked against the entry's path, and a mismatch stops
    the export.
    """

    patch = git(git_dir, "diff", *_DIFF, "--src-prefix=a/", "--dst-prefix=b/", seed, head)
    starts = [match.start() for match in _HEADER.finditer(patch or b"")]
    expected = sum(entry["_blocks"] for entry in entries)
    if patch is None or len(starts) != expected:
        raise ValueError(f"{git_dir.name}: the diff has {len(starts)} blocks and the raw diff {expected}")
    ends = [*starts[1:], len(patch)]
    blocks, at = [], 0
    for entry in entries:
        blocks.append(patch[starts[at] : ends[at + entry["_blocks"] - 1]])
        at += entry["_blocks"]
    for block, entry in zip(blocks, entries):
        header = block.split(b"\n", 1)[0]
        # A path git quotes (a quote, a backslash, a control or non-ASCII character) is not compared.
        if not header.endswith(b'"') and not header.endswith(b" b/" + entry["_header"]):
            raise ValueError(f"{git_dir.name}: the diff of {entry['path']} is not where the raw diff lists it")
    return blocks


def lines_of(block: bytes) -> tuple[bool, int | None, int | None, str | None]:
    """(binary, lines added, lines deleted, the hunks from the first `@@` line) of one file's block.

    A type change's block holds two blocks: their lines are counted together,
    and the second's hunks follow the first's.
    """

    starts = [match.start() for match in _HEADER.finditer(block)]
    if len(starts) > 1:
        parts = [lines_of(block[start:end]) for start, end in zip(starts, [*starts[1:], len(block)])]
        if any(binary for binary, _, _, _ in parts):
            return True, None, None, None
        hunks = [part for _, _, _, part in parts if part is not None]
        return False, sum(a for _, a, _, _ in parts), sum(d for _, _, d, _ in parts), "\n".join(hunks) or None

    hunk = _HUNK.search(block)
    head = block[: hunk.start()] if hunk else block
    if _BINARY.search(head):
        return True, None, None, None
    if hunk is None:
        # A rename without changes, or a new mode: no lines.
        return False, 0, 0, None
    body = block[hunk.start() :].decode(errors="replace")
    added = deleted = 0
    for line in body.split("\n"):
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return False, added, deleted, body.removesuffix("\n")


def blobs(git_dir: Path, shas: list[str]) -> dict[str, bytes]:
    """sha → the blob's content, for the blobs the repository holds."""

    return objects(git_dir, shas, b"blob")


def objects(git_dir: Path, shas: list[str], kind: bytes) -> dict[str, bytes]:
    """sha → the object's content, for the objects of this kind (`blob`, `commit`) the repository holds."""

    if not shas:
        return {}
    data = git(git_dir, "cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode()) or b""
    contents, at = {}, 0
    while at < len(data):
        end = data.find(b"\n", at)
        if end < 0:
            break
        header = data[at:end].split()
        at = end + 1
        if len(header) != 3:
            # "<sha> missing"
            continue
        size = int(header[2])
        if header[1] == kind:
            contents[header[0].decode()] = data[at : at + size]
        at += size + 1
    return contents


def tree(git_dir: Path, head: str, redactor: Redactor) -> list[dict[str, Any]]:
    """The top-level entries of the head commit's tree: directories first, each group in git's order."""

    out = git(git_dir, "ls-tree", "-z", "--long", head) or b""
    entries = []
    for item in out.split(b"\0"):
        if not item:
            continue
        meta, name = item.split(b"\t", 1)
        mode, kind, _, size = meta.decode().split()
        entries.append(
            {
                "name": redactor.line(name.decode(errors="replace")) or "",
                "kind": "link" if mode == "120000" else KINDS.get(kind, "file"),
                "size": int(size) if size.isdigit() else None,
            }
        )
    return sorted(entries, key=lambda entry: entry["kind"] != "dir")


def repo_code(
    git_dir: Path,
    seed: str | None,
    head: str | None,
    redactor: Redactor,
    *,
    file_limit: int | None = None,
    repo_limit: int | None = None,
) -> dict[str, Any]:
    """The body of `code/<repo>.json`: the commits compared, the listing at head and the changed files.

    When the repository does not hold both commits, `seed` and `head` are
    null and there is nothing to list.
    """

    if not git_dir.is_dir() or not has_commit(git_dir, seed) or not has_commit(git_dir, head):
        return {"seed": None, "head": None, "tree": [], "files": []}
    entries = changed(git_dir, seed, head) if seed != head else []
    blocks = patches(git_dir, seed, head, entries) if entries else []
    contents = blobs(git_dir, sorted({e["_blob"] for e in entries if e["_blob"] and e["_mode"] != "160000"}))

    files = []
    for entry, block in zip(entries, blocks):
        binary, added, deleted, hunks = lines_of(block)
        content = contents.get(entry["_blob"] or "")
        if content is not None and b"\0" in content[:_BINARY_SPAN]:
            binary, added, deleted, hunks = True, None, None, None
        files.append(
            {
                "path": redactor.line(entry["path"]) or "",
                "from": redactor.line(entry["from"]),
                "status": entry["status"],
                "binary": binary,
                "additions": added,
                "deletions": deleted,
                "size": len(content) if content is not None else None,
                "diff": hunks,
                "text": None if binary or content is None else content.decode(errors="replace"),
                "omitted": False,
            }
        )

    # Diffs first, then texts, while the repository's bound, if any, lasts.
    budget = repo_limit
    for key in ("diff", "text"):
        for file in files:
            if file[key] is None:
                continue
            kept = redactor.text(file[key], file_limit)
            if budget is not None and len(kept["text"]) > budget:
                file[key], file["omitted"] = None, True
                continue
            if budget is not None:
                budget -= len(kept["text"])
            file[key] = kept
    return {"seed": seed, "head": head, "tree": tree(git_dir, head, redactor), "files": files}


def count(body: dict[str, Any], counts: Counter) -> None:
    """Add a code file's figures to the export's report."""

    files = body["files"]
    counts["repositories"] += 1
    counts["without both commits"] += body["head"] is None
    counts["changed files"] += len(files)
    counts["binary"] += sum(1 for f in files if f["binary"])
    counts["texts cut"] += sum(1 for f in files if f["text"] and f["text"]["truncated"])
    counts["diffs cut"] += sum(1 for f in files if f["diff"] and f["diff"]["truncated"])
    counts["omitted"] += sum(1 for f in files if f["omitted"])


def write(run: Path, evidence: Path, redactor: Redactor) -> dict[str, Any]:
    """Write `code/` in an exported run's directory, replacing it and nothing else; what was written.

    The files are built beside the old ones and moved into place together.
    """

    # The exporter imports this module, so its helpers are imported when used.
    from export_bundle import BUNDLE_SCHEMA, dump, file_name

    core = json.loads((run / "core.json").read_text(encoding="utf-8"))
    root = evidence / "forge" / "git" / "repositories" / core["world"]["org"]
    building, old = run / ".code.building", run / ".code.old"
    for leftover in (building, old):
        if leftover.exists():
            shutil.rmtree(leftover)
    building.mkdir()
    counts: Counter[str] = Counter()
    sizes = []
    for repo in core["repos"]:
        body = repo_code(root / f"{repo['name']}.git", repo["seed"], repo["head"], redactor)
        count(body, counts)
        sizes.append(dump(building / f"{file_name(repo['name'])}.json", {"schema": BUNDLE_SCHEMA, "repo": repo["name"], **body}))
    if (run / "code").exists():
        (run / "code").rename(old)
    building.rename(run / "code")
    if old.exists():
        shutil.rmtree(old)
    return {"code": dict(counts), "bytes": {"files": len(sizes), "total": sum(sizes), "largest": max(sizes, default=0)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the code files of an exported run, leaving its other files as they are.")
    parser.add_argument("--evidence", required=True, help="the analyzer's directory, whose forge/ holds the bare repositories")
    parser.add_argument("--out", required=True, help="the run's directory in the bundles directory, holding its core.json")
    parser.add_argument(
        "--known-secrets",
        help="a file the operator prepares, outside the analyzer's directory, with one secret value per line;"
        " every exact occurrence is redacted",
    )
    args = parser.parse_args(argv)
    run = Path(args.out)
    if not (run / "core.json").is_file():
        parser.error(f"{run} holds no core.json: export the run first")
    known: list[str] = []
    if args.known_secrets:
        path, evidence = Path(args.known_secrets), Path(args.evidence)
        # As in export_bundle.py: the operator's own file, outside what the analyzer copied from the checkpoint.
        paths = {Path(os.path.abspath(path)), path.resolve()}
        roots = {Path(os.path.abspath(evidence)), evidence.resolve()}
        if any(p.is_relative_to(root) for p in paths for root in roots):
            parser.error("--known-secrets must name a file the operator prepares, outside the analyzer's directory")
        known = known_secrets(path)
    redactor = Redactor(known)
    report = write(run, Path(args.evidence), redactor)
    print(json.dumps({**report, "redacted": dict(redactor.counts)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
