"""The wake action vocabulary, and the derivation that applies it.

These run against recorded invocations rather than against a live `fj`, which
is the point of the split: the runner image records what was run, and every
question about what it meant is settled here, where it can be re-asked of a
recording that has already been collected.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from swecraft_runner import forge_actions

WRAPPER = Path(__file__).resolve().parents[3] / "services/runner-image/fj-wrapper"


def fj_call(argv, *, stdout="", stderr="", exit_code=0, actor="rich-bot"):
    return {
        "ts": "2026-08-12T08:20:15Z",
        "tool": "fj",
        "cwd": "/app/repository",
        "actor": actor,
        "exit": exit_code,
        "argv": list(argv),
        "stdout": stdout,
        "stderr": stderr,
    }


def forge_call(argv, *, stdout="", exit_code=0, actor="rich-bot"):
    return {
        "ts": "2026-08-12T08:20:15Z",
        "tool": "forge",
        "cwd": "/app/repository",
        "actor": actor,
        "exit": exit_code,
        "argv": list(argv),
        "stdout": stdout,
        "stderr": "",
    }


def one(call, **kwargs):
    rows = forge_actions.derive([call], **kwargs)
    return rows[0] if rows else None


class TableTests(unittest.TestCase):
    def test_the_table_loads_and_every_source_names_a_declared_key(self) -> None:
        # load_table raises when a `with:` key is absent from `metadata:`, so
        # loading is itself the check that the file's two halves agree.
        table = forge_actions.load_table()
        self.assertTrue(table.fj)
        self.assertTrue(table.forge)

    def test_the_wrappers_read_list_matches_the_table(self) -> None:
        """The one list that must be duplicated is the one that must agree.

        The wrapper decides in shell, before the table is reachable, whether a
        command is a read that skips recording. That decision is the only way
        this design can lose a mutation, so the two copies are compared here
        rather than left to drift.
        """

        table = forge_actions.load_table()
        text = WRAPPER.read_text(encoding="utf-8")
        self.assertEqual(
            _case_words(text, r'case "\$\{2:-\}" in'), set(table.fj_read_verbs)
        )
        self.assertEqual(
            _case_words(text, r'case "\$\{1:-\}" in') - {"", "-*"},
            set(table.fj_read_groups),
        )


def _case_words(text: str, header: str) -> set[str]:
    match = re.search(header + r"\s*\n(.*?)\)", text, re.S)
    assert match, f"no case block for {header}"
    body = match.group(1).replace("\\\n", "").replace("\n", "")
    return {word.strip().strip('"') for word in body.split("|") if word.strip()}


class FjDerivationTests(unittest.TestCase):
    def test_a_comment_is_identified_by_the_issue_it_is_on(self) -> None:
        """The number in the argument wins over anything in the output.

        fj prints a URL whose fragment is the *comment* — `…/issues/1#issue
        comment-900`. Reading the first `#N` out of the output recorded 900 as
        the issue, so the row named the comment in the field meant for the
        object it was on and left `comment_id` null.
        """

        row = one(
            fj_call(
                ["issue", "comment", "hub/rich#1", "--body", "ok"],
                stdout="http://forge/hub/rich/issues/1#issuecomment-900\n",
            )
        )
        self.assertEqual(row["action"], "issue.comment")
        self.assertEqual(row["metadata"], {"issue": 1, "comment_id": None})

    def test_a_release_tag_survives_a_value_taking_flag_before_it(self) -> None:
        """`-b <notes>` must not be read as the tag.

        fj takes the tag as its last positional, so a release cut with
        `fj release create --repo R --create-tag -b <notes> v1.2.3` put the
        whole release-notes body in `metadata.tag` and lost the tag: the
        positional scan kept the value after every bare flag, and the body is
        the value after `-b`. Those releases then had no usable ledger entry
        at all.
        """

        row = one(
            fj_call(
                [
                    "release",
                    "create",
                    "--repo",
                    "hub/rich",
                    "--create-tag",
                    "-b",
                    "**Improvements**\n\n- cookie round trip skipped\n",
                    "v2.34.9",
                ]
            )
        )
        self.assertEqual(row["action"], "release.create")
        self.assertEqual(row["metadata"], {"tag": "v2.34.9", "id": None})

    def test_help_is_not_an_action(self) -> None:
        # `fj release create --help` prints usage and touches nothing; recording
        # it produced null-tag release.create rows that outnumbered the real
        # releases in one run's ledger.
        self.assertIsNone(one(fj_call(["release", "create", "--help"])))

    def test_a_positional_value_of_help_is_not_a_help_request(self) -> None:
        # `fj issue comment hub/rich#1 help` posts the word "help" as a
        # comment; only the flag spellings ask fj for usage.
        row = one(
            fj_call(
                ["issue", "comment", "hub/rich#1", "help"],
                stderr="created comment #⁨12⁩\n",
            )
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["repo"], "hub/rich")

    def test_release_notes_reading_help_are_still_notes(self) -> None:
        # Only the flag spellings ask for help. Matching the bare word would
        # let a one-word body erase the release.
        row = one(
            fj_call(["release", "create", "--repo", "hub/rich", "-b", "help", "v1.2.3"])
        )
        self.assertEqual(row["metadata"], {"tag": "v1.2.3", "id": None})

    def test_the_flag_naming_the_tag_beats_the_trailing_positional(self) -> None:
        """`--tag <tag> ... <title>` names both, and only one is the tag.

        fj accepts the tag as a flag and takes a trailing positional as the
        release *title*. Reading the positional put `idna 3.20` in the tag of
        every release cut this way, so the release could not be matched to the
        wake that cut it.
        """

        row = one(
            fj_call(
                [
                    "release",
                    "create",
                    "--repo",
                    "hub/idna",
                    "--tag",
                    "v3.20",
                    "--branch",
                    "main",
                    "--body",
                    "Two performance changes since 3.18.\n",
                    "idna 3.20",
                ]
            )
        )
        self.assertEqual(row["metadata"], {"tag": "v3.20", "id": None})

    def test_a_bare_create_tag_leaves_the_tag_to_the_positional(self) -> None:
        # `--create-tag` is boolean in this form and value-taking in the other,
        # so the flag only claims the next argument when it is not itself a
        # flag. Both spellings must land on the same tag.
        with_value = one(
            fj_call(
                ["release", "create", "--repo", "hub/rich", "--create-tag", "v1.2.3"]
            )
        )
        bare = one(
            fj_call(
                [
                    "release",
                    "create",
                    "--repo",
                    "hub/rich",
                    "--create-tag",
                    "-b",
                    "notes",
                    "v1.2.3",
                ]
            )
        )
        self.assertEqual(with_value["metadata"]["tag"], "v1.2.3")
        self.assertEqual(bare["metadata"]["tag"], "v1.2.3")

    def test_flag_arity_follows_fj_s_documented_grammar(self) -> None:
        """The flag table is read off `fj release create --help`, not guessed.

        Every wrong tag in two of our runs came from a flag whose arity we had
        assumed: `-C <dir>`, `-t <tag>`, and a bare `-b` all put the wrong
        string in `metadata.tag`.
        """

        cwd_flag = one(
            fj_call(
                [
                    "release",
                    "create",
                    "--repo",
                    "hub/rich",
                    "-C",
                    "/app/repository",
                    "v1.2.3",
                ]
            )
        )
        self.assertEqual(cwd_flag["metadata"]["tag"], "v1.2.3")

        short_tag = one(
            fj_call(["release", "create", "-r", "hub/idna", "-t", "v3.20", "idna 3.20"])
        )
        self.assertEqual(short_tag["repo"], "hub/idna")
        self.assertEqual(short_tag["metadata"]["tag"], "v3.20")

        short_create = one(
            fj_call(["release", "create", "--repo", "hub/rich", "-T", "v1.2.3", "Rich"])
        )
        self.assertEqual(short_create["metadata"]["tag"], "v1.2.3")

    def test_a_bare_optional_value_flag_keeps_the_release_name(self) -> None:
        # `-b` with no argument opens an editor, so the argument after it is
        # only its body when it is not itself a flag.
        row = one(
            fj_call(
                ["release", "create", "--repo", "hub/rich", "-b", "--draft", "v1.2.3"]
            )
        )
        self.assertEqual(row["metadata"]["tag"], "v1.2.3")

    def test_an_asset_path_is_not_a_repository_reference(self) -> None:
        """`--asset dist/urllib3-2.7.11-py3-none-any.whl` names no repository.

        The repository scan took the first `owner/name`-shaped argument, and an
        asset path is that shape. Thirteen urllib3 releases were filed under a
        `dist/...` repository that does not exist, which left them unattributed
        under the repository that cut them.
        """

        row = one(
            fj_call(
                [
                    "release",
                    "create",
                    "--tag",
                    "2.7.11",
                    "--asset",
                    "dist/urllib3-2.7.11-py3-none-any.whl",
                ],
                actor="urllib3-bot",
            ),
            repository="http://forge:3800/hub/urllib3.git",
        )
        self.assertEqual(row["repo"], "hub/urllib3")
        self.assertEqual(row["metadata"]["tag"], "2.7.11")

    def test_a_create_takes_its_number_from_what_fj_announced(self) -> None:
        # The object does not exist before the call, so the output is the only
        # source. fj announces creates on stderr and wraps the number in bidi
        # isolates, so the record reads `created issue #<FSI>34<PDI>`.
        row = one(
            fj_call(
                ["issue", "create", "hub/rich", "--title", "T"],
                stderr="created issue #⁨34⁩\n",
            )
        )
        self.assertEqual(row["action"], "issue.create")
        self.assertEqual(row["metadata"], {"number": 34})

    def test_a_subcommand_outside_the_vocabulary_still_records(self) -> None:
        """No allowlist decides whether a command is worth recording.

        `issue transfer` is in no vocabulary entry. It reaches the ledger as
        `other` with its repository and command, so extending the table later
        costs nothing and loses nothing in the meantime.
        """

        row = one(fj_call(["issue", "transfer", "hub/rich#9", "hub/other"]))
        self.assertEqual(row["action"], "other")
        self.assertEqual(row["repo"], "hub/rich")
        self.assertEqual(row["cmd"], "fj issue transfer 'hub/rich#9' hub/other")

    def test_repository_lifecycle_is_recorded_under_its_own_name(self) -> None:
        row = one(fj_call(["repo", "delete", "hub/rich"]))
        self.assertEqual(row["action"], "repo.delete")

    def test_the_repository_falls_back_to_the_one_the_run_checked_out(self) -> None:
        # `fj pr create` inside the checkout names no repository.
        row = one(
            fj_call(
                ["pr", "create", "--title", "T"], stderr="created pull request #7\n"
            ),
            repository="hub/rich",
        )
        self.assertEqual(row["repo"], "hub/rich")
        self.assertEqual(row["metadata"], {"number": 7})

    def test_the_repository_fallback_normalises_clone_urls(self) -> None:
        row = one(
            fj_call(
                ["pr", "create", "--title", "T"], stderr="created pull request #7\n"
            ),
            repository="http://host.docker.internal:3000/hub/rich.git",
        )
        self.assertEqual(row["repo"], "hub/rich")

    def test_both_spellings_of_the_repo_flag_are_read(self) -> None:
        for argv in (
            ["pr", "merge", "--repo", "hub/rich", "2", "--squash"],
            ["pr", "merge", "--repo=hub/rich", "2", "--squash"],
        ):
            row = one(fj_call(argv))
            self.assertEqual(row["repo"], "hub/rich", argv)
            self.assertEqual(row["metadata"], {"number": 2, "method": "squash"}, argv)

    def test_direction_lives_in_metadata_rather_than_in_the_vocabulary(self) -> None:
        assigned = one(fj_call(["pr", "assign", "hub/rich#4", "ada", "grace"]))
        removed = one(fj_call(["pr", "unassign", "hub/rich#4", "ada"]))
        self.assertEqual(assigned["action"], removed["action"])
        self.assertEqual(
            assigned["metadata"],
            {"pr": 4, "assignees": ["ada", "grace"], "removed": False},
        )
        self.assertEqual(
            removed["metadata"], {"pr": 4, "assignees": ["ada"], "removed": True}
        )

    def test_a_failed_command_is_not_an_action(self) -> None:
        self.assertEqual(
            forge_actions.derive([fj_call(["pr", "close", "1"], exit_code=1)]), []
        )


class ForgeDerivationTests(unittest.TestCase):
    def test_a_comment_on_a_pull_is_told_apart_by_the_response(self) -> None:
        # Both go to the same endpoint; only the response says which it was.
        on_pull = one(
            forge_call(
                ["POST", "/repos/hub/rich/issues/3/comments", "-d", '{"body":"hi"}'],
                stdout='{"id":812,"pull_request_url":"http://forge/hub/rich/pulls/3"}',
            )
        )
        on_issue = one(
            forge_call(
                ["POST", "/repos/hub/rich/issues/8/comments", "-d", '{"body":"hi"}'],
                stdout='{"id":813}',
            )
        )
        self.assertEqual(on_pull["action"], "pr.comment")
        self.assertEqual(on_pull["metadata"], {"pr": 3, "comment_id": 812})
        self.assertEqual(on_issue["action"], "issue.comment")
        self.assertEqual(on_issue["metadata"], {"issue": 8, "comment_id": 813})

    def test_a_close_is_decided_by_the_request(self) -> None:
        # The response reports the object's current state, so reading it there
        # recorded every edit of an already-closed issue as another close.
        edit = one(
            forge_call(
                ["PATCH", "/repos/hub/rich/issues/5", "-d", '{"title":"renamed"}'],
                stdout='{"number":5,"state":"closed"}',
            )
        )
        close = one(
            forge_call(
                ["PATCH", "/repos/hub/rich/issues/5", "-d", '{"state":"closed"}'],
                stdout='{"number":5,"state":"closed"}',
            )
        )
        self.assertEqual(
            [edit["action"], close["action"]], ["issue.edit", "issue.close"]
        )

    def test_pull_assignment_beats_the_generic_edit_rule(self) -> None:
        row = one(
            forge_call(
                [
                    "PATCH",
                    "/repos/hub/rich/pulls/7",
                    "-d",
                    '{"assignees":["ada","grace"]}',
                ],
                stdout="{}",
            )
        )
        self.assertEqual(row["action"], "pr.assign")
        self.assertEqual(
            row["metadata"],
            {"pr": 7, "assignees": ["ada", "grace"], "removed": False},
        )

    def test_the_agent_marking_its_own_inbox_read_is_not_a_forge_action(self) -> None:
        self.assertEqual(
            forge_actions.derive([forge_call(["POST", "/notifications/threads/9"])]), []
        )

    def test_a_doubled_api_prefix_still_records(self) -> None:
        row = one(forge_call(["DELETE", "/api/v1/repos/hub/rich"]))
        self.assertEqual(row["action"], "repo.delete")

    def test_a_review_state_is_normalised_from_either_side(self) -> None:
        row = one(
            forge_call(
                [
                    "POST",
                    "/repos/hub/rich/pulls/3/reviews",
                    "-d",
                    '{"event":"APPROVE"}',
                ],
                stdout='{"id":71,"state":"APPROVED"}',
            )
        )
        self.assertEqual(
            row["metadata"], {"pr": 3, "review_id": 71, "state": "approved"}
        )

    def test_issue_and_pr_reactions_are_thread_reactions(self) -> None:
        added = one(
            forge_call(
                [
                    "POST",
                    "/repos/hub/rich/issues/7/reactions",
                    "-d",
                    '{"content":"eyes"}',
                ],
                stdout="{}",
            )
        )
        removed = one(
            forge_call(
                [
                    "DELETE",
                    "/repos/hub/rich/issues/7/reactions",
                    "-d",
                    '{"content":"+1"}',
                ],
                stdout="{}",
            )
        )

        self.assertEqual(added["action"], "thread.react")
        self.assertEqual(
            added["metadata"],
            {"number": 7, "reaction": "eyes", "removed": False},
        )
        self.assertEqual(removed["action"], "thread.react")
        self.assertEqual(
            removed["metadata"],
            {"number": 7, "reaction": "+1", "removed": True},
        )

    def test_comment_reactions_are_first_class_actions(self) -> None:
        added = one(
            forge_call(
                [
                    "POST",
                    "/repos/hub/rich/issues/comments/812/reactions",
                    "-d",
                    '{"content":"heart"}',
                ],
                stdout="{}",
            )
        )
        removed = one(
            forge_call(
                [
                    "DELETE",
                    "/repos/hub/rich/issues/comments/812/reactions",
                    "-d",
                    '{"content":"-1"}',
                ],
                stdout="{}",
            )
        )

        self.assertEqual(added["action"], "comment.react")
        self.assertEqual(
            added["metadata"],
            {"comment_id": 812, "reaction": "heart", "removed": False},
        )
        self.assertEqual(removed["action"], "comment.react")
        self.assertEqual(
            removed["metadata"],
            {"comment_id": 812, "reaction": "-1", "removed": True},
        )


class ParityTests(unittest.TestCase):
    """The same act through either wrapper lands on the same action.

    This is the property the vocabulary exists to provide: a reader should
    not need to know which binary an agent reached for. Metadata differs
    where the producers differ, which is why only the action is compared.
    """

    CASES = [
        (
            fj_call(["issue", "comment", "hub/rich#3", "body"]),
            forge_call(
                ["POST", "/repos/hub/rich/issues/3/comments", "-d", '{"body":"x"}'],
                stdout='{"id":812}',
            ),
        ),
        (
            fj_call(["pr", "comment", "hub/rich#3", "body"]),
            forge_call(
                ["POST", "/repos/hub/rich/issues/3/comments", "-d", '{"body":"x"}'],
                stdout='{"id":812,"pull_request_url":"http://forge/x/pulls/3"}',
            ),
        ),
        (
            fj_call(["pr", "merge", "hub/rich#3", "--squash"]),
            forge_call(
                [
                    "POST",
                    "/repos/hub/rich/pulls/3/merge",
                    "-d",
                    '{"merge_method":"squash"}',
                ],
                stdout="{}",
            ),
        ),
        (
            fj_call(["issue", "close", "hub/rich#3"]),
            forge_call(
                ["PATCH", "/repos/hub/rich/issues/3", "-d", '{"state":"closed"}'],
                stdout="{}",
            ),
        ),
        (
            fj_call(["repo", "delete", "hub/rich"]),
            forge_call(["DELETE", "/repos/hub/rich"], stdout="{}"),
        ),
        (
            fj_call(["repo", "unstar", "hub/rich"]),
            forge_call(["DELETE", "/user/starred/hub/rich"], stdout="{}"),
        ),
        (
            fj_call(["repo", "watch", "hub/rich"]),
            forge_call(["PUT", "/repos/hub/rich/subscription"], stdout="{}"),
        ),
    ]

    def test_the_two_producers_agree_on_the_action(self) -> None:
        for through_fj, through_forge in self.CASES:
            left, right = one(through_fj), one(through_forge)
            self.assertIsNotNone(left, through_fj["argv"])
            self.assertIsNotNone(right, through_forge["argv"])
            self.assertEqual(
                left["action"],
                right["action"],
                (through_fj["argv"], through_forge["argv"]),
            )
            self.assertEqual(left["repo"], right["repo"])


class EnvelopeTests(unittest.TestCase):
    def test_the_envelope_is_the_same_shape_from_either_producer(self) -> None:
        keys = {"ts", "source", "actor", "repo", "action", "metadata", "cmd"}
        for call in (
            fj_call(["issue", "close", "hub/rich#3"]),
            forge_call(["DELETE", "/repos/hub/rich"], stdout="{}"),
        ):
            row = one(call)
            self.assertEqual(set(row), keys)
            self.assertEqual(row["source"], call["tool"])
            self.assertEqual(row["actor"], "rich-bot")

    def test_the_command_is_redacted_against_the_runs_secret_list(self) -> None:
        """Redaction is possible here and was not possible in the sandbox.

        Scrubbing inside the container required shipping every secret in as
        one environment variable. The host already holds the list, so the
        command can be kept in full and cleaned on the way out.
        """

        row = one(
            forge_call(
                [
                    "POST",
                    "/repos/hub/rich/issues",
                    "--user-agent",
                    "tok-abcdefgh",
                ],
                stdout='{"number":12}',
            ),
            secrets=("tok-abcdefgh",),
        )
        self.assertNotIn("tok-abcdefgh", row["cmd"])
        self.assertIn("[REDACTED]", row["cmd"])

    def test_a_long_or_multiline_value_is_elided(self) -> None:
        row = one(fj_call(["issue", "comment", "hub/rich#1", "--body", "a\nb"]))
        self.assertEqual(row["cmd"], "fj issue comment 'hub/rich#1' --body <...>")

    def test_sensitive_payload_header_and_body_values_are_elided(self) -> None:
        cases = [
            (
                forge_call(
                    [
                        "POST",
                        "/repos/hub/rich/issues",
                        "-d",
                        '{"title":"short"}',
                    ],
                    stdout='{"number":12}',
                ),
                "forge POST /repos/hub/rich/issues -d <...>",
            ),
            (
                forge_call(
                    [
                        "POST",
                        "/repos/hub/rich/issues",
                        "--header",
                        "X-Trace: abc",
                        "--data",
                        '{"title":"short"}',
                    ],
                    stdout='{"number":12}',
                ),
                "forge POST /repos/hub/rich/issues --header <...> --data <...>",
            ),
            (
                fj_call(["issue", "comment", "hub/rich#1", "--body", "ok"]),
                "fj issue comment 'hub/rich#1' --body <...>",
            ),
        ]
        for call, expected in cases:
            with self.subTest(call=call["argv"]):
                self.assertEqual(one(call)["cmd"], expected)


class RecordingTests(unittest.TestCase):
    def test_calls_are_read_oldest_first_and_bad_files_are_skipped(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / "call-b").write_text(
                json.dumps(
                    fj_call(["issue", "close", "hub/rich#2"])
                    | {"ts": "2026-01-02T00:00:00Z"}
                ),
                encoding="utf-8",
            )
            (directory / "call-a").write_text(
                json.dumps(
                    fj_call(["issue", "close", "hub/rich#1"])
                    | {"ts": "2026-01-01T00:00:00Z"}
                ),
                encoding="utf-8",
            )
            (directory / "call-c").write_text("truncated{", encoding="utf-8")

            rows = forge_actions.derive(forge_actions.read_calls(directory))
            self.assertEqual([row["metadata"]["number"] for row in rows], [1, 2])

    def test_a_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(forge_actions.read_calls(Path("/nonexistent")), [])


if __name__ == "__main__":
    unittest.main()
