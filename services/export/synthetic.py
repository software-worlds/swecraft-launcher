#!/usr/bin/env python3
"""
A tiny synthetic society, built from nothing, for the exporter's tests and
for the bundle fixture the app's tests parse.

Three repositories (alpha, beta, gamma) and a held-out one (omega), one agent
each, a bootstrap session per agent and four scheduled sessions. The forge
holds every kind of record the exporter reads: a merged pull request with a
push, a review and a commit reference; an issue one maintainer opened on
another's repository, commented on and closed; a pull request closed
unmerged a few seconds after its session ended; a release and an imported
upstream tag; a comment made outside any session. alpha's repository holds
its seed commit and a head commit with a change of every kind: a file
modified, added, deleted and renamed, and a binary one.

The transcripts take each path the exporter has. alpha's first scheduled
session has a whole Claude Code trajectory: the prompt, steps with tool calls
and metrics, an output longer than the analyzer keeps, and a closing message
without a tool call. alpha's second is kimi-cli's, with reasoning and no times
or prompt step; its session log holds the prompt and the steps' times, beside
a subagent's log, which begins later. beta's session has only the analyzer's
tool calls, and gamma's failed before its harness started and keeps only its
prompt.

Fake credentials, to be redacted, sit in alpha's code, an issue, a commit
message, a tool's output and a step's message. Nothing here comes from a real
run.

    python3 services/export/synthetic.py services/export/fixtures

writes the fixture bundle `tiny` and its `index.json` under the folder given.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ORG = "hub"
FAKE_GITHUB_TOKEN = "ghp_" + "Zx8" * 12
FAKE_FORGE_TOKEN = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"

# The analyzer's tables (swecraft_analyzer/store.py) that the exporter reads.
EVIDENCE_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE agents (agent TEXT NOT NULL, repo TEXT NOT NULL, PRIMARY KEY (agent, repo));
CREATE TABLE repos (repo TEXT PRIMARY KEY, default_branch TEXT, seed TEXT, head TEXT, is_world INTEGER);
CREATE TABLE wakes (wake_id INTEGER, run_id TEXT, agent TEXT, day TEXT, started TEXT, finished TEXT, status TEXT,
    outcome TEXT, summary TEXT, cost_usd REAL, error TEXT, harness TEXT, model TEXT, transcript TEXT,
    tool_calls INTEGER, response TEXT);
CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, wake_id INTEGER, run_id TEXT, agent TEXT, step INTEGER, call INTEGER,
    tool TEXT, kind TEXT, text TEXT, path TEXT, output TEXT);
CREATE TABLE forge_actions (id INTEGER PRIMARY KEY, wake_id INTEGER, run_id TEXT, agent TEXT, ts TEXT, repo TEXT,
    action TEXT, number INTEGER, metadata TEXT, cmd TEXT);
CREATE TABLE pulls (repo TEXT, number INTEGER, title TEXT, body TEXT, author TEXT, state TEXT, merged INTEGER,
    merger TEXT, merged_at TEXT, created_at TEXT, closed_at TEXT, base_branch TEXT, head_branch TEXT, merge_base TEXT,
    head_commit TEXT, merged_commit TEXT, additions INTEGER, deletions INTEGER, files INTEGER, diff_error TEXT,
    opened_wake_id INTEGER, opened_run_id TEXT, PRIMARY KEY (repo, number));
CREATE TABLE pull_files (repo TEXT, number INTEGER, path TEXT, additions INTEGER, deletions INTEGER, category TEXT);
CREATE TABLE pull_patches (repo TEXT, number INTEGER, patch TEXT, truncated INTEGER, PRIMARY KEY (repo, number));
CREATE TABLE issues (repo TEXT, number INTEGER, title TEXT, body TEXT, author TEXT, state TEXT, created_at TEXT,
    closed_at TEXT, PRIMARY KEY (repo, number));
CREATE TABLE comments (id INTEGER PRIMARY KEY, repo TEXT, number INTEGER, is_pull INTEGER, type INTEGER, kind TEXT,
    author TEXT, body TEXT, created_at TEXT, updated_at TEXT, review_id INTEGER, force_push INTEGER);
CREATE TABLE reviews (id INTEGER PRIMARY KEY, repo TEXT, number INTEGER, reviewer TEXT, pr_author TEXT, kind TEXT,
    body TEXT, created_at TEXT);
CREATE TABLE releases (repo TEXT, tag TEXT, title TEXT, author TEXT, sha TEXT, created_at TEXT, is_draft INTEGER,
    is_tag INTEGER);
"""

