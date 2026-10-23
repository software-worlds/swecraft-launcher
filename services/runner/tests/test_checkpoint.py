from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swecraft_runner import RunnerError
from swecraft_runner.checkpoint import _pack_harbor, export_run


class RunCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_completed_claude_run_is_projected_without_mutating_source(self) -> None:
        source = self.root / "runs/run-1"
        _write_json(
            source / "input/request.json",
            {
                "agent": "claude-code",
                "model": "test/model",
                "repository": "http://forge/hub/demo.git",
                "resolved_sha": "a" * 40,
            },
        )
        (source / "input/prompt.md").write_text("Do the work.\n", encoding="utf-8")
        (source / "input/repository/.git").mkdir(parents=True)
        (source / "input/repository/.git/config").write_text("git\n")
        _write_json(
            source / "output/result.json",
            {"run_id": "run-1", "status": "completed", "usage": {"cost_usd": 1.5}},
        )
        (source / "output/response.md").write_text("Done.\n", encoding="utf-8")
        _write_json(source / "output/changes.json", {"modified": ["demo.py"]})
        (source / "output/changes").mkdir()
        (source / "output/changes/demo.py").write_text("changed\n")
        _write_json(source / "output/agent-result.json", {"outcome": "progress"})

        agent = source / "harbor/jobs/job/trial/agent"
        _write_json(agent / "trajectory.json", {"schema_version": "ATIF-v1.7"})
        (agent / "claude-code.txt").write_text('{"type":"result"}\n')
        sessions = agent / "sessions/projects/-app"
        sessions.mkdir(parents=True)
        (sessions / "session.jsonl").write_text('{"turn":1}\n')
        tool_results = sessions / "session/tool-results"
        tool_results.mkdir(parents=True)
        (tool_results / "tool.txt").write_text("large output\n")
        _write_json(source / "harbor/jobs/job/trial/result.json", {"agent_result": {}})
        git_dir = source / "harbor/tasks/task/environment/repository/.git"
        git_dir.mkdir(parents=True)
        (git_dir / "config").write_text("harbor git\n")
        outside = self.root / "outside.txt"
        outside.write_text("outside\n")
        (source / "harbor/link").symlink_to(outside)
        (source / "output/session").mkdir()
        (source / "output/session/session.jsonl").write_text('{"duplicate":true}\n')

        before = _inventory(source)
        destination = self.root / "checkpoint/run-1"
        export_run(source, destination)

        self.assertEqual(_inventory(source), before)
        self.assertEqual(
            sorted(
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            ),
            [
                "harbor.tar.gz",
                "output/agent-stdout.txt",
                "output/changes.json",
                "output/response.md",
                "output/result.json",
                "output/session/projects/-app/session.jsonl",
                "output/session/projects/-app/session/tool-results/tool.txt",
                "output/trajectory.json",
            ],
        )
        self.assertFalse((destination / "input").exists())
        self.assertFalse((destination / "output/changes").exists())
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["agent"], "claude-code")
        self.assertEqual(receipt["structured_result"], {"outcome": "progress"})
        self.assertEqual(
            receipt["session_files"],
            [
                "projects/-app/session.jsonl",
                "projects/-app/session/tool-results/tool.txt",
            ],
        )
        with tarfile.open(destination / "harbor.tar.gz") as archive:
            names = archive.getnames()
            self.assertIn("harbor/tasks/task/environment/repository/.git/config", names)
            link = archive.getmember("harbor/link")
            self.assertTrue(link.issym())
            self.assertNotIn("outside.txt", names)

        second_generation = self.root / "second-checkpoint/run-1"
        export_run(destination, second_generation)
        self.assertEqual(_inventory(second_generation), _inventory(destination))

    def test_omit_harbor_keeps_normalized_evidence_without_the_archive(self) -> None:
        source = self.root / "runs/run-2"
        _write_json(
            source / "input/request.json",
            {"agent": "claude-code", "model": "test/model"},
        )
        _write_json(source / "output/result.json", {"status": "completed"})
        (source / "output/response.md").write_text("Done.\n", encoding="utf-8")
        agent = source / "harbor/jobs/job/trial/agent"
        _write_json(agent / "trajectory.json", {"schema_version": "ATIF-v1.7"})
        _write_json(source / "harbor/jobs/job/trial/result.json", {"agent_result": {}})

        destination = self.root / "checkpoint/run-2"
        export_run(source, destination, include_harbor=False)

        self.assertFalse((destination / "harbor.tar.gz").exists())
        self.assertTrue((destination / "output/trajectory.json").is_file())
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertNotIn("harbor_archive", receipt)

    def test_failed_codex_run_keeps_small_inputs_and_recovers_usage(self) -> None:
        source = self.root / "runs/run-2"
        request = {
            "agent": "codex",
            "model": "test/model",
            "repository": "http://forge/hub/demo.git",
            "resolved_sha": "b" * 40,
        }
        _write_json(source / "input/request.json", request)
        (source / "input/prompt.md").write_text("Try the work.\n", encoding="utf-8")
        (source / "input/repository/.git").mkdir(parents=True)
        (source / "input/repository/.git/config").write_text("git\n")
        _write_json(
            source / "output/result.json",
            {"run_id": "run-2", "status": "failed", "error": "agent failed"},
        )
        trial = source / "harbor/jobs/job/trial"
        _write_json(
            trial / "result.json",
            {
                "agent_result": {
                    "n_input_tokens": 10,
                    "n_cache_tokens": 4,
                    "n_output_tokens": 2,
                    "cost_usd": 0.25,
                }
            },
        )
        (trial / "exception.txt").write_text("failure\n")
        _write_json(trial / "agent/trajectory.json", {"steps": []})
        (trial / "agent/codex.txt").write_text('{"type":"error"}\n')
        rollout = trial / "agent/sessions/2026/08/03/rollout-one.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"session":"one"}\n')

        destination = self.root / "checkpoint/run-2"
        export_run(source, destination)

        self.assertEqual(
            (destination / "input/prompt.md").read_text(), "Try the work.\n"
        )
        self.assertEqual(
            json.loads((destination / "input/request.json").read_text()), request
        )
        self.assertFalse(any(".git" in path.parts for path in destination.rglob("*")))
        self.assertTrue(
            (
                destination / "output/session/sessions/2026/08/03/rollout-one.jsonl"
            ).is_file()
        )
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertEqual(receipt["resolved_sha"], "b" * 40)
        self.assertEqual(
            receipt["usage"],
            {
                "cache_tokens": 4,
                "cost_usd": 0.25,
                "input_tokens": 10,
                "output_tokens": 2,
            },
        )

    def test_antigravity_run_projects_the_cli_transcript_and_stdout(self) -> None:
        source = self.root / "runs/run-agy"
        _write_json(
            source / "input/request.json",
            {
                "agent": "antigravity-cli",
                "model": "google/gemini-3.8-flash",
                "repository": "http://forge/hub/demo.git",
                "resolved_sha": "e" * 40,
            },
        )
        _write_json(
            source / "output/result.json",
            {"run_id": "run-agy", "status": "completed", "agent": "antigravity-cli"},
        )
        trial = source / "harbor/jobs/job/trial"
        _write_json(trial / "result.json", {"agent_result": {}})
        _write_json(trial / "agent/trajectory.json", {"steps": []})
        (trial / "agent/antigravity-cli.txt").write_text("DONE\n")
        # Harbor copies the CLI's step transcript beside its stdout.
        (trial / "agent/antigravity-cli.trajectory.jsonl").write_text(
            '{"type":"USER_INPUT"}\n{"type":"PLANNER_RESPONSE"}\n'
        )
        (trial / "agent/antigravity-cli.trajectory.jsonl.bak").write_text("{}\n")

        destination = self.root / "checkpoint/run-agy"
        export_run(source, destination)

        session = (
            destination / "output/session/sessions/antigravity-cli.trajectory.jsonl"
        )
        self.assertTrue(session.is_file())
        self.assertTrue((destination / "output/agent-stdout.txt").is_file())
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertEqual(
            receipt["session_files"], ["sessions/antigravity-cli.trajectory.jsonl"]
        )
        self.assertEqual(receipt["agent_stdout"], "output/agent-stdout.txt")

    def test_pi_run_projects_log_session_and_derived_trajectory(self) -> None:
        source = self.root / "runs/run-pi"
        _write_json(
            source / "input/request.json",
            {
                "agent": "pi",
                "model": "openai-codex/gpt-6-astra",
                "repository": "http://forge/hub/demo.git",
                "resolved_sha": "f" * 40,
            },
        )
        _write_json(
            source / "output/result.json",
            {"run_id": "run-pi", "status": "completed", "agent": "pi"},
        )
        trial = source / "harbor/jobs/job/trial"
        _write_json(trial / "result.json", {"agent_result": {}})
        (trial / "agent").mkdir(parents=True)
        (trial / "agent/pi.txt").write_text('{"type":"session","id":"s1"}\n')
        _write_json(trial / "agent/trajectory.json", {"agent": {"name": "pi"}})
        sessions = trial / "agent/pi/sessions"
        sessions.mkdir(parents=True)
        (sessions / "2026-09-21T23-39-53-570Z_s1.jsonl").write_text(
            '{"type":"session"}\n'
        )
        (trial / "agent/setup").mkdir()
        (trial / "agent/setup/pi.log").write_text("installed\n")

        destination = self.root / "checkpoint/run-pi"
        export_run(source, destination)

        self.assertEqual(
            (destination / "output/agent-stdout.txt").read_text(),
            '{"type":"session","id":"s1"}\n',
        )
        self.assertEqual(
            json.loads((destination / "output/trajectory.json").read_text()),
            {"agent": {"name": "pi"}},
        )
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertEqual(
            receipt["session_files"], ["sessions/2026-09-21T23-39-53-570Z_s1.jsonl"]
        )
        self.assertEqual(receipt["agent_stdout"], "output/agent-stdout.txt")
        self.assertEqual(receipt["trajectory"], "output/trajectory.json")
        self.assertFalse((destination / "output/session/setup").exists())

    def test_kimi_run_projects_native_session_jsonl_without_stdout(self) -> None:
        source = self.root / "runs/run-kimi"
        _write_json(
            source / "input/request.json",
            {
                "agent": "kimi-cli",
                "model": "openai/@cf/moonshotai/kimi-k2.7-code",
                "repository": "http://forge/hub/demo.git",
                "resolved_sha": "c" * 40,
            },
        )
        _write_json(
            source / "output/result.json",
            {"run_id": "run-kimi", "status": "completed", "agent": "kimi-cli"},
        )
        trial = source / "harbor/jobs/job/trial"
        _write_json(trial / "result.json", {"agent_result": {}})
        _write_json(trial / "agent/trajectory.json", {"steps": []})
        (trial / "agent").mkdir(parents=True, exist_ok=True)
        (trial / "agent/kimi-cli.txt").write_text('{"jsonrpc":"2.0"}\n')
        session = trial / "agent/kimi/share/sessions/hash/session-1"
        session.mkdir(parents=True)
        (session / "context.jsonl").write_text('{"role":"user"}\n')
        (session / "wire.jsonl").write_text('{"request":1}\n')
        (session / "state.json").write_text('{"id":"session-1"}\n')
        (session / "scratch.jsonl").write_text('{"debug":true}\n')
        subagent = session / "subagents/sub-1"
        subagent.mkdir(parents=True)
        (subagent / "context.jsonl").write_text('{"role":"assistant"}\n')
        (subagent / "wire.jsonl").write_text('{"request":2}\n')
        (subagent / "state.json").write_text('{"id":"sub-1"}\n')
        (subagent / "events.jsonl").write_text('{"event":"debug"}\n')
        (session / "notifications").mkdir()
        (session / "notifications/delivery.json").write_text("{}\n")

        destination = self.root / "checkpoint/run-kimi"
        export_run(source, destination)

        base = destination / "output/session/sessions/hash/session-1"
        self.assertTrue((base / "context.jsonl").is_file())
        self.assertTrue((base / "wire.jsonl").is_file())
        self.assertTrue((base / "subagents/sub-1/context.jsonl").is_file())
        self.assertTrue((base / "subagents/sub-1/wire.jsonl").is_file())
        self.assertFalse((base / "state.json").exists())
        self.assertFalse((base / "scratch.jsonl").exists())
        self.assertFalse((base / "subagents/sub-1/state.json").exists())
        self.assertFalse((base / "subagents/sub-1/events.jsonl").exists())
        self.assertFalse((base / "notifications/delivery.json").exists())
        self.assertFalse((destination / "output/agent-stdout.txt").exists())
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertNotIn("agent_stdout", receipt)
        kimi_session_files = [
            "sessions/hash/session-1/context.jsonl",
            "sessions/hash/session-1/subagents/sub-1/context.jsonl",
            "sessions/hash/session-1/subagents/sub-1/wire.jsonl",
            "sessions/hash/session-1/wire.jsonl",
        ]
        self.assertEqual(receipt["session_files"], kimi_session_files)

        (destination / "output/agent-stdout.txt").write_text(
            '{"jsonrpc":"2.0"}\n', encoding="utf-8"
        )
        (base / "state.json").write_text('{"legacy":true}\n', encoding="utf-8")
        (base / "scratch.jsonl").write_text('{"legacy":true}\n', encoding="utf-8")
        receipt["agent_stdout"] = "output/agent-stdout.txt"
        receipt["session_files"].extend(
            [
                "sessions/hash/session-1/scratch.jsonl",
                "sessions/hash/session-1/state.json",
            ]
        )
        _write_json(destination / "output/result.json", receipt)

        second_generation = self.root / "second-checkpoint/run-kimi"
        export_run(destination, second_generation)

        second_base = second_generation / "output/session/sessions/hash/session-1"
        self.assertTrue((second_base / "context.jsonl").is_file())
        self.assertTrue((second_base / "wire.jsonl").is_file())
        self.assertTrue((second_base / "subagents/sub-1/context.jsonl").is_file())
        self.assertTrue((second_base / "subagents/sub-1/wire.jsonl").is_file())
        self.assertFalse((second_base / "state.json").exists())
        self.assertFalse((second_base / "scratch.jsonl").exists())
        self.assertFalse((second_generation / "output/agent-stdout.txt").exists())
        second_receipt = json.loads(
            (second_generation / "output/result.json").read_text()
        )
        self.assertNotIn("agent_stdout", second_receipt)
        self.assertEqual(second_receipt["session_files"], kimi_session_files)

    def test_failed_run_uses_pricing_recorded_with_the_request(self) -> None:
        source = self.root / "runs/run-priced"
        _write_json(
            source / "input/request.json",
            {
                "agent": "kimi-cli",
                "model": "openai/@cf/moonshotai/kimi-k2.7-code",
                "model_pricing": {
                    "input_per_million": 0.95,
                    "cached_input_per_million": 0.19,
                    "output_per_million": 4.0,
                },
            },
        )
        _write_json(source / "output/result.json", {"status": "failed"})
        _write_json(
            source / "harbor/jobs/job/trial/result.json",
            {
                "agent_result": {
                    "n_input_tokens": 2_000_000,
                    "n_cache_tokens": 1_000_000,
                    "n_output_tokens": 500_000,
                    "cost_usd": None,
                }
            },
        )

        destination = self.root / "checkpoint/run-priced"
        export_run(source, destination)

        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertAlmostEqual(receipt["usage"]["cost_usd"], 3.14)

    def test_forge_actions_travel_with_the_run_they_belong_to(self) -> None:
        # A checkpoint is the experiment's durable record. Leaving the ledger
        # behind while the receipt kept pointing at it produced a checkpoint
        # that referenced provenance it did not contain.
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                source = self.root / f"runs/run-forge-{status}"
                _write_json(
                    source / "output/result.json",
                    {
                        "run_id": f"run-forge-{status}",
                        "status": status,
                        "forge_actions": "output/forge-actions.jsonl",
                        "forge_calls": "output/forge-calls.jsonl",
                    },
                )
                (source / "output/response.md").write_text("Done.\n", encoding="utf-8")
                (source / "output/forge-actions.jsonl").write_text(
                    '{"action":"pr.merge","repo":"hub/demo",'
                    '"metadata":{"number":2,"method":null}}\n',
                    encoding="utf-8",
                )
                (source / "output/forge-calls.jsonl").write_text(
                    '{"tool":"fj","argv":["pr","merge","hub/demo#2"],"exit":0}\n',
                    encoding="utf-8",
                )
                (source / "output/forge-bodies.jsonl").write_text(
                    '{"kind":"comment","repo":"hub/demo","id":40,'
                    '"title":null,"body":"fetched from the forge","missing":false}\n',
                    encoding="utf-8",
                )

                destination = self.root / f"export/run-forge-{status}"
                export_run(source, destination)

                projected = destination / "output/forge-actions.jsonl"
                self.assertTrue(projected.is_file(), "ledger was not projected")
                self.assertIn("pr.merge", projected.read_text())
                receipt = json.loads((destination / "output/result.json").read_text())
                self.assertEqual(receipt["forge_actions"], "output/forge-actions.jsonl")
                # The recording travels with the ledger derived from it.
                recording = destination / "output/forge-calls.jsonl"
                self.assertTrue(recording.is_file(), "recording was not projected")
                self.assertEqual(receipt["forge_calls"], "output/forge-calls.jsonl")
                # And the forge's backfilled bodies: the only copy of
                # words the recording never held.
                bodies = destination / "output/forge-bodies.jsonl"
                self.assertTrue(bodies.is_file(), "bodies were not projected")
                self.assertIn("fetched from the forge", bodies.read_text())
                self.assertEqual(receipt["forge_bodies"], "output/forge-bodies.jsonl")

    def test_a_run_without_forge_actions_carries_no_pointer(self) -> None:
        # The pointer must not outlive the file in either direction: a wake
        # that only read the forge writes no ledger, and a receipt claiming one
        # would be the same dangling reference from the other side.
        source = self.root / "runs/run-no-forge"
        _write_json(
            source / "output/result.json",
            {
                "run_id": "run-no-forge",
                "status": "completed",
                "forge_actions": "output/forge-actions.jsonl",
            },
        )
        (source / "output/response.md").write_text("Done.\n", encoding="utf-8")

        destination = self.root / "export/run-no-forge"
        export_run(source, destination)

        self.assertFalse((destination / "output/forge-actions.jsonl").exists())
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertIsNone(receipt.get("forge_actions"))

    def test_missing_optional_artifacts_are_omitted(self) -> None:
        source = self.root / "runs/run-3"
        _write_json(
            source / "output/result.json",
            {"run_id": "run-3", "status": "completed"},
        )

        destination = self.root / "checkpoint/run-3"
        export_run(source, destination)

        self.assertEqual(
            [
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            ],
            ["output/result.json"],
        )
        receipt = json.loads((destination / "output/result.json").read_text())
        self.assertEqual(receipt["session_files"], [])
        self.assertNotIn("harbor_archive", receipt)

    def test_nonterminal_run_is_refused_without_creating_destination(self) -> None:
        source = self.root / "runs/run-4"
        _write_json(source / "output/result.json", {"status": "running"})
        destination = self.root / "checkpoint/run-4"

        with self.assertRaisesRegex(RunnerError, "not terminal"):
            export_run(source, destination)

        self.assertFalse(destination.exists())

    def test_missing_or_truncated_receipt_is_nonterminal(self) -> None:
        for name, content in (("missing", None), ("truncated", "{\n")):
            with self.subTest(name=name):
                source = self.root / f"runs/{name}"
                source.mkdir(parents=True)
                if content is not None:
                    output = source / "output"
                    output.mkdir()
                    (output / "result.json").write_text(content)
                with self.assertRaisesRegex(RunnerError, "not terminal"):
                    export_run(source, self.root / f"checkpoint/{name}")

    def test_destination_inside_live_run_is_refused(self) -> None:
        source = self.root / "runs/run-5"
        _write_json(source / "output/result.json", {"status": "completed"})

        with self.assertRaisesRegex(RunnerError, "outside the live run"):
            export_run(source, source / "checkpoint-copy")

        self.assertFalse((source / "checkpoint-copy").exists())


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")


