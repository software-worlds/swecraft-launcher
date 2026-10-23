import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from swecraft_analyzer.collect import _openers, collect

FUTURE = [
    "        self._decoder_initialized = False",
    "        if not self._decoder_initialized and self._decoder is None:",
    "            self._decoder = _get_decoder(content_encoding)",
]
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()


def commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def build_history(root: Path) -> dict[str, str]:
    """pin → future (upstream tag v2.0); pin → baseline → agent PR copying it."""

    source = root / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    pin = commit(source, "r.py", "def f():\n    return 1\n", "pin")
    git(source, "tag", "v1.0")
    future = commit(source, "r.py", "def f():\n" + "\n".join(FUTURE) + "\n", "future")
    git(source, "tag", "v2.0")
    git(source, "checkout", "-q", "-B", "main", pin)
    baseline = commit(source, "seed.txt", "seeded\n", "seed baseline")
    git(source, "checkout", "-q", "-b", "perf")
    head = commit(
        source, "r.py", "def f():\n" + "\n".join(FUTURE) + "\n    return 2\n", "faster"
    )
    git(source, "tag", "v1.1")
    bare = root / "forge" / "git" / "repositories" / "hub" / "demo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(source), str(bare)],
        check=True,
        env=GIT_ENV,
    )
    git(bare, "update-ref", "refs/pull/1/head", head)
    return {"pin": pin, "future": future, "baseline": baseline, "head": head}