# The Forgejo columns the exporter reads, and no others.
FORGE_SCHEMA = """
CREATE TABLE repository (id INTEGER PRIMARY KEY, owner_name TEXT, lower_name TEXT, description TEXT, default_branch TEXT);
CREATE TABLE issue (id INTEGER PRIMARY KEY, repo_id INTEGER, "index" INTEGER, is_pull INTEGER);
CREATE TABLE comment (id INTEGER PRIMARY KEY, type INTEGER, ref_issue_id INTEGER, old_title TEXT, new_title TEXT,
    old_ref TEXT, new_ref TEXT);
CREATE TABLE release (id INTEGER PRIMARY KEY, repo_id INTEGER, tag_name TEXT, note TEXT);
"""

WORLD = {
    "schema": "sim-browser/simulation-v0",
    "id": "tiny-world",
    "label": "Tiny world",
    "world": {
        "id": "world-t",
        "label": "World-T · three repositories",
        "org": ORG,
        "repos": [
            {"name": "alpha", "upstream": "example/alpha", "hex": [0, 0], "languages": {"Python": 1200}, "landmark": "bridge"},
            {"name": "beta", "upstream": "example/beta", "hex": [1, 0], "languages": {"Python": 800, "C": 200}},
            {"name": "gamma", "upstream": None, "hex": [0, 1], "languages": {"Rust": 500}},
            {"name": "omega", "upstream": "example/omega", "hex": [2, 0], "languages": {"Python": 3000}, "held_out": True},
        ],
        "deps": [["beta", "alpha"]],
        "held_out_reach": {"omega": ["alpha"]},
    },
    "run": {"label": "tiny", "agents": [{"name": "alpha-bot", "repo": "alpha", "avatar": "owl"}]},
}

SEED = "1" * 40
HEAD = "2" * 40

# alpha's files in upstream's release before the seed, at its seed commit and at its head; beta and
# gamma have no repository, so no code.
ALPHA_UPSTREAM = {"README.md": "# alpha\n"}
ALPHA_SEED = {
    "README.md": "# alpha\n\nA parser.\n",
    "alpha/parser.py": "def parse(text):\n    grammar = load()\n    return grammar.parse(text)\n",
    "alpha/legacy.py": "def parse_old(text):\n    return parse(text)\n",
    "alpha/scratch.py": "# notes\n",
    "logo.png": "\x89PNG\r\n\x1a\n\0\0\0\rIHDR",
}
ALPHA_HEAD = {
    "README.md": "# alpha\n\nA parser.\n",
    "alpha/parser.py": "_GRAMMAR = None\n\n\ndef parse(text):\n    grammar = _GRAMMAR or load()\n    return grammar.parse(text)\n",
    "alpha/compat.py": "def parse_old(text):\n    return parse(text)\n",
    "alpha/settings.py": f'TOKEN = "{FAKE_GITHUB_TOKEN}"\n',
    "logo.png": "\x89PNG\r\n\x1a\n\0\0\0\rIHDR\0\0\0\x10",
}

# alpha's other branches: a pull request's, with a link, a submodule and a compiled file whose bytes hold
# the token beside the files it shares with main, and alpha-bot's notes.
ALPHA_PERF = {
    **ALPHA_HEAD,
    "alpha/__pycache__/settings.cpython-312.pyc": f"\0\r\r\n\0\0\0\0TOKEN\0{FAKE_GITHUB_TOKEN}\0",
    "alpha/cache.py": "_CACHE = {}\n",
    "docs/README.md": ("120000", "../README.md"),
    "vendor/grammar": ("160000", "3" * 40),
}
# The head of a pull request whose branch was deleted.
ALPHA_TRIED = {**ALPHA_PERF, "alpha/cache.py": "_CACHE = {}\n_LIMIT = 1024\n"}
ALPHA_MEMORY = {
    "MEMORY.md": "# Memory\n\n- The grammar loads in 40 ms; cache it.\n",
    "notebook.md": "2026-08-01: opened a pull request for the cache.\n",
}
ALPHA_CHARTER = "# Goal\n\nMake `alpha` measurably faster without changing what it does.\n"
# The message of the release branch's merge commit, with a fake credential to be redacted.
ALPHA_MERGE_MESSAGE = f"Merge branch 'perf/cache-grammar' into release/1.0\n\nThe CI token {FAKE_GITHUB_TOKEN} was pasted here by mistake.\n"

