#!/usr/bin/env python3
"""Stdlib harness staged into an evaluator image: run one plan, write one outcome.

The host mounts one directory at ``/eval`` containing ``plan.json``, the
subject sources, and this file; the container runs offline. Every command's
stdout/stderr lands in ``/eval/out`` beside ``outcome.json``.

Plan shape (all commands are ``{argv, env?, cwd?, timeout_seconds}``)::

    {"setup": [cmd...], "install": [cmd...], "gate": cmd,
     "tasks": [{"name": ..., "command": cmd,
                "walltime_repeats": 3, "metrics": ["walltime", ...]}]}

Metric semantics:

- ``walltime`` — N timed repeats, raw seconds.
- ``codspeed-walltime`` — one pytest-codspeed timing run; the sample is the
  sum over benchmarks of each benchmark's minimum per-iteration time.
- ``codspeed-cpu-sim`` — one pytest-codspeed simulation run under stock
  Callgrind; the sample is the task-level ``Collected`` instruction total.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path("/eval")
OUT = ROOT / "out"
DEFAULT_CWD = "/study/ruler"
MIN_TIME_PLUGIN = "_hub_codspeed_min_time"
MIN_TIME_PLUGIN_SOURCE = """\
import pytest

def pytest_collection_modifyitems(config, items):
    # Pin round sizing on existing benchmarks only: marking every item
    # would turn plain tests into measured benchmarks.
    marker = pytest.mark.benchmark(min_time=0.001)
    for item in items:
        if "benchmark" in getattr(item, "fixturenames", ()) or (
            item.get_closest_marker("benchmark") is not None
        ):
            item.add_marker(marker)
