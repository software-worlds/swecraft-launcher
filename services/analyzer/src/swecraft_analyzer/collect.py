"""Collect: one checkpoint directory in, one evidence store out.

The analysis directory receives ``evidence.db`` (see ``store.SCHEMA``), the
unpacked forge under ``forge/``, and ``collect.json``: the counts per table
and what could not be read. Nothing in the checkpoint is modified, so the
checkpoint can sit on a read-only mount.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swecraft_analyzer import __version__, store
from swecraft_analyzer.checkpoint import AnalyzerError, Checkpoint, extract_forge
from swecraft_analyzer.contamination import future_history, world_repositories
from swecraft_analyzer.forge import PATCH_LIMIT, Forge
from swecraft_analyzer.transcripts import read_live_run, read_run

_MEMORY_OWNER = re.compile(r"/([^/]+)/[^/]+?(?:\.git)?$")


def _receipt(output: Path) -> dict[str, Any]:
    try:
        payload = json.loads((output / "result.json").read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _agent_from_receipt(receipt: dict[str, Any], owners: dict[str, str]) -> str | None:
    """The agent of a run the ledger never booked (each agent's bootstrap).

    A run with a memory repository belongs to that repository's owner (the
    agent's home); otherwise the repository it worked on names its maintainer
    through the coordinator's agent-to-repository table.
    """

    for field in ("memory_repository", "repository"):
        match = _MEMORY_OWNER.search(str(receipt.get(field) or ""))
        if not match:
            continue
        owner, name = match.group(1), match.group(0).rsplit("/", 1)[-1]
        name = name.removesuffix(".git")
        if owner in owners.values():
            return owner
        if name in owners:
            return owners[name]
    return None


def _forge_actions(output: Path) -> list[dict[str, Any]]:
    path = output / "forge-actions.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _number(metadata: Any) -> int | None:
    if not isinstance(metadata, dict):
        return None
    for key in ("number", "issue", "index"):
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _epoch(stamp: Any) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Seconds of clock difference allowed between the coordinator's wake times
# and the forge's record of when a pull request was opened.
OPENER_SLACK_SECONDS = 5


def _openers(
    pulls: list[dict[str, Any]],
    wake_rows: list[dict[str, Any]],
    action_rows: list[dict[str, Any]],
) -> None:
    """Attribute each pull request to the wake that opened it, in place.

    The forge records who opened a pull request and when; the opening wake is
    that agent's wake running at the time. The runner's ledger names the same
    wake for each ``pr.create`` it booked, but it misses pull requests opened
    through the raw API and files some under the agent's home repository, so
    it only fills what the forge's own record cannot.
    """

    windows: dict[str, list[tuple[float, float | None, Any, Any]]] = {}
    for wake in wake_rows:
        start = _epoch(wake.get("started"))
        if wake.get("agent") and start is not None:
            windows.setdefault(str(wake["agent"]), []).append(
                (start, _epoch(wake.get("finished")), wake["wake_id"], wake["run_id"])
            )
    ledger: dict[tuple[Any, Any], tuple[Any, Any]] = {}
    for row in action_rows:
        if row["action"] == "pr.create" and row["number"] is not None:
            ledger.setdefault(
                (row["repo"], row["number"]), (row["wake_id"], row["run_id"])
            )
    slack = OPENER_SLACK_SECONDS
    for pull in pulls:
        created = _epoch(pull.get("created_at"))
        running = [
            window
            for window in windows.get(str(pull.get("author")), [])
            if created is not None
            and window[0] - slack <= created
            and (window[1] is None or created <= window[1] + slack)
        ]
        if running:
            latest = max(running, key=lambda window: window[0])
            pull["opened_wake_id"], pull["opened_run_id"] = latest[2], latest[3]
        else:
            found = ledger.get((pull["repo"], pull["number"]), (None, None))
            pull["opened_wake_id"], pull["opened_run_id"] = found


def collect(
    checkpoint_dir: Path,
    out: Path,
    *,
    forge: bool = True,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the evidence store; ``runs_dir`` (the cell's live ``.swecraft/runs``)
    supplies transcripts for runs whose checkpoint projection kept none."""

    checkpoint = Checkpoint.open(checkpoint_dir)
    out.mkdir(parents=True, exist_ok=True)
    building = out / "evidence.db.building"
    connection = store.create(building)
    problems: list[str] = []
    counts: dict[str, int] = {}

    wakes, agent_repos = checkpoint.coordinator_rows()
    if not wakes:
        problems.append(
            "coordinator ledger missing or empty: wakes come from receipts only"
        )
    by_run = {str(wake.get("run_id")): wake for wake in wakes if wake.get("run_id")}
    owners = {
        str(row["repo"]).rsplit("/", 1)[-1]: str(row["agent"]) for row in agent_repos
    }

    wake_rows: list[dict[str, Any]] = []
    call_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    transcripts: dict[str, int] = {}
    for run_id, output in checkpoint.run_outputs().items():
        receipt = _receipt(output)
        wake = by_run.get(run_id, {})
        agent = wake.get("agent") or _agent_from_receipt(receipt, owners)
        usage = receipt.get("usage") if isinstance(receipt.get("usage"), dict) else {}
        source, calls = read_run(output)
        if source == "none" and runs_dir is not None:
            source, calls = read_live_run(runs_dir / run_id)
        transcripts[source] = transcripts.get(source, 0) + 1
        response = output / "response.md"
        wake_rows.append(
            {
                "wake_id": wake.get("id"),
                "run_id": run_id,
                "agent": agent,
                "day": wake.get("day"),
                "started": wake.get("started"),
                "finished": wake.get("finished"),
                "status": wake.get("status") or receipt.get("status"),
                # Runs the ledger never booked (each agent's bootstrap) keep
                # their transcript but carry no wake id.
                "outcome": wake.get("outcome") if wake else "unledgered",
                "summary": wake.get("summary"),
                "cost_usd": wake.get("cost_usd", usage.get("cost_usd")),
                "error": wake.get("error"),
                "harness": receipt.get("agent"),
                "model": receipt.get("model"),
                "transcript": source,
                "tool_calls": len(calls),
                "response": response.read_text(encoding="utf-8", errors="replace")
                if response.is_file()
                else None,
            }
        )
        for call in calls:
            call_rows.append(
                {
                    "wake_id": wake.get("id"),
                    "run_id": run_id,
                    "agent": agent,
                    "step": call.step,
                    "call": call.call,
                    "tool": call.tool,
                    "kind": call.kind,
                    "text": call.text,
                    "path": call.path,
                    "output": call.output,
                }
            )
        for row in _forge_actions(output):
            action_rows.append(
                {
                    "wake_id": wake.get("id"),
                    "run_id": run_id,
                    "agent": agent,
                    "ts": row.get("ts"),
                    "repo": row.get("repo"),
                    "action": row.get("action"),
                    "number": _number(row.get("metadata")),
                    "metadata": json.dumps(row.get("metadata"), sort_keys=True),
                    "cmd": row.get("cmd"),
                }
            )
    counts["wakes"] = store.insert(connection, "wakes", wake_rows)
    counts["tool_calls"] = store.insert(connection, "tool_calls", call_rows)
    counts["forge_actions"] = store.insert(connection, "forge_actions", action_rows)
    counts["agents"] = store.insert(connection, "agents", agent_repos)
    missing_runs = sorted(
        str(wake["run_id"])
        for wake in wakes
        if wake.get("run_id") and str(wake["run_id"]) not in checkpoint.run_outputs()
    )
    if missing_runs:
        problems.append(f"{len(missing_runs)} ledger wakes have no captured run")

    heads = {head["repo"]: head for head in checkpoint.heads()}
    # The world's organisation, from its lock; checkpoints from before the
    # rename name it `hub`, as their locks do.
    lock_text = checkpoint.apparatus_file("world.lock.json")
    try:
        org = str((json.loads(lock_text) if lock_text else {}).get("org") or "hub")
    except ValueError:
        org = "hub"
    repo_rows: dict[str, dict[str, Any]] = {}
    for name, head in heads.items():
        repo_rows[f"{org}/{name}"] = {
            "repo": f"{org}/{name}",
            "default_branch": None,
            "seed": head["seed"],
            "head": head["head"],
            "is_world": 1,
        }

    if forge and checkpoint.forge_tar.is_file():
        try:
            reader = Forge(extract_forge(checkpoint.forge_tar, out / "forge"))
        except (OSError, FileNotFoundError) as error:
            problems.append(f"forge unreadable: {error}")
            reader = None
        if reader is not None:
            for repository in reader.repositories():
                row = repo_rows.setdefault(
                    repository["repo"],
                    {
                        "repo": repository["repo"],
                        "seed": None,
                        "head": None,
                        "is_world": 0,
                    },
                )
                row["default_branch"] = repository["default_branch"]
            pulls, files, patches = [], [], []
            for pull in reader.pulls():
                changed, patch, error = reader.diff(
                    pull["repo"], pull["merge_base"], pull["head_commit"]
                )
                pull["additions"] = sum(item["additions"] or 0 for item in changed)
                pull["deletions"] = sum(item["deletions"] or 0 for item in changed)
                pull["files"] = len(changed)
                pull["diff_error"] = error
                pulls.append(pull)
                files.extend(
                    {"repo": pull["repo"], "number": pull["number"], **item}
                    for item in changed
                )
                patches.append(
                    {
                        "repo": pull["repo"],
                        "number": pull["number"],
                        "patch": patch[:PATCH_LIMIT],
                        "truncated": int(len(patch) > PATCH_LIMIT),
                    }
                )
            diff_errors = sum(1 for pull in pulls if pull["diff_error"])
            if diff_errors:
                problems.append(f"{diff_errors} pull requests have no diff")
            _openers(pulls, wake_rows, action_rows)
            unopened = sum(1 for pull in pulls if pull["opened_run_id"] is None)
            if unopened:
                problems.append(f"{unopened} pull requests have no opening wake")
            counts["pulls"] = store.insert(connection, "pulls", pulls)
            counts["pull_files"] = store.insert(connection, "pull_files", files)
            counts["pull_patches"] = store.insert(connection, "pull_patches", patches)
            counts["issues"] = store.insert(connection, "issues", reader.issues_only())
            counts["comments"] = store.insert(connection, "comments", reader.comments())
            counts["reviews"] = store.insert(connection, "reviews", reader.reviews())
            counts["content_edits"] = store.insert(
                connection, "content_edits", reader.content_edits()
            )
            counts["releases"] = store.insert(connection, "releases", reader.releases())
            counts["ci_runs"] = store.insert(connection, "ci_runs", reader.ci_runs())
            counts["ci_schedules"] = store.insert(
                connection, "ci_schedules", reader.ci_schedules()
            )
            reader.close()
            lock_text = checkpoint.apparatus_file("world.lock.json")
            if lock_text:
                tags, commits, lines = [], [], []
                for repo, bare, pin, baseline in world_repositories(
                    json.loads(lock_text), out / "forge" / "git" / "repositories"
                ):
                    found = future_history(bare, repo, pin, baseline)
                    tags += found[0]
                    commits += found[1]
                    lines += found[2]
                counts["future_tags"] = store.insert(connection, "future_tags", tags)
                counts["future_commits"] = store.insert(
                    connection, "future_commits", commits
                )
                counts["future_lines"] = store.insert(connection, "future_lines", lines)
                if tags:
                    problems.append(
                        f"world carries {len(tags)} upstream tags from after its pin "
                        f"({len(commits)} commits): see future_tags"
                    )
            else:
                problems.append("no world.lock.json: future history not checked")
    elif forge:
        problems.append("checkpoint has no forge volume: forge tables are empty")
    counts["repos"] = store.insert(connection, "repos", repo_rows.values())

    meta = {
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_path": str(checkpoint.root),
        "runs_dir": str(runs_dir.resolve()) if runs_dir is not None else None,
        "world_id": checkpoint.manifest.get("world_id"),
        "checkpoint_created_at": checkpoint.manifest.get("created_at"),
        "hub_commit": checkpoint.manifest.get("hub_commit"),
        "forge_url": checkpoint.forge_url,
        "agent_image": json.dumps(checkpoint.manifest.get("agent_image")),
        # The ledger's completed wakes, as ``StateStore.completed_wakes``
        # counts them.
        "wakes_completed": str(
            sum(1 for wake in wakes if wake.get("status") == "completed")
        ),
        "collected_at": datetime.now(UTC).isoformat(),
        "analyzer_version": __version__,
    }
    for name in ("world.yaml", "evaluation.yaml", "coordinator.yaml"):
        text = checkpoint.apparatus_file(name)
        if text is not None:
            meta[f"apparatus/{name}"] = text
    store.insert(connection, "meta", ({"key": k, "value": v} for k, v in meta.items()))
    connection.commit()
    connection.close()
    building.replace(out / "evidence.db")

    summary = {
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_path": str(checkpoint.root),
        "collected_at": meta["collected_at"],
        "analyzer_version": __version__,
        "counts": counts,
        "transcripts": transcripts,
        "problems": problems,
    }
    (out / "collect.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


__all__ = ["AnalyzerError", "collect"]