# (wake_id, run_id, agent, repo, started, finished, status, outcome, summary, cost). The ledger books
# no bootstrap, so its rows have no wake id, agent or times, as the analyzer leaves them.
WAKES = [
    (None, "20260801T090000Z-0000000a", None, "alpha", None, None, "completed", "unledgered", None, 0.5),
    (None, "20260801T090000Z-0000000b", None, "beta", None, None, "completed", "unledgered", None, 0.5),
    (None, "20260801T090100Z-0000000c", None, "gamma", None, None, "completed", "unledgered", None, 0.5),
    (1, "20260801T100000Z-aaaaaaa1", "alpha-bot", "alpha", "2026-08-01T10:00:00+00:00", "2026-08-01T10:30:00+00:00",
     "completed", "worked", "Cached the grammar and released 1.0.0.", 1.25),
    (2, "20260801T100500Z-bbbbbbb1", "beta-bot", "beta", "2026-08-01T10:05:00+00:00", "2026-08-01T10:41:00+00:00",
     "completed", "worked", "Asked alpha about parse times; opened a pull request.", 2.0),
    (3, "20260801T110000Z-ccccccc1", "gamma-bot", "gamma", "2026-08-01T11:00:00+00:00", "2026-08-01T11:05:00+00:00",
     "failed", None, None, 0.25),
    (4, "20260801T120000Z-aaaaaaa2", "alpha-bot", "alpha", "2026-08-01T12:00:00+00:00", "2026-08-01T12:20:00+00:00",
     "completed", "idle", "Nothing new to do.", 0.1),
]

# An output longer than the analyzer keeps, whole in the trajectory; the evidence holds its first 3,000
# and last 1,000 characters.
LONG_OUTPUT = "x" * 3000 + "m" * 5000 + "y" * 1000
TRIMMED_OUTPUT = "x" * 3000 + "\n[... 5000 characters trimmed ...]\n" + "y" * 1000

ALPHA, ALPHA_AGAIN = "20260801T100000Z-aaaaaaa1", "20260801T120000Z-aaaaaaa2"
BETA, GAMMA = "20260801T100500Z-bbbbbbb1", "20260801T110000Z-ccccccc1"
# The harness and model of a session, when it is not Claude Code's.
HARNESSES = {ALPHA_AGAIN: ("kimi-cli", "kimi-test")}
# The prompt a run started with, as the checkpoint keeps it in `input/prompt.md`.
PROMPTS = {
    ALPHA_AGAIN: "You maintain alpha. Look for new issues and pull requests.\n",
    GAMMA: "You maintain gamma. Make it measurably faster without changing what it does.\n",
}
ALPHA_PROMPT = "You maintain alpha. Make it measurably faster without changing what it does.\n"
FORGE_CALL = 'curl -H "Authorization: token $HUB_FORGE_TOKEN" "$HUB_FORGE_URL/api/v1/repos/hub/alpha"'
REMOTES = f"origin\thttp://alpha-bot:{FAKE_FORGE_TOKEN}@forge.invalid:3000/hub/alpha.git (push)"
NOTES = "# Notes\n\nThe grammar is cached between calls now.\n"
# A step's metrics as each converter records them: Claude Code's with what it adds under `extra`.
CLAUDE_METRICS = {
    "prompt_tokens": 1200,
    "completion_tokens": 80,
    "cached_tokens": 1000,
    "extra": {"cache_creation": {"ephemeral_5m_input_tokens": 200}, "service_tier": "standard", "iterations": []},
}
KIMI_METRICS = {"prompt_tokens": 900, "completion_tokens": 40, "cached_tokens": 800}


