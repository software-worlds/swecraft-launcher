"""End-to-end: the synthetic society exported, checked, and compared with the app's fixture bundle."""

import argparse
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import synthetic
from export_bundle import AVATARS, Export, call_kind, first_line, kimi_session_log, last_step_time, trajectory_steps
from redact import Redactor

FIXTURE = Path(__file__).resolve().parent / "fixtures"


class CallKindTest(unittest.TestCase):
    def test_kimi_tools_take_the_kinds_of_their_equivalents(self):
        self.assertEqual(
            [call_kind(tool, "other") for tool in ("ReadFile", "WriteFile", "StrReplaceFile", "FetchURL", "SearchWeb", "SetTodoList")],
            ["read", "write", "edit", "web", "web", "other"],
        )

    def test_the_analyzers_kind_otherwise(self):
        self.assertEqual([call_kind("Bash", "shell"), call_kind("Read", "read"), call_kind("x", "")], ["shell", "read", None])


class LastStepTimeTest(unittest.TestCase):
    def output(self, root: Path, trajectory: dict | None, logs: dict[str, str]) -> Path:
        output = root / "output"
        output.mkdir()
        if trajectory is not None:
            (output / "trajectory.json").write_text(json.dumps(trajectory))
        for name, text in logs.items():
            (output / "session" / name).parent.mkdir(parents=True, exist_ok=True)
            (output / "session" / name).write_text(text)
        return output

    def test_the_trajectorys_step_times(self):
        with tempfile.TemporaryDirectory() as scratch:
            steps = [{"timestamp": "2026-08-01T09:00:05.250Z"}, {"timestamp": "2026-08-01T09:04:30.900Z"}, {}]
            log = {"a/wire.jsonl": '{"timestamp": 1785600000}\n'}
            self.assertEqual(last_step_time(self.output(Path(scratch), {"steps": steps}, log)), "2026-08-01T09:04:30Z")

    def test_the_session_log_when_the_trajectory_has_no_times(self):
        with tempfile.TemporaryDirectory() as scratch:
            log = {
                "s/1/wire.jsonl": '{"type": "a", "timestamp": 1785574800.75}\n{"message": {"timestamp": 1785575100}}\nnot json\n',
                "s/1/context.jsonl": '{"timestamp": "2026-08-01T09:06:00Z"}\n{"timestamp": 1785575000123}\n',
            }
            # 1785575100 is 2026-08-01T09:05:00Z; the ISO time a minute later is the latest.
            self.assertEqual(last_step_time(self.output(Path(scratch), {"steps": [{"step_id": 1}]}, log)), "2026-08-01T09:06:00Z")

    def test_no_times_at_all(self):
        with tempfile.TemporaryDirectory() as scratch:
            self.assertIsNone(last_step_time(self.output(Path(scratch), None, {})))


def call(call_id: str, tool: str, arguments: object) -> dict:
    return {"tool_call_id": call_id, "function_name": tool, "arguments": arguments}


def results(*pairs: tuple[str, str]) -> dict:
    return {"results": [{"source_call_id": call_id, "content": content} for call_id, content in pairs]}


