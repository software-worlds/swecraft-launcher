from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swecraft_runner.pricing import ModelPricing
from swecraft_runner.runner import (
    DockerDaemonConfig,
    RunnerError,
    RunRequest,
    WorkRepository,
    _checkout,
    _collect_session,
    _fetch_environment,
    run_once,
)

HERE = Path(__file__).parent
FAKE_HARBOR = (sys.executable, str(HERE / "fake_harbor.py"))


def seed_repository(root: Path) -> tuple[Path, str]:
    repository = root / "source"
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Hub Test"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "hub@localhost"),
        check=True,
    )
    (repository / "existing.txt").write_text("original\n", encoding="utf-8")
    (repository / "delete.txt").write_text("delete me\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-m", "fixture"),
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, sha


def add_commit(repository: Path, name: str, content: str) -> str:
    (repository / name).write_text(content, encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-m", f"add {name}"),
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def checkout_git(run_dir: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(run_dir / "input" / "repository"), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class RunnerTests(unittest.TestCase):
    def test_returns_response_and_precise_changed_file_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, sha = seed_repository(root)
            tools = root / "supplied-tools"
            tools.mkdir()
            (tools / "inspect.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            secret = "secret-that-must-not-land-in-logs"
            with patch.dict(
                os.environ,
                {
                    "TEST_AGENT_SECRET": secret,
                    "FAKE_EXPECT_TOOLS": "1",
                },
            ):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Make a focused change.",
                        model="test/model",
                        output_dir=root / "runs",
                        tool_directory=tools,
                        agent_env_names=("TEST_AGENT_SECRET",),
                        harbor_command=FAKE_HARBOR,
                    )
                )

            self.assertEqual(result.source_sha, sha)
            self.assertEqual(
                result.changed_files,
                ("delete.txt", "existing.txt", "new.txt", "outside-link"),
            )
            self.assertEqual(
                result.response_path.read_text(), "Fake agent completed the task.\n"
            )
            assert result.repository_path is not None
            self.assertEqual(
                (result.repository_path / "existing.txt").read_text(), "changed\n"
            )
            self.assertTrue((result.repository_path / ".git").is_dir())
            self.assertEqual(
                (result.changes_path / "existing.txt").read_text(), "changed\n"
            )
            self.assertEqual((result.changes_path / "new.txt").read_text(), "new\n")
            self.assertFalse((result.changes_path / "delete.txt").exists())
            self.assertFalse((result.changes_path / "outside-link").exists())
            changes = json.loads(
                (result.run_dir / "output" / "changes.json").read_text()
            )
            symlink = next(
                item for item in changes["files"] if item["path"] == "outside-link"
            )
            self.assertEqual(symlink["kind"], "symlink")
            self.assertEqual(symlink["target"], "/etc/passwd")
            self.assertEqual((repository / "existing.txt").read_text(), "original\n")
            self.assertTrue((repository / "delete.txt").exists())
            self.assertFalse((repository / "new.txt").exists())
            self.assertIn(
                "/app/tools", (result.run_dir / "input" / "prompt.md").read_text()
            )
            harbor_files = {
                path: path.read_text(errors="replace")
                for path in (result.run_dir / "harbor").rglob("*")
                if path.is_file()
            }
            for path, content in harbor_files.items():
                self.assertNotIn(secret, content, f"secret leaked into {path}")
            logs = "".join(
                content
                for path, content in harbor_files.items()
                if path.suffix == ".log"
            )
            self.assertIn("[REDACTED]", logs)
            config_text = harbor_files[result.run_dir / "harbor" / "exec-config.json"]
            self.assertIn("[REDACTED]", config_text)
            receipt = json.loads(result.result_path.read_text())
            self.assertTrue(receipt["changed"])
            self.assertEqual(receipt["usage"]["input_tokens"], 10)
            self.assertNotIn(secret, result.result_path.read_text())

    def test_disposable_docker_daemon_uses_a_private_compose_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            tools = root / "supplied-tools"
            tools.mkdir()
            (tools / "shakeout").write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "FAKE_EXPECT_TOOLS": "1",
                    "FAKE_EXPECT_DOCKER_DAEMON": "1",
                },
            ):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Build and test the draft.",
                        model="test/model",
                        output_dir=root / "runs",
                        image="benchmark-author:1",
                        tool_directory=tools,
                        docker_daemon=DockerDaemonConfig(),
                        harbor_command=FAKE_HARBOR,
                    )
                )

            request = json.loads(
                (result.run_dir / "input" / "request.json").read_text()
            )
            self.assertEqual(
                request["docker_daemon"]["image"], DockerDaemonConfig().image
            )
            prompt = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertIn("not the host daemon", prompt)
            self.assertIn("--network none", prompt)
            # The trusted source checkout remains the comparison baseline even
            # though compose mounted a separate compiled copy into the agent.
            self.assertEqual((repository / "existing.txt").read_text(), "original\n")

    def test_docker_daemon_image_must_be_digest_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(RunnerError, "must be pinned"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        docker_daemon=DockerDaemonConfig(image="docker:dind"),
                        harbor_command=FAKE_HARBOR,
                    )
                )

    def test_collection_honors_the_repository_gitignore(self) -> None:
        # A run that builds or tests leaves virtualenvs and caches behind;
        # collection must report what git would commit, not the debris.
        # Tracked files stay reported even when an ignore pattern matches
        # them (git never ignores tracked files).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            add_commit(repository, ".gitignore", ".venv/\n__pycache__/\nexisting.txt\n")
            with patch.dict(os.environ, {"FAKE_DEBRIS": "1"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Make a focused change.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                    )
                )

            self.assertEqual(
                result.changed_files,
                ("delete.txt", "existing.txt", "new.txt", "outside-link"),
            )
            assert result.changes_path is not None
            self.assertFalse((result.changes_path / ".venv").exists())
            self.assertFalse((result.changes_path / "__pycache__").exists())
            changes = json.loads(
                (result.run_dir / "output" / "changes.json").read_text()
            )
            reported = {item["path"] for item in changes["files"]}
            self.assertNotIn(".venv/lib/site.py", reported)
            self.assertNotIn("__pycache__/module.pyc", reported)
            self.assertIn("existing.txt", reported)
            # The trusted input checkout's git metadata is left untouched.
            self.assertEqual(checkout_git(result.run_dir, "status", "--porcelain"), "")

    def test_no_changes_produces_no_changes_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_HARBOR_MODE": "unchanged"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Inspect only.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                    )
                )

            self.assertEqual(result.changed_files, ())
            self.assertIsNone(result.changes_path)
            changes = json.loads(
                (result.run_dir / "output" / "changes.json").read_text()
            )
            self.assertEqual(changes["files"], [])

    def test_unset_agent_environment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RunnerError, "is not set"):
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            agent_env_names=("MISSING_SECRET",),
                            harbor_command=FAKE_HARBOR,
                        )
                    )
            result_files = list((root / "runs").glob("*/output/result.json"))
            self.assertEqual(len(result_files), 1)
            self.assertEqual(
                json.loads(result_files[0].read_text())["status"], "failed"
            )

    def test_codex_agent_and_login_auth_path_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            auth = root / "codex-auth.json"
            auth.write_text('{"tokens":"secret-token"}\n', encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_AUTH_JSON_PATH": str(auth)}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="gpt-5.6-sol",
                        output_dir=root / "runs",
                        agent="codex",
                        agent_env_names=("CODEX_AUTH_JSON_PATH",),
                        harbor_command=FAKE_HARBOR,
                    )
                )

            request_record = json.loads(
                (result.run_dir / "input" / "request.json").read_text()
            )
            self.assertEqual(request_record["agent"], "codex")
            self.assertEqual(
                request_record["agent_environment"], ["CODEX_AUTH_JSON_PATH"]
            )
            job_config = json.loads(
                (
                    result.run_dir / "harbor" / "jobs" / result.run_id / "config.json"
                ).read_text()
            )
            agent = job_config["agents"][0]
            self.assertEqual(agent["name"], "codex")
            self.assertEqual(agent["model_name"], "gpt-5.6-sol")
            self.assertEqual(agent["env"]["CODEX_AUTH_JSON_PATH"], "[REDACTED]")
            self.assertEqual(auth.read_text(), '{"tokens":"secret-token"}\n')
            harbor_text = "".join(
                path.read_text(errors="replace")
                for path in (result.run_dir / "harbor").rglob("*")
                if path.is_file()
            )
            self.assertNotIn(str(auth), harbor_text)

    def test_kimi_cli_forwards_base_url_and_uses_fallback_pricing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            pricing = ModelPricing(
                input_per_million=0.95,
                cached_input_per_million=0.19,
                output_per_million=4.0,
            )
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "cloudflare-token",
                    "FAKE_INPUT_TOKENS": "61073",
                    "FAKE_CACHE_TOKENS": "33472",
                    "FAKE_OUTPUT_TOKENS": "255",
                    "FAKE_COST_USD": "none",
                },
            ):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="openai/@cf/moonshotai/kimi-k2.7-code",
                        model_pricing=pricing,
                        output_dir=root / "runs",
                        agent="kimi-cli",
                        agent_env_names=("OPENAI_API_KEY",),
                        agent_kwargs={"base_url": "https://example.invalid/ai/v1"},
                        harbor_command=FAKE_HARBOR,
                    )
                )

            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(receipt["usage"]["cache_tokens"], 33_472)
            self.assertAlmostEqual(receipt["usage"]["cost_usd"], 0.03360063)
            self.assertEqual(
                receipt["model_pricing"],
                {
                    "input_per_million": 0.95,
                    "cached_input_per_million": 0.19,
                    "output_per_million": 4.0,
                },
            )
            job_config = json.loads(
                (
                    result.run_dir / "harbor" / "jobs" / result.run_id / "config.json"
                ).read_text()
            )
            agent = job_config["agents"][0]
            self.assertEqual(agent["name"], "kimi-cli")
            self.assertEqual(
                agent["model_name"], "openai/@cf/moonshotai/kimi-k2.7-code"
            )
            self.assertEqual(agent["env"]["OPENAI_API_KEY"], "[REDACTED]")
            self.assertEqual(
                agent["kwargs"], {"base_url": "https://example.invalid/ai/v1"}
            )

    def test_secret_like_agent_kwarg_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(
                RunnerError, "must be passed through agent_env"
            ):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        agent="kimi-cli",
                        agent_kwargs={"api_key": "must-not-persist"},
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                    )
                )

    def test_empty_agent_name_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(RunnerError, "agent"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        agent=" ",
                        harbor_command=FAKE_HARBOR,
                    )
                )

    def test_default_fetch_is_full_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            add_commit(repository, "second.txt", "two\n")
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="main",
                    prompt="Task.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                )
            )

            self.assertEqual(
                checkout_git(result.run_dir, "rev-list", "--count", "HEAD"), "2"
            )
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(receipt["fetch_depth"], 0)
            self.assertEqual(receipt["extra_refs"], {})
            prompt_text = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertIn("do not\nfetch from the network", prompt_text)

    def test_positive_fetch_depth_stays_shallow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            add_commit(repository, "second.txt", "two\n")
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="main",
                    prompt="Task.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                    fetch_depth=1,
                )
            )

            self.assertEqual(
                checkout_git(result.run_dir, "rev-list", "--count", "HEAD"), "1"
            )

    def test_extra_refs_are_fetched_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, main_sha = seed_repository(root)
            subprocess.run(
                ("git", "-C", str(repository), "checkout", "-b", "feature"),
                check=True,
                capture_output=True,
            )
            feature_sha = add_commit(repository, "feature.txt", "feature\n")
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="feature",
                    prompt="Task.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                    extra_refs=("main",),
                )
            )

            self.assertEqual(result.source_sha, feature_sha)
            self.assertEqual(result.extra_ref_shas, {"main": main_sha})
            self.assertEqual(
                checkout_git(result.run_dir, "rev-parse", "origin/main"), main_sha
            )
            prompt_text = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertIn("origin/<name>", prompt_text)
            self.assertIn("main", prompt_text)
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(receipt["extra_refs"], {"main": main_sha})

    def test_extra_ref_names_follow_git_branch_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            for bad in ("-evil", "foo.lock", "foo//bar", "a..b", "@{-1}", ""):
                with self.assertRaisesRegex(
                    RunnerError, "invalid extra ref|must be unique"
                ):
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="main",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            harbor_command=FAKE_HARBOR,
                            extra_refs=(bad,),
                        )
                    )
            # Valid-but-unusual names pass validation and fail later only
            # because the branch does not exist.
            with self.assertRaisesRegex(RunnerError, "Git failed"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        extra_refs=("release@2026",),
                    )
                )

    def _seed_memory(self, root: Path) -> tuple[Path, str]:
        memory = root / "memory-source"
        subprocess.run(
            ("git", "init", "--initial-branch=agent/memory", str(memory)),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ("git", "-C", str(memory), "config", "user.name", "Hub Test"),
            check=True,
        )
        subprocess.run(
            ("git", "-C", str(memory), "config", "user.email", "hub@localhost"),
            check=True,
        )
        (memory / "MEMORY.md").write_text("- old lesson\n", encoding="utf-8")
        (memory / "PROJECT.md").write_text("context\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(memory), "add", "."), check=True)
        subprocess.run(
            ("git", "-C", str(memory), "commit", "-m", "seed memory"),
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ("git", "-C", str(memory), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return memory, sha

    def test_memory_mount_collects_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            memory, memory_sha = self._seed_memory(root)
            with patch.dict(os.environ, {"FAKE_MEMORY_MODE": "changed"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        memory_repository=str(memory),
                        memory_revision="agent/memory",
                    )
                )

            self.assertEqual(result.memory_source_sha, memory_sha)
            self.assertEqual(
                result.memory_changed_files, ("MEMORY.md", "daily/2026-01-01.md")
            )
            workspace = result.memory_workspace
            assert workspace is not None
            self.assertTrue((workspace / ".git").exists())
            self.assertIn("- learned something", (workspace / "MEMORY.md").read_text())
            # The memory source checkout itself is untouched.
            self.assertEqual((memory / "MEMORY.md").read_text(), "- old lesson\n")
            prompt_text = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertIn("/app/memory", prompt_text)
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(receipt["memory_resolved_sha"], memory_sha)
            self.assertEqual(
                receipt["memory_changed_files"],
                ["MEMORY.md", "daily/2026-01-01.md"],
            )

    def test_committed_memory_edits_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            memory, memory_sha = self._seed_memory(root)
            with patch.dict(os.environ, {"FAKE_MEMORY_MODE": "committed"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        memory_repository=str(memory),
                    )
                )

            self.assertEqual(result.memory_source_sha, memory_sha)
            self.assertEqual(result.memory_changed_files, ("MEMORY.md",))
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(receipt["memory_changed_files"], ["MEMORY.md"])

    def test_memory_collection_ignores_agent_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            memory, memory_sha = self._seed_memory(root)
            marker = root / "fsmonitor-executed"
            hook = root / "fsmonitor.sh"
            hook.write_text(
                f'#!/bin/sh\ntouch "{marker}"\nprintf "{{}}\\n"\n',
                encoding="utf-8",
            )
            hook.chmod(0o755)
            with patch.dict(
                os.environ,
                {
                    "FAKE_MEMORY_MODE": "hostile-git",
                    "FAKE_MEMORY_FSMONITOR": str(hook),
                },
            ):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        memory_repository=str(memory),
                    )
                )

            self.assertEqual(result.memory_source_sha, memory_sha)
            self.assertEqual(result.memory_changed_files, ("MEMORY.md",))
            self.assertFalse(marker.exists(), "collected core.fsmonitor executed")
            workspace = result.memory_workspace
            assert workspace is not None
            self.assertIn(str(hook), (workspace / ".git" / "config").read_text())

    def test_unchanged_memory_reports_no_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            memory, memory_sha = self._seed_memory(root)
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="main",
                    prompt="Task.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                    memory_repository=str(memory),
                )
            )

            self.assertEqual(result.memory_source_sha, memory_sha)
            self.assertEqual(result.memory_changed_files, ())
            self.assertIsNotNone(result.memory_workspace)

    def test_runs_without_memory_have_no_memory_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="main",
                    prompt="Task.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                )
            )

            self.assertIsNone(result.memory_source_sha)
            self.assertEqual(result.memory_changed_files, ())
            self.assertNotIn(
                "/app/memory", (result.run_dir / "input" / "prompt.md").read_text()
            )
            receipt = json.loads(result.result_path.read_text())
            self.assertIsNone(receipt["memory_resolved_sha"])

    def test_session_transcripts_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="HEAD",
                    prompt="Task.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                )
            )

            assert result.session_path is not None
            self.assertEqual(
                result.session_files,
                ("fake-session.jsonl", "subagents/nested-session.jsonl"),
            )
            self.assertIn(
                "prior turn",
                (result.session_path / "fake-session.jsonl").read_text(),
            )
            self.assertIn(
                "subagent turn",
                (
                    result.session_path / "subagents" / "nested-session.jsonl"
                ).read_text(),
            )
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(
                receipt["session_files"],
                ["fake-session.jsonl", "subagents/nested-session.jsonl"],
            )
            self.assertFalse(receipt["resume"])

    def test_symlinked_session_root_is_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / "trial"
            session_parent = trial / "agent" / "sessions" / "projects"
            session_parent.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (outside / "leak.jsonl").write_text("secret\n", encoding="utf-8")
            (session_parent / "-app").symlink_to(outside, target_is_directory=True)
            output = root / "output"
            output.mkdir()

            session_path, session_files = _collect_session(trial, output)

            self.assertIsNone(session_path)
            self.assertEqual(session_files, ())
            self.assertFalse((output / "session").exists())

    def test_symlinked_session_ancestor_is_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trial = root / "trial"
            sessions = trial / "agent" / "sessions"
            sessions.mkdir(parents=True)
            outside = root / "outside"
            transcripts = outside / "-app"
            transcripts.mkdir(parents=True)
            (transcripts / "leak.jsonl").write_text("secret\n", encoding="utf-8")
            (sessions / "projects").symlink_to(outside, target_is_directory=True)
            output = root / "output"
            output.mkdir()

            session_path, session_files = _collect_session(trial, output)

            self.assertIsNone(session_path)
            self.assertEqual(session_files, ())
            self.assertFalse((output / "session").exists())

    def test_missing_transcripts_yield_no_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_SESSIONS": "absent"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                    )
                )

            self.assertIsNone(result.session_path)
            self.assertEqual(result.session_files, ())

    def test_resume_bundle_stages_mount_and_resuming_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "prior.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
            (bundle / "notes.txt").write_text("not a transcript\n", encoding="utf-8")
            (bundle / "subagents").mkdir()
            (bundle / "subagents" / "task.jsonl").write_text(
                '{"turn": "sub"}\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {"FAKE_EXPECT_RESUME": "1"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Continue the task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        resume_bundle=bundle,
                    )
                )

            staged = result.run_dir / "input" / "prior-session"
            self.assertEqual(
                sorted(
                    path.relative_to(staged).as_posix()
                    for path in staged.rglob("*")
                    if path.is_file()
                ),
                ["prior.jsonl", "subagents/task.jsonl"],
            )
            receipt = json.loads(result.result_path.read_text())
            self.assertTrue(receipt["resume"])
            request_record = json.loads(
                (result.run_dir / "input" / "request.json").read_text()
            )
            self.assertTrue(request_record["resume"])

    def test_default_command_is_the_packaged_hub_launcher(self) -> None:
        # One command shape for every arm: swecraft-harbor applies the in-process
        # fixes (the scrub switch) to stock agents too, and runs from the
        # runner's project so import-path classes and resume need no swap.
        from swecraft_runner.runner import DEFAULT_HARBOR_COMMAND

        self.assertEqual(DEFAULT_HARBOR_COMMAND[:2], ("uv", "run"))
        self.assertIn("--project", DEFAULT_HARBOR_COMMAND)
        self.assertIn("--with", DEFAULT_HARBOR_COMMAND)
        self.assertIn("harbor==0.22.0", DEFAULT_HARBOR_COMMAND)
        self.assertEqual(DEFAULT_HARBOR_COMMAND[-1], "swecraft-harbor")
        default = RunRequest(repository="r", revision="HEAD", prompt="p", model="m")
        self.assertEqual(default.harbor_command, DEFAULT_HARBOR_COMMAND)

    def test_resume_bundle_without_transcripts_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            bundle = root / "bundle"
            bundle.mkdir()
            with self.assertRaisesRegex(RunnerError, "no transcripts"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        resume_bundle=bundle,
                    )
                )

    def test_resume_requires_claude_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "prior.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RunnerError, "only supported for claude-code"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        agent="codex",
                        harbor_command=FAKE_HARBOR,
                        resume_bundle=bundle,
                    )
                )

    def test_import_path_agent_replaces_the_stock_name(self) -> None:
        import_path = "swecraft_runner.harbor_agents:SwecraftPi"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_EXPECT_IMPORT_PATH": import_path}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Maintain the repository.",
                        model="anthropic/claude-opus-5",
                        agent="pi",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        agent_import_path=import_path,
                    )
                )
            request_record = json.loads(
                (result.run_dir / "input" / "request.json").read_text()
            )
            # The stock name stays as the ledger label; Harbor sees the class.
            self.assertEqual(request_record["agent"], "pi")
            self.assertEqual(request_record["agent_import_path"], import_path)
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(receipt["agent_import_path"], import_path)
            self.assertFalse(receipt["resume"])

    def test_import_path_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            for bad in (
                "swecraft_runner.harbor_agents.SwecraftPi",
                "bad-module:Class",
                "m:",
            ):
                with self.assertRaisesRegex(RunnerError, "module.path:ClassName"):
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            harbor_command=FAKE_HARBOR,
                            agent_import_path=bad,
                        )
                    )
            bundle = root / "bundle"
            bundle.mkdir()
            (bundle / "prior.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RunnerError, "cannot be combined"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        resume_bundle=bundle,
                        agent_import_path="swecraft_runner.harbor_agents:SwecraftPi",
                    )
                )

    def test_structured_output_collects_agent_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="HEAD",
                    prompt="Implement the change.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                    structured_output=True,
                )
            )

            assert result.structured is not None
            self.assertEqual(
                result.structured["commit_message"], "fake: focused change"
            )
            assert result.structured_path is not None
            self.assertTrue(result.structured_path.is_file())
            self.assertIn(
                "/app/result.json", (result.run_dir / "input" / "prompt.md").read_text()
            )
            receipt = json.loads(result.result_path.read_text())
            self.assertTrue(receipt["structured"])

    def test_missing_structured_output_yields_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_STRUCTURED": "absent"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Implement the change.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        structured_output=True,
                    )
                )

            self.assertIsNone(result.structured)
            self.assertIsNone(result.structured_path)
            receipt = json.loads(result.result_path.read_text())
            self.assertFalse(receipt["structured"])

    def test_invalid_structured_output_fails_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_STRUCTURED": "invalid"}):
                with self.assertRaisesRegex(RunnerError, "missing or invalid JSON"):
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Implement the change.",
                            model="test/model",
                            output_dir=root / "runs",
                            harbor_command=FAKE_HARBOR,
                            structured_output=True,
                        )
                    )

    def test_lenient_structured_treats_invalid_result_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_STRUCTURED": "invalid"}):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Implement the change.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        structured_output=True,
                        lenient_structured=True,
                    )
                )

            self.assertIsNone(result.structured)
            self.assertIsNone(result.structured_path)
            receipt = json.loads(result.result_path.read_text())
            self.assertFalse(receipt["structured"])

    def test_lenient_structured_requires_structured_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(RunnerError, "requires structured_output"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        lenient_structured=True,
                    )
                )

    def test_option_like_refs_and_repositories_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(RunnerError, "revision must not start"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="--upload-pack=/bin/echo",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                    )
                )
            with self.assertRaisesRegex(RunnerError, "repository must not start"):
                run_once(
                    RunRequest(
                        repository="--upload-pack=/bin/echo",
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                    )
                )
            with self.assertRaisesRegex(
                RunnerError, "memory repository must not start"
            ):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        memory_repository="--upload-pack=/bin/echo",
                    )
                )
            with self.assertRaisesRegex(RunnerError, "memory revision must not start"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        harbor_command=FAKE_HARBOR,
                        memory_repository=str(repository),
                        memory_revision="--upload-pack=/bin/echo",
                    )
                )

    def test_overlapping_secrets_redact_longest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            prefix = "secret-prefix-value"
            longer = f"{prefix}-and-confidential-suffix"
            with patch.dict(
                os.environ,
                {"TEST_AGENT_PREFIX": prefix, "TEST_AGENT_SECRET": longer},
            ):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        agent_env_names=("TEST_AGENT_PREFIX", "TEST_AGENT_SECRET"),
                        harbor_command=FAKE_HARBOR,
                    )
                )

            logs = "".join(
                path.read_text(errors="replace")
                for path in (result.run_dir / "harbor").rglob("*")
                if path.is_file()
            )
            self.assertNotIn("confidential-suffix", logs)
            self.assertNotIn(prefix, logs)

    def test_short_flag_values_are_not_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            secret = "secret-that-must-not-land-in-logs"
            with patch.dict(
                os.environ,
                {"TEST_AGENT_SECRET": secret, "TEST_AGENT_FLAG": "1"},
            ):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        agent_env_names=("TEST_AGENT_SECRET", "TEST_AGENT_FLAG"),
                        harbor_command=FAKE_HARBOR,
                    )
                )

            job_dir = result.run_dir / "harbor" / "jobs" / result.run_id
            job = json.loads((job_dir / "config.json").read_text())
            env = job["agents"][0]["env"]
            self.assertEqual(env["TEST_AGENT_FLAG"], "1")
            self.assertEqual(env["TEST_AGENT_SECRET"], "[REDACTED]")
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(receipt["status"], "completed")

    def test_every_sandbox_caps_xdist_auto_and_a_caller_may_override(self) -> None:
        """A task container sees every core of the host, so `pytest -n auto`
        sizes itself by the host and overruns the container's memory cap. The
        runner gives every sandbox a worker count for `auto`; the caller's
        ``agent_env`` still wins, and the value is too short to be redacted."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)

            def xdist_workers(tag: str, agent_env: dict[str, str]) -> str:
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / tag,
                        agent_env=agent_env,
                        harbor_command=FAKE_HARBOR,
                    )
                )
                job_dir = result.run_dir / "harbor" / "jobs" / result.run_id
                job = json.loads((job_dir / "config.json").read_text())
                return job["agents"][0]["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"]

            self.assertEqual(xdist_workers("default", {}), "8")
            self.assertEqual(
                xdist_workers("override", {"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"}), "2"
            )

    def test_timeout_redacts_secrets_and_keeps_partial_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            secret = "secret-that-must-not-land-in-logs"
            with (
                patch.dict(
                    os.environ,
                    {"TEST_AGENT_SECRET": secret, "FAKE_HARBOR_MODE": "hang"},
                ),
                patch("swecraft_runner.runner.HARBOR_OVERHEAD_SECONDS", 0),
            ):
                with self.assertRaisesRegex(RunnerError, "timeout") as context:
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            agent_env_names=("TEST_AGENT_SECRET",),
                            harbor_command=FAKE_HARBOR,
                            timeout_seconds=2,
                        )
                    )

            self.assertNotIn(secret, str(context.exception))
            run_dir = next((root / "runs").iterdir())
            receipt_text = (run_dir / "output" / "result.json").read_text()
            self.assertNotIn(secret, receipt_text)
            self.assertEqual(json.loads(receipt_text)["status"], "failed")
            logs = "".join(
                path.read_text() for path in (run_dir / "harbor").glob("*.log")
            )
            self.assertNotIn(secret, logs)
            self.assertIn("[REDACTED]", logs)

    def test_rejects_wrong_harbor_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_HARBOR_MODE": "bad-version"}):
                with self.assertRaisesRegex(RunnerError, "0.22.0 is required"):
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            harbor_command=FAKE_HARBOR,
                        )
                    )

    def test_rejects_missing_required_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_HARBOR_MODE": "missing-artifact"}):
                with self.assertRaisesRegex(RunnerError, "response.md"):
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            harbor_command=FAKE_HARBOR,
                        )
                    )

    def test_raw_forge_call_harbor_artifact_is_removed_after_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            secret = "forge-call-secret"
            with patch.dict(
                os.environ,
                {
                    "TEST_AGENT_SECRET": secret,
                    "FAKE_FORGE_CALL_SECRET": secret,
                },
            ):
                result = run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="HEAD",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        agent_env_names=("TEST_AGENT_SECRET",),
                        harbor_command=FAKE_HARBOR,
                    )
                )

            raw_artifacts = list(
                result.run_dir.glob(
                    "harbor/jobs/*/*/artifacts/app/.swecraft/forge-calls"
                )
            )
            self.assertEqual(raw_artifacts, [])
            calls_text = (result.run_dir / "output/forge-calls.jsonl").read_text()
            actions_text = (result.run_dir / "output/forge-actions.jsonl").read_text()
            self.assertNotIn(secret, calls_text)
            self.assertNotIn(secret, actions_text)
            self.assertIn("[REDACTED]", calls_text)
            self.assertIn("[REDACTED]", actions_text)

    def test_failed_run_keeps_the_forge_actions_it_already_performed(self) -> None:
        # `failed` means the wake raised, not that it did nothing: the agent
        # may already have opened a pull request or cut a release before
        # collection broke. Those side effects outlive the run, so the ledger
        # has to survive the failure that lost everything else.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_HARBOR_MODE": "missing-artifact"}):
                with self.assertRaises(RunnerError) as caught:
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            harbor_command=FAKE_HARBOR,
                        )
                    )

            run_dir = Path(caught.exception.run_dir)
            receipt = json.loads((run_dir / "output/result.json").read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["forge_actions"], "output/forge-actions.jsonl")
            self.assertEqual(receipt["forge_calls"], "output/forge-calls.jsonl")

            lines = [
                json.loads(line)
                for line in (run_dir / "output/forge-actions.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(
                [(entry["action"], entry["metadata"]["number"]) for entry in lines],
                [("issue.create", 4), ("pr.merge", 2)],
            )
            # The recording is collected beside the ledger, so a vocabulary
            # corrected later can be applied to this run without re-running it.
            recorded = [
                json.loads(line)
                for line in (run_dir / "output/forge-calls.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(
                [call["argv"][:2] for call in recorded],
                [["issue", "create"], ["pr", "merge"]],
            )
            raw_artifacts = list(
                run_dir.glob("harbor/jobs/*/*/artifacts/app/.swecraft/forge-calls")
            )
            self.assertEqual(raw_artifacts, [])

    def test_failures_carry_the_run_location_for_cost_accounting(self) -> None:
        # A failed run still spent money; Harbor's trial receipts survive
        # under the run directory, so the exception must say where to look.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"FAKE_HARBOR_MODE": "missing-artifact"}):
                with self.assertRaises(RunnerError) as caught:
                    run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Task.",
                            model="test/model",
                            output_dir=root / "runs",
                            harbor_command=FAKE_HARBOR,
                        )
                    )
            error = caught.exception
            self.assertIsNotNone(error.run_id)
            assert error.run_dir is not None
            self.assertTrue(error.run_dir.is_dir())
            receipts = list(error.run_dir.glob("harbor/jobs/*/*/result.json"))
            self.assertTrue(receipts)
            trial = json.loads(receipts[0].read_text())
            self.assertEqual(trial["agent_result"]["cost_usd"], 0.01)


class DirectActionTests(unittest.TestCase):
    def _direct_request(self, repository: Path, root: Path, **overrides) -> RunRequest:
        values = dict(
            repository=str(repository),
            revision="main",
            prompt="Maintain the repository.",
            model="test/model",
            output_dir=root / "runs",
            agent_env_names=("TEST_AGENT_SECRET",),
            harbor_command=FAKE_HARBOR,
            allow_remote_writes=True,
            push_remote_url="http://forge.internal:3000/hub/demo.git",
            git_auth_token_env="TEST_AGENT_SECRET",
            git_author_name="demo-bot",
            git_author_email="demo-bot@localhost",
        )
        values.update(overrides)
        return RunRequest(**values)

    def test_rejects_direct_fields_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(RunnerError, "allow_remote_writes"):
                run_once(
                    self._direct_request(repository, root, allow_remote_writes=False)
                )

    def test_rejects_incomplete_direct_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(RunnerError, "requires push_remote_url"):
                run_once(self._direct_request(repository, root, push_remote_url=None))
            with self.assertRaisesRegex(RunnerError, "http\\(s\\)"):
                run_once(
                    self._direct_request(
                        repository, root, push_remote_url="ssh://forge/hub/demo.git"
                    )
                )
            with self.assertRaisesRegex(RunnerError, "agent_env_names"):
                run_once(
                    self._direct_request(
                        repository, root, git_auth_token_env="OTHER_TOKEN"
                    )
                )

    def test_rejects_reserved_git_config_environment_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with self.assertRaisesRegex(RunnerError, "reserved for git"):
                run_once(
                    RunRequest(
                        repository=str(repository),
                        revision="main",
                        prompt="Task.",
                        model="test/model",
                        output_dir=root / "runs",
                        agent_env_names=("GIT_CONFIG_KEY_0",),
                        harbor_command=FAKE_HARBOR,
                    )
                )

    def test_direct_action_rewires_origin_auth_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            secret = "direct-action-token-value"
            with patch.dict(os.environ, {"TEST_AGENT_SECRET": secret}):
                result = run_once(self._direct_request(repository, root))

            origin = checkout_git(result.run_dir, "remote", "get-url", "origin")
            self.assertEqual(origin, "http://forge.internal:3000/hub/demo.git")
            prompt = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertNotIn("Do not push to any remote.", prompt)
            self.assertIn("Never force-push", prompt)
            config = json.loads(
                (result.run_dir / "harbor" / "exec-config.json").read_text()
            )
            env = config["map"]["job"]["agents"][0]["env"]
            self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
            self.assertEqual(
                env["GIT_CONFIG_KEY_0"],
                "http.http://forge.internal:3000/.extraheader",
            )
            self.assertIn("[REDACTED]", env["GIT_CONFIG_VALUE_0"])
            self.assertEqual(env["GIT_CONFIG_KEY_1"], "user.name")
            self.assertEqual(env["GIT_CONFIG_VALUE_1"], "demo-bot")
            self.assertEqual(env["GIT_CONFIG_KEY_2"], "user.email")
            receipt = json.loads(result.result_path.read_text())
            self.assertTrue(receipt["direct_action"])
            self.assertNotIn(secret, result.result_path.read_text())
            for path in (result.run_dir / "harbor").rglob("*"):
                if path.is_file():
                    self.assertNotIn(
                        secret,
                        path.read_text(errors="replace"),
                        f"secret leaked into {path}",
                    )

    def test_agent_env_by_value_never_touches_this_process(self) -> None:
        # Concurrent callers cannot share one process environment: each agent
        # carries its own token, so names resolved globally would let two
        # in-flight turns run under each other's credentials.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            request = self._direct_request(
                repository,
                root,
                agent_env_names=(),
                agent_env={"TEST_AGENT_SECRET": "by-value-token"},
            )
            with patch.dict(os.environ, {"FAKE_EXPECT_RESOLVABLE_ENV": "1"}):
                self.assertNotIn("TEST_AGENT_SECRET", os.environ)
                result = run_once(request)
                self.assertNotIn("TEST_AGENT_SECRET", os.environ)

            config = json.loads(
                (result.run_dir / "harbor" / "exec-config.json").read_text()
            )
            env = config["map"]["job"]["agents"][0]["env"]
            self.assertIn("[REDACTED]", env["GIT_CONFIG_VALUE_0"])
            self.assertNotIn("by-value-token", result.result_path.read_text())

    def test_agent_env_rejects_a_name_supplied_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            with patch.dict(os.environ, {"TEST_AGENT_SECRET": "x"}):
                with self.assertRaisesRegex(RunnerError, "by name and by value"):
                    run_once(
                        self._direct_request(
                            repository, root, agent_env={"TEST_AGENT_SECRET": "y"}
                        )
                    )

    def test_direct_git_env_is_resolvable_by_harbor(self) -> None:
        # Regression: Harbor treats any env name matching
        # KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH as sensitive and, when it
        # cannot resolve the value from its own environment, writes a
        # redacted placeholder into the trial config the container receives.
        # GIT_CONFIG_KEY_* matches, so agents got config keys like
        # "http****der" and every git command failed with
        # "key does not contain a section".
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            environment = {
                "TEST_AGENT_SECRET": "direct-action-token-value",
                "FAKE_EXPECT_RESOLVABLE_ENV": "1",
            }
            with patch.dict(os.environ, environment):
                result = run_once(self._direct_request(repository, root))
            self.assertTrue(json.loads(result.result_path.read_text())["direct_action"])
            # The runner's own git must not inherit the agent's settings.
            self.assertNotIn("GIT_CONFIG_KEY_0", os.environ)

    def test_standard_runs_keep_the_no_push_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            result = run_once(
                RunRequest(
                    repository=str(repository),
                    revision="main",
                    prompt="Task.",
                    model="test/model",
                    output_dir=root / "runs",
                    harbor_command=FAKE_HARBOR,
                )
            )
            prompt = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertIn("Do not push to any remote.", prompt)
            receipt = json.loads(result.result_path.read_text())
            self.assertFalse(receipt["direct_action"])


class WorkRepositoryTests(unittest.TestCase):
    """Further repositories staged beside the home checkout, under /app/repos."""

    def _seed_work(self, root: Path, name: str) -> tuple[Path, str]:
        work = root / f"{name}-source"
        subprocess.run(
            ("git", "init", "--initial-branch=main", str(work)),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ("git", "-C", str(work), "config", "user.name", "Hub Test"), check=True
        )
        subprocess.run(
            ("git", "-C", str(work), "config", "user.email", "hub@localhost"),
            check=True,
        )
        (work / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(work), "add", "."), check=True)
        subprocess.run(
            ("git", "-C", str(work), "commit", "-m", "seed"),
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ("git", "-C", str(work), "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return work, sha

    def _base(self, repository: Path, root: Path) -> dict:
        return dict(
            repository=str(repository),
            revision="main",
            prompt="Task.",
            model="test/model",
            output_dir=root / "runs",
            harbor_command=FAKE_HARBOR,
        )

    def test_work_repositories_are_staged_recorded_and_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            lib, lib_sha = self._seed_work(root, "lib")
            tool, tool_sha = self._seed_work(root, "tool")
            result = run_once(
                RunRequest(
                    **self._base(repository, root),
                    work_repositories=(
                        WorkRepository(
                            name="lib", repository=str(lib), revision="main"
                        ),
                        WorkRepository(
                            name="tool", repository=str(tool), revision="main"
                        ),
                    ),
                )
            )

            self.assertEqual(
                result.work_repository_shas, {"lib": lib_sha, "tool": tool_sha}
            )
            staged = result.run_dir / "input" / "repos"
            self.assertTrue((staged / "lib" / ".git").is_dir())
            self.assertEqual((staged / "tool" / "README.md").read_text(), "# tool\n")
            config = json.loads(
                (result.run_dir / "harbor" / "exec-config.json").read_text()
            )
            paths = config["map"]["compile"]["environments"][0]["paths"]
            self.assertIn(str(staged), paths)
            prompt = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertIn("`/app/repos/lib`, `/app/repos/tool`", prompt)
            self.assertIn("changes there are discarded", prompt)
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(
                [
                    (work["name"], work["resolved_sha"], work["push_remote_url"])
                    for work in receipt["work_repositories"]
                ],
                [("lib", lib_sha, None), ("tool", tool_sha, None)],
            )
            request_record = json.loads(
                (result.run_dir / "input" / "request.json").read_text()
            )
            self.assertEqual(
                request_record["work_repositories"][0]["requested_ref"], "main"
            )
            # Only the home checkout is collected and compared.
            self.assertEqual(
                result.changed_files,
                ("delete.txt", "existing.txt", "new.txt", "outside-link"),
            )

    def test_direct_action_points_work_origins_at_their_push_remotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            lib, _ = self._seed_work(root, "lib")
            with patch.dict(os.environ, {"TEST_AGENT_SECRET": "direct-token-value"}):
                result = run_once(
                    RunRequest(
                        **self._base(repository, root),
                        agent_env_names=("TEST_AGENT_SECRET",),
                        allow_remote_writes=True,
                        push_remote_url="http://forge.internal:3000/hub/demo.git",
                        git_auth_token_env="TEST_AGENT_SECRET",
                        git_author_name="demo-bot",
                        git_author_email="demo-bot@localhost",
                        work_repositories=(
                            WorkRepository(
                                name="lib",
                                repository=str(lib),
                                revision="main",
                                push_remote_url="http://forge.internal:3000/hub/lib.git",
                            ),
                        ),
                    )
                )

            origin = subprocess.run(
                (
                    "git",
                    "-C",
                    str(result.run_dir / "input" / "repos" / "lib"),
                    "remote",
                    "get-url",
                    "origin",
                ),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(origin, "http://forge.internal:3000/hub/lib.git")
            prompt = (result.run_dir / "input" / "prompt.md").read_text()
            self.assertIn("each has an authenticated `origin`", prompt)
            receipt = json.loads(result.result_path.read_text())
            self.assertEqual(
                receipt["work_repositories"][0]["push_remote_url"],
                "http://forge.internal:3000/hub/lib.git",
            )

    def test_fetch_token_rides_as_a_header_scoped_to_the_origin(self) -> None:
        url = "http://forge.local:3000/hub/x.git"
        with patch.dict(
            os.environ,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "x",
                "GIT_CONFIG_VALUE_0": "y",
            },
        ):
            env = _fetch_environment(url, "tok-value")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(
            env["GIT_CONFIG_KEY_0"], "http.http://forge.local:3000/.extraheader"
        )
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: token tok-value")
        # A local path gets no header; without a token there is none either.
        self.assertNotIn("GIT_CONFIG_COUNT", _fetch_environment("/srv/repo", "tok"))
        self.assertNotIn("GIT_CONFIG_COUNT", _fetch_environment(url, None))
        # Every git command of a checkout runs with that environment.
        seen: list[dict[str, str] | None] = []

        def record(command, revision, logs, log_path, env=None):
            seen.append(env)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("swecraft_runner.runner._run_git", side_effect=record),
                patch("swecraft_runner.runner._rev_parse", return_value="a" * 40),
            ):
                _checkout(
                    url,
                    "main",
                    root / "checkout",
                    root,
                    fetch_depth=0,
                    extra_refs=(),
                    env=env,
                )
        self.assertTrue(seen)
        self.assertTrue(
            all(
                entry is not None and "tok-value" in entry["GIT_CONFIG_VALUE_0"]
                for entry in seen
            )
        )

    def test_fetch_token_is_declared_and_never_logged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            lib, _ = self._seed_work(root, "lib")
            base = self._base(repository, root)
            with self.assertRaisesRegex(RunnerError, "fetch_auth_token_env"):
                run_once(RunRequest(**base, fetch_auth_token_env="MISSING"))
            result = run_once(
                RunRequest(
                    **base,
                    agent_env={"SWECRAFT_FORGE_TOKEN": "fetch-token-value"},
                    fetch_auth_token_env="SWECRAFT_FORGE_TOKEN",
                    work_repositories=(WorkRepository("lib", str(lib), "main"),),
                )
            )
            request_record = json.loads(
                (result.run_dir / "input" / "request.json").read_text()
            )
            self.assertEqual(
                request_record["fetch_auth_token_env"], "SWECRAFT_FORGE_TOKEN"
            )
            for log in result.run_dir.glob("git*.log"):
                self.assertNotIn("fetch-token-value", log.read_text())

    def test_work_repositories_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            lib, _ = self._seed_work(root, "lib")
            base = self._base(repository, root)
            with self.assertRaisesRegex(RunnerError, "must be unique"):
                run_once(
                    RunRequest(
                        **base,
                        work_repositories=(
                            WorkRepository("lib", str(lib)),
                            WorkRepository("lib", str(lib)),
                        ),
                    )
                )
            with self.assertRaisesRegex(RunnerError, "invalid work repository name"):
                run_once(
                    RunRequest(
                        **base, work_repositories=(WorkRepository("../lib", str(lib)),)
                    )
                )
            with self.assertRaisesRegex(RunnerError, "require allow_remote_writes"):
                run_once(
                    RunRequest(
                        **base,
                        work_repositories=(
                            WorkRepository(
                                "lib",
                                str(lib),
                                push_remote_url="http://forge.internal:3000/hub/lib.git",
                            ),
                        ),
                    )
                )
            with self.assertRaisesRegex(RunnerError, "share"):
                run_once(
                    RunRequest(
                        **base,
                        agent_env_names=("TEST_AGENT_SECRET",),
                        allow_remote_writes=True,
                        push_remote_url="http://forge.internal:3000/hub/demo.git",
                        git_auth_token_env="TEST_AGENT_SECRET",
                        git_author_name="demo-bot",
                        git_author_email="demo-bot@localhost",
                        work_repositories=(
                            WorkRepository(
                                "lib",
                                str(lib),
                                push_remote_url="http://elsewhere:3000/hub/lib.git",
                            ),
                        ),
                    )
                )


if __name__ == "__main__":
    unittest.main()


class PreloadImageTests(unittest.TestCase):
    def test_image_tarball_is_cached_by_image_id(self) -> None:
        from swecraft_runner.runner import _ensure_image_tarball

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "images"
            calls: list[tuple[str, ...]] = []

            def fake_run(command, **kwargs):
                calls.append(tuple(command))
                if command[1] == "image":
                    return subprocess.CompletedProcess(
                        command, 0, stdout="sha256:abcdef0123456789ff\n", stderr=""
                    )
                Path(command[3]).write_bytes(b"tar-bytes")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch("swecraft_runner.runner.subprocess.run", side_effect=fake_run):
                first = _ensure_image_tarball("benchmark-python:1", cache)
                second = _ensure_image_tarball("benchmark-python:1", cache)
            # The rootless DinD seed reads the staged copy as uid 1000, so
            # the cached tarball must not stay at docker save's private 0600.
            mode = first.stat().st_mode & 0o777

        self.assertEqual(first, second)
        self.assertEqual(first.name, "benchmark-python-1-abcdef0123456789.tar")
        saves = [call for call in calls if call[1] == "save"]
        self.assertEqual(len(saves), 1)
        self.assertEqual(mode, 0o644)

    def test_missing_host_image_is_a_clear_error(self) -> None:
        from swecraft_runner.runner import RunnerError, _ensure_image_tarball

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="No such image"
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch("swecraft_runner.runner.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(RunnerError, "not present on the host"):
                    _ensure_image_tarball("absent:1", Path(directory))

    def test_preload_adds_a_seed_service_before_the_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            cache = root / "cache"
            cache.mkdir()
            tarball = cache / "benchmark-python-1-abc.tar"
            tarball.write_bytes(b"tar-bytes")

            with patch.dict(os.environ, {"FAKE_EXPECT_DOCKER_DAEMON": "1"}):
                with patch(
                    "swecraft_runner.runner._ensure_image_tarball", return_value=tarball
                ):
                    result = run_once(
                        RunRequest(
                            repository=str(repository),
                            revision="HEAD",
                            prompt="Build and test the draft.",
                            model="test/model",
                            output_dir=root / "runs",
                            image="benchmark-author:1",
                            docker_daemon=DockerDaemonConfig(
                                preload_images=("benchmark-python:1",),
                                preload_cache_dir=cache,
                            ),
                            harbor_command=FAKE_HARBOR,
                        )
                    )

            environment = result.run_dir / "input" / "agent-environment"
            compose = json.loads((environment / "docker-compose.yaml").read_text())
            self.assertIn("seed", compose["services"])
            seed = compose["services"]["seed"]
            self.assertEqual(
                seed["depends_on"], {"dind": {"condition": "service_healthy"}}
            )
            # Compose interpolates single-dollar variables before sh runs;
            # the loop variable must reach the shell as a literal.
            self.assertIn("$$archive", seed["entrypoint"][2])
            self.assertNotIn(' "$archive"', seed["entrypoint"][2])
            self.assertEqual(
                compose["services"]["main"]["depends_on"]["seed"],
                {"condition": "service_completed_successfully"},
            )
            self.assertTrue(
                (environment / "preload" / "benchmark-python-1-abc.tar").is_file()
            )
            request = json.loads(
                (result.run_dir / "input" / "request.json").read_text()
            )
            self.assertEqual(
                request["docker_daemon"]["preload_images"], ["benchmark-python:1"]
            )


class TurnDeadlineTests(unittest.TestCase):
    def test_pid1_deadline_covers_agent_timeout_plus_overhead(self) -> None:
        from swecraft_runner.runner import (
            HARBOR_OVERHEAD_SECONDS,
            _turn_deadline_seconds,
        )

        request = RunRequest(
            repository="https://example.invalid/repo.git",
            revision="HEAD",
            prompt="p",
            model="m",
            timeout_seconds=2_700,
        )
        deadline = _turn_deadline_seconds(request)
        # Strictly after Harbor's own agent timeout, so the host-side limit
        # fires first in the normal case and PID 1 is only the backstop...
        self.assertGreater(deadline, request.timeout_seconds)
        # ...but bounded: the overhead allowance Harbor itself gets, no more.
        self.assertEqual(deadline, 2_700 + HARBOR_OVERHEAD_SECONDS)


class MaterializedSymlinkTests(unittest.TestCase):
    """Harbor's artifact download dereferences symlinks; identical-content
    materializations must not read as agent changes."""

    def _changed(self, before: Path, after: Path, out: Path, **kwargs):
        from swecraft_runner.runner import _collect_changes

        return _collect_changes(before, after, out, **kwargs)[1]

    def test_identical_file_materialization_is_not_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = root / "before", root / "after"
            before.mkdir(), after.mkdir()
            (before / "AGENTS.md").write_text("notes\n")
            os.symlink("AGENTS.md", before / "CLAUDE.md")
            (after / "AGENTS.md").write_text("notes\n")
            (after / "CLAUDE.md").write_text("notes\n")
            self.assertEqual(self._changed(before, after, root / "out"), ())

    def test_identical_directory_materialization_is_not_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = root / "before", root / "after"
            (before / "certs").mkdir(parents=True)
            (before / "certs" / "ca.pem").write_text("PEM\n")
            (before / "client").mkdir()
            os.symlink("../certs", before / "client" / "ca")
            (after / "certs").mkdir(parents=True)
            (after / "certs" / "ca.pem").write_text("PEM\n")
            (after / "client" / "ca").mkdir(parents=True)
            (after / "client" / "ca" / "ca.pem").write_text("PEM\n")
            self.assertEqual(self._changed(before, after, root / "out"), ())

    def test_tampered_materialization_stays_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = root / "before", root / "after"
            before.mkdir(), after.mkdir()
            (before / "README.md").write_text("readme\n")
            os.symlink("README.md", before / "README")
            (after / "README.md").write_text("readme\n")
            (after / "README").write_text("EDITED\n")
            self.assertEqual(self._changed(before, after, root / "out"), ("README",))

    def test_genuinely_deleted_directory_symlink_stays_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = root / "before", root / "after"
            (before / "certs").mkdir(parents=True)
            (before / "certs" / "ca.pem").write_text("PEM\n")
            os.symlink("certs", before / "alias")
            (after / "certs").mkdir(parents=True)
            (after / "certs" / "ca.pem").write_text("PEM\n")
            self.assertEqual(self._changed(before, after, root / "out"), ("alias",))

    def test_out_of_tree_target_stays_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after = root / "before", root / "after"
            before.mkdir(), after.mkdir()
            os.symlink("../outside", before / "escape")
            (after / "escape").mkdir()
            (after / "escape" / "x").write_text("x\n")
            self.assertIn("escape", self._changed(before, after, root / "out"))


class CollectPrefixTests(unittest.TestCase):
    def test_gitignored_contract_path_needs_the_prefix_to_be_collected(self) -> None:
        import shutil

        from swecraft_runner.runner import _collect_changes

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, _ = seed_repository(root)
            (repository / ".gitignore").write_text(".*\n")
            subprocess.run(
                ("git", "-C", str(repository), "add", "-f", ".gitignore"), check=True
            )
            subprocess.run(
                ("git", "-C", str(repository), "commit", "-qm", "ignore dotfiles"),
                check=True,
            )
            collected = root / "collected"
            shutil.copytree(repository, collected, symlinks=True)
            contract = collected / ".swecraft-eval"
            contract.mkdir()
            (contract / "recipe.yaml").write_text("status: drafted\n")

            _, changed, _ = _collect_changes(repository, collected, root / "o1")
            self.assertNotIn(".swecraft-eval/recipe.yaml", changed)

            _, changed, _ = _collect_changes(
                repository,
                collected,
                root / "o2",
                collect_prefixes=(".swecraft-eval/",),
            )
            self.assertIn(".swecraft-eval/recipe.yaml", changed)