def _inventory(root: Path) -> tuple[tuple[str, str, str], ...]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((relative, "file", digest))
        elif path.is_dir():
            entries.append((relative, "directory", ""))
    return tuple(entries)


class HarborArchiveCacheTests(unittest.TestCase):
    def _run_dir(self, root: Path, name: str) -> Path:
        run = root / name
        (run / "output").mkdir(parents=True)
        (run / "output" / "result.json").write_text(
            json.dumps({"status": "completed", "run_id": name})
        )
        (run / "harbor" / "jobs").mkdir(parents=True)
        (run / "harbor" / "jobs" / "log.txt").write_text("evidence\n")
        return run

    def test_first_export_fills_cache_and_second_skips_packing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = self._run_dir(root, "20260101T000000Z-aaaaaaaa")
            cache = root / "harbor-cache"

            export_run(run, root / "first", include_harbor=True, harbor_cache=cache)
            cached = cache / f"{run.name}.harbor.tar.gz"
            self.assertTrue(cached.is_file())
            self.assertEqual(
                cached.read_bytes(), (root / "first" / "harbor.tar.gz").read_bytes()
            )

            with patch(
                "swecraft_runner.checkpoint._pack_harbor",
                side_effect=AssertionError("cache hit must not re-pack"),
            ):
                export_run(
                    run, root / "second", include_harbor=True, harbor_cache=cache
                )
            self.assertEqual(
                (root / "second" / "harbor.tar.gz").read_bytes(),
                cached.read_bytes(),
            )

    def test_degraded_cache_entries_fall_back_to_packing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = self._run_dir(root, "20260101T000000Z-bbbbbbbb")
            cache = root / "harbor-cache"
            cache.mkdir()
            # An empty file is a crashed fill; it must be repacked over.
            (cache / f"{run.name}.harbor.tar.gz").write_bytes(b"")

            export_run(run, root / "out", include_harbor=True, harbor_cache=cache)

            staged = (root / "out" / "harbor.tar.gz").read_bytes()
            self.assertGreater(len(staged), 0)
            self.assertEqual((cache / f"{run.name}.harbor.tar.gz").read_bytes(), staged)

    def test_no_cache_behaves_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run = self._run_dir(root, "20260101T000000Z-cccccccc")
            export_run(run, root / "out", include_harbor=True)
            self.assertTrue((root / "out" / "harbor.tar.gz").is_file())