class TrajectoryStepsTest(unittest.TestCase):
    """A session's steps, read from its trajectory whole."""

    def steps(self, trajectory: dict) -> tuple[list[dict], Counter]:
        notes: Counter[str] = Counter()
        return trajectory_steps(trajectory, Redactor(), notes), notes

    def test_steps_without_a_tool_call(self):
        steps, _ = self.steps({"steps": [
            {"step_id": 1, "source": "system", "message": "You are a maintainer."},
            {"step_id": 2, "source": "user", "message": "Do the work."},
            {"step_id": 3, "source": "agent", "message": "", "tool_calls": [call("a", "Bash", {"command": "ls"})], "observation": results(("a", "x"))},
            {"step_id": 4, "source": "agent", "message": "Done."},
        ]})
        self.assertEqual(
            [(s["step"], s["role"], s["message"] and s["message"]["text"], len(s["calls"])) for s in steps],
            [(1, "system", "You are a maintainer.", 0), (2, "user", "Do the work.", 0), (3, "agent", None, 1), (4, "agent", "Done.", 0)],
        )

    def test_texts_longer_than_the_analyzer_keeps(self):
        output, command = "a" * 3000 + "b" * 50_000 + "c" * 1000, "echo " + "d" * 30_000
        steps, _ = self.steps({"steps": [{"step_id": 1, "source": "agent", "message": "",
                                          "tool_calls": [call("a", "Bash", {"command": command})], "observation": results(("a", output))}]})
        (bash,) = steps[0]["calls"]
        self.assertEqual(bash["output"], {"text": output, "redacted": 0, "truncated": False})
        self.assertEqual(bash["input"], {"text": command, "redacted": 0, "truncated": False})

    def test_a_secret_in_a_steps_text(self):
        token = "ghp_" + "Q7w" * 12
        steps, _ = self.steps({"steps": [{"step_id": 1, "source": "agent", "message": f"Pushed with {token}.", "reasoning_content": f"The token is {token}"}]})
        self.assertEqual(steps[0]["message"], {"text": "Pushed with [redacted].", "redacted": 1, "truncated": False})
        self.assertEqual(steps[0]["reasoning"], {"text": "The token is [redacted]", "redacted": 1, "truncated": False})

    def test_the_trajectorys_order_and_numbers(self):
        # A step keeps its id; one without an id takes its position in the list, as the analyzer numbers it.
        steps, notes = self.steps({"steps": [{"step_id": 1, "source": "user"}, {"source": "agent"}, "not a step", {"step_id": 7, "source": "agent"}]})
        self.assertEqual([s["step"] for s in steps], [1, 2, 7])
        self.assertEqual(notes, {"steps that are not objects": 1})
        # Numbers that do not increase are replaced by positions, so the steps stay in the trajectory's order.
        steps, notes = self.steps({"steps": [{"step_id": 3, "message": "c"}, {"step_id": 2, "message": "b"}, {"step_id": 2, "message": "d"}]})
        self.assertEqual([(s["step"], s["message"]["text"]) for s in steps], [(1, "c"), (2, "b"), (3, "d")])
        self.assertEqual(notes, {"trajectories numbered by position": 1, "steps without a known source, taken as the agent's": 3})

    def test_times_to_the_second(self):
        times = ["2026-08-01T10:00:09.250Z", "2026-08-01T12:00:00.999+02:00", None, "yesterday"]
        steps, _ = self.steps({"steps": [{"step_id": i, "source": "agent", "timestamp": t} for i, t in enumerate(times, start=1)]})
        self.assertEqual([s["at"] for s in steps], ["2026-08-01T10:00:09Z", "2026-08-01T10:00:00Z", None, None])

    def test_each_call_takes_its_own_result(self):
        # Codex records a call repeated under one id with a result for each time; a call may have no result.
        steps, notes = self.steps({"steps": [{
            "step_id": 1, "source": "agent", "message": "",
            "tool_calls": [call("c", "exec", {"input": "run()"}), call("d", "wait", {"cell_id": "1"}), call("c", "exec", {"input": "run()"})],
            "observation": results(("c", "first"), ("c", "second"), ("e", "orphan")),
        }]})
        self.assertEqual([c["output"] and c["output"]["text"] for c in steps[0]["calls"]], ["first", None, "second"])
        self.assertEqual(notes, {"results that name no call": 1})

    def test_a_calls_command_and_arguments(self):
        steps, _ = self.steps({"steps": [{"step_id": 1, "source": "agent", "message": "", "tool_calls": [
            call("a", "Shell", {"command": "make test", "timeout": 120, "edit": {"old": "a", "new": "b"}}),
            call("b", "Write", {"file_path": "x.py", "content": "print()\n"}),
            call("c", "StrReplaceFile", '{"path": "y.py", "edit": '),
            call("d", "TaskList", None),
        ]}]})
        shell, write, raw, empty = steps[0]["calls"]
        self.assertEqual((shell["kind"], shell["input"]["text"], shell["path"]), ("shell", "make test", None))
        self.assertEqual([(a["name"], a["value"]["text"]) for a in shell["arguments"]], [("timeout", "120"), ("edit", '{\n  "old": "a",\n  "new": "b"\n}')])
        self.assertEqual((write["kind"], write["input"], write["path"]), ("write", None, "x.py"))
        self.assertEqual([(a["name"], a["value"]["text"]) for a in write["arguments"]], [("file_path", "x.py"), ("content", "print()\n")])
        # Arguments that are not an object, as a harness that could not parse them records them.
        self.assertEqual((raw["kind"], raw["input"]["text"], raw["arguments"]), ("edit", '{"path": "y.py", "edit": ', []))
        self.assertEqual((empty["input"], empty["arguments"], empty["output"]), (None, [], None))

    def test_kimi_clis_message_without_text(self):
        step = {"step_id": 1, "source": "agent", "message": "(tool use)", "reasoning_content": "Check the issues."}
        steps, _ = self.steps({"agent": {"name": "kimi-cli"}, "steps": [step]})
        self.assertEqual((steps[0]["message"], steps[0]["reasoning"]["text"], steps[0]["at"]), (None, "Check the issues.", None))
        # The same words from another harness are the agent's.
        steps, _ = self.steps({"agent": {"name": "claude-code"}, "steps": [step]})
        self.assertEqual(steps[0]["message"]["text"], "(tool use)")

    def test_metrics_as_the_trajectory_records_them(self):
        token = "ghp_" + "Q7w" * 12
        metrics = {
            "prompt_tokens": 1200, "completion_tokens": 80, "cached_tokens": 1000, "cost_usd": 0.0125,
            "extra": {"cache_creation": {"ephemeral_5m_input_tokens": 200}, "iterations": [], "speed": None, "note": f"key {token}",
                      "rate": float("nan")},
        }
        steps, notes = self.steps({"steps": [{"step_id": 1, "source": "agent", "metrics": metrics}, {"step_id": 2, "source": "user"}]})
        self.assertEqual(
            steps[0]["metrics"],
            {"prompt_tokens": 1200, "completion_tokens": 80, "cached_tokens": 1000, "cost_usd": 0.0125,
             "extra": {"cache_creation": {"ephemeral_5m_input_tokens": 200}, "iterations": [], "speed": None, "note": "key [redacted]",
                       "rate": None}},
        )
        # A step the trajectory records no metrics for has none.
        self.assertIsNone(steps[1]["metrics"])
        self.assertEqual(notes, {"metric values that are not finite numbers": 1})


