"""Pure helpers for the Antigravity CLI arm."""

from __future__ import annotations

import json
import unittest

from swecraft_runner import antigravity


class UsageFromCliOutputTests(unittest.TestCase):
    def test_reads_the_json_summary_and_maps_it_onto_harbor_counters(self) -> None:
        summary = {
            "conversation_id": "c",
            "status": "SUCCESS",
            "response": "DONE\n",
            "duration_seconds": 24.8,
            "num_turns": 1,
            "usage": {
                "input_tokens": 19300,
                "output_tokens": 188,
                "thinking_tokens": 0,
                "cache_read_tokens": 7238,
                "total_tokens": 19488,
            },
        }

        usage = antigravity.usage_from_cli_output(json.dumps(summary) + "\n")

        assert usage is not None
        self.assertEqual(
            (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens),
            (19300, 188, 7238),
        )
        # Sent and cached are disjoint in the CLI's count; Harbor's
        # n_input_tokens is the whole prompt.
        self.assertEqual(usage.prompt_tokens, 26538)

    def test_finds_the_summary_among_teed_stderr_lines(self) -> None:
        text = (
            "Loading credentials...\n"
            + json.dumps({"usage": {"input_tokens": 5, "output_tokens": 2}})
            + "\nwarning: printed after the summary\n{not json\n"
        )

        usage = antigravity.usage_from_cli_output(text)

        assert usage is not None
        self.assertEqual(
            (
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.thinking_tokens,
            ),
            (5, 2, 0, 0),
        )

    def test_stream_json_takes_the_result_event_not_a_step(self) -> None:
        step = {
            "event": "step_update",
            "step_update": {"usage": {"input_tokens": 8584, "output_tokens": 60}},
        }
        result = {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "usage": {
                    "input_tokens": 19300,
                    "output_tokens": 188,
                    "cache_read_tokens": 7238,
                },
            },
        }
        text = "\n".join(json.dumps(r) for r in ({"event": "init"}, step, result))

        usage = antigravity.usage_from_cli_output(text + "\n")

        assert usage is not None
        self.assertEqual(usage.input_tokens, 19300)
        # Cut before the result: a step's usage is not a run total.
        self.assertIsNone(antigravity.usage_from_cli_output(json.dumps(step) + "\n"))

    def test_no_summary_means_unknown(self) -> None:
        self.assertIsNone(antigravity.usage_from_cli_output(""))
        self.assertIsNone(antigravity.usage_from_cli_output("DONE\n"))
        self.assertIsNone(
            antigravity.usage_from_cli_output(json.dumps({"status": "ERROR"}) + "\n")
        )
        self.assertIsNone(antigravity.usage_from_cli_output('{"usage": "n/a"}\n'))
        self.assertIsNone(antigravity.usage_from_cli_output("[1, 2]\n"))

    def test_counters_are_non_negative_integers(self) -> None:
        text = json.dumps(
            {
                "usage": {
                    "input_tokens": -3,
                    "output_tokens": "7",
                    "cache_read_tokens": 2.0,
                    "thinking_tokens": None,
                }
            }
        )

        usage = antigravity.usage_from_cli_output(text)

        assert usage is not None
        self.assertEqual(
            (
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.thinking_tokens,
            ),
            (0, 7, 2, 0),
        )
