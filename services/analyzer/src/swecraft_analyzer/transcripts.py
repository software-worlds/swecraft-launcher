"""Tool calls out of one run's transcript, whatever harness produced it.

Every harness the runner runs leaves an ATIF ``trajectory.json`` (pi's is derived
from its session log), but each one names and shapes its tools differently: claude-code
calls ``Bash`` with ``command``, pi calls ``bash`` with ``command``, and the
codex CLI in code mode calls ``exec`` with JavaScript whose
``tools.exec_command({...})`` calls carry the shell command as ``cmd``. This
module flattens all of them into one row per call: the tool, a coarse kind
(shell, edit, write, read, web, other), the command or path, and the
observation the agent saw, trimmed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OUTPUT_HEAD = 3_000
OUTPUT_TAIL = 1_000
TEXT_LIMIT = 20_000

SHELL = {
    "bash",
    "exec_command",
    "shell",
    "local_shell",
    "run_shell_command",
    "run_terminal_cmd",
    "terminal",
    "write_stdin",
}
EDIT = {
    "edit",
    "multiedit",
    "apply_patch",
    "str_replace",
    "str_replace_editor",
    "replace",
    "edit_file",
    "notebookedit",
}
WRITE = {"write", "write_file", "create_file", "create"}
READ = {"read", "read_file", "view", "grep", "glob", "ls", "list_dir", "find"}
WEB = {"webfetch", "websearch", "web_search", "fetch", "fetch_url", "browser"}

_JS_TOOL = re.compile(r"\btools\.(\w+)\s*\(\s*")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)


@dataclass(frozen=True)
class ToolCall:
    step: int
    call: int
    tool: str
    kind: str
    text: str
    path: str
    output: str


def kind_of(tool: str) -> str:
    name = tool.lower()
    if name in SHELL:
        return "shell"
    if name in EDIT:
        return "edit"
    if name in WRITE:
        return "write"
    if name in READ:
        return "read"
    if name in WEB:
        return "web"
    return "other"


def trim(text: str) -> str:
    if len(text) <= OUTPUT_HEAD + OUTPUT_TAIL:
        return text
    skipped = len(text) - OUTPUT_HEAD - OUTPUT_TAIL
    return (
        text[:OUTPUT_HEAD]
        + f"\n[... {skipped} characters trimmed ...]\n"
        + text[-OUTPUT_TAIL:]
    )


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return " ".join(value)
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return json.dumps(value, ensure_ascii=False)


def _describe(tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """The (text, path) a call is about: its command, patch, path or url."""

    kind = kind_of(tool)
    path = _string(
        arguments.get("file_path")
        or arguments.get("path")
        or arguments.get("filename")
        or arguments.get("notebook_path")
    )
    if kind == "shell":
        text = _string(
            arguments.get("cmd")
            or arguments.get("command")
            or arguments.get("chars")
            or arguments.get("input")
        )
        return text[:TEXT_LIMIT], path
    if tool.lower() == "apply_patch":
        patch = _string(arguments.get("input") or arguments.get("patch") or "")
        files = _PATCH_FILE.findall(patch)
        return patch[:TEXT_LIMIT], path or (files[0] if files else "")
    if kind in ("edit", "write"):
        return path, path
    if kind == "web":
        return _string(arguments.get("url") or arguments.get("query")), path
    text = _string(
        arguments.get("command")
        or arguments.get("cmd")
        or path
        or arguments.get("pattern")
        or arguments.get("url")
        or arguments.get("query")
    )
    return text[:TEXT_LIMIT], path


_DECODER = json.JSONDecoder()
_ASSIGN = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*")
_FOR_OF = re.compile(
    r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s+of\s+([A-Za-z_$][\w$]*)"
)
_FIELDS = ("cmd", "command", "input", "patch", "chars", "path", "file_path")
_FIELD = re.compile(r"[{,]\s*[\"']?(" + "|".join(_FIELDS) + r")[\"']?\s*(:|,|\})")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


def _js_value(source: str, start: int, names: dict[str, Any]) -> Any:
    """The literal, array of literals, or known variable at ``source[start]``."""

    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source):
        return None
    head = source[start]
    if head in '"[':
        try:
            return _DECODER.raw_decode(source, start)[0]
        except ValueError:
            return None
    if head in "'`":
        out, index = [], start + 1
        while index < len(source) and source[index] != head:
            if source[index] == "\\" and index + 1 < len(source):
                escaped = source[index + 1]
                out.append({"n": "\n", "t": "\t"}.get(escaped, escaped))
                index += 2
                continue
            out.append(source[index])
            index += 1
        return "".join(out)
    identifier = _IDENTIFIER.match(source, start)
    if identifier:
        return names.get(identifier.group(0))
    return None


def _js_object_end(source: str, start: int) -> int:
    """Index just past the ``{...}`` opening at ``start``, quotes respected."""

    depth, index, quote = 0, start, ""
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(source)


def _js_call_end(source: str, open_paren: int) -> int:
    """Index just past the ``)`` closing the call opened at ``open_paren``."""

    depth, index, quote = 0, open_paren, ""
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(source)


def js_tool_calls(source: str) -> list[tuple[str, dict[str, Any]]]:
    """``tools.<name>(...)`` calls inside a codex code-mode ``exec`` input."""

    return js_parse(source)[0]


def js_parse(source: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """(resolved tool calls, the source with those calls cut out).

    Codex writes JavaScript, not JSON: arguments arrive as object literals
    with bare keys, shorthand properties (``{cmd, workdir}``), variables
    assigned earlier (``const patch = "..."; tools.apply_patch(patch)``) and
    loops over arrays of commands. String and array literals bound to names
    are resolved; a list value yields one call per element. A call counts as
    resolved only when every command-bearing field it names was resolved; the
    rest of the source, unresolved calls included, comes back as leftover text
    so that nothing an agent ran is invisible to the detectors.
    """

    names: dict[str, Any] = {}
    for match in _ASSIGN.finditer(source):
        value = _js_value(source, match.end(), names)
        if isinstance(value, str) or (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            names[match.group(1)] = value
    for match in _FOR_OF.finditer(source):
        if isinstance(names.get(match.group(2)), list):
            names[match.group(1)] = names[match.group(2)]

    calls: list[tuple[str, dict[str, Any]]] = []
    resolved_spans: list[tuple[int, int]] = []
    for match in _JS_TOOL.finditer(source):
        name, start = match.group(1), match.end()
        open_paren = source.rfind("(", match.start(), match.end())
        arguments: dict[str, Any] = {}
        complete = True
        if source.startswith("{", start):
            try:
                value = _DECODER.raw_decode(source, start)[0]
            except ValueError:
                value = None
            if isinstance(value, dict):
                arguments = value
            else:
                body = source[start : _js_object_end(source, start)]
                for field in _FIELD.finditer(body):
                    key, separator = field.group(1), field.group(2)
                    if separator == ":":
                        resolved = _js_value(body, field.end(), names)
                    else:
                        resolved = names.get(key)
                    if resolved is None:
                        complete = False
                    elif key not in arguments:
                        arguments[key] = resolved
        else:
            value = _js_value(source, start, names)
            if value is not None:
                arguments = {"input": value}
        if not arguments or not complete:
            continue
        resolved_spans.append((match.start(), _js_call_end(source, open_paren)))
        lists = [key for key, value in arguments.items() if isinstance(value, list)]
        if lists:
            for item in arguments[lists[0]]:
                calls.append((name, {**arguments, lists[0]: item}))
        else:
            calls.append((name, arguments))
    leftover, cursor = [], 0
    for begin, end in resolved_spans:
        leftover.append(source[cursor:begin])
        cursor = max(cursor, end)
    leftover.append(source[cursor:])
    return calls, "".join(leftover)


def _observations(step: dict[str, Any]) -> dict[str, str]:
    observation = step.get("observation") or {}
    results = observation.get("results") if isinstance(observation, dict) else None
    outputs: dict[str, str] = {}
    for result in results or []:
        if isinstance(result, dict):
            key = str(result.get("source_call_id") or "")
            outputs[key] = _string(result.get("content"))
    return outputs


def from_atif(payload: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, step in enumerate(payload.get("steps") or []):
        if not isinstance(step, dict):
            continue
        step_id = step.get("step_id")
        step_no = step_id if isinstance(step_id, int) else index + 1
        outputs = _observations(step)
        for call in step.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            tool = str(call.get("function_name") or call.get("name") or "?")
            arguments = call.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {"input": _string(arguments)}
            output = trim(outputs.get(str(call.get("tool_call_id") or ""), ""))
            code_mode = tool == "exec" and "input" in arguments
            inner, leftover = (
                js_parse(_string(arguments.get("input"))) if code_mode else ([], "")
            )
            if inner and _JS_TOOL.search(leftover):
                # Some calls resolved, others did not: the unresolved rest is
                # kept as shell text beside the resolved calls.
                calls.append(
                    ToolCall(
                        step=step_no,
                        call=len(calls),
                        tool="exec",
                        kind="shell",
                        text=leftover[:TEXT_LIMIT],
                        path="",
                        output="",
                    )
                )
            if code_mode and not inner:
                # Unparsed code-mode JavaScript still reaches every detector
                # as shell text: a parser gap must never become a blind spot.
                calls.append(
                    ToolCall(
                        step=step_no,
                        call=len(calls),
                        tool="exec",
                        kind="shell",
                        text=_string(arguments.get("input"))[:TEXT_LIMIT],
                        path="",
                        output=output,
                    )
                )
                continue
            if inner:
                for position, (name, inner_arguments) in enumerate(inner):
                    text, path = _describe(name, inner_arguments)
                    calls.append(
                        ToolCall(
                            step=step_no,
                            call=len(calls),
                            tool=name,
                            kind=kind_of(name),
                            text=text,
                            path=path,
                            output=output if position == 0 else "",
                        )
                    )
                continue
            text, path = _describe(tool, arguments)
            calls.append(
                ToolCall(
                    step=step_no,
                    call=len(calls),
                    tool=tool,
                    kind=kind_of(tool),
                    text=text,
                    path=path,
                    output=output,
                )
            )
    return calls


def from_pi_log(text: str) -> list[ToolCall]:
    """Pi runs captured before their trajectory was derived.

    The log goes through the same converter that writes pi's
    ``trajectory.json``, so step numbers are the ones the console's
    transcript tab shows and a ``wake`` pointer lands on the cited step.
    Collect runs on the host, where ``swecraft_runner`` is installed; an analyst's
    container never reads pi logs, so the import stays inside.
    """

    from swecraft_runner.pi.trajectory import convert

    return from_atif(convert(text))


def _read(trajectories: list[Path], pi_logs: list[Path]) -> tuple[str, list[ToolCall]]:
    for trajectory in trajectories:
        if not trajectory.is_file():
            continue
        try:
            payload = json.loads(
                trajectory.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            return "trajectory", from_atif(payload)
    for log in pi_logs:
        if log.is_file():
            calls = from_pi_log(log.read_text(encoding="utf-8", errors="replace"))
            if calls:
                return "pi-log", calls
    return "none", []


def read_run(output_dir: Path) -> tuple[str, list[ToolCall]]:
    """(transcript source, tool calls) for one run's ``output/`` directory."""

    return _read([output_dir / "trajectory.json"], [output_dir / "agent-stdout.txt"])


def read_live_run(run_dir: Path) -> tuple[str, list[ToolCall]]:
    """The same from a live ``.swecraft/runs/<run-id>/`` tree, Harbor's job included.

    Checkpoints taken before pi runs carried a derived trajectory keep
    no transcript for them; the cell's own run directory still holds pi's log
    under ``harbor/jobs/*/*/agent/``.
    """

    agents = sorted(run_dir.glob("harbor/jobs/*/*/agent"))
    source, calls = _read(
        [run_dir / "output" / "trajectory.json"]
        + [agent / "trajectory.json" for agent in agents],
        [run_dir / "output" / "agent-stdout.txt"]
        + [agent / "pi.txt" for agent in agents],
    )
    return (f"live-{source}" if calls else "none"), calls