def agent_step(step_id: int, at: str | None, message: str, calls: list[tuple[str, str, dict, str]] | None = None, **more) -> dict:
    """An ATIF agent step; `calls` are (call id, tool, arguments, output)."""

    step = {"step_id": step_id, "source": "agent", "model_name": more.pop("model", "anthropic/claude-test"), "message": message, **more}
    if at is not None:
        step["timestamp"] = at
    if calls:
        step["tool_calls"] = [{"tool_call_id": call_id, "function_name": tool, "arguments": args} for call_id, tool, args, _ in calls]
        step["observation"] = {"results": [{"source_call_id": call_id, "content": output} for call_id, _, _, output in calls]}
    return step


# Each session's trajectory, as its harness's converter writes it. A bootstrap's is written in `checkpoint`.
TRAJECTORIES = {
    ALPHA: {
        "schema_version": "ATIF-v1.7",
        "session_id": "synthetic-alpha-1",
        "agent": {"name": "claude-code", "version": "0.0.0", "model_name": "anthropic/claude-test"},
        "steps": [
            {"step_id": 1, "timestamp": "2026-08-01T10:00:01.500Z", "source": "user", "message": ALPHA_PROMPT},
            agent_step(2, "2026-08-01T10:00:09.250Z", "The forge first.", [
                ("toolu_1", "Bash", {"command": FORGE_CALL, "description": "Read the repository"}, '{"id": 1}'),
                ("toolu_2", "Bash", {"command": "git remote -v"}, REMOTES),
            ], metrics=CLAUDE_METRICS),
            agent_step(3, "2026-08-01T10:01:00.000Z", "", [("toolu_3", "Read", {"file_path": "alpha/parser.py"}, LONG_OUTPUT)]),
            agent_step(4, "2026-08-01T10:08:12.999Z", "", [
                ("toolu_4", "Write", {"file_path": "NOTES.md", "content": NOTES}, "File created successfully at: NOTES.md"),
            ]),
            agent_step(5, "2026-08-01T10:29:30.750Z", f"Released 1.0.0. The CI token {FAKE_GITHUB_TOKEN} was in the log; I removed it."),
        ],
    },
    ALPHA_AGAIN: {
        "schema_version": "ATIF-v1.6",
        "session_id": "synthetic-alpha-2",
        "agent": {"name": "kimi-cli", "version": "kimi, version 0.0.0", "model_name": "kimi-test"},
        "steps": [
            agent_step(1, None, "(tool use)", [
                ("Shell:0", "Shell", {"command": "git log --oneline -1", "timeout": 60}, "2222222 perf: cache the grammar\n"),
            ], model="kimi-test", reasoning_content="Anything new since the release?", metrics=KIMI_METRICS),
            agent_step(2, None, "Nothing new to do.", model="kimi-test", reasoning_content="No open issue or pull request.", metrics=KIMI_METRICS),
        ],
    },
}


def wire(*records: tuple[float, str, dict | None]) -> str:
    """A kimi-cli session log (`wire.jsonl`): its header, then a (time, type, payload) record per line."""

    lines = [json.dumps({"type": "metadata", "protocol_version": "1.0"})]
    lines += [json.dumps({"timestamp": at, "message": {"type": kind, "payload": payload or {}}}) for at, kind, payload in records]
    return "\n".join(lines) + "\n"


# alpha's kimi-cli session keeps a log for itself and one for a subagent it started, which begins later.
# 1785585600 is 2026-08-01T12:00:00Z.
SESSION_LOGS = {
    ALPHA_AGAIN: {
        "sessions/0f1e2d3c4b5a69788796a5b4c3d2e1f0/6c1d-alpha/wire.jsonl": wire(
            (1785585601.25, "TurnBegin", {"user_input": PROMPTS[ALPHA_AGAIN]}),
            (1785585605.5, "StepBegin", {"n": 1}),
            (1785585606.0, "ToolCall", {"type": "function", "id": "Shell:0", "function": {"name": "Shell"}}),
            (1785585607.0, "ToolResult", {"tool_call_id": "Shell:0", "return_value": {}}),
            (1785586780.75, "StepBegin", {"n": 2}),
            (1785586790.0, "TurnEnd", None),
        ),
        "sessions/0f1e2d3c4b5a69788796a5b4c3d2e1f0/9a7b-subagent/wire.jsonl": wire(
            (1785585900.0, "TurnBegin", {"user_input": "Count the open issues."}),
            (1785585901.0, "StepBegin", {"n": 1}),
        ),
    },
}


