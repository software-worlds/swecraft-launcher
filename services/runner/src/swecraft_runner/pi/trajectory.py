"""Harbor's trajectory format for pi runs.

pi writes its own session log, one JSON event per line, to ``agent/pi.txt``
(``swecraft_runner.pi.OUTPUT_FILENAME``); claude-code and codex write Harbor's
``agent/trajectory.json`` as well, which the run packaging projects to
``output/trajectory.json`` and the console's transcript view reads. This module
derives that file from pi's log so a pi run is a first-class transcript
everywhere: ``SwecraftPi`` calls :func:`write_trajectory` after each run, and
``python -m swecraft_runner.pi.trajectory --runs <dir>`` backfills runs that predate
it. The raw log stays beside it, untouched.

Mapping: each ``message_end`` with role ``user`` is a ``user`` step; each with
role ``assistant`` is an ``agent`` step whose text parts form the message, whose
``thinking`` parts form ``reasoning_content``, whose ``toolCall`` parts become
``tool_calls`` and whose ``usage`` (the provider's own count for that call)
becomes the step's metrics; each ``message_end`` with role ``toolResult``
attaches to the step that made the call as an ``observation`` result. Token
conventions follow Harbor's claude-code trajectories: ``prompt_tokens`` is the
whole prompt (uncached, cache-read and cache-written tokens), ``cached_tokens``
the cache-read part. Streaming ``tool_execution_*`` events and the ``turn_end``
and ``agent_end`` recaps are dropped. A log cut short (no ``agent_end``) still
converts: whatever messages completed are kept.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ATIF-v1.7"  # what Harbor 0.22 writes for claude-code and codex
TRAJECTORY_FILENAME = "trajectory.json"
LOG_FILENAME = "pi.txt"
AGENT_NAME = "pi"


def _iso(stamp: Any) -> str | None:
    """A timestamp as ISO 8601 UTC; pi writes epoch milliseconds or ISO text."""
    if isinstance(stamp, bool):
        return None
    if isinstance(stamp, (int, float)) and stamp > 0:
        return (
            dt.datetime.fromtimestamp(stamp / 1000, tz=dt.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(stamp, str) and stamp:
        return stamp
    return None


def _parts(content: Any, kind: str, field: str) -> str:
    if isinstance(content, str):
        return content if kind == "text" else ""
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get(field, ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == kind
    ).strip()


def _int(value: Any) -> int:
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0
    )


def events(log: str) -> list[dict[str, Any]]:
    """The JSON events of a pi log, skipping lines that are not JSON objects."""
    out: list[dict[str, Any]] = []
    for line in log.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def convert(log: str) -> dict[str, Any]:
    """A Harbor trajectory for one pi log (the text of ``pi.txt``)."""
    steps: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}  # tool call id -> the agent step that made it
    session_id = None
    started_at = None
    model_name = None
    provider = None
    api = None
    totals = {
        "prompt": 0,
        "completion": 0,
        "cached": 0,
        "cache_write": 0,
        "reasoning": 0,
    }
    cost = 0.0
    priced = False
    for event in events(log):
        kind = event.get("type")
        if kind == "session":
            session_id = event.get("id") or session_id
            started_at = started_at or _iso(event.get("timestamp"))
            continue
        if kind != "message_end":
            continue
        message = event.get("message") or {}
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        stamp = _iso(message.get("timestamp"))
        content = message.get("content")
        if role == "user":
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "timestamp": stamp,
                    "source": "user",
                    "message": _parts(content, "text", "text"),
                    "extra": {},
                }
            )
        elif role == "assistant":
            model_name = message.get("model") or model_name
            provider = message.get("provider") or provider
            api = message.get("api") or api
            usage = (
                message.get("usage") if isinstance(message.get("usage"), dict) else {}
            )
            uncached = _int(usage.get("input"))
            cache_read = _int(usage.get("cacheRead"))
            cache_write = _int(usage.get("cacheWrite"))
            metrics: dict[str, Any] = {
                "prompt_tokens": uncached + cache_read + cache_write,
                "completion_tokens": _int(usage.get("output")),
                "cached_tokens": cache_read,
                "extra": {
                    "input_tokens": uncached,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                    "reasoning_tokens": _int(usage.get("reasoning")),
                },
            }
            step_cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
            if isinstance(step_cost.get("total"), (int, float)):
                metrics["cost_usd"] = float(step_cost["total"])
                cost += float(step_cost["total"])
                priced = True
            step: dict[str, Any] = {
                "step_id": len(steps) + 1,
                "timestamp": stamp,
                "source": "agent",
                "model_name": message.get("model") or model_name,
                "message": _parts(content, "text", "text"),
                "tool_calls": [],
                "observation": {"results": []},
                "metrics": metrics,
                "llm_call_count": 1,
                "extra": {},
            }
            thinking = _parts(content, "thinking", "thinking")
            if thinking:
                step["reasoning_content"] = thinking
            if message.get("stopReason"):
                step["extra"]["stop_reason"] = message["stopReason"]
            for part in content if isinstance(content, list) else []:
                if not (isinstance(part, dict) and part.get("type") == "toolCall"):
                    continue
                call_id = str(part.get("id") or "")
                arguments = part.get("arguments")
                step["tool_calls"].append(
                    {
                        "tool_call_id": call_id,
                        "function_name": part.get("name"),
                        "arguments": arguments
                        if isinstance(arguments, dict)
                        else {"input": arguments},
                    }
                )
                if call_id:
                    calls[call_id] = step
            if not step["tool_calls"]:
                del step["tool_calls"]
                del step["observation"]
            steps.append(step)
            totals["prompt"] += metrics["prompt_tokens"]
            totals["completion"] += metrics["completion_tokens"]
            totals["cached"] += cache_read
            totals["cache_write"] += cache_write
            totals["reasoning"] += metrics["extra"]["reasoning_tokens"]
        elif role == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            step = calls.get(call_id)
            if step is None:
                continue
            step["observation"]["results"].append(
                {
                    "source_call_id": call_id,
                    "content": _parts(content, "text", "text"),
                    "extra": {
                        "tool": message.get("toolName"),
                        "error": bool(message.get("isError")),
                        "timestamp": stamp,
                    },
                }
            )
    for step in steps:
        if step["source"] == "agent" and not step.get("model_name"):
            step["model_name"] = model_name
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "agent": {
            "name": AGENT_NAME,
            "version": None,
            "model_name": model_name,
            "extra": {"provider": provider, "api": api, "log": LOG_FILENAME},
        },
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": totals["prompt"],
            "total_completion_tokens": totals["completion"],
            "total_cached_tokens": totals["cached"],
            "total_cost_usd": cost if priced else None,
            "total_steps": len(steps),
            "extra": {
                "started_at": started_at,
                "total_cache_creation_input_tokens": totals["cache_write"],
                "total_reasoning_tokens": totals["reasoning"],
            },
        },
    }


def write_trajectory(agent_dir: Path, *, force: bool = False) -> Path | None:
    """Write ``trajectory.json`` beside ``pi.txt`` in ``agent_dir``; None if no log.

    An existing file is kept unless ``force``; the raw log is never touched.
    The file is written whole (temporary name, then rename), so a reader
    never sees a half-written trajectory.
    """
    log_path = Path(agent_dir) / LOG_FILENAME
    if not log_path.is_file():
        return None
    target = Path(agent_dir) / TRAJECTORY_FILENAME
    if target.exists() and not force:
        return target
    trajectory = convert(log_path.read_text(encoding="utf-8", errors="replace"))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(trajectory, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return target


def job_finished(job_dir: Path) -> bool:
    """True once Harbor recorded the job's terminal state.

    Harbor 0.22 writes the job's ``result.json`` before the trials start and
    updates it while ``finished_at`` is still null, so the file's existence says
    nothing about completion: only a parseable result with a non-null
    ``finished_at`` does. An unreadable or unfinished result counts as a run
    still streaming, which the backfill leaves alone rather than freezing a
    truncated trajectory that the default run would then keep.
    """
    path = Path(job_dir) / "result.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("finished_at"))


def finished_logs(runs_dir: Path) -> list[Path]:
    """The ``pi.txt`` of every finished run under a coordinator's ``runs`` directory.

    A run is finished once Harbor's job ``result.json`` carries ``finished_at``
    (see ``job_finished``); a wake still streaming is left alone.
    """
    out = []
    for log_path in sorted(
        Path(runs_dir).glob("*/harbor/jobs/*/*/agent/" + LOG_FILENAME)
    ):
        if job_finished(log_path.parents[2]):
            out.append(log_path)
    return out


def backfill(runs_dir: Path, *, force: bool = False) -> dict[str, int]:
    """Write trajectories for every finished pi run under ``runs_dir``."""
    counts = {"written": 0, "kept": 0}
    for log_path in finished_logs(runs_dir):
        target = log_path.with_name(TRAJECTORY_FILENAME)
        if target.exists() and not force:
            counts["kept"] += 1
            continue
        write_trajectory(log_path.parent, force=force)
        counts["written"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive Harbor trajectory.json files from pi session logs.",
    )
    parser.add_argument(
        "--runs",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "a coordinator's .swecraft/runs directory: every finished pi run under it"
            " (repeatable)"
        ),
    )
    parser.add_argument(
        "logs", nargs="*", metavar="PI_TXT", help="individual pi.txt files"
    )
    parser.add_argument(
        "--force", action="store_true", help="rewrite existing trajectory.json files"
    )
    arguments = parser.parse_args(argv)
    if not arguments.runs and not arguments.logs:
        parser.error("give --runs DIR or pi.txt paths")
    total = {"written": 0, "kept": 0}
    for runs in arguments.runs:
        counts = backfill(Path(runs), force=arguments.force)
        print(f"{runs}: written {counts['written']}, kept {counts['kept']}")
        total["written"] += counts["written"]
        total["kept"] += counts["kept"]
    for log in arguments.logs:
        target = write_trajectory(Path(log).parent, force=arguments.force)
        if target is None:
            print(f"{log}: no {LOG_FILENAME}", file=sys.stderr)
            return 1
        print(f"{log} -> {target}")
        total["written"] += 1
    if len(arguments.runs) > 1:
        print(f"total: written {total['written']}, kept {total['kept']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
