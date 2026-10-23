"""
A run's forge actions, and the session each one belongs to.

The runner keeps a ledger of the forge writes each session made
(`forge_actions` in the evidence store); when it has rows, the events are
its rows. A checkpoint whose ledger is empty still has the forge's own
record of what happened, and the events are derived from it, named with the
hub's action vocabulary (`services/runner/src/swecraft_runner/forge_actions.yaml`):

- a pull request opened, merged or closed: pr.create, pr.merge, pr.close;
- an issue opened or closed: issue.create, issue.close;
- a comment: issue.comment or pr.comment;
- a review: pr.review (a review request is not a review);
- a release an agent published during the run: release.create.

Forge user names are agent names, so the author of a record is the agent
that acted; a record by anyone else (an operator, an imported upstream tag)
is not an action of the society. Reopens, title changes, references and
pushes stay in the thread timelines and are not events.

A derived event belongs to the session that opened the pull request when
the evidence names it, else to the acting agent's session running at the
time, with the analyzer's few seconds of allowance for the two clocks.
"""

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

# Seconds of clock difference allowed between the coordinator's session times
# and the forge's timestamps, as the analyzer's OPENER_SLACK_SECONDS.
SLACK_SECONDS = 5

# Ties at the same second keep cause before effect: a thread opens before it
# is commented on, and is commented on before it is merged or closed.
_RANK = {"create": 0, "comment": 1, "review": 1, "close": 2, "merge": 2}


def stamp(value: Any) -> str | None:
    """An ISO-8601 UTC time to the second, ending in Z; None when absent or unreadable."""

    if value is None or value == "":
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class Sessions:
    """Each agent's sessions, to find the one running at a moment."""

    def __init__(self, rows: Iterable[tuple[str, str, str | None, str | None]]) -> None:
        """`rows` are (session id, agent, started, finished); a session without a start is never found.

        A session without an end covers only its start: a failed attempt that
        never ran the agent has no end, and must not take the agent's later
        actions.
        """

        self.windows: dict[str, list[tuple[float, float, str]]] = {}
        for session, agent, started, finished in rows:
            if started is None:
                continue
            start = epoch(started)
            end = epoch(finished) if finished is not None else start
            self.windows.setdefault(agent, []).append((start, end, session))

    def at(self, agent: str, when: str) -> str | None:
        """The agent's session running at `when`: of those whose [started, finished]
        contains it, the latest to start."""

        moment = epoch(when)
        running = [
            (start, session)
            for start, end, session in self.windows.get(agent, [])
            if start - SLACK_SECONDS <= moment <= end + SLACK_SECONDS
        ]
        return max(running)[1] if running else None


def _event(
    t: str, agent: str, repo: str, action: str, *, ref: int | None = None, tag: str | None = None, text: str | None = None
) -> dict[str, Any]:
    return {"t": t, "session": None, "agent": agent, "repo": repo, "action": action, "ref": ref, "tag": tag, "text": text}


def from_forge(
    records: Mapping[str, list[Mapping[str, Any]]],
    agents: set[str],
    sessions: Sessions,
    since: str | None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """(events oldest first, records left out by reason).

    `records` holds the evidence store's `pulls`, `issues`, `comments`,
    `reviews` and `releases`, with repositories named as in the world. An
    event's `text` is what it carried, unredacted; the exporter scrubs it.
    """

    found: list[tuple[str, int, int, dict[str, Any]]] = []
    skipped: Counter[str] = Counter()

    def add(rank: str, seq: int, event: dict[str, Any], opened: str | None = None) -> None:
        if event["agent"] not in agents:
            skipped[f"{event['action']} by a user who is not an agent"] += 1
            return
        event["session"] = opened or sessions.at(event["agent"], event["t"])
        found.append((event["t"], _RANK.get(rank, 3), seq, event))

    for seq, pull in enumerate(records.get("pulls", [])):
        created = stamp(pull["created_at"])
        if created is None:
            skipped["pull request without a creation time"] += 1
            continue
        add(
            "create",
            seq,
            _event(created, pull["author"], pull["repo"], "pr.create", ref=pull["number"], text=pull["body"]),
            pull.get("opened_run_id"),
        )
        merged = stamp(pull["merged_at"]) if pull["merged"] else None
        if merged is not None and pull["merger"]:
            add("merge", seq, _event(merged, pull["merger"], pull["repo"], "pr.merge", ref=pull["number"]))

    for seq, issue in enumerate(records.get("issues", [])):
        created = stamp(issue["created_at"])
        if created is None:
            skipped["issue without a creation time"] += 1
            continue
        add("create", seq, _event(created, issue["author"], issue["repo"], "issue.create", ref=issue["number"], text=issue["body"]))

    for comment in records.get("comments", []):
        at = stamp(comment["created_at"])
        if at is None:
            continue
        kind = "pr" if comment["is_pull"] else "issue"
        if comment["kind"] == "comment":
            event = _event(at, comment["author"], comment["repo"], f"{kind}.comment", ref=comment["number"], text=comment["body"])
            add("comment", comment["id"], event)
        elif comment["kind"] == "close":
            add("close", comment["id"], _event(at, comment["author"], comment["repo"], f"{kind}.close", ref=comment["number"]))

    for review in records.get("reviews", []):
        at = stamp(review["created_at"])
        if at is None or review["kind"] == "request":
            continue
        add("review", review["id"], _event(at, review["reviewer"], review["repo"], "pr.review", ref=review["number"], text=review["body"]))

    for seq, release in enumerate(records.get("releases", [])):
        at = stamp(release["created_at"])
        if release["is_tag"] or release["is_draft"]:
            continue
        if at is None or (since is not None and at < since):
            skipped["release from before the run"] += 1
            continue
        event = _event(at, release["author"], release["repo"], "release.create", tag=release["tag"], text=release.get("notes"))
        add("release", seq, event)

    found.sort(key=lambda item: item[:3])
    return [event for *_, event in found], skipped


def from_ledger(
    rows: Iterable[Mapping[str, Any]],
    texts: Mapping[tuple[str, str, Any], str | None],
) -> list[dict[str, Any]]:
    """Events from the runner's ledger, oldest first.

    `texts` finds what an action carried in the forge's record, keyed as the
    hub keys its sidecar of bodies: ("comment", repo, comment id), ("issue" or
    "pull", repo, number), ("review", repo, review id), ("release", repo, tag).
    """

    events = []
    for row in rows:
        at = stamp(row["ts"])
        if at is None or not row["agent"]:
            continue
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except ValueError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        action, repo = str(row["action"]), str(row["repo"])
        tag = metadata.get("tag") if action.startswith("release.") else None
        key: tuple[str, str, Any] | None = None
        if action in ("issue.comment", "pr.comment"):
            key = ("comment", repo, metadata.get("comment_id"))
        elif action == "issue.create":
            key = ("issue", repo, row["number"])
        elif action == "pr.create":
            key = ("pull", repo, row["number"])
        elif action == "pr.review":
            key = ("review", repo, metadata.get("review_id"))
        elif action == "release.create":
            key = ("release", repo, tag)
        event = _event(
            at,
            str(row["agent"]),
            repo,
            action,
            ref=row["number"],
            tag=tag if isinstance(tag, str) else None,
            text=texts.get(key) if key else None,
        )
        event["session"] = row["run_id"]
        events.append(event)
    events.sort(key=lambda event: event["t"])
    return events