def git(*args: str, cwd: Path, stdin: str = "", date: str = "2026-08-01T10:09:00Z") -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "alpha-bot",
        "GIT_AUTHOR_EMAIL": "alpha-bot@forge.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": "alpha-bot",
        "GIT_COMMITTER_EMAIL": "alpha-bot@forge.invalid",
        "GIT_COMMITTER_DATE": date,
    }
    done = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True, text=True, env=env, check=True)
    return done.stdout.strip()


def write_tree(bare: Path, files: dict[str, str | tuple[str, str]]) -> str:
    """Store `files` (path → text, or (mode, text)) in the bare repository as a tree; the tree's id.

    A link's text is its target; a submodule's (mode 160000) is the commit it points to.
    """

    entries, folders = [], {}
    for path, content in files.items():
        name, _, rest = path.partition("/")
        if rest:
            folders.setdefault(name, {})[rest] = content
            continue
        mode, text = content if isinstance(content, tuple) else ("100644", content)
        if mode == "160000":
            entries.append(f"{mode} commit {text}\t{name}")
            continue
        entries.append(f"{mode} blob {git('hash-object', '-w', '--stdin', cwd=bare, stdin=text)}\t{name}")
    for name, inner in folders.items():
        entries.append(f"040000 tree {write_tree(bare, inner)}\t{name}")
    return git("mktree", cwd=bare, stdin="".join(f"{entry}\n" for entry in entries))


def commit(
    bare: Path, files: dict[str, str | tuple[str, str]], message: str, parent: str | None = None, date: str = "2026-08-01T10:09:00Z"
) -> str:
    """A commit of `files` in the bare repository, on `parent` when one is given; its id."""

    return git("commit-tree", write_tree(bare, files), *(["-p", parent] if parent else []), "-m", message, cwd=bare, date=date)


