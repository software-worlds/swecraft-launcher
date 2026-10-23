"""Upstream history from after the world's date that is reachable on the forge.

A world is upstream frozen at a pin. Worlds seeded by older seeders also carried
every upstream release tag cut after the pin, and with it the commits behind
them, so an agent listing tags could read (and copy) the future. This module
finds those tags in the checkpoint's own forge: a tag is from the future when
the pin is its strict ancestor and the seeded baseline is not (agents' own
release tags all descend from the baseline). It also lists what that future
added to Python files, minus lines the seed already had, so a pull request
that reproduces it verbatim can be matched.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MIN_LINE = 25
_SKIP_PREFIXES = ("#", '"""', "'''", "import ", "from ")


def normalize(line: str) -> str | None:
    """A code line worth matching verbatim, whitespace-normalized."""

    text = " ".join(line.split())
    if len(text) < MIN_LINE or text.startswith(_SKIP_PREFIXES):
        return None
    return text


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


def future_tags(repo: Path, pin: str, baseline: str) -> list[tuple[str, str]]:
    listing = _git(
        repo,
        "for-each-ref",
        "refs/tags",
        "--format=%(refname:short)%09%(objectname)%09%(*objectname)",
    )
    found = []
    for line in listing.stdout.splitlines():
        name, obj, peeled = (line.split("\t") + ["", ""])[:3]
        commit = peeled or obj
        if commit == pin or not commit:
            continue
        if _is_ancestor(repo, pin, commit) and not _is_ancestor(repo, baseline, commit):
            found.append((name, commit))
    return found


def future_history(
    repo: Path, repo_name: str, pin: str, baseline: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(tags, commits, lines) of the future reachable in one bare repository."""

    tags = future_tags(repo, pin, baseline)
    if not tags:
        return [], [], []
    tips = [commit for _, commit in tags]
    log = _git(repo, "log", "--no-merges", "--format=%H%x09%s", *tips, f"^{pin}")
    commits = []
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition("\t")
        commits.append({"repo": repo_name, "sha": sha, "subject": subject})
    patch = _git(
        repo,
        "log",
        "--no-merges",
        "-p",
        "--format=@@@commit %H",
        *tips,
        f"^{pin}",
        "--",
        "*.py",
    )
    added: dict[str, tuple[str, str]] = {}
    touched: set[str] = set()
    commit, path = "", ""
    for line in patch.stdout.splitlines():
        if line.startswith("@@@commit "):
            commit = line.split()[1]
        elif line.startswith("+++ b/"):
            path = line[6:]
            touched.add(path)
        elif line.startswith("+") and not line.startswith("+++"):
            text = normalize(line[1:])
            if text and text not in added:
                added[text] = (commit, path)
    seeded: set[str] = set()
    for path in touched:
        shown = _git(repo, "show", f"{baseline}:{path}")
        if shown.returncode == 0:
            seeded.update(filter(None, map(normalize, shown.stdout.splitlines())))
    lines = [
        {"repo": repo_name, "line": text, "commit": commit, "path": path}
        for text, (commit, path) in added.items()
        if text not in seeded
    ]
    tag_rows = [{"repo": repo_name, "tag": name, "commit": sha} for name, sha in tags]
    return tag_rows, commits, lines


def world_repositories(
    lock: dict[str, Any], git_root: Path
) -> Iterator[tuple[str, Path, str, str]]:
    """(forge repo, bare path, pin, baseline) for each locked world repository."""

    org = str(lock.get("org") or "hub")
    for entry in lock.get("repos") or []:
        name, pin = entry.get("name"), entry.get("pin_commit")
        baseline = entry.get("baseline_commit")
        bare = git_root / org / f"{name}.git"
        if name and pin and baseline and bare.is_dir():
            yield f"{org}/{name}", bare, pin, baseline
