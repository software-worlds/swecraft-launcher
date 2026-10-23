"""Pure helpers for the Antigravity CLI arm; ``SwecraftAntigravityCli`` uses them.

Kept free of Harbor imports, like :mod:`swecraft_runner.pi`, so they are
unit-tested in the runner's own environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Where Harbor's antigravity-cli agent tees the CLI's stdout and stderr.
CLI_OUTPUT_NAME = "antigravity-cli.txt"
# The CLI's headless output formats; SwecraftAntigravityCli asks for the JSON
# run summary, the only place the CLI reports token counters.
OUTPUT_FORMATS = ("text", "json", "stream-json")
DEFAULT_OUTPUT_FORMAT = "json"


@dataclass(frozen=True)
class CliUsage:
    """Token counters from the CLI's run summary.

    The CLI counts a request's prompt as the tokens it *sent*
    (``input_tokens``) plus the tokens the model served from cache
    (``cache_read_tokens``), two disjoint numbers, unlike Harbor's
    ``n_input_tokens``, which is the whole prompt with ``n_cache_tokens`` as
    its cached part. Verified on agy 1.2.6 in stream-json mode: one request
    reported ``input_tokens`` 1892 with ``cache_read_tokens`` 7238 against a
    prompt of about 9,100 tokens, and the run summary is the per-request
    sum. ``output_tokens`` includes thinking (``total_tokens`` is
    ``input_tokens`` plus ``output_tokens``).
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    thinking_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """The whole prompt: Harbor's ``n_input_tokens``."""

        return self.input_tokens + self.cache_read_tokens


def usage_from_cli_output(text: str) -> CliUsage | None:
    """Read the run summary the CLI prints with ``--output-format json``.

    The CLI's stdout and stderr are teed into one file, so the summary is
    found by scanning lines from the end for the last JSON object carrying
    a ``usage`` block: the summary itself (``json``) or the ``result`` event
    (``stream-json``; a step's usage is not a run total and is skipped).
    Returns ``None`` when there is none, typically a run the trial timeout
    cut before the CLI could print it, which leaves the counters unknown
    rather than zero.
    """

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if "event" in record:
            if record["event"] != "result":
                continue
            record = record.get("result")
            if not isinstance(record, dict):
                continue
        usage = record.get("usage")
        if isinstance(usage, dict):
            return CliUsage(
                input_tokens=_count(usage.get("input_tokens")),
                output_tokens=_count(usage.get("output_tokens")),
                cache_read_tokens=_count(usage.get("cache_read_tokens")),
                thinking_tokens=_count(usage.get("thinking_tokens")),
            )
    return None


def _count(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
