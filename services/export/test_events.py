"""Tests for event derivation and session linking, against the synthetic society's SQLite."""

import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import synthetic
from events import SLACK_SECONDS, Sessions, from_forge, from_ledger, stamp
from export_bundle import EVIDENCE_QUERIES, Names

AGENTS = {"alpha-bot", "beta-bot", "gamma-bot"}
ALPHA_1, BETA_1, ALPHA_2 = "20260801T100000Z-aaaaaaa1", "20260801T100500Z-bbbbbbb1", "20260801T120000Z-aaaaaaa2"


def read(db: sqlite3.Connection, table: str, names: Names) -> list[dict]:
    rows = []
    for row in db.execute(EVIDENCE_QUERIES[table]):
        item = dict(row)
        item["repo"] = names(item["repo"])
        if item["repo"]:
            rows.append(item)
    return rows


class StampTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(stamp("2026-08-06T10:02:35+00:00"), "2026-08-06T10:02:35Z")
        self.assertEqual(stamp("2026-08-06T09:43:34.165Z"), "2026-08-06T09:43:34Z")
        self.assertEqual(stamp("2026-08-06T12:02:35+02:00"), "2026-08-06T10:02:35Z")
        self.assertEqual(stamp("2026-08-06 10:02:35"), "2026-08-06T10:02:35Z")
        self.assertIsNone(stamp(None))
        self.assertIsNone(stamp(""))
        self.assertIsNone(stamp("yesterday"))


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.sessions = Sessions(
            [
                ("one", "a", "2026-08-01T10:00:00Z", "2026-08-01T10:30:00Z"),
                ("two", "a", "2026-08-01T10:30:02Z", "2026-08-01T11:00:00Z"),
                ("open", "b", "2026-08-01T10:00:00Z", None),
                ("unknown start", "c", None, None),
                # A failed attempt with no end, then the agent's real session.
                ("attempt", "d", "2026-08-01T09:00:00Z", None),
                ("real", "d", "2026-08-01T09:10:00Z", "2026-08-01T09:40:00Z"),
            ]
        )

    def test_containment_with_the_allowance(self):
        self.assertEqual(self.sessions.at("a", "2026-08-01T10:10:00Z"), "one")
        self.assertEqual(self.sessions.at("a", f"2026-08-01T11:00:0{SLACK_SECONDS}Z"), "two")
        self.assertIsNone(self.sessions.at("a", f"2026-08-01T11:00:0{SLACK_SECONDS + 1}Z"))
        self.assertIsNone(self.sessions.at("a", "2026-08-01T09:59:54Z"))

    def test_overlap_goes_to_the_latest_start(self):
        # 10:30:01 is inside "one" by the allowance and inside "two" by it too.
        self.assertEqual(self.sessions.at("a", "2026-08-01T10:30:01Z"), "two")

    def test_other_agents_and_unknown_starts(self):
        self.assertIsNone(self.sessions.at("b", "2026-08-01T09:00:00Z"))
        self.assertIsNone(self.sessions.at("c", "2026-08-01T10:00:00Z"))
        self.assertIsNone(self.sessions.at("nobody", "2026-08-01T10:00:00Z"))

    def test_a_session_without_an_end_covers_only_its_start(self):
        self.assertEqual(self.sessions.at("b", f"2026-08-01T10:00:0{SLACK_SECONDS}Z"), "open")
        self.assertIsNone(self.sessions.at("b", f"2026-08-01T10:00:0{SLACK_SECONDS + 1}Z"))
        self.assertIsNone(self.sessions.at("b", "2026-08-02T00:00:00Z"))
        # The agent's actions belong to its real session, and outside it to none.
        self.assertEqual(self.sessions.at("d", "2026-08-01T09:20:00Z"), "real")
        self.assertIsNone(self.sessions.at("d", "2026-08-01T09:50:00Z"))


class FromForgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        paths = synthetic.build(Path(cls.scratch.name))
        db = sqlite3.connect(f"file:{paths['evidence'] / 'evidence.db'}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        names = Names("hub", ["alpha", "beta", "gamma"])
        cls.records = {table: read(db, table, names) for table in ("pulls", "issues", "comments", "reviews", "releases")}
        for release in cls.records["releases"]:
            release["notes"] = f"notes of {release['tag']}"
        wakes = db.execute("SELECT run_id, agent, started, finished FROM wakes").fetchall()
        db.close()
        cls.sessions = Sessions((w["run_id"], w["agent"], stamp(w["started"]), stamp(w["finished"])) for w in wakes)
        cls.events, cls.skipped = from_forge(cls.records, AGENTS, cls.sessions, "2026-08-01T09:00:00Z")

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def find(self, action: str, repo: str) -> dict:
        matches = [e for e in self.events if e["action"] == action and e["repo"] == repo]
        self.assertEqual(len(matches), 1, f"{action} on {repo}")
        return matches[0]

    def test_every_kind_of_record_becomes_its_action(self):
        self.assertEqual(
            Counter(e["action"] for e in self.events),
            {
                "pr.create": 2,
                "pr.merge": 1,
                "pr.close": 1,
                "pr.review": 1,
                "issue.create": 1,
                "issue.comment": 2,
                "issue.close": 1,
                "release.create": 1,
            },
        )

    def test_oldest_first(self):
        times = [e["t"] for e in self.events]
        self.assertEqual(times, sorted(times))
        self.assertEqual([e["action"] for e in self.events][:4], ["pr.create", "issue.create", "pr.review", "pr.merge"])

    def test_within_a_second_cause_comes_before_effect(self):
        at = "2026-08-01T10:10:00Z"
        pull = {**self.records["pulls"][0], "created_at": at, "merged_at": at}
        comment = {**self.records["comments"][4], "repo": "alpha", "number": 1, "is_pull": 1, "created_at": at}
        events, _ = from_forge({"comments": [comment], "pulls": [pull]}, AGENTS, self.sessions, None)
        self.assertEqual([e["action"] for e in events], ["pr.create", "pr.comment", "pr.merge"])

    def test_the_opening_session_named_by_the_evidence(self):
        self.assertEqual(self.find("pr.create", "alpha")["session"], ALPHA_1)
        self.assertEqual(self.find("pr.create", "beta")["session"], BETA_1)

    def test_the_session_running_at_the_time(self):
        self.assertEqual(self.find("pr.merge", "alpha")["session"], ALPHA_1)
        self.assertEqual(self.find("issue.create", "alpha")["session"], BETA_1)
        self.assertEqual(self.find("issue.close", "alpha")["session"], ALPHA_1)
        # Closed three seconds after beta's session ended: inside the allowance.
        self.assertEqual(self.find("pr.close", "beta")["session"], BETA_1)

    def test_an_action_outside_every_session(self):
        late = [e for e in self.events if e["agent"] == "gamma-bot"]
        self.assertEqual(len(late), 1)
        self.assertIsNone(late[0]["session"])

    def test_the_author_is_the_agent_and_the_repo_is_acted_on(self):
        opened = self.find("issue.create", "alpha")
        self.assertEqual((opened["agent"], opened["repo"], opened["ref"]), ("beta-bot", "alpha", 2))

    def test_texts_carried(self):
        self.assertIn("caching the grammar", self.find("pr.create", "alpha")["text"])
        self.assertIn("I maintain `beta`", self.find("issue.create", "alpha")["text"])
        self.assertEqual(self.find("pr.review", "alpha")["text"], "Self-review: the cache is cleared on reload.")
        self.assertIsNone(self.find("pr.merge", "alpha")["text"])

    def test_only_agent_releases_from_the_run(self):
        release = self.find("release.create", "alpha")
        self.assertEqual((release["tag"], release["ref"], release["text"]), ("v1.0.0", None, "notes of v1.0.0"))
        self.assertEqual(release["session"], ALPHA_1)

    def test_records_by_users_who_are_not_agents(self):
        records = {**self.records, "comments": [{**c, "author": "operator"} for c in self.records["comments"]]}
        events, skipped = from_forge(records, AGENTS, self.sessions, None)
        self.assertFalse(any(e["agent"] == "operator" for e in events))
        self.assertEqual(skipped["issue.comment by a user who is not an agent"], 2)

    def test_a_release_before_the_run(self):
        _, skipped = from_forge(self.records, AGENTS, self.sessions, "2026-08-01T11:00:00Z")
        self.assertEqual(skipped["release from before the run"], 1)

    def test_an_unmerged_pull_request_is_never_merged(self):
        # beta#1 carries a merged_at, as Forgejo leaves one on every pull request.
        self.assertFalse(any(e["action"] == "pr.merge" and e["repo"] == "beta" for e in self.events))


class FromLedgerTest(unittest.TestCase):
    def test_rows_with_their_texts(self):
        rows = [
            {"run_id": "r2", "agent": "a-bot", "ts": "2026-08-01T10:05:00+00:00", "repo": "b", "action": "issue.comment",
             "number": 4, "metadata": json.dumps({"issue": 4, "comment_id": 9})},
            {"run_id": "r1", "agent": "a-bot", "ts": "2026-08-01T10:00:00Z", "repo": "a", "action": "release.create",
             "number": None, "metadata": json.dumps({"tag": "v2", "id": 1})},
            {"run_id": "r1", "agent": "a-bot", "ts": "2026-08-01T10:01:00Z", "repo": "a", "action": "ci.dispatch",
             "number": None, "metadata": "not json"},
            {"run_id": "r1", "agent": None, "ts": "2026-08-01T10:02:00Z", "repo": "a", "action": "pr.create", "number": 1, "metadata": None},
        ]
        texts = {("comment", "b", 9): "Thanks!", ("release", "a", "v2"): "Notes."}
        events = from_ledger(rows, texts)
        self.assertEqual([e["action"] for e in events], ["release.create", "ci.dispatch", "issue.comment"])
        self.assertEqual([e["session"] for e in events], ["r1", "r1", "r2"])
        self.assertEqual(events[0]["tag"], "v2")
        self.assertEqual(events[0]["text"], "Notes.")
        self.assertIsNone(events[1]["text"])
        self.assertEqual((events[2]["ref"], events[2]["text"], events[2]["t"]), (4, "Thanks!", "2026-08-01T10:05:00Z"))


if __name__ == "__main__":
    unittest.main()