"""
COLLECTED_RE = re.compile(r"\bCollected\s*:\s*([0-9][0-9,]*)")


# Default thread-pool caps for numerical runtimes. Every BLAS/OpenMP/torch
# pool otherwise sizes itself to the host's logical CPUs, so N concurrent
# benchmark containers on a 256-CPU host spawn N x 256 threads for work
# that fits in a few (about 60 concurrent turns once oversubscribed 128
# physical cores several-fold and drove load past 40k). Set through setdefault so a
# recipe's own ``env`` still wins per command.
THREAD_CAP_DEFAULTS = {
    "OMP_NUM_THREADS": "4",
    "OPENBLAS_NUM_THREADS": "4",
    "MKL_NUM_THREADS": "4",
    "NUMEXPR_NUM_THREADS": "4",
    "VECLIB_MAXIMUM_THREADS": "4",
}


def main() -> int:
    os.environ.setdefault("PYTHONHASHSEED", "0")
    for name, value in THREAD_CAP_DEFAULTS.items():
        os.environ.setdefault(name, value)
    OUT.mkdir(parents=True, exist_ok=True)
    plan = json.loads((ROOT / "plan.json").read_text(encoding="utf-8"))
    outcome: dict[str, Any] = {"gate": None, "tasks": {}, "error": ""}
    try:
        for index, command in enumerate(plan.get("setup", [])):
            _run(command, f"setup-{index}", check=True)
        for index, command in enumerate(plan.get("install", [])):
            _run(command, f"install-{index}", check=True)
        gate = _run(plan["gate"], "gate", check=False)
        outcome["gate"] = gate.returncode == 0
        if outcome["gate"]:
            for task in plan["tasks"]:
                outcome["tasks"][task["name"]] = _task(task)
    except Exception as error:  # recorded; the host decides what it voids
        outcome["error"] = f"{type(error).__name__}: {error}"[:1000]
    finally:
        (OUT / "outcome.json").write_text(
            json.dumps(outcome, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


def _task(task: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"error": ""}
    try:
        for metric in task["metrics"]:
            if metric == "walltime":
                result["walltime"] = _walltime(
                    task["command"],
                    f"{task['name']}-walltime",
                    int(task.get("walltime_repeats", 3)),
                )
            elif metric == "codspeed-walltime":
                result["codspeed-walltime"] = _codspeed_walltime(
                    task["command"], f"{task['name']}-codspeed-walltime"
                )
            elif metric == "codspeed-cpu-sim":
                result["codspeed-cpu-sim"] = _codspeed_cpu_sim(
                    task["command"], f"{task['name']}-codspeed-cpu-sim"
                )
            else:
                raise RuntimeError(f"unknown metric: {metric}")
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"[:1000]
    return result


def _walltime(command: dict[str, Any], label: str, repeats: int) -> dict[str, Any]:
    samples = []
    for repeat in range(repeats):
        start = time.perf_counter()
        _run(command, f"{label}-{repeat}", check=True)
        samples.append(time.perf_counter() - start)
    return {"samples": samples}


def _codspeed_walltime(command: dict[str, Any], label: str) -> dict[str, Any]:
    results_dir = Path(command.get("cwd") or DEFAULT_CWD) / ".codspeed"
    shutil.rmtree(results_dir, ignore_errors=True)
    _run(_pytest_codspeed_command(command, ["--codspeed"]), label, check=True)
    files = sorted(results_dir.glob("results_*.json"))
    if not files:
        raise RuntimeError(f"{label} wrote no codspeed results file")
    benchmarks: dict[str, dict[str, Any]] = {}
    for file in files:
        benchmarks.update(codspeed_benchmarks(file.read_text(encoding="utf-8")))
    if not benchmarks:
        raise RuntimeError(f"{label} reported no benchmarks")
    return {
        "seconds": _codspeed_seconds(benchmarks),
        "benchmarks": benchmarks,
    }


def _codspeed_cpu_sim(command: dict[str, Any], label: str) -> dict[str, Any]:
    profile = OUT / f"{_safe(label)}.callgrind"
    _delete_callgrind_profiles(profile)
    try:
        measured = _pytest_codspeed_command(
            command,
            ["--codspeed", "--codspeed-mode", "simulation"],
            {"CODSPEED_ENV": "1"},
        )
        measured = {
            **measured,
            "argv": [
                "valgrind",
                "--tool=callgrind",
                "--instr-atstart=no",
                f"--callgrind-out-file={profile}",
                *measured["argv"],
            ],
        }
        completed = _run(measured, label, check=True)
        benchmarks = callgrind_benchmarks(profile)
        if not benchmarks:
            raise RuntimeError(f"{label} reported no benchmark callgrind dumps")
        return {
            "instructions": callgrind_collected(completed.stderr),
            "benchmarks": benchmarks,
        }
    finally:
        _delete_callgrind_profiles(profile)


def codspeed_benchmarks(text: str) -> dict[str, dict[str, Any]]:
    payload = json.loads(text)
    benchmarks: dict[str, dict[str, Any]] = {}
    for bench in payload.get("benchmarks", []):
        stats = bench.get("stats")
        if not isinstance(stats, dict):
            continue
        key = str(bench.get("uri") or bench.get("name"))
        benchmarks[key] = stats
    return benchmarks


def callgrind_collected(stderr: str) -> int:
    matches = COLLECTED_RE.findall(stderr)
    if not matches:
        raise RuntimeError("callgrind stderr did not contain a Collected total")
    return int(matches[-1].replace(",", ""))


def callgrind_benchmarks(profile: Path) -> dict[str, dict[str, int]]:
    benchmarks: dict[str, dict[str, int]] = {}
    for path in _callgrind_profiles(profile):
        parsed = callgrind_benchmark(path.read_text(encoding="utf-8"))
        if parsed is None:
            continue
        uri, instructions = parsed
        entry = benchmarks.setdefault(uri, {"instructions": 0})
        entry["instructions"] += instructions
    return benchmarks


def callgrind_benchmark(text: str) -> tuple[str, int] | None:
    # Real part dumps report zeros on the ``summary:`` header line; the Ir
    # total for the part is the ``totals:`` line at its end, and the trigger
    # is prefixed with ``Client Request:`` by Callgrind's dump-stats-at.
    trigger = None
    events: list[str] = []
    totals: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("desc: Trigger:"):
            trigger = line.split(":", 2)[2].strip()
        elif line.startswith("events:"):
            events = line.split(":", 1)[1].split()
        elif line.startswith("totals:"):
            totals = line.split(":", 1)[1].split()
    if trigger is None or trigger == "Program termination":
        return None
    trigger = trigger.removeprefix("Client Request:").strip()
    if trigger.startswith("Metadata:"):
        return None
    if "Ir" not in events:
        raise RuntimeError(f"callgrind dump for {trigger} has no Ir event")
    index = events.index("Ir")
    if index >= len(totals):
        raise RuntimeError(f"callgrind dump for {trigger} has no Ir total")
    return trigger, int(totals[index].replace(",", ""))


def _codspeed_seconds(benchmarks: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for uri, stats in benchmarks.items():
        value = stats.get("min_ns")
        if not isinstance(value, int | float):
            raise RuntimeError(f"codspeed benchmark {uri} has no min_ns")
        total += float(value) / 1e9
    return total


def _pytest_codspeed_command(
    command: dict[str, Any],
    extra_args: list[str],
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    _stage_min_time_plugin()
    env = {
        **command.get("env", {}),
        **(extra_env or {}),
    }
    env["PYTHONPATH"] = _prepend_pythonpath(ROOT, env.get("PYTHONPATH"))
    return {
        **command,
        "argv": _append_pytest_args(
            command["argv"],
            [
                "-p",
                "no:codspeed",
                "-p",
                "pytest_codspeed.plugin",
                "-p",
                MIN_TIME_PLUGIN,
                *extra_args,
            ],
        ),
        "env": env,
    }


def _stage_min_time_plugin() -> None:
    (ROOT / f"{MIN_TIME_PLUGIN}.py").write_text(
        MIN_TIME_PLUGIN_SOURCE, encoding="utf-8"
    )


def _prepend_pythonpath(path: Path, current: str | None) -> str:
    prefix = str(path)
    if not current:
        return prefix
    return f"{prefix}{os.pathsep}{current}"


def _append_pytest_args(argv: list[str], args: list[str]) -> list[str]:
    if "--" not in argv:
        return [*argv, *args]
    index = argv.index("--")
    return [*argv[:index], *args, *argv[index:]]


def _callgrind_profiles(profile: Path) -> list[Path]:
    return sorted(
        path for path in profile.parent.glob(f"{profile.name}*") if path.is_file()
    )


def _delete_callgrind_profiles(profile: Path) -> None:
    for path in _callgrind_profiles(profile):
        path.unlink(missing_ok=True)


def _run(
    command: dict[str, Any], label: str, *, check: bool
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command["argv"],
            cwd=command.get("cwd") or DEFAULT_CWD,
            env={**os.environ, **command.get("env", {})},
            capture_output=True,
            text=True,
            timeout=command["timeout_seconds"],
        )
    except subprocess.TimeoutExpired as error:
        # The captured output is the only diagnostic a timed-out command
        # leaves behind; drop it and the timeout is undebuggable.
        _write_logs(label, error.stdout, error.stderr)
        raise
    _write_logs(label, completed.stdout, completed.stderr)
    if check and completed.returncode:
        raise RuntimeError(f"{label} exited with {completed.returncode}")
    return completed


def _write_logs(label: str, stdout: object, stderr: object) -> None:
    for stream, name in ((stdout, "stdout"), (stderr, "stderr")):
        text = (
            stream.decode("utf-8", errors="replace")
            if isinstance(stream, bytes)
            else (stream or "")
        )
        (OUT / f"{_safe(label)}.{name}.log").write_text(text, encoding="utf-8")


def _safe(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in ".-" else "_"
        for character in value
    )


if __name__ == "__main__":
    raise SystemExit(main())