def evidence_db(path: Path, seed: str, head: str) -> None:
    db = sqlite3.connect(path)
    db.executescript(EVIDENCE_SCHEMA)
    # The analyzer's rows for alpha's calls, as it reads them from the trajectory, and for beta's, which it
    # read from the run's live tree because the checkpoint kept no trajectory.
    calls = [
        (1, ALPHA, "alpha-bot", 2, 0, "Bash", "shell", FORGE_CALL, "", '{"id": 1}'),
        (1, ALPHA, "alpha-bot", 2, 1, "Bash", "shell", "git remote -v", "", REMOTES),
        (1, ALPHA, "alpha-bot", 3, 2, "Read", "read", "alpha/parser.py", "alpha/parser.py", TRIMMED_OUTPUT),
        (1, ALPHA, "alpha-bot", 4, 3, "Write", "write", "NOTES.md", "NOTES.md", "File created successfully at: NOTES.md"),
        (4, ALPHA_AGAIN, "alpha-bot", 1, 0, "Shell", "shell", "git log --oneline -1", "", "2222222 perf: cache the grammar\n"),
        (2, BETA, "beta-bot", 2, 0, "Edit", "edit", "beta/client.py", "beta/client.py", "ok"),
    ]
    for wake in WAKES:
        wake_id, run_id, agent, _, started, finished, status, outcome, summary, cost = wake
        response = None if status == "failed" else f"Session {run_id} done.\n"
        error = "Harbor did not complete exactly one trial" if status == "failed" else None
        harness, model = HARNESSES.get(run_id, ("claude-code", "anthropic/claude-test"))
        transcript = "trajectory" if run_id in TRAJECTORIES or wake_id is None else "live-trajectory" if run_id == BETA else "none"
        db.execute(
            "INSERT INTO wakes VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wake_id, run_id, agent, started, finished, status, outcome, summary, cost, error, harness, model, transcript,
             sum(1 for call in calls if call[1] == run_id), response),
        )
    db.executemany(
        "INSERT INTO tool_calls (wake_id, run_id, agent, step, call, tool, kind, text, path, output) VALUES (?,?,?,?,?,?,?,?,?,?)",
        calls,
    )
    db.executemany(
        "INSERT INTO repos VALUES (?,?,?,?,1)",
        [(f"{ORG}/alpha", "main", seed, head), *((f"{ORG}/{name}", "main", SEED, HEAD) for name in ("beta", "gamma"))],
    )
    db.executemany(
        "INSERT INTO pulls (repo, number, title, body, author, state, merged, merger, merged_at, created_at, closed_at,"
        " base_branch, head_branch, additions, deletions, files, opened_wake_id, opened_run_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (f"{ORG}/alpha", 1, "perf: cache the grammar", "## What\n\nSpeeds up parsing by caching the grammar.\n",
             "alpha-bot", "merged", 1, "alpha-bot", "2026-08-01T10:20:00Z", "2026-08-01T10:10:00Z", "2026-08-01T10:20:00Z",
             "main", "perf/cache-grammar", 12, 3, 1, 1, "20260801T100000Z-aaaaaaa1"),
            # Forgejo keeps updating merged_unix on an unmerged pull request; the exporter must not report it.
            (f"{ORG}/beta", 1, "Use alpha's cached grammar", "<!-- template -->\nSwitches beta to alpha's new API.\n",
             "beta-bot", "closed", 0, None, "2026-08-01T10:41:03Z", "2026-08-01T10:30:00Z", "2026-08-01T10:41:03Z",
             "main", "use-cached-grammar", 4, 4, 1, 2, "20260801T100500Z-bbbbbbb1"),
        ],
    )
    db.executemany(
        "INSERT INTO pull_files (repo, number, path, additions, deletions, category) VALUES (?,?,?,?,?,?)",
        [(f"{ORG}/alpha", 1, "alpha/parser.py", 12, 3, "src"), (f"{ORG}/beta", 1, "beta/client.py", 4, 4, "src")],
    )
    db.executemany(
        "INSERT INTO pull_patches VALUES (?,?,?,0)",
        [(f"{ORG}/alpha", 1, "diff --git a/alpha/parser.py b/alpha/parser.py\n+_GRAMMAR = None\n"), (f"{ORG}/beta", 1, "")],
    )
    db.execute(
        "INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)",
        (f"{ORG}/alpha", 2, "How long does a parse take?",
         f"Hi @alpha-bot — I maintain `beta`.\n\nOur CI token {FAKE_GITHUB_TOKEN} leaked into this text by accident.\n",
         "beta-bot", "closed", "2026-08-01T10:15:00Z", "2026-08-01T10:29:58Z"),
    )
    push = json.dumps({"is_force_push": False, "commit_ids": [head]})
    link = f'<a href="/{ORG}/alpha/commit/{head}">perf: cache the grammar (#1)</a>'
    review_body = "Self-review: the cache is cleared on reload."
    db.executemany(
        "INSERT INTO comments (id, repo, number, is_pull, type, kind, author, body, created_at, review_id, force_push)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, f"{ORG}/alpha", 1, 1, 29, "pull_push", "alpha-bot", push, "2026-08-01T10:10:00Z", None, 0),
            (2, f"{ORG}/alpha", 1, 1, 22, "review", "alpha-bot", review_body, "2026-08-01T10:18:00Z", 1, None),
            (3, f"{ORG}/alpha", 1, 1, 28, "merge_pull", "alpha-bot", "", "2026-08-01T10:20:00Z", None, None),
            (4, f"{ORG}/alpha", 1, 1, 4, "commit_ref", "alpha-bot", link, "2026-08-01T10:20:01Z", None, None),
            (5, f"{ORG}/alpha", 2, 0, 0, "comment", "alpha-bot", "About 40 ms per file.\n\nMeasured on 3.13.",
             "2026-08-01T10:25:00Z", None, None),
            (6, f"{ORG}/alpha", 2, 0, 2, "close", "alpha-bot", "", "2026-08-01T10:29:58Z", None, None),
            (7, f"{ORG}/alpha", 2, 0, 6, "pull_ref", "beta-bot", "", "2026-08-01T10:31:00Z", None, None),
            (8, f"{ORG}/beta", 1, 1, 10, "change_title", "beta-bot", "", "2026-08-01T10:35:00Z", None, None),
            (9, f"{ORG}/beta", 1, 1, 2, "close", "beta-bot", "", "2026-08-01T10:41:03Z", None, None),
            (10, f"{ORG}/alpha", 2, 0, 0, "comment", "gamma-bot", "Same here on gamma.", "2026-08-01T13:00:00Z", None, None),
        ],
    )
    db.execute(
        "INSERT INTO reviews VALUES (1, ?, 1, 'alpha-bot', 'alpha-bot', 'pending', ?, '2026-08-01T10:18:00Z')",
        (f"{ORG}/alpha", review_body),
    )
    db.executemany(
        "INSERT INTO releases VALUES (?,?,?,?,?,?,0,?)",
        [
            (f"{ORG}/alpha", "v0.9", "v0.9", "user-0", "3" * 40, "2020-01-01T00:00:00Z", 1),
            (f"{ORG}/alpha", "v1.0.0", "alpha 1.0.0", "alpha-bot", head, "2026-08-01T10:22:00Z", 0),
        ],
    )
    db.commit()
    db.close()