class HarborArchiveDeterminismTests(unittest.TestCase):
    def test_identical_trees_pack_to_identical_bytes(self) -> None:
        """Re-packing an unchanged run must be a byte-level no-op.

        The checkpoint pipeline re-exports every terminal run on every
        capture, and the published dataset deduplicates by content hash --
        so any nondeterminism here re-uploads and re-stores every run's
        archive on every checkpoint. Plain ``w:gz`` failed this two ways:
        gzip stamps wall-clock time into its header, and it embeds the
        output filename, which is a randomized tempfile name.
        """

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "harbor"
            (source / "jobs" / "a").mkdir(parents=True)
            (source / "jobs" / "a" / "result.json").write_text("{}\n")
            (source / "stdout.log").write_text("hello\n")

            first = root / "first" / "harbor.tar.gz"
            second = root / "second" / "harbor.tar.gz"
            _pack_harbor(source, first)
            # A different destination directory exercises the randomized
            # tempfile name; gzip's clock resolution is one second, so a
            # sleep would be needed to exercise mtime -- the mtime=0 pin
            # makes the clock irrelevant instead.
            _pack_harbor(source, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_archive_remains_a_valid_harbor_tarball(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "harbor"
            (source / "tasks").mkdir(parents=True)
            (source / "tasks" / "log.txt").write_text("x\n")

            destination = root / "out" / "harbor.tar.gz"
            _pack_harbor(source, destination)

            with tarfile.open(destination, mode="r:gz") as archive:
                names = sorted(archive.getnames())
            self.assertIn("harbor/tasks/log.txt", names)
            self.assertTrue(
                all(n == "harbor" or n.startswith("harbor/") for n in names)
            )


if __name__ == "__main__":
    unittest.main()
