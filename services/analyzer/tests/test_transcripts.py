import json
import tempfile
import unittest
from pathlib import Path

from swecraft_analyzer.transcripts import from_atif, js_tool_calls, read_run, trim


def _step(step_id, calls, outputs=None):
    return {
        "step_id": step_id,
        "source": "agent",
        "tool_calls": calls,
        "observation": {
            "results": [
                {"source_call_id": key, "content": value}
                for key, value in (outputs or {}).items()
            ]
        },
    }


def _pi_log(command: str, result: str) -> str:
    """A pi event log: the prompt, one bash call, and its result."""

    events = [
        {"type": "session", "id": "s1", "timestamp": "2026-09-29T12:30:37Z"},
        {
            "type": "message_end",
            "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "t1",
                        "name": "bash",
                        "arguments": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "toolCallId": "t1",
                "toolName": "bash",
                "content": [{"type": "text", "text": result}],
            },
        },
    ]
    return "\n".join(map(json.dumps, events))


class CodexCodeModeTest(unittest.TestCase):
    def test_json_object_argument(self) -> None:
        calls = js_tool_calls(
            'const r = await tools.exec_command({"cmd":"git status","workdir":"/app"});'
        )
        self.assertEqual(
            calls, [("exec_command", {"cmd": "git status", "workdir": "/app"})]
        )

    def test_bare_keys_variables_and_shorthand(self) -> None:
        source = (
            'const cmd = "pytest -q tests";\n'
            "await tools.exec_command({cmd, workdir: '/app/repository'});\n"
            'const patch = "*** Begin Patch\\n*** Update File: src/a.py'
            '\\n@@\\n-x\\n+y\\n";\n'
            "await tools.apply_patch(patch);\n"
        )
        calls = js_tool_calls(source)
        self.assertEqual(calls[0], ("exec_command", {"cmd": "pytest -q tests"}))
        self.assertEqual(calls[1][0], "apply_patch")
        self.assertIn("*** Update File: src/a.py", calls[1][1]["input"])

    def test_loop_over_array_yields_one_call_per_command(self) -> None:
        source = (
            'const cmds = ["git fetch", "git log -1"];\n'
            "for (const c of cmds) { await tools.exec_command({cmd: c}); }"
        )
        self.assertEqual(
            [arguments["cmd"] for _, arguments in js_tool_calls(source)],
            ["git fetch", "git log -1"],
        )

    def test_unparsed_exec_still_reaches_detectors_as_shell_text(self) -> None:
        payload = {
            "steps": [
                _step(
                    1,
                    [
                        {
                            "tool_call_id": "c1",
                            "function_name": "exec",
                            "arguments": {"input": "const x = compute(); text(x);"},
                        }
                    ],
                )
            ]
        }
        (call,) = from_atif(payload)
        self.assertEqual((call.tool, call.kind), ("exec", "shell"))
        self.assertIn("compute()", call.text)

    def test_unresolved_calls_beside_resolved_ones_stay_visible(self) -> None:
        source = (
            'await tools.exec_command({cmd:"pwd"}); '
            "await tools.exec_command("
            '{cmd:getCommand("git push --force origin HEAD")});'
        )
        payload = {
            "steps": [
                _step(
                    1,
                    [
                        {
                            "tool_call_id": "c1",
                            "function_name": "exec",
                            "arguments": {"input": source},
                        }
                    ],
                )
            ]
        }
        texts = [(call.tool, call.text) for call in from_atif(payload)]
        self.assertIn(("exec_command", "pwd"), texts)
        (leftover,) = [text for tool, text in texts if tool == "exec"]
        self.assertIn("git push --force origin HEAD", leftover)
        self.assertNotIn('cmd:"pwd"', leftover)

    def test_apply_patch_names_the_patched_file(self) -> None:
        source = (
            'await tools.apply_patch("*** Begin Patch\\n'
            '*** Update File: /app/memory/GOAL.md\\n@@\\n+x\\n*** End Patch");'
        )
        payload = {
            "steps": [
                _step(
                    3,
                    [
                        {
                            "tool_call_id": "c1",
                            "function_name": "exec",
                            "arguments": {"input": source},
                        }
                    ],
                    {"c1": "Done"},
                )
            ]
        }
        (call,) = from_atif(payload)
        self.assertEqual(
            (call.kind, call.path, call.output), ("edit", "/app/memory/GOAL.md", "Done")
        )


class OtherHarnessTest(unittest.TestCase):
    def test_pi_and_claude_tools_map_to_kinds(self) -> None:
        payload = {
            "steps": [
                _step(
                    2,
                    [
                        {
                            "tool_call_id": "a",
                            "function_name": "bash",
                            "arguments": {"command": "ls"},
                        },
                        {
                            "tool_call_id": "b",
                            "function_name": "read",
                            "arguments": {"path": "/app/memory/GOAL.md"},
                        },
                        {
                            "tool_call_id": "c",
                            "function_name": "Edit",
                            "arguments": {
                                "file_path": "x.py",
                                "old_string": "a",
                                "new_string": "b",
                            },
                        },
                        {
                            "tool_call_id": "d",
                            "function_name": "WebFetch",
                            "arguments": {"url": "https://pypi.org/x"},
                        },
                    ],
                    {"a": [{"type": "text", "text": "README.md"}]},
                )
            ]
        }
        calls = from_atif(payload)
        self.assertEqual(
            [call.kind for call in calls], ["shell", "read", "edit", "web"]
        )
        self.assertEqual(calls[0].output, "README.md")
        self.assertEqual(calls[1].text, "/app/memory/GOAL.md")
        self.assertEqual(calls[3].text, "https://pypi.org/x")

    def test_read_run_falls_back_to_the_pi_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "agent-stdout.txt").write_text(_pi_log("git push", "ok"))
            source, calls = read_run(output)
        self.assertEqual(source, "pi-log")
        self.assertEqual((calls[0].text, calls[0].output), ("git push", "ok"))
        # The pi converter numbers the steps: the prompt is step 1, so the
        # call is step 2.
        self.assertEqual(calls[0].step, 2)

    def test_live_run_tree_supplies_a_missing_transcript(self) -> None:
        from swecraft_analyzer.transcripts import read_live_run

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "20260929T123037Z-923e247f"
            agent = run / "harbor" / "jobs" / "job" / "trial" / "agent"
            agent.mkdir(parents=True)
            (agent / "pi.txt").write_text(_pi_log("git push --force origin perf", "ok"))
            source, calls = read_live_run(run)
            missing, _ = read_live_run(Path(tmp) / "absent")
        self.assertEqual(
            (source, calls[0].text), ("live-pi-log", "git push --force origin perf")
        )
        self.assertEqual(missing, "none")

    def test_long_outputs_keep_head_and_tail(self) -> None:
        text = "a" * 5_000 + "TAIL"
        trimmed = trim(text)
        self.assertTrue(trimmed.endswith("TAIL"))
        self.assertIn("characters trimmed", trimmed)


if __name__ == "__main__":
    unittest.main()