def forge_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(FORGE_SCHEMA)
    db.executemany("INSERT INTO repository VALUES (?,?,?,?,'main')", [(1, ORG, "alpha", "A parser"), (2, ORG, "beta", ""), (3, ORG, "gamma", "")])
    # alpha#1 is a pull, alpha#2 an issue, beta#1 a pull.
    db.executemany('INSERT INTO issue VALUES (?,?,?,?)', [(11, 1, 1, 1), (12, 1, 2, 0), (21, 2, 1, 1)])
    db.executemany(
        "INSERT INTO comment VALUES (?,?,?,?,?,?,?)",
        [(7, 6, 21, None, None, None, None), (8, 10, None, "Use alpha cache", "Use alpha's cached grammar", None, None)],
    )
    db.executemany("INSERT INTO release VALUES (?,?,?,?)", [(1, 1, "v0.9", ""), (2, 1, "v1.0.0", "## Changes\n\nFaster parsing.\n")])
    db.commit()
    db.close()


def checkpoint(root: Path) -> None:
    (root / "state" / "coordinator").mkdir(parents=True)
    db = sqlite3.connect(root / "state" / "coordinator" / "coordinator.db")
    db.execute("CREATE TABLE wakes (id INTEGER PRIMARY KEY, agent TEXT, run_id TEXT)")
    db.commit()
    db.close()
    manifest = {
        "schema_version": 1,
        "checkpoint_id": "world-t-20260801T130000Z",
        "created_at": "2026-08-01T13:00:00.123456Z",
        "hub_commit": "4" * 40,
        "world_id": "world-t",
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "apparatus" / "charters").mkdir(parents=True)
    (root / "apparatus" / "charters" / "alpha-bot.md").write_text(ALPHA_CHARTER)
    for wake_id, run_id, _, repo, _, _, status, *_ in WAKES:
        output = root / "runs" / run_id / "output"
        output.mkdir(parents=True)
        harness, model = HARNESSES.get(run_id, ("claude-code", "anthropic/claude-test"))
        result = {
            "agent": harness,
            "model": model,
            "status": status,
            "image": "hub-test-agent:1",
            "repository": f"http://forge.invalid:3000/{ORG}/{repo}.git",
            "requested_ref": "main",
            "resolved_sha": SEED,
            "usage": {"cost_usd": 1.0, "input_tokens": 1000, "output_tokens": 100},
        }
        (output / "result.json").write_text(json.dumps(result))
        if status != "failed":
            (output / "response.md").write_text(f"Session {run_id} done.\n")
            changes = {"added": ["NOTES.md"], "modified": [f"{repo}/parser.py"], "deleted": [], "files": []}
            (output / "changes.json").write_text(json.dumps(changes))
        if wake_id is None:
            minute = run_id[11:13]
            steps = [
                {"step_id": 1, "timestamp": f"2026-08-01T09:{minute}:05.250Z", "source": "user", "message": f"Set up {repo}.\n"},
                agent_step(2, f"2026-08-01T09:{int(minute) + 4:02d}:30.900Z", "Set up."),
            ]
            (output / "trajectory.json").write_text(json.dumps({"schema_version": "ATIF-v1.7", "steps": steps}))
        elif run_id in TRAJECTORIES:
            (output / "trajectory.json").write_text(json.dumps(TRAJECTORIES[run_id]))
        for name, text in SESSION_LOGS.get(run_id, {}).items():
            (output / "session" / name).parent.mkdir(parents=True)
            (output / "session" / name).write_text(text)
        if run_id in PROMPTS:
            (output.parent / "input").mkdir()
            (output.parent / "input" / "prompt.md").write_text(PROMPTS[run_id])


