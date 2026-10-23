"""Turn recorded forge invocations into the wake's action ledger.

The runner image records what an agent ran and nothing else. Deciding which
action a command performed happens here, which is what makes the decision
revisable: the recording is kept beside the ledger, so a table corrected later
can be applied to runs already collected. It is also the only place the run's
secret list exists, so redaction happens here rather than inside the sandbox.

The vocabulary lives in `forge_actions.yaml`. This module is the small amount
of machinery that file needs and nothing about the vocabulary itself.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

TABLE_PATH = Path(__file__).with_name("forge_actions.yaml")

# fj wraps interpolated values in Unicode bidi isolates, so a created issue is
# announced as `created issue #<FSI>34<PDI>`. Only those separators are
# stepped over: matching any non-digit would read `#issuecomment-812` as the
# object's number, which is the comment rather than the issue it is on.
_ANNOUNCED = re.compile("#[⁦-⁩‎‏]*([0-9]+)")
_REPO_ARG = re.compile(r"^([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)(?:#[0-9]+)?$")
_REF_NUMBER = re.compile(r"#([0-9]+)$")
_SEGMENT = re.compile(r"\{([a-z_]+)(:int)?\}")

CMD_MAX_BYTES = 4096
ARG_MAX_CHARS = 60
ELIDED = "<...>"
SENSITIVE_VALUE_FLAGS = frozenset(
    {
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--json",
        "-H",
        "--header",
        "--body",
    }
)


class TableError(Exception):
    """The vocabulary file is unusable."""


@dataclass(frozen=True)
class FjEntry:
    words: tuple[str, ...]
    sources: dict[str, Any]


@dataclass(frozen=True)
class ForgeEntry:
    method: str
    pattern: re.Pattern[str]
    when: dict[str, Any]
    sources: dict[str, Any]


@dataclass(frozen=True)
class Action:
    action: str
    metadata: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    fj_read_verbs: frozenset[str]
    fj_read_groups: frozenset[str]
    fj: tuple[tuple[Action, FjEntry], ...] = field(default=())
    forge: tuple[tuple[Action, ForgeEntry], ...] = field(default=())


def load_table(path: Path = TABLE_PATH) -> Table:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise TableError(f"cannot read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise TableError(f"{path} must be a mapping")

    fj: list[tuple[Action, FjEntry]] = []
    forge: list[tuple[Action, ForgeEntry]] = []
    for spec in raw.get("actions") or []:
        name = spec.get("action")
        if not name:
            raise TableError("an action entry has no `action`")
        keys = tuple(spec.get("metadata") or ())
        action = Action(action=name, metadata=keys)
        for entry in spec.get("fj") or []:
            sources = _checked_sources(name, keys, entry.get("with"))
            fj.append((action, FjEntry(tuple(entry["words"]), sources)))
        for entry in spec.get("forge") or []:
            sources = _checked_sources(name, keys, entry.get("with"))
            forge.append(
                (
                    action,
                    ForgeEntry(
                        method=str(entry["method"]).upper(),
                        pattern=_compile_path(entry["path"]),
                        when=dict(entry.get("when") or {}),
                        sources=sources,
                    ),
                )
            )
    # Longest command first, so `release asset create` is preferred over any
    # shorter entry that shares its prefix.
    fj.sort(key=lambda pair: len(pair[1].words), reverse=True)
    return Table(
        fj_read_verbs=frozenset(raw.get("fj_read_verbs") or ()),
        fj_read_groups=frozenset(raw.get("fj_read_groups") or ()),
        fj=tuple(fj),
        forge=tuple(forge),
    )


def _checked_sources(
    action: str, keys: tuple[str, ...], sources: Any
) -> dict[str, Any]:
    sources = dict(sources or {})
    unknown = set(sources) - set(keys)
    if unknown:
        raise TableError(
            f"{action}: {sorted(unknown)} is not in that action's metadata keys"
        )
    return sources


def _compile_path(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    for match in _SEGMENT.finditer(pattern):
        parts.append(re.escape(pattern[index : match.start()]))
        name, is_int = match.group(1), match.group(2)
        parts.append(f"(?P<{name}>[0-9]+)" if is_int else f"(?P<{name}>[^/]+)")
        index = match.end()
    parts.append(re.escape(pattern[index:]))
    return re.compile("".join(parts) + "/?$")


def read_calls(directory: Path) -> list[dict[str, Any]]:
    """Every recorded invocation, oldest first.

    One file per call, because an agent may run these in the background and a
    record carrying captured output is larger than an atomic append.
    """

    if not directory.is_dir() or directory.is_symlink():
        return []
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("argv"), list):
            calls.append((str(value.get("ts") or ""), path.name, value))
    calls.sort(key=lambda item: (item[0], item[1]))
    return [value for _, _, value in calls]


def derive(
    calls: list[dict[str, Any]],
    *,
    repository: str | None = None,
    secrets: tuple[str, ...] = (),
    table: Table | None = None,
) -> list[dict[str, Any]]:
    """The ledger rows for these calls, in the order they were made.

    A call that failed is skipped: an action that did not happen is worse to
    write down than one that is missing. A call naming no repository is
    skipped too — the ledger answers what a wake did to a repository.
    """

    table = table or load_table()
    rows: list[dict[str, Any]] = []
    for call in calls:
        if call.get("exit") != 0:
            continue
        tool = call.get("tool")
        if tool == "fj":
            row = _derive_fj(call, repository, table)
        elif tool == "forge":
            row = _derive_forge(call, table)
        else:
            row = None
        if row is None:
            continue
        actor = call.get("actor")
        rows.append(
            {
                "ts": str(call.get("ts") or ""),
                "source": tool,
                "actor": actor if isinstance(actor, str) else None,
                "repo": row["repo"],
                "action": row["action"],
                "metadata": row["metadata"],
                "cmd": _command(tool, call.get("argv") or [], secrets),
            }
        )
    return rows


def _derive_fj(
    call: dict[str, Any], repository: str | None, table: Table
) -> dict[str, Any] | None:
    argv = [str(value) for value in call.get("argv") or []]
    if not argv:
        return None
    # `fj release create --help` prints usage, exits 0, and acts on nothing;
    # recording it as a release.create put null-tag rows in the ledger that
    # outnumbered the real releases. Help is not an action.
    if _asks_for_help(argv):
        return None
    matched: tuple[Action, FjEntry] | None = None
    for action, entry in table.fj:
        if tuple(argv[: len(entry.words)]) == entry.words:
            matched = (action, entry)
            break

    rest = argv[len(matched[1].words) :] if matched else argv
    repo = _repo_from_args(rest) or _repo_from_fallback(repository)
    if not repo:
        return None
    if matched is None:
        # Recorded rather than dropped: the command is in the recording either
        # way, and a row saying a repository was acted on by a command outside
        # the vocabulary is more use than no row at all.
        return {"repo": repo, "action": "other", "metadata": {}}

    action, entry = matched
    context = {
        "args": _positional(rest),
        # `{arg: number}` reads this rather than `args`, because the number is
        # usually the tail of the repository reference that `args` drops:
        # `fj issue comment swecraft/rich#1` names the issue and the repository in
        # one argument.
        "raw": rest,
        "flags": [value for value in rest if value.startswith("-")],
        "output": f"{call.get('stdout') or ''}\n{call.get('stderr') or ''}",
    }
    return {
        "repo": repo,
        "action": action.action,
        "metadata": _metadata(action, entry.sources, context),
    }


def _derive_forge(call: dict[str, Any], table: Table) -> dict[str, Any] | None:
    argv = [str(value) for value in call.get("argv") or []]
    if len(argv) < 2:
        return None
    method, raw_path = argv[0].upper(), argv[1]
    path = _normalise_path(raw_path)
    request = _request_json(argv[2:])
    response = _parse_json(call.get("stdout"))

    for action, entry in table.forge:
        if entry.method != method:
            continue
        match = entry.pattern.match(path)
        if not match:
            continue
        captures = {
            name: unquote(value) for name, value in (match.groupdict() or {}).items()
        }
        context = {
            "captures": captures,
            "request": request,
            "response": response,
            "args": [],
            "output": "",
        }
        if not _matches(entry.when, context):
            continue
        repo = _repo_from_captures(captures)
        if not repo:
            return None
        return {
            "repo": repo,
            "action": action.action,
            "metadata": _metadata(action, entry.sources, context),
        }

    repo = _repo_from_path(path)
    if not repo:
        # Not an action on a repository. Nearly half of the mutating calls
        # agents make are the agent marking its own inbox read.
        return None
    return {"repo": repo, "action": "other", "metadata": {}}


def _matches(when: dict[str, Any], context: dict[str, Any]) -> bool:
    for key, expected in when.items():
        holder, _, name = key.partition(".")
        value = (context.get(holder) or {}).get(name)
        if expected == "present":
            if value in (None, "", [], {}):
                return False
        elif value != expected:
            return False
    return True


def _metadata(
    action: Action, sources: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    # Every key the action declares, so a row's shape is a property of the
    # action rather than of which producer happened to write it. A key this
    # producer cannot fill is null.
    return {key: _resolve(sources.get(key), context) for key in action.metadata}


def _resolve(source: Any, context: dict[str, Any]) -> Any:
    if source is None:
        return None
    if not isinstance(source, dict):
        raise TableError(f"source must be a mapping: {source!r}")
    if "lookup" in source:
        value = _resolve(source.get("from"), context)
        if not isinstance(value, str):
            return None
        return source["lookup"].get(value.lower(), value.lower())
    if "const" in source:
        return source["const"]
    if "first" in source:
        for candidate in source["first"]:
            value = _resolve(candidate, context)
            if value is not None:
                return value
        return None
    if "choice" in source:
        for flag, value in source["choice"].items():
            if flag in context.get("flags", ()):
                return value
        return None
    if "path" in source:
        return _as_int(context.get("captures", {}).get(source["path"]))
    if "request" in source:
        return _plain(context.get("request", {}).get(source["request"]))
    if "response" in source:
        return _plain(context.get("response", {}).get(source["response"]))
    if "out" in source:
        match = _ANNOUNCED.search(context.get("output") or "")
        return int(match.group(1)) if match else None
    if "flag" in source:
        return _flag_value(source["flag"], context)
    if "arg" in source:
        return _from_args(source["arg"], context)
    raise TableError(f"unknown source: {source!r}")


def _asks_for_help(argv: list[str]) -> bool:
    """Whether this invocation asks fj for usage rather than acting.

    The flag spellings only -- a name or body reading `help` is data -- and
    only in flag position, so a value that happens to be `--help` cannot
    erase the action that carried it.
    """

    index = 0
    while index < len(argv):
        value = argv[index]
        if value in ("--help", "-h"):
            return True
        following = argv[index + 1] if index + 1 < len(argv) else None
        index += 2 if _consumes_next(value, following) else 1
    return False


def _flag_value(names: Any, context: dict[str, Any]) -> str | None:
    """The value carried by the first of these flags that carries one.

    `--create-tag` is boolean in one fj form (`--create-tag -b <notes> <tag>`)
    and value-taking in another (`--create-tag <tag>`), so a following argument
    is only its value when it does not itself look like a flag.
    """

    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise TableError(f"flag source must be a name or a list of names: {names!r}")
    raw: list[str] = context.get("raw") or []
    for name in names:
        prefix = f"{name}="
        for index, value in enumerate(raw):
            if value.startswith(prefix):
                return value[len(prefix) :] or None
            if value == name and index + 1 < len(raw):
                candidate = raw[index + 1]
                if not candidate.startswith("-"):
                    return candidate
    return None


def _from_args(kind: str, context: dict[str, Any]) -> Any:
    args: list[str] = context.get("args") or []
    if kind == "number":
        skip = False
        for value in context.get("raw") or []:
            if skip:
                skip = False
                continue
            if value == "--repo":
                skip = True
                continue
            if value.startswith("--repo=") or value.startswith("-"):
                continue
            ref = _REF_NUMBER.search(value)
            if ref:
                return int(ref.group(1))
            if value.isdigit():
                return int(value)
        return None
    if kind == "rest":
        values = [value for value in args if not value.isdigit()]
        return values or None
    if kind == "first":
        return args[0] if args else None
    if kind == "last":
        return args[-1] if args else None
    if kind == "basename":
        return os.path.basename(args[-1]) if args else None
    raise TableError(f"unknown arg source: {kind}")


# fj flag arity, read off fj's own `--help` rather than guessed. A flag missing
# from these sets has its value read as a positional argument, which is how a
# release body became a tag and an asset path became a repository.
#
#   Usage: fj-bin release create [OPTIONS] <NAME>
#     -T, --create-tag [<CREATE_TAG>]  Defaults to release's name
#     -t, --tag <TAG>                  Pre-existing tag to use
#     -b, --body [<BODY>]              without an argument, opens your editor
#     -C, --cwd <CWD>
_FJ_VALUE_FLAGS = frozenset(
    {
        "-a",
        "--asset",
        "--attach",
        "-B",
        "--branch",
        "-C",
        "--cwd",
        "-H",
        "--host",
        "-m",
        "--message",
        "-R",
        "--remote",
        "-r",
        "--repo",
        "-t",
        "--tag",
        "--title",
        "--assignee",
        "--base",
        "--head",
        "--label",
        "--limit",
        "--milestone",
        "--note",
        "--notes",
        "--output",
        "--page",
        "--state",
        "--target",
        "--token",
    }
)

# Flags whose value is optional. `-b` with no argument opens an editor, and a
# bare `--create-tag` defaults the tag to the release's name, so the following
# argument is only theirs when it is not itself a flag. Consuming it blindly
# would eat the release name; never consuming it would eat the body.
_FJ_OPTIONAL_VALUE_FLAGS = frozenset({"-b", "--body", "-T", "--create-tag"})


def _consumes_next(flag: str, following: str | None) -> bool:
    """Whether this flag takes the argument after it as its value."""

    if flag in _FJ_VALUE_FLAGS:
        return True
    if flag not in _FJ_OPTIONAL_VALUE_FLAGS:
        return False
    return following is not None and not following.startswith("-")


def _positional(args: list[str]) -> list[str]:
    """Positional arguments, with the repository reference removed.

    A value following a *known* value-taking flag is dropped; after an unknown
    bare `--flag` the value is kept, because fj's full flag arity is not known
    here and dropping it would lose `fj release create <repo> <tag>` when a
    bare flag precedes the tag. The recording keeps the full vector either way.
    """

    values: list[str] = []
    dropped_repo = False
    index = 0
    while index < len(args):
        value = args[index]
        if value.startswith("--repo="):
            index += 1
            continue
        following = args[index + 1] if index + 1 < len(args) else None
        if _consumes_next(value, following):
            index += 2
            continue
        if value.startswith("-"):
            index += 1
            continue
        if not dropped_repo and _REPO_ARG.match(value):
            dropped_repo = True
            index += 1
            continue
        values.append(value)
        index += 1
    return values


def _repo_from_args(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        value = args[index]
        if value in ("--repo", "-r") and index + 1 < len(args):
            match = _REPO_ARG.match(args[index + 1])
            if match:
                return match.group(1)
            index += 2
            continue
        if value.startswith("--repo="):
            match = _REPO_ARG.match(value[len("--repo=") :])
            if match:
                return match.group(1)
            index += 1
            continue
        # A value-taking flag's value is not a repository reference, however
        # much it looks like one: `--asset dist/urllib3-2.7.11-py3-none-any.whl`
        # filed thirteen urllib3 releases under a `dist/...` repository that
        # does not exist.
        following = args[index + 1] if index + 1 < len(args) else None
        if _consumes_next(value, following):
            index += 2
            continue
        if not value.startswith("-"):
            match = _REPO_ARG.match(value)
            if match:
                return match.group(1)
        index += 1
    return None


def _repo_from_fallback(repository: str | None) -> str | None:
    if not repository:
        return None
    match = _REPO_ARG.match(repository)
    if match:
        return match.group(1)
    parsed = urlsplit(repository)
    if not parsed.scheme or not parsed.netloc:
        return repository
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return repository
    repo = parts[-1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    candidate = f"{parts[-2]}/{repo}"
    match = _REPO_ARG.match(candidate)
    return match.group(1) if match else repository


def _repo_from_captures(captures: dict[str, str]) -> str | None:
    owner, repo = captures.get("owner"), captures.get("repo")
    if owner and repo:
        return f"{owner}/{repo}"
    return None


def _repo_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{unquote(parts[1])}/{unquote(parts[2])}"
    return None


def _normalise_path(value: str) -> str:
    path = value.split("?", 1)[0].split("#", 1)[0]
    if path.startswith("/api/v1/"):
        path = path[len("/api/v1") :]
    return path


def _request_json(args: list[str]) -> dict[str, Any]:
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"-d", "--data", "--data-raw", "--json"} and index + 1 < len(args):
            return _parse_json(args[index + 1])
        for prefix in ("--data=", "--data-raw=", "--json="):
            if value.startswith(prefix):
                return _parse_json(value[len(prefix) :])
        index += 1
    return {}


def _parse_json(text: Any) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def _as_int(value: Any) -> Any:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _command(tool: Any, argv: list[Any], secrets: tuple[str, ...]) -> str:
    """What was run, with values that could carry a credential taken out.

    Redaction happens against the run's real secret list, which is why this is
    on the host. Long or multi-line values are elided as well: a comment body
    is not a secret, but it is not what a reader of this field wants either.
    """

    words = [str(tool or "")]
    index = 0
    while index < len(argv):
        value = argv[index]
        text = _redact_value(str(value), secrets)
        if text.startswith("-"):
            name, sep, _ = text.partition("=")
            words.append(f"{name}={ELIDED}" if sep else name)
            if not sep and name in SENSITIVE_VALUE_FLAGS and index + 1 < len(argv):
                words.append(ELIDED)
                index += 1
            index += 1
            continue
        if len(text) > ARG_MAX_CHARS or "\n" in text:
            words.append(ELIDED)
            index += 1
            continue
        words.append(text)
        index += 1
    rendered = " ".join(shlex.quote(word) if word != ELIDED else word for word in words)
    return _truncate(rendered, CMD_MAX_BYTES)


def _redact_value(text: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _truncate(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix = "...[truncated]"
    limit = max_bytes - len(suffix)
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", errors="ignore") + suffix
