#!/usr/bin/env python3
"""Small Harbor CLI test double that writes a realistic one-trial job."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    arguments = sys.argv[1:]
    mode = os.environ.get("FAKE_HARBOR_MODE", "changed")
    if arguments == ["--version"]:
        print("0.18.0" if mode == "bad-version" else "0.22.0")
        return 0
    if arguments[:2] != ["exec", "--config"] or len(arguments) != 3:
        return 2
    if mode == "hang":
        secret = os.environ.get("TEST_AGENT_SECRET", "")
        print(f"partial stdout secret: {secret}", flush=True)
        print(f"partial stderr secret: {secret}", file=sys.stderr, flush=True)
        time.sleep(30)
        return 0
    config = json.loads(Path(arguments[2]).read_text(encoding="utf-8"))
    compile_config = config["map"]["compile"]
    job_config = config["map"]["job"]
    environment = compile_config["environments"][0]
    if "path" in environment:
        environment_root = Path(environment["path"])
        paths = [
            str(path)
            for path in environment_root.iterdir()
            if path.name != "docker-compose.yaml"
        ]
        repository = environment_root / "repository"
    else:
        environment_root = None
        paths = environment["paths"]
        repository = Path(paths[0])
    if os.environ.get("FAKE_EXPECT_TOOLS") == "1":
        assert any(Path(path).name == "tools" for path in paths)
    if os.environ.get("FAKE_EXPECT_DOCKER_DAEMON") == "1":
        assert environment_root is not None
        compose_text = (environment_root / "docker-compose.yaml").read_text()
        compose = json.loads(compose_text)
        main = compose["services"]["main"]
        dind = compose["services"]["dind"]
        assert main["environment"]["DOCKER_HOST"] == "tcp://dind:2375"
        assert main.get("privileged") is None
        assert dind["privileged"] is True
        assert "dind-rootless@sha256:" in dind["image"]
        assert dind["volumes"] == ["dind-data:/home/rootless/.local/share/docker"]
        assert "docker.sock" not in compose_text
        assert "ports" not in dind
        # PID 1 must be a finite clock, never `sleep infinity`: a turn whose
        # supervisor dies has to reap itself on schedule.
        assert main["command"][:2] == ["sh", "-c"]
        assert main["command"][2].startswith("sleep ")
        assert main["command"][2].split()[1].isdigit()
        assert "infinity" not in main["command"][2]
        # The sidecar gets the same finite clock via busybox timeout so its
        # daemon — and every nested workload it hosts — dies on schedule.
        assert dind["entrypoint"][:3] == ["timeout", "-s", "KILL"]
        assert dind["entrypoint"][3].isdigit()
        assert dind["entrypoint"][3] == main["command"][2].split()[1]
        assert dind["entrypoint"][4] == "dockerd-entrypoint.sh"
    agent = job_config["agents"][0]
    assert agent["override_timeout_sec"] >= 1
    if os.environ.get("FAKE_EXPECT_RESOLVABLE_ENV") == "1":
        # Real Harbor redacts any env value whose name looks sensitive when
        # it cannot resolve the same value from its own environment, and the
        # redacted string is what reaches the container. Stand in for that
        # rule: every agent env value must be visible to this process.
        for name, value in agent["env"].items():
            assert os.environ.get(name) == value, name
    if os.environ.get("FAKE_EXPECT_IMPORT_PATH"):
        assert agent["import_path"] == os.environ["FAKE_EXPECT_IMPORT_PATH"]
        assert "name" not in agent
        assert "SWECRAFT_RESUME" not in agent.get("env", {})
    if os.environ.get("FAKE_EXPECT_RESUME") == "1":
        assert any(Path(path).name == "prior-session" for path in paths)
        assert agent["import_path"].endswith("ResumingClaudeCode")
        assert agent["env"].get("SWECRAFT_RESUME") == "1"
        # Harbor prefers `name` over `import_path`; it must be absent.
        assert "name" not in agent
    jobs_dir = Path(job_config["jobs_dir"])
    job_name = job_config["job_name"]
    job_dir = jobs_dir / job_name
    # Real Harbor persists job/trial configs and locks, secrets included.
    write_json(job_dir / "config.json", job_config)
    write_json(job_dir / "lock.json", {"agents": [agent]})
    write_json(job_dir / "trial-1" / "config.json", {"agent": agent})
    write_json(job_dir / "trial-1" / "lock.json", {"agent": agent})
    trial_dir = job_dir / "trial-1"
    artifacts = trial_dir / "artifacts" / "app"
    collected = artifacts / "repository"
    shutil.copytree(repository, collected, symlinks=True)

    if mode == "changed":
        existing = collected / "existing.txt"
        if existing.exists():
            existing.write_text("changed\n", encoding="utf-8")
        else:
            readme = collected / "README.md"
            readme.write_text(readme.read_text() + "\nChanged by fake Harbor.\n")
        (collected / "new.txt").write_text("new\n", encoding="utf-8")
        delete = collected / "delete.txt"
        if delete.exists():
            delete.unlink()
        os.symlink("/etc/passwd", collected / "outside-link")
    if os.environ.get("FAKE_DEBRIS") == "1":
        # Stand in for a test run's build artifacts: a virtualenv tree and
        # a bytecode cache the repository's .gitignore excludes.
        site = collected / ".venv" / "lib"
        site.mkdir(parents=True, exist_ok=True)
        (site / "site.py").write_text("debris\n", encoding="utf-8")
        cache = collected / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "module.pyc").write_text("debris\n", encoding="utf-8")
    (artifacts / "response.md").write_text("Fake agent completed the task.\n")
    # An agent that touched the forge leaves recorded invocations behind, one
    # file per call, exactly as the image's wrappers write them. They are
    # written unconditionally, including in the failure modes below, because a
    # wake that mutated the forge and then failed collection is exactly the
    # case whose record must survive.
    calls = artifacts / ".swecraft" / "forge-calls"
    calls.mkdir(parents=True, exist_ok=True)
    call_secret = os.environ.get("FAKE_FORGE_CALL_SECRET", "")
    (calls / "call-aaaaaaaaaa").write_text(
        json.dumps(
            {
                "ts": "2026-08-12T08:20:15Z",
                "tool": "fj",
                "cwd": "/app/repository",
                "actor": "demo-bot",
                "exit": 0,
                "argv": [
                    "issue",
                    "create",
                    "hub/demo",
                    "--title",
                    call_secret or "T",
                ],
                "stdout": call_secret,
                "stderr": f"created issue #4\n{call_secret}",
            }
        ),
        encoding="utf-8",
    )
    (calls / "call-bbbbbbbbbb").write_text(
        "truncated, dropped on the way out{", "utf-8"
    )
    (calls / "call-cccccccccc").write_text(
        json.dumps(
            {
                "ts": "2026-08-12T08:20:16Z",
                "tool": "fj",
                "cwd": "/app/repository",
                "actor": "demo-bot",
                "exit": 0,
                "argv": ["pr", "merge", "hub/demo#2", "--squash"],
                "stdout": "",
                "stderr": "",
            }
        ),
        encoding="utf-8",
    )
    manifest = [
        {"source": "/app/response.md", "status": "ok"},
        {"source": "/app/repository", "status": "ok"},
        {"source": "/app/.swecraft/forge-calls", "status": "ok"},
    ]
    if mode == "missing-artifact":
        manifest[0]["status"] = "missing"
    if "/app/memory" in compile_config.get("artifacts", []):
        memory_source = next(
            Path(path) for path in paths if Path(path).name == "memory"
        )
        collected_memory = artifacts / "memory"
        shutil.copytree(memory_source, collected_memory, symlinks=True)
        memory_mode = os.environ.get("FAKE_MEMORY_MODE", "unchanged")
        if memory_mode == "changed":
            notes = collected_memory / "MEMORY.md"
            previous = notes.read_text() if notes.exists() else ""
            notes.write_text(previous + "- learned something\n", encoding="utf-8")
            daily = collected_memory / "daily"
            daily.mkdir(exist_ok=True)
            (daily / "2026-01-01.md").write_text("- did a thing\n", encoding="utf-8")
        elif memory_mode == "committed":
            # An agent that commits inside /app/memory instead of leaving
            # working-tree edits.
            notes = collected_memory / "MEMORY.md"
            previous = notes.read_text() if notes.exists() else ""
            notes.write_text(previous + "- committed lesson\n", encoding="utf-8")
            for command in (
                ("git", "-C", str(collected_memory), "add", "--all"),
                (
                    "git",
                    "-C",
                    str(collected_memory),
                    "-c",
                    "user.name=Agent",
                    "-c",
                    "user.email=agent@localhost",
                    "commit",
                    "-m",
                    "agent memory commit",
                ),
            ):
                subprocess.run(command, check=True, capture_output=True)
        elif memory_mode == "hostile-git":
            notes = collected_memory / "MEMORY.md"
            previous = notes.read_text() if notes.exists() else ""
            notes.write_text(
                previous + "- hostile metadata ignored\n", encoding="utf-8"
            )
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(collected_memory),
                    "config",
                    "core.fsmonitor",
                    os.environ["FAKE_MEMORY_FSMONITOR"],
                ),
                check=True,
            )
        manifest.append({"source": "/app/memory", "status": "ok"})
    if "/app/result.json" in compile_config.get("artifacts", []):
        structured_mode = os.environ.get("FAKE_STRUCTURED", "valid")
        if structured_mode == "valid":
            write_json(
                artifacts / "result.json",
                {
                    "commit_message": "fake: focused change",
                    "pr_title": "Fake focused change",
                    "pr_body": "Fake body.",
                },
            )
            manifest.append({"source": "/app/result.json", "status": "ok"})
        elif structured_mode == "invalid":
            (artifacts / "result.json").write_text("not json", encoding="utf-8")
            manifest.append({"source": "/app/result.json", "status": "ok"})
        else:
            manifest.append({"source": "/app/result.json", "status": "missing"})
    write_json(trial_dir / "artifacts" / "manifest.json", manifest)
    if os.environ.get("FAKE_SESSIONS", "present") == "present":
        transcripts = trial_dir / "agent" / "sessions" / "projects" / "-app"
        transcripts.mkdir(parents=True, exist_ok=True)
        (transcripts / "fake-session.jsonl").write_text(
            '{"type": "assistant", "text": "prior turn"}\n', encoding="utf-8"
        )
        # Claude Code also writes nested transcripts that belong to the
        # session (e.g. subagent conversations).
        nested = transcripts / "subagents"
        nested.mkdir(exist_ok=True)
        (nested / "nested-session.jsonl").write_text(
            '{"type": "assistant", "text": "subagent turn"}\n', encoding="utf-8"
        )
    write_json(
        trial_dir / "result.json",
        {
            "finished_at": "2026-01-01T00:00:01Z",
            "exception_info": None,
            "agent": {"env": agent.get("env", {})},
            "agent_result": {
                "n_input_tokens": int(os.environ.get("FAKE_INPUT_TOKENS", "10")),
                "n_cache_tokens": int(os.environ.get("FAKE_CACHE_TOKENS", "0")),
                "n_output_tokens": int(os.environ.get("FAKE_OUTPUT_TOKENS", "5")),
                "cost_usd": (
                    None
                    if os.environ.get("FAKE_COST_USD") == "none"
                    else float(os.environ.get("FAKE_COST_USD", "0.01"))
                ),
            },
        },
    )
    completed = 0 if mode == "bad-receipt" else 1
    write_json(
        job_dir / "result.json",
        {
            "finished_at": "2026-01-01T00:00:02Z",
            "n_total_trials": 1,
            "stats": {
                "n_completed_trials": completed,
                "n_errored_trials": 1 - completed,
            },
        },
    )
    secret = os.environ.get("TEST_AGENT_SECRET")
    if secret:
        print(f"stdout secret: {secret}")
        print(f"stderr secret: {secret}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
