"""pi's session log becomes Harbor's trajectory.json."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from swecraft_runner.pi import trajectory

CALL = "call_1|fc_1"


def _usage(**counts: int) -> dict:
    usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0}
    usage.update(counts)
    usage["totalTokens"] = sum(usage.values())
    usage["cost"] = {"total": usage["output"] * 0.0001}
    return usage


def _assistant(content: list, usage: dict, stamp: int, stop: str) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "api": "openai-responses",
        "provider": "openai",
        "model": "gpt-6-astra",
        "usage": usage,
        "stopReason": stop,
        "timestamp": stamp,
        "responseId": "resp_1",
    }


def _log(truncated: bool = False) -> str:
    user = {
        "role": "user",
        "content": [{"type": "text", "text": "You are requests-bot."}],
        "timestamp": 1790033969931,
    }
    call = {
        "type": "toolCall",
        "id": CALL,
        "name": "read",
        "arguments": {"path": "/app/memory/GOAL.md"},
    }
    first = _assistant(
        [{"type": "thinking", "thinking": "look at the goal"}, call],
        _usage(input=3, output=24, cacheWrite=3249),
        1790033970000,
        "toolUse",
    )
    result = {
        "role": "toolResult",
        "toolCallId": CALL,
        "toolName": "read",
        "content": [{"type": "text", "text": "# Goal\n\nMake requests faster."}],
        "isError": False,
        "timestamp": 1790033970100,
    }
    second = _assistant(
        [{"type": "text", "text": "Nothing to do this wake."}],
        _usage(input=4000, output=12, cacheRead=3249),
        1790033980000,
        "stop",
    )
    ev = [
        {
            "type": "session",
            "version": 3,
            "id": "sess-1",
            "timestamp": "2026-09-21T23:39:29.618Z",
            "cwd": "/app",
        },
        {"type": "agent_start"},
        {"type": "turn_start"},
        {"type": "message_start", "message": user},
        {"type": "message_end", "message": user},
        {
            "type": "message_start",
            "message": {"role": "assistant", "content": [], "timestamp": 1790033969950},
        },
        {"type": "message_end", "message": first},
        {
            "type": "tool_execution_start",
            "toolCallId": CALL,
            "toolName": "read",
            "args": {"path": "/app/memory/GOAL.md"},
        },
        {
            "type": "tool_execution_update",
            "toolCallId": CALL,
            "toolName": "read",
            "args": {},
            "partialResult": {"content": []},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": CALL,
            "toolName": "read",
            "result": {
                "content": [{"type": "text", "text": "# Goal\n\nMake requests faster."}]
            },
            "isError": False,
        },
        {"type": "message_end", "message": result},
        {"type": "turn_end", "message": first, "toolResults": [result]},
        {"type": "turn_start"},
        {"type": "message_end", "message": second},
    ]
    if not truncated:
        ev += [
            {"type": "turn_end", "message": second, "toolResults": []},
            {
                "type": "agent_end",
                "messages": [user, first, result, second],
                "willRetry": False,
            },
            {"type": "agent_settled"},
        ]
    return "\n".join(json.dumps(e) for e in ev) + "\nnot json\n"


class ConvertTests(unittest.TestCase):
    def test_steps_tool_calls_observations_and_metrics(self) -> None:
        t = trajectory.convert(_log())
        self.assertEqual(t["schema_version"], trajectory.SCHEMA_VERSION)
        self.assertEqual(t["session_id"], "sess-1")
        self.assertEqual(t["agent"]["name"], "pi")
        self.assertEqual(t["agent"]["model_name"], "gpt-6-astra")
        self.assertEqual(t["agent"]["extra"]["provider"], "openai")
        steps = t["steps"]
        self.assertEqual([s["source"] for s in steps], ["user", "agent", "agent"])
        self.assertEqual([s["step_id"] for s in steps], [1, 2, 3])
        self.assertEqual(steps[0]["message"], "You are requests-bot.")
        first = steps[1]
        self.assertEqual(first["timestamp"], "2026-09-21T23:39:30.000Z")
        self.assertEqual(first["message"], "")
        self.assertEqual(first["reasoning_content"], "look at the goal")
        self.assertNotIn("reasoning_content", t["steps"][2])
        self.assertEqual(first["extra"]["stop_reason"], "toolUse")
        self.assertEqual(
            first["tool_calls"],
            [
                {
                    "tool_call_id": CALL,
                    "function_name": "read",
                    "arguments": {"path": "/app/memory/GOAL.md"},
                }
            ],
        )
        (observed,) = first["observation"]["results"]
        self.assertEqual(observed["source_call_id"], CALL)
        self.assertIn("Make requests faster", observed["content"])
        self.assertEqual(observed["extra"]["tool"], "read")
        self.assertFalse(observed["extra"]["error"])
        # Harbor's convention: prompt_tokens is the whole prompt and
        # cached_tokens the cache-read part of it
        self.assertEqual(first["metrics"]["prompt_tokens"], 3 + 3249)
        self.assertEqual(first["metrics"]["completion_tokens"], 24)
        self.assertEqual(first["metrics"]["cached_tokens"], 0)
        self.assertEqual(first["metrics"]["extra"]["cache_creation_input_tokens"], 3249)
        self.assertAlmostEqual(first["metrics"]["cost_usd"], 0.0024)
        second = steps[2]
        self.assertEqual(second["message"], "Nothing to do this wake.")
        self.assertNotIn("tool_calls", second)
        self.assertEqual(second["metrics"]["prompt_tokens"], 4000 + 3249)
        self.assertEqual(second["metrics"]["cached_tokens"], 3249)
        final = t["final_metrics"]
        self.assertEqual(final["total_prompt_tokens"], 3 + 3249 + 4000 + 3249)
        self.assertEqual(final["total_completion_tokens"], 36)
        self.assertEqual(final["total_cached_tokens"], 3249)
        self.assertAlmostEqual(final["total_cost_usd"], 0.0036)
        self.assertEqual(final["total_steps"], 3)
        self.assertEqual(final["extra"]["started_at"], "2026-09-21T23:39:29.618Z")

    def test_a_log_cut_short_still_converts(self) -> None:
        t = trajectory.convert(_log(truncated=True))
        self.assertEqual(len(t["steps"]), 3)
        self.assertEqual(t["final_metrics"]["total_completion_tokens"], 36)

    def test_no_usage_means_no_price(self) -> None:
        log = "\n".join(
            json.dumps(e)
            for e in [
                {"type": "session", "id": "s"},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": "plain text",
                        "model": "m",
                    },
                },
            ]
        )
        t = trajectory.convert(log)
        self.assertEqual(t["steps"][0]["message"], "plain text")
        self.assertEqual(t["steps"][0]["metrics"]["prompt_tokens"], 0)
        self.assertNotIn("cost_usd", t["steps"][0]["metrics"])
        self.assertIsNone(t["final_metrics"]["total_cost_usd"])

    def test_an_empty_log_is_an_empty_trajectory(self) -> None:
        t = trajectory.convert("")
        self.assertEqual(t["steps"], [])
        self.assertIsNone(t["session_id"])


class WriteTests(unittest.TestCase):
    def _run(self, root: Path, run_id: str) -> Path:
        agent_dir = (
            root
            / run_id
            / "harbor/jobs"
            / run_id
            / f"{run_id.lower()}-0001__abc"
            / "agent"
        )
        agent_dir.mkdir(parents=True)
        (agent_dir / "pi.txt").write_text(_log(), encoding="utf-8")
        return agent_dir

    def test_written_beside_the_log(self) -> None:
        root = Path(tempfile.mkdtemp())
        run_id = "20260921T233921Z-a30ff150"
        agent_dir = self._run(root, run_id)
        target = trajectory.write_trajectory(agent_dir)
        self.assertEqual(target, agent_dir / "trajectory.json")
        self.assertEqual((agent_dir / "pi.txt").read_text(encoding="utf-8"), _log())
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8"))["agent"]["name"], "pi"
        )
        # a second call keeps the file; force rewrites it
        before = os.stat(target).st_mtime_ns
        self.assertEqual(trajectory.write_trajectory(agent_dir), target)
        self.assertEqual(os.stat(target).st_mtime_ns, before)
        (agent_dir / "pi.txt").write_text(_log(truncated=True), encoding="utf-8")
        trajectory.write_trajectory(agent_dir, force=True)
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8"))["final_metrics"][
                "total_steps"
            ],
            3,
        )
        self.assertFalse((agent_dir / "trajectory.json.tmp").exists())
        steps = json.loads(target.read_text(encoding="utf-8"))["steps"]
        self.assertEqual([s["step_id"] for s in steps], [1, 2, 3])
        self.assertEqual(steps[1]["source"], "agent")

    def test_no_log_means_nothing_written(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.assertIsNone(trajectory.write_trajectory(root))
        self.assertEqual(list(root.iterdir()), [])

    def test_backfill_takes_finished_runs_only(self) -> None:
        root = Path(tempfile.mkdtemp())
        live = self._run(root, "20260921T000000Z-11111111")
        done = self._run(root, "20260921T000001Z-22222222")
        (done.parents[1] / "result.json").write_text(
            json.dumps({"finished_at": "2026-09-21T00:00:09"}), encoding="utf-8"
        )
        self.assertEqual(trajectory.backfill(root), {"written": 1, "kept": 0})
        self.assertTrue((done / "trajectory.json").exists())
        self.assertFalse((live / "trajectory.json").exists())
        self.assertEqual(trajectory.backfill(root), {"written": 0, "kept": 1})
        self.assertEqual(
            trajectory.backfill(root, force=True), {"written": 1, "kept": 0}
        )

    def test_backfill_waits_for_the_job_terminal_state(self) -> None:
        # Harbor 0.22 writes the job result before the trials start and updates it
        # while finished_at is still null; a run in that state is still streaming.
        root = Path(tempfile.mkdtemp())
        streaming = self._run(root, "20260921T000002Z-33333333")
        result = streaming.parents[1] / "result.json"
        result.write_text(
            json.dumps({"started_at": "2026-09-21T00:00:02", "finished_at": None}),
            encoding="utf-8",
        )
        self.assertEqual(trajectory.backfill(root), {"written": 0, "kept": 0})
        self.assertFalse((streaming / "trajectory.json").exists())
        result.write_text("not json", encoding="utf-8")
        self.assertEqual(trajectory.backfill(root), {"written": 0, "kept": 0})
        result.write_text(
            json.dumps(
                {
                    "started_at": "2026-09-21T00:00:02",
                    "finished_at": "2026-09-21T00:00:09",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(trajectory.backfill(root), {"written": 1, "kept": 0})
        self.assertTrue((streaming / "trajectory.json").exists())

    def test_cli(self) -> None:
        root = Path(tempfile.mkdtemp())
        done = self._run(root, "20260921T000001Z-22222222")
        (done.parents[1] / "result.json").write_text(
            json.dumps({"finished_at": "2026-09-21T00:00:09"}), encoding="utf-8"
        )
        self.assertEqual(trajectory.main(["--runs", str(root)]), 0)
        self.assertTrue((done / "trajectory.json").exists())
        self.assertEqual(trajectory.main([str(done / "pi.txt"), "--force"]), 0)
        self.assertEqual(trajectory.main([str(root / "missing" / "pi.txt")]), 1)


if __name__ == "__main__":
    unittest.main()