class KimiSessionLogTest(unittest.TestCase):
    """kimi-cli's session log: the prompt, and the time each step began."""

    def log(self, root: Path, name: str, records: list[tuple[object, str, dict | None]]) -> None:
        (root / "session" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "session" / name).write_text(synthetic.wire(*records) + "not json\n")

    def test_the_sessions_own_log_and_its_times(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            # A subagent's log begins after the session's own, and is not read.
            self.log(root, "s/b/wire.jsonl", [(1785585700, "TurnBegin", {"user_input": "a subagent's task"}), (1785585701, "StepBegin", {"n": 1})])
            self.log(root, "s/a/wire.jsonl", [
                (1785585600.25, "TurnBegin", {"user_input": "Do the work."}),
                (1785585605.75, "StepBegin", {"n": 1}),
                (1785585650, "SubagentEvent", {"event": {"type": "StepBegin", "payload": {"n": 1}}}),
                (1785585900123, "StepBegin", {"n": 2}),
                ("2026-08-01T12:10:00.500Z", "StepBegin", {"n": 3}),
                (None, "StepBegin", {"n": 4}),
            ])
            notes: Counter[str] = Counter()
            prompt, times = kimi_session_log(root, notes)
        self.assertEqual(prompt, "Do the work.")
        # Epoch seconds and milliseconds and ISO times, each to the second; a step without a time has none.
        self.assertEqual(times, ["2026-08-01T12:00:05Z", "2026-08-01T12:05:00Z", "2026-08-01T12:10:00Z", None])

    def test_no_log(self):
        with tempfile.TemporaryDirectory() as scratch:
            self.assertEqual(kimi_session_log(Path(scratch), Counter()), (None, []))


class SummaryTest(unittest.TestCase):
    def test_the_first_paragraph_of_prose(self):
        self.assertEqual(first_line("## What\n\nSpeeds up the parser.\n\n## Why\nBecause."), "Speeds up the parser.")
        self.assertEqual(first_line("<!-- Thank you! -->\n<!--\nlong\n-->\nFixes #3."), "Fixes #3.")
        self.assertEqual(first_line("No CI existed here yet. This adds one\nminimal workflow.\n\nMore."), "No CI existed here yet. This adds one minimal workflow.")
        self.assertEqual(first_line("- first item\n- second item"), "first item")
        self.assertEqual(first_line("```\ncode\n```"), "code")
        self.assertIsNone(first_line("## Only a heading\n---\n"))
        self.assertIsNone(first_line(None))

    def test_cut_at_a_word(self):
        summary = first_line("word " * 60, limit=40)
        self.assertLessEqual(len(summary), 40)
        self.assertTrue(summary.endswith("word…"))


def files(folder: Path) -> dict[str, object]:
    found: dict[str, object] = {str(path.relative_to(folder)): json.loads(path.read_text()) for path in sorted(folder.rglob("*.json"))}
    found.update({str(path.relative_to(folder)): path.read_bytes() for path in sorted(folder.rglob("*.bin"))})
    return found


class ExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        root = Path(cls.scratch.name)
        cls.out = root / "bundles"
        cls.report = synthetic.export(root / "inputs", cls.out)
        cls.bundle = cls.out / "tiny"
        cls.core = json.loads((cls.bundle / "core.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_one_file_per_repository_and_per_session(self):
        self.assertEqual(sorted(p.name for p in (self.bundle / "threads").iterdir()), ["alpha.json", "beta.json", "gamma.json"])
        self.assertEqual(sorted(p.name for p in (self.bundle / "code").iterdir()), ["alpha.json", "beta.json", "gamma.json"])
        sessions = sorted(p.stem for p in (self.bundle / "sessions").iterdir())
        self.assertEqual(sessions, sorted(s["id"] for s in self.core["sessions"]))

    def test_written_compact(self):
        self.assertNotIn("\n", (self.bundle / "core.json").read_text())

    def test_no_fake_credential_reaches_any_file(self):
        for path in self.out.rglob("*.json"):
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertNotIn(synthetic.FAKE_GITHUB_TOKEN, text)
                self.assertNotIn(synthetic.FAKE_FORGE_TOKEN, text)
        for path in self.out.rglob("*.bin"):
            with self.subTest(file=path.name):
                self.assertNotIn(synthetic.FAKE_GITHUB_TOKEN.encode(), path.read_bytes())
                self.assertNotIn(synthetic.FAKE_FORGE_TOKEN.encode(), path.read_bytes())
        # The token in alpha's code is in its diff, in its text at head and in the blob of alpha/settings.py,
        # in the diff of the commit that added it, and in the message of the release branch's merge commit;
        # the issue and alpha's closing message hold it too, and a tool's output holds the forge token.
        self.assertEqual(self.report["redacted"], {"github": 8, "url-password": 1})

    def test_only_listed_fields(self):
        self.assertEqual(
            list(self.core),
            ["schema", "id", "label", "source", "run", "world", "agents", "sessions", "events", "repos", "threads", "releases"],
        )
        session = json.loads((self.bundle / "sessions" / "20260801T100000Z-aaaaaaa1.json").read_text())
        self.assertEqual(sorted(session["receipt"]), sorted(["image", "requestedRef", "resolvedSha", "inputTokens", "outputTokens", "cacheTokens"]))
        self.assertNotIn("repository", json.dumps(session["receipt"]))

    def test_bootstrap_sessions(self):
        bootstrap = [s for s in self.core["sessions"] if s["bootstrap"]]
        self.assertEqual([s["agent"] for s in bootstrap], ["alpha-bot", "beta-bot", "gamma-bot"])
        first = bootstrap[0]
        # The start from the run id, the end from the transcript's last step.
        self.assertEqual((first["started"], first["finished"], first["outcome"]), ("2026-08-01T09:00:00Z", "2026-08-01T09:04:30Z", None))
        self.assertEqual(self.core["run"]["started"], "2026-08-01T09:00:00Z")

    def session(self, run_id: str) -> dict:
        return json.loads((self.bundle / "sessions" / f"{run_id}.json").read_text())

    def test_steps_from_the_trajectory(self):
        detail = self.session(synthetic.ALPHA)
        self.assertEqual(detail["transcript"], "trajectory")
        # Every step in the trajectory's order, the prompt and the closing message without a tool call included.
        self.assertEqual(
            [(s["step"], s["role"], len(s["calls"])) for s in detail["steps"]],
            [(1, "user", 0), (2, "agent", 2), (3, "agent", 1), (4, "agent", 1), (5, "agent", 0)],
        )
        self.assertEqual(detail["steps"][0]["message"]["text"], synthetic.ALPHA_PROMPT)
        # Each step's metrics as the trajectory records them; the prompt has none.
        self.assertEqual([s["metrics"] for s in detail["steps"][:2]], [None, synthetic.CLAUDE_METRICS])
        # The prompt is the first step, so it is not repeated beside the steps.
        self.assertIsNone(detail["prompt"])
        self.assertEqual(
            [s["at"] for s in detail["steps"]],
            ["2026-08-01T10:00:01Z", "2026-08-01T10:00:09Z", "2026-08-01T10:01:00Z", "2026-08-01T10:08:12Z", "2026-08-01T10:29:30Z"],
        )
        # The output the evidence keeps 4,000 characters of, whole.
        read = detail["steps"][2]["calls"][0]
        self.assertEqual(read["output"], {"text": synthetic.LONG_OUTPUT, "redacted": 0, "truncated": False})
        # A secret in a step's message.
        self.assertEqual(
            detail["steps"][4]["message"],
            {"text": "Released 1.0.0. The CI token [redacted] was in the log; I removed it.", "redacted": 1, "truncated": False},
        )
        # What a write wrote is one of its arguments; a shell call's command is its input.
        write = detail["steps"][3]["calls"][0]
        self.assertEqual([(a["name"], a["value"]["text"]) for a in write["arguments"]], [("file_path", "NOTES.md"), ("content", synthetic.NOTES)])
        self.assertEqual(detail["steps"][1]["calls"][0]["input"]["text"], synthetic.FORGE_CALL)
        # The list in core.json counts the calls the file holds.
        summary = next(s for s in self.core["sessions"] if s["id"] == synthetic.ALPHA)
        self.assertEqual((summary["toolCalls"], detail["toolCalls"]), (4, 4))

    def test_kimi_cli_steps(self):
        detail = self.session(synthetic.ALPHA_AGAIN)
        steps = detail["steps"]
        # Its trajectory records no times and no prompt step; its session log has both.
        self.assertEqual([(s["step"], s["at"]) for s in steps], [(1, "2026-08-01T12:00:05Z"), (2, "2026-08-01T12:19:40Z")])
        self.assertEqual(detail["prompt"]["text"], synthetic.PROMPTS[synthetic.ALPHA_AGAIN])
        self.assertEqual([s["metrics"] for s in steps], [synthetic.KIMI_METRICS, synthetic.KIMI_METRICS])
        self.assertEqual([s["message"] and s["message"]["text"] for s in steps], [None, "Nothing new to do."])
        self.assertEqual([s["reasoning"]["text"] for s in steps], ["Anything new since the release?", "No open issue or pull request."])
        self.assertEqual(steps[0]["model"], "kimi-test")

    def test_steps_from_the_evidence(self):
        detail = self.session(synthetic.BETA)
        self.assertEqual(detail["transcript"], "evidence")
        self.assertEqual([(s["step"], s["role"], [c["tool"] for c in s["calls"]]) for s in detail["steps"]], [(2, "agent", ["Edit"])])

    def test_a_session_without_a_transcript(self):
        detail = self.session(synthetic.GAMMA)
        self.assertEqual((detail["transcript"], detail["steps"], detail["toolCalls"]), (None, [], 0))
        # What the checkpoint holds for it: the prompt, the error and the receipt.
        self.assertEqual(detail["prompt"]["text"], synthetic.PROMPTS[synthetic.GAMMA])
        self.assertEqual(detail["error"], "Harbor did not complete exactly one trial")
        self.assertEqual(detail["receipt"]["image"], "hub-test-agent:1")

    def test_the_report_counts_the_transcripts(self):
        self.assertEqual(
            self.report["transcripts"],
            {"sessions from a trajectory": 5, "sessions from the evidence": 1, "sessions without a transcript": 1, "steps": 14,
             "steps timed from the session log": 2, "prompts from the session log": 1, "prompts from input/prompt.md": 1},
        )
        self.assertEqual(self.report["kimi-cli sessions without step times"], [])

    def test_agents_from_the_naming_rule(self):
        self.assertEqual(self.report["agents from"], "the <repo>-bot naming rule")
        agents = {a["name"]: a for a in self.core["agents"]}
        self.assertEqual(agents["alpha-bot"]["writeRepos"], ["alpha"])
        self.assertEqual(agents["alpha-bot"]["avatar"], "owl")
        self.assertEqual((agents["alpha-bot"]["sessions"], agents["alpha-bot"]["worked"]), (3, 1))

    def test_threads(self):
        alpha = json.loads((self.bundle / "threads" / "alpha.json").read_text())
        pull, issue = alpha["threads"]
        self.assertEqual([e["type"] for e in pull["timeline"]], ["push", "review", "event", "event"])
        self.assertEqual(pull["timeline"][0]["commits"][0]["subject"], "perf: cache the grammar")
        self.assertEqual(pull["timeline"][1]["verdict"], "pending")
        self.assertEqual(issue["timeline"][2]["what"], "referenced from pull request beta#1")
        beta = json.loads((self.bundle / "threads" / "beta.json").read_text())["threads"][0]
        self.assertEqual((beta["state"], beta["merged"], beta["mergedAt"], beta["merger"]), ("closed", False, None, None))
        self.assertEqual([r["tag"] for r in alpha["releases"]], ["v1.0.0"])

    def test_code(self):
        alpha = json.loads((self.bundle / "code" / "alpha.json").read_text())
        repo = next(r for r in self.core["repos"] if r["name"] == "alpha")
        self.assertEqual((alpha["repo"], alpha["seed"], alpha["head"]), ("alpha", repo["seed"], repo["head"]))
        self.assertEqual(
            {f["path"]: f["status"] for f in alpha["files"]},
            {"alpha/compat.py": "renamed", "alpha/parser.py": "modified", "alpha/scratch.py": "deleted",
             "alpha/settings.py": "added", "logo.png": "modified"},
        )
        parser = next(f for f in alpha["files"] if f["path"] == "alpha/parser.py")
        self.assertEqual((parser["additions"], parser["deletions"]), (4, 1))
        self.assertEqual([(e["name"], e["kind"]) for e in alpha["tree"]], [("alpha", "dir"), ("README.md", "file"), ("logo.png", "file")])
        # beta and gamma have no repository on the synthetic forge.
        beta = json.loads((self.bundle / "code" / "beta.json").read_text())
        self.assertEqual(beta, {"schema": "sim-browser/bundle-v1", "repo": "beta", "seed": None, "head": None, "tree": [], "files": []})
        self.assertEqual(self.report["code"]["changed files"], 5)
        self.assertEqual(self.report["code"]["without both commits"], 2)

    def test_counts(self):
        repos = {r["name"]: r for r in self.core["repos"]}
        self.assertEqual(repos["alpha"]["issues"], {"open": 0, "closed": 1})
        self.assertEqual(repos["alpha"]["pulls"], {"open": 0, "merged": 1, "closed": 0})
        self.assertEqual(repos["beta"]["pulls"], {"open": 0, "merged": 0, "closed": 1})
        self.assertEqual(repos["alpha"]["releases"], 1)
        self.assertEqual(repos["alpha"]["description"], "A parser")
        self.assertIsNone(repos["beta"]["description"])

    def test_index(self):
        index = json.loads((self.out / "index.json").read_text())
        self.assertEqual(index["schema"], "sim-browser/index-v1")
        self.assertEqual([r["id"] for r in index["runs"]], ["tiny"])
        # As Table 7 counts them: the three completed scheduled sessions, 30 + 36 + 20 minutes.
        self.assertEqual((index["runs"][0]["sessions"], index["runs"][0]["hours"]), (3, 1.43))
        self.assertEqual(index["groups"][0]["id"], "world-t")

    def test_events_from_the_forge(self):
        self.assertEqual(self.core["source"]["events"], "forge")
        self.assertEqual(self.report["linked to a session"], 9)
        self.assertEqual(self.report["unlinked"], {"issue.comment": 1})

    def test_the_app_fixture_is_current(self):
        # The example bundle in fixtures/; sim-browser's tests parse the same bundle. Regenerate it with
        #   python3 services/export/synthetic.py services/export/fixtures
        # Other fixtures may sit beside it (a factsheet card), so only the exporter's files are compared.
        synthetic.settle(self.out)
        self.assertEqual(files(self.out / "tiny"), files(FIXTURE / "tiny"), "the fixture bundle is out of date")
        self.assertEqual(json.loads((self.out / "index.json").read_text()), json.loads((FIXTURE / "index.json").read_text()))


class ReexportTest(unittest.TestCase):
    """Exporting a run again replaces the exporter's files and keeps what other tools added."""

    def test_other_files_stay(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            out = root / "bundles"
            synthetic.export(root / "first", out)
            run = out / "tiny"
            (run / "evaluation.json").write_text('{"schema": "sim-browser/evaluation-v1"}')
            (run / "audit" / "card").mkdir(parents=True)
            (run / "audit" / "card" / "factsheet.json").write_text("{}")
            (run / "sessions" / "20260101T000000Z-00000000.json").write_text("{}")
            synthetic.export(root / "second", out)
            self.assertEqual((run / "evaluation.json").read_text(), '{"schema": "sim-browser/evaluation-v1"}')
            self.assertTrue((run / "audit" / "card" / "factsheet.json").is_file())
            # A session file the new export does not write is gone with the old sessions.
            self.assertFalse((run / "sessions" / "20260101T000000Z-00000000.json").exists())
            self.assertEqual(len(list((run / "sessions").iterdir())), 7)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["index.json", "tiny"])

    def test_the_index_keeps_its_order(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            out = root / "bundles"
            synthetic.export(root / "first", out)
            index = json.loads((out / "index.json").read_text())
            index["runs"].append({**index["runs"][0], "id": "later", "bundle": None, "setting": "kept"})
            (out / "index.json").write_text(json.dumps(index))
            synthetic.export(root / "second", out)
            runs = json.loads((out / "index.json").read_text())["runs"]
            self.assertEqual([(r["id"], r["setting"]) for r in runs], [("tiny", None), ("later", "kept")])


class RecordShapesTest(unittest.TestCase):
    """Records the Codex campaign has: a failed bootstrap attempt, a review submitted twice, an input at the analyzer's cut."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        root = Path(cls.scratch.name)
        paths = synthetic.build(root / "inputs")
        failed = "20260801T085500Z-0000000d"
        db = sqlite3.connect(paths["evidence"] / "evidence.db")
        db.execute(
            "INSERT INTO wakes (wake_id, run_id, agent, status, outcome, harness, model, transcript, tool_calls)"
            " VALUES (NULL, ?, NULL, 'failed', 'unledgered', 'codex', 'gpt-test', 'none', 0)",
            (failed,),
        )
        db.execute(
            "INSERT INTO comments (id, repo, number, is_pull, type, kind, author, body, created_at, review_id)"
            " VALUES (11, 'hub/alpha', 1, 1, 22, 'review', 'alpha-bot', 'First pass: looks right.', '2026-08-01T10:17:50Z', 1)"
        )
        # beta's session has no trajectory, so its steps are the evidence's.
        db.executemany(
            "INSERT INTO tool_calls (wake_id, run_id, agent, step, call, tool, kind, text, path, output)"
            " VALUES (2, '20260801T100500Z-bbbbbbb1', 'beta-bot', ?, ?, ?, 'shell', ?, '', ?)",
            [(3, 1, "Bash", "cat build.log", synthetic.TRIMMED_OUTPUT), (4, 2, "exec", "x" * 20_000, "")],
        )
        db.commit()
        db.close()
        output = paths["checkpoint"] / "runs" / failed / "output"
        output.mkdir(parents=True)
        receipt = {"status": "failed", "agent": "codex", "repository": "http://forge.invalid:3000/hub/gamma.git",
                   "error": "could not execute Harbor: timed out after 300 seconds"}
        (output / "result.json").write_text(json.dumps(receipt))
        args = argparse.Namespace(
            evidence=str(paths["evidence"]), checkpoint=str(paths["checkpoint"]), world=str(paths["world"]), id="shapes",
            label="Shapes", out=str(root / "bundles"), group=None, setting=None, avatars=str(root),
        )
        Export(args).run()
        cls.bundle = root / "bundles" / "shapes"
        cls.core = json.loads((cls.bundle / "core.json").read_text())
        cls.failed = failed

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def test_a_failed_bootstrap_attempt(self):
        session = next(s for s in self.core["sessions"] if s["id"] == self.failed)
        self.assertEqual(
            {k: session[k] for k in ("agent", "status", "bootstrap", "started", "finished", "toolCalls", "costUsd")},
            {"agent": "gamma-bot", "status": "failed", "bootstrap": True, "started": "2026-08-01T08:55:00Z",
             "finished": None, "toolCalls": 0, "costUsd": None},
        )
        detail = json.loads((self.bundle / "sessions" / f"{self.failed}.json").read_text())
        self.assertEqual((detail["error"], detail["steps"], detail["response"]), ("could not execute Harbor: timed out after 300 seconds", [], None))
        self.assertEqual(self.core["run"]["started"], "2026-08-01T08:55:00Z")
        index = json.loads((self.bundle.parent / "index.json").read_text())
        self.assertEqual(index["runs"][0]["sessions"], 3)

    def test_a_review_submitted_twice(self):
        alpha = json.loads((self.bundle / "threads" / "alpha.json").read_text())
        pull = alpha["threads"][0]
        self.assertEqual([e["type"] for e in pull["timeline"]], ["push", "comment", "review", "event", "event"])
        self.assertEqual(pull["timeline"][1]["body"]["text"], "First pass: looks right.")
        self.assertEqual(pull["comments"], 1)

    def test_an_input_at_the_analyzers_cut(self):
        detail = json.loads((self.bundle / "sessions" / f"{synthetic.BETA}.json").read_text())
        self.assertEqual(detail["transcript"], "evidence")
        call = detail["steps"][-1]["calls"][0]
        self.assertEqual((len(call["input"]["text"]), call["input"]["truncated"]), (20_000, True))
        self.assertFalse(detail["steps"][0]["calls"][0]["input"]["truncated"])

    def test_an_output_the_analyzer_trimmed(self):
        detail = json.loads((self.bundle / "sessions" / f"{synthetic.BETA}.json").read_text())
        output = detail["steps"][1]["calls"][0]["output"]
        self.assertEqual(output, {"text": synthetic.TRIMMED_OUTPUT, "redacted": 0, "truncated": True})


class KimiStepCountTest(unittest.TestCase):
    """A kimi-cli session whose log has another number of steps than its trajectory: its steps have no times."""

    def test_no_times_and_the_session_listed(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            paths = synthetic.build(root / "inputs")
            own = next(name for name in synthetic.SESSION_LOGS[synthetic.ALPHA_AGAIN] if "alpha" in name)
            log = paths["checkpoint"] / "runs" / synthetic.ALPHA_AGAIN / "output" / "session" / own
            log.write_text(log.read_text() + synthetic.wire((1785586800.0, "StepBegin", {"n": 3})))
            args = argparse.Namespace(
                evidence=str(paths["evidence"]), checkpoint=str(paths["checkpoint"]), world=str(paths["world"]), id="kimi",
                label="Kimi", out=str(root / "bundles"), group=None, setting=None, avatars=str(root), known_secrets=None,
            )
            report = Export(args).run()
            detail = json.loads((root / "bundles" / "kimi" / "sessions" / f"{synthetic.ALPHA_AGAIN}.json").read_text())
        self.assertEqual([s["at"] for s in detail["steps"]], [None, None])
        self.assertEqual(report["kimi-cli sessions without step times"], [synthetic.ALPHA_AGAIN])
        # The prompt does not depend on the steps.
        self.assertEqual(detail["prompt"]["text"], synthetic.PROMPTS[synthetic.ALPHA_AGAIN])


class KnownSecretsExportTest(unittest.TestCase):
    """`--known-secrets`: a value with no context around it, replaced everywhere it occurs."""

    VALUE = "7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b"

    def build(self, root: Path) -> tuple[dict, argparse.Namespace]:
        paths = synthetic.build(root / "inputs")
        db = sqlite3.connect(paths["evidence"] / "evidence.db")
        db.execute(
            "INSERT INTO tool_calls (wake_id, run_id, agent, step, call, tool, kind, text, path, output)"
            " VALUES (2, '20260801T100500Z-bbbbbbb1', 'beta-bot', 3, 1, 'Bash', 'shell', 'printenv HUB_FORGE_TOKEN', '', ?)",
            (f"{self.VALUE}\n",),
        )
        db.commit()
        db.close()
        args = argparse.Namespace(
            evidence=str(paths["evidence"]), checkpoint=str(paths["checkpoint"]), world=str(paths["world"]), id="known",
            label="Known", out=str(root / "bundles"), group=None, setting=None, avatars=str(root), known_secrets=None,
        )
        return paths, args

    def test_a_bare_value_reaches_no_file(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            paths, args = self.build(root)
            (root / "known-secrets").write_text(self.VALUE + "\n")
            args.known_secrets = str(root / "known-secrets")
            report = Export(args).run()
            for path in (root / "bundles").rglob("*.json"):
                with self.subTest(file=path.name):
                    self.assertNotIn(self.VALUE, path.read_text())
            self.assertEqual(report["redacted"]["known"], 1)
            self.assertNotIn(self.VALUE, json.dumps(report))

    def test_a_file_inside_the_checkpoint_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            paths, args = self.build(root)
            inside = paths["checkpoint"] / "state" / "coordinator" / "values"
            inside.write_text(self.VALUE + "\n")
            args.known_secrets = str(inside)
            with self.assertRaises(SystemExit):
                Export(args)
            # A link from outside into the checkpoint is refused too.
            link = root / "link"
            link.symlink_to(inside)
            args.known_secrets = str(link)
            with self.assertRaises(SystemExit):
                Export(args)


def unknown_sessions(core: dict) -> list[str]:
    """Where the core names a session it does not list, as parseCore checks it."""

    listed = {s["id"] for s in core["sessions"]}
    return [
        f"{part}[{i}]: {item['session']}"
        for part in ("events", "threads", "releases")
        for i, item in enumerate(core[part])
        if item["session"] is not None and item["session"] not in listed
    ]


class SkippedRunTest(unittest.TestCase):
    """A run the bundle leaves out, named by the pull request it opened."""

    def test_the_core_names_only_listed_sessions(self):
        unbound, no_start = "20260801T101000Z-dddddddd", "manual-run-1"
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            paths = synthetic.build(root / "inputs")
            db = sqlite3.connect(paths["evidence"] / "evidence.db")
            db.execute(
                "INSERT INTO wakes (wake_id, run_id, agent, started, finished, status, outcome, tool_calls)"
                " VALUES (5, ?, 'delta-bot', '2026-08-01T10:10:00+00:00', '2026-08-01T10:40:00+00:00', 'completed', 'worked', 0)",
                (unbound,),
            )
            db.execute(
                "INSERT INTO wakes (wake_id, run_id, agent, status, outcome, tool_calls) VALUES (6, ?, 'alpha-bot', 'completed', 'worked', 0)",
                (no_start,),
            )
            db.execute("UPDATE pulls SET opened_run_id = ? WHERE repo = 'hub/beta' AND number = 1", (unbound,))
            db.execute("UPDATE pulls SET opened_run_id = ? WHERE repo = 'hub/alpha' AND number = 1", (no_start,))
            db.commit()
            db.close()
            args = argparse.Namespace(
                evidence=str(paths["evidence"]), checkpoint=str(paths["checkpoint"]), world=str(paths["world"]), id="skipped",
                label="Skipped", out=str(root / "bundles"), group=None, setting=None, avatars=str(root), known_secrets=None,
            )
            report = Export(args).run()
            core = json.loads((root / "bundles" / "skipped" / "core.json").read_text())
        self.assertEqual(report["left out"]["sessions of an agent the bindings do not name"], 1)
        self.assertEqual(report["left out"]["sessions without a start time"], 1)
        self.assertEqual(unknown_sessions(core), [])
        threads = {(t["repo"], t["number"]): t for t in core["threads"]}
        self.assertIsNone(threads[("beta", 1)]["session"])
        self.assertIsNone(threads[("alpha", 1)]["session"])


class LedgerExportTest(unittest.TestCase):
    """A checkpoint whose runner kept a ledger: its rows are the events."""

    def test_rows_become_events(self):
        rows = [
            ("20260801T100500Z-bbbbbbb1", "beta-bot", "2026-08-01T10:15:00+00:00", "hub/alpha", "issue.create", 2, '{"number": 2}'),
            ("20260801T100000Z-aaaaaaa1", "alpha-bot", "2026-08-01T10:25:00+00:00", "hub/alpha", "issue.comment", 2,
             '{"issue": 2, "comment_id": 5}'),
            ("20260801T100000Z-aaaaaaa1", "operator", "2026-08-01T10:26:00+00:00", "hub/alpha", "issue.comment", 2, "{}"),
            ("20260801T000000Z-00000000", "alpha-bot", "2026-08-01T10:27:00+00:00", "hub/alpha", "thread.react", 2, "{}"),
            ("20260801T100000Z-aaaaaaa1", "alpha-bot", "2026-08-01T10:28:00+00:00", "elsewhere/x", "issue.comment", 1, "{}"),
        ]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            paths = synthetic.build(root / "inputs")
            db = sqlite3.connect(paths["evidence"] / "evidence.db")
            db.executemany("INSERT INTO forge_actions (run_id, agent, ts, repo, action, number, metadata) VALUES (?,?,?,?,?,?,?)", rows)
            db.commit()
            db.close()
            args = argparse.Namespace(
                evidence=str(paths["evidence"]), checkpoint=str(paths["checkpoint"]), world=str(paths["world"]), id="ledger",
                label="Ledger", out=str(root / "bundles"), group=None, setting=None, avatars=str(root),
            )
            report = Export(args).run()
            core = json.loads((root / "bundles" / "ledger" / "core.json").read_text())
            session = json.loads((root / "bundles" / "ledger" / "sessions" / "20260801T100000Z-aaaaaaa1.json").read_text())
        self.assertEqual(core["source"]["events"], "ledger")
        self.assertEqual([e["action"] for e in core["events"]], ["issue.create", "issue.comment", "thread.react"])
        # A session the bundle does not list is not linked.
        self.assertIsNone(core["events"][2]["session"])
        self.assertEqual(core["events"][0]["summary"], "Hi @alpha-bot — I maintain `beta`.")
        self.assertEqual(session["actions"][0]["text"]["text"], "About 40 ms per file.\n\nMeasured on 3.13.")
        self.assertEqual(report["left out"], {"issue.comment by a user who is not an agent": 1, "forge_actions rows outside the world": 1})
        # Without portraits to pick from, an agent the world fixture names keeps its own; the others have none.
        self.assertEqual([a["avatar"] for a in core["agents"]], ["owl", None, None])


class BindingsFileTest(unittest.TestCase):
    """A run from before the coordinator kept agent_repos, with one agent for every repository."""

    def export(self, root, paths, bindings):
        args = argparse.Namespace(
            evidence=str(paths["evidence"]), checkpoint=str(paths["checkpoint"]), world=str(paths["world"]), id="one",
            label="One agent", out=str(root / "bundles"), group=None, setting=None, avatars=str(root), known_secrets=None, bindings=bindings,
        )
        report = Export(args).run()
        return report, json.loads((root / "bundles" / "one" / "core.json").read_text())

    def test_the_file_binds_an_agent_the_naming_rule_cannot(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            paths = synthetic.build(root / "inputs")
            db = sqlite3.connect(paths["evidence"] / "evidence.db")
            # One agent was onboarded, on its own repository alpha, and booked every wake after.
            db.execute("UPDATE wakes SET agent = 'society-bot' WHERE agent IS NOT NULL")
            db.execute("DELETE FROM wakes WHERE agent IS NULL AND run_id != '20260801T090000Z-0000000a'")
            db.commit()
            db.close()
            unbound, _ = self.export(root, paths, None)
            (root / "bindings.json").write_text(json.dumps({"society-bot": ["alpha", "hub/beta", "gamma"]}))
            report, core = self.export(root, paths, str(root / "bindings.json"))
        # Without the file the naming rule binds alpha-bot, beta-bot and gamma-bot, and every booked session is left out.
        self.assertEqual(unbound["agents from"], "the <repo>-bot naming rule")
        self.assertEqual(unbound["left out"]["sessions of an agent the bindings do not name"], 4)
        self.assertEqual(report["agents from"], "the bindings file")
        self.assertNotIn("sessions of an agent the bindings do not name", report["left out"])
        self.assertEqual(report["sessions"], 5)
        self.assertEqual([(a["name"], a["repo"]) for a in core["agents"]], [("society-bot", "alpha")])


class PortraitsTest(unittest.TestCase):
    def test_the_browsers_portraits_ship_beside_the_exporter(self):
        # The default for --avatars: without portraits to pick from, every agent the world
        # fixture names none for would be drawn with its initials.
        names = sorted(path.stem for path in AVATARS.glob("*.png"))
        self.assertIn("owl", names)
        self.assertGreater(len(names), 1)


if __name__ == "__main__":
    unittest.main()