def build(root: Path) -> dict[str, Path]:
    """Write the synthetic inputs under `root`; the paths the exporter takes."""

    evidence, ckpt = root / "analysis", root / "checkpoint"
    (evidence / "forge" / "data").mkdir(parents=True)
    bare = evidence / "forge" / "git" / "repositories" / ORG / "alpha.git"
    bare.mkdir(parents=True)
    git("init", "--bare", "-q", ".", cwd=bare)
    # Upstream's history: the release the seed is made from, and a later maintenance release that only
    # an imported tag reaches.
    upstream = commit(bare, ALPHA_UPSTREAM, "Upstream: release 1.0", date="2026-07-01T00:00:00Z")
    maintenance = commit(bare, {**ALPHA_UPSTREAM, "CHANGES.md": "1.1: a fix\n"}, "Upstream: release 1.1", upstream, date="2026-07-20T00:00:00Z")
    seed = commit(bare, ALPHA_SEED, "Seed alpha", upstream)
    # The society's: main, a pull request's branch and one whose branch was deleted, alpha-bot's notes,
    # and a release branch that merges the pull request's branch into main's head.
    head = commit(bare, ALPHA_HEAD, "perf: cache the grammar", seed)
    perf = commit(bare, ALPHA_PERF, "perf: keep the grammar between calls", head, date="2026-08-01T10:15:00Z")
    tried = commit(bare, ALPHA_TRIED, "perf: bound the cache", perf, date="2026-08-01T10:20:00Z")
    memory = commit(bare, ALPHA_MEMORY, "alpha-bot: notes", date="2026-08-01T10:29:00Z")
    merged = git("rev-parse", f"{perf}^{{tree}}", cwd=bare)
    release = git("commit-tree", merged, "-p", head, "-p", perf, "-m", ALPHA_MERGE_MESSAGE, cwd=bare, date="2026-08-01T10:35:00Z")
    for ref, sha in (
        ("heads/main", head),
        ("heads/perf/cache-grammar", perf),
        ("heads/agent/memory", memory),
        ("heads/release/1.0", release),
        ("pull/2/head", tried),
        ("tags/v1.1", maintenance),
    ):
        git("update-ref", f"refs/{ref}", sha, cwd=bare)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    evidence_db(evidence / "evidence.db", seed, head)
    forge_db(evidence / "forge" / "data" / "forgejo.db")
    checkpoint(ckpt)
    world = root / "world.json"
    world.write_text(json.dumps(WORLD))
    return {"evidence": evidence, "checkpoint": ckpt, "world": world}


def export(root: Path, out: Path) -> dict:
    """Build the inputs under `root` and export them to `out` as the bundle `tiny`."""

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from export_bundle import Export

    paths = build(root)
    args = argparse.Namespace(
        evidence=str(paths["evidence"]),
        checkpoint=str(paths["checkpoint"]),
        world=str(paths["world"]),
        id="tiny",
        label="Tiny · World-T",
        out=str(out),
        group=None,
        setting=None,
        avatars=str(Path(__file__).resolve().parents[2] / "public" / "avatars"),
    )
    return Export(args).run()


def settle(folder: Path) -> None:
    """Fix the two fields that change with every export, so the fixture is stable."""

    core_path = folder / "tiny" / "core.json"
    core = json.loads(core_path.read_text())
    core["source"]["exporter"] = None
    core["source"]["generated"] = "2026-08-01T13:30:00Z"
    core_path.write_text(json.dumps(core, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: synthetic.py <folder for the fixture bundle>")
    target = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as scratch:
        export(Path(scratch), target)
    settle(target)
    print(f"wrote {target / 'tiny'} and {target / 'index.json'}")