def build_forge_db(path: Path, shas: dict[str, str]) -> None:
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE user (id INTEGER, lower_name TEXT);
        CREATE TABLE repository (id INTEGER, owner_name TEXT, lower_name TEXT,
            default_branch TEXT);
        CREATE TABLE issue (id INTEGER, repo_id INTEGER, "index" INTEGER,
            poster_id INTEGER, name TEXT, content TEXT, is_pull INTEGER,
            is_closed INTEGER, created_unix INTEGER, closed_unix INTEGER);
        CREATE TABLE pull_request (id INTEGER, issue_id INTEGER, base_repo_id INTEGER,
            head_branch TEXT, base_branch TEXT, merge_base TEXT, has_merged INTEGER,
            merged_commit_id TEXT, merger_id INTEGER, merged_unix INTEGER);
        CREATE TABLE comment (id INTEGER, type INTEGER, poster_id INTEGER,
            issue_id INTEGER, content TEXT, created_unix INTEGER,
            updated_unix INTEGER, review_id INTEGER);
        CREATE TABLE review (id INTEGER, type INTEGER, reviewer_id INTEGER,
            issue_id INTEGER, content TEXT, created_unix INTEGER);
        CREATE TABLE issue_content_history (id INTEGER, poster_id INTEGER,
            issue_id INTEGER, comment_id INTEGER, edited_unix INTEGER,
            is_first_created INTEGER, is_deleted INTEGER);
        CREATE TABLE release (id INTEGER, repo_id INTEGER, publisher_id INTEGER,
            tag_name TEXT, title TEXT, sha1 TEXT, created_unix INTEGER,
            is_draft INTEGER, is_tag INTEGER);
        """
    )
    db.executemany("INSERT INTO user VALUES (?, ?)", [(1, "hub"), (2, "a-bot")])
    db.execute("INSERT INTO repository VALUES (1, 'hub', 'demo', 'main')")
    db.execute(
        "INSERT INTO issue VALUES (1, 1, 1, 2, 'Decode once', "
        "'Makes decoding 1.5x faster', 1, 1, 1758000000, 1758000100)"
    )
    db.execute(
        "INSERT INTO pull_request VALUES "
        "(1, 1, 1, 'perf', 'main', ?, 1, ?, 2, 1758000100)",
        (shas["baseline"], shas["head"]),
    )
    db.execute(
        "INSERT INTO comment VALUES (1, 29, 2, 1, ?, 1758000050, 1758000050, 0)",
        (json.dumps({"is_force_push": True, "commit_ids": [shas["head"]]}),),
    )
    db.execute(
        "INSERT INTO release VALUES (1, 1, 2, 'v1.1', 'v1.1', ?, 1758000200, 0, 0)",
        (shas["head"],),
    )
    db.commit()
    db.close()


def build_checkpoint(root: Path) -> tuple[Path, dict[str, str]]:
    shas = build_history(root)
    build_forge_db(root / "forge" / "data" / "forgejo.db", shas)
    (root / "forge" / "data" / "avatars").mkdir()
    (root / "forge" / "data" / "avatars" / "x.png").write_bytes(b"png")

    checkpoint = root / "checkpoint"
    (checkpoint / "state" / "forge").mkdir(parents=True)
    with tarfile.open(checkpoint / "state" / "forge" / "forgejo-data.tar", "w") as tar:
        tar.add(root / "forge" / "data", arcname="./data")
        tar.add(root / "forge" / "git", arcname="./git")
    (checkpoint / "manifest.json").write_text(
        json.dumps(
            {
                "checkpoint_id": "cp-1",
                "world_id": "w",
                "forge": {"source_url": "http://172.17.0.1:5930"},
                "heads": [
                    {
                        "name": "demo",
                        "baseline_commit": shas["baseline"],
                        "current_head": shas["head"],
                    }
                ],
            }
        )
    )
    apparatus = checkpoint / "apparatus"
    apparatus.mkdir()
    (apparatus / "world.lock.json").write_text(
        json.dumps(
            {
                "org": "hub",
                "repos": [
                    {
                        "name": "demo",
                        "pin_commit": shas["pin"],
                        "baseline_commit": shas["baseline"],
                    }
                ],
            }
        )
    )
    (apparatus / "evaluation.yaml").write_text("evaluators:\n  judge-pkg: {}\n")

    coordinator = checkpoint / "state" / "coordinator"
    coordinator.mkdir(parents=True)
    db = sqlite3.connect(coordinator / "coordinator.db")
    db.executescript(
        """
        CREATE TABLE wakes (id INTEGER PRIMARY KEY, agent TEXT, day TEXT, started TEXT,
            finished TEXT, run_id TEXT, status TEXT, outcome TEXT, summary TEXT,
            cost_usd REAL, error TEXT);
        CREATE TABLE agent_repos (agent TEXT, repo TEXT);
        INSERT INTO wakes VALUES (1, 'a-bot', '2026-09-20', 't0', 't1', 'r1',
            'completed', 'worked', 'sped up decoding', 0.5, NULL);
        INSERT INTO agent_repos VALUES ('a-bot', 'demo');
        """
    )
    db.commit()
    db.close()

    wake = checkpoint / "runs" / "r1" / "output"
    wake.mkdir(parents=True)
    commands = [
        f"git show --stat {shas['future'][:8]}",
        "git push --force origin perf",
        "curl -s https://api.github.com/repos/urllib3/urllib3/pulls",
    ]
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "steps": [
            {
                "step_id": index + 1,
                "source": "agent",
                "tool_calls": [
                    {
                        "tool_call_id": f"c{index}",
                        "function_name": "bash",
                        "arguments": {"command": text},
                    }
                ],
                "observation": {
                    "results": [{"source_call_id": f"c{index}", "content": "1.5x"}]
                },
            }
            for index, text in enumerate(commands)
        ],
    }
    (wake / "trajectory.json").write_text(json.dumps(trajectory))
    (wake / "result.json").write_text(
        json.dumps({"agent": "pi", "model": "m", "status": "completed"})
    )
    (wake / "response.md").write_text("Merged #1.")
    (wake / "forge-actions.jsonl").write_text(
        json.dumps(
            {
                "action": "pr.create",
                "repo": "hub/demo",
                "metadata": {"number": 1},
                "ts": "t",
            }
        )
        + "\n"
    )
    bootstrap = checkpoint / "runs" / "r0" / "output"
    bootstrap.mkdir(parents=True)
    (bootstrap / "result.json").write_text(
        json.dumps(
            {"status": "completed", "memory_repository": "http://f/a-bot/home.git"}
        )
    )
    return checkpoint, shas


class CollectTest(unittest.TestCase):
    def test_checkpoint_to_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint, shas = build_checkpoint(root)
            before = sorted(str(path) for path in checkpoint.rglob("*"))
            analysis = root / "analysis"

            summary = collect(checkpoint, analysis)

            self.assertEqual(
                before, sorted(str(path) for path in checkpoint.rglob("*"))
            )
            counts = summary["counts"]
            self.assertEqual(
                (counts["wakes"], counts["pulls"], counts["releases"]), (2, 1, 1)
            )
            self.assertEqual((counts["future_tags"], counts["future_commits"]), (1, 1))
            self.assertEqual(counts["future_lines"], len(FUTURE))
            self.assertTrue(any("1 upstream tags" in p for p in summary["problems"]))
            self.assertFalse((analysis / "forge" / "data" / "avatars").exists())

            db = sqlite3.connect(analysis / "evidence.db")
            self.assertEqual(
                db.execute(
                    "SELECT agent, outcome FROM wakes WHERE run_id = 'r0'"
                ).fetchone(),
                ("a-bot", "unledgered"),
            )
            self.assertEqual(
                db.execute("SELECT files, additions, merged FROM pulls").fetchone(),
                (1, len(FUTURE) + 1, 1),
            )
            self.assertEqual(
                db.execute("SELECT tag FROM future_tags").fetchone()[0], "v2.0"
            )
            db.close()


class OpenersTest(unittest.TestCase):
    def test_the_forge_record_names_the_opening_wake(self) -> None:
        wakes = [
            {
                "wake_id": 1,
                "run_id": "r1",
                "agent": "a-bot",
                "started": "2026-09-29T06:00:00+00:00",
                "finished": "2026-09-29T06:10:00+00:00",
            },
            {
                "wake_id": 2,
                "run_id": "r2",
                "agent": "b-bot",
                "started": "2026-09-29T06:00:00+00:00",
                "finished": "2026-09-29T06:10:00+00:00",
            },
            {
                "wake_id": 3,
                "run_id": "r3",
                "agent": "a-bot",
                "started": "2026-09-29T07:00:00+00:00",
                "finished": None,
            },
        ]
        pulls = [
            # Opened through the raw API: no ledger row, the window decides.
            {
                "repo": "hub/a",
                "number": 1,
                "author": "a-bot",
                "created_at": "2026-09-29T06:05:00Z",
            },
            # The ledger filed it under the agent's home; the window still
            # finds the wake by author and time.
            {
                "repo": "hub/a",
                "number": 2,
                "author": "b-bot",
                "created_at": "2026-09-29T06:09:59Z",
            },
            # A wake that never finished still covers what it opened.
            {
                "repo": "hub/a",
                "number": 3,
                "author": "a-bot",
                "created_at": "2026-09-29T07:30:00Z",
            },
            # Outside every window of its author: only the ledger knows.
            {
                "repo": "hub/a",
                "number": 4,
                "author": "a-bot",
                "created_at": "2026-09-29T06:30:00Z",
            },
            {
                "repo": "hub/a",
                "number": 5,
                "author": "c-bot",
                "created_at": "2026-09-29T06:05:00Z",
            },
        ]
        actions = [
            {
                "action": "pr.create",
                "repo": "b-bot/home",
                "number": 2,
                "wake_id": 9,
                "run_id": "r9",
            },
            {
                "action": "pr.create",
                "repo": "hub/a",
                "number": 4,
                "wake_id": 1,
                "run_id": "r1",
            },
        ]
        _openers(pulls, wakes, actions)
        self.assertEqual(
            [(pull["opened_wake_id"], pull["opened_run_id"]) for pull in pulls],
            [(1, "r1"), (2, "r2"), (3, "r3"), (1, "r1"), (None, None)],
        )


if __name__ == "__main__":
    unittest.main()
