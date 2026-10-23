"""Evaluator measurements for the read API: where they are, and the views.

The evaluator owns the numbers. Everything here is a slice of
:func:`swecraft_evaluator.report.build_report` joined with what only this side
knows: the coordinator ledger (how many wakes had finished when a reading
started, and what they had cost) and the agent bindings (which wake cut a
release). The report is cached on the set of record ids — a record
directory is complete once ``record.json`` exists and is never rewritten,
so the id set plus the manifest's mtime is the whole cache key.

Two layouts are recognised without configuration: a live checkout
(``evaluation.yaml`` beside ``.swecraft/evaluator/``) and a loaded checkpoint
(``apparatus/evaluation.yaml`` beside ``evaluations/``), so the same routes
read a running campaign and an archived one.
"""

# Vendored from the read API this repository grew out of (its evaluation
# views and its read-only state store), which is not part of this release,
# for the evaluation exporter (evaluation.py) here.

from __future__ import annotations

import bisect
import json
import os
import re
import sqlite3
import statistics
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import CoordinatorConfig
from swecraft_coordinator.state import StateStore
from swecraft_evaluator.manifest import Manifest, load_manifest
from swecraft_evaluator.report import build_report
from swecraft_evaluator.score import load_records

# (manifest, records, loop state), relative to an instance root.
LAYOUTS = (
    ("evaluation.yaml", ".swecraft/evaluator/records", ".swecraft/evaluator/state.json"),
    ("apparatus/evaluation.yaml", "evaluations", "state/evaluator/state.json"),
)
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SUMMARY_WAKE_LIMIT = 200


@dataclass(frozen=True)
class EvaluationSource:
    """Where one simulation's measurements live."""

    manifest_path: Path
    records_dir: Path
    state_path: Path

    @classmethod
    def discover(cls, root: Path, raw: object = None) -> EvaluationSource | None:
        """Explicit paths from a manifest entry, or a recognised layout, or None."""

        def rooted(value: object, default: str) -> Path:
            path = Path(str(value or default)).expanduser()
            return path if path.is_absolute() else root / path

        if raw is not None:
            if not isinstance(raw, dict):
                raise CoordinatorError("evaluation must be a mapping")
            unknown = set(raw) - {"manifest", "records", "state"}
            if unknown:
                raise CoordinatorError(
                    f"unknown evaluation keys: {', '.join(sorted(unknown))}"
                )
            manifest, records, state = LAYOUTS[0]
            return cls(
                manifest_path=rooted(raw.get("manifest"), manifest),
                records_dir=rooted(raw.get("records"), records),
                state_path=rooted(raw.get("state"), state),
            )
        for manifest, records, state in LAYOUTS:
            if (root / manifest).is_file():
                return cls(root / manifest, root / records, root / state)
        return None


class EvaluationReader:
    """The derived report for one source, rebuilt only when records arrive."""

    def __init__(self, source: EvaluationSource) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._key: tuple[int, tuple[str, ...]] | None = None
        self._manifest: Manifest | None = None
        self._report: dict[str, Any] | None = None
        # Release record id -> the wake that cut it. A match never changes
        # once found (the wake is over and the tag is immutable), so it is
        # kept for the reader's lifetime; misses are retried on every read.
        self.wakes: dict[str, dict[str, Any]] = {}
        # (repo, tag) -> run_id from the action ledger; built once per reader.
        self.release_ledger: dict[tuple[str, str], str] | None = None

    def record_ids(self) -> tuple[str, ...]:
        try:
            entries = os.scandir(self.source.records_dir)
        except OSError:
            return ()
        with entries:
            return tuple(
                sorted(
                    entry.name
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                    and (Path(entry.path) / "record.json").is_file()
                )
            )

    def _cache_key(self) -> tuple[int, tuple[str, ...]]:
        try:
            stamp = self.source.manifest_path.stat().st_mtime_ns
        except OSError:
            stamp = -1
        return stamp, self.record_ids()

    def manifest(self) -> Manifest:
        self.report()
        assert self._manifest is not None
        return self._manifest

    def report(self) -> dict[str, Any]:
        key = self._cache_key()
        with self._lock:
            if self._report is None or key != self._key:
                manifest = load_manifest(self.source.manifest_path)
                records = load_records(self.source.records_dir)
                self._manifest = manifest
                self._report = build_report(manifest, records)
                self._key = key
                # A new release row can only appear when a new record lands,
                # which is exactly this rebuild -- so the run ledger derived
                # for the previous rows is stale now, not merely older. A
                # process-lifetime ledger left live experiments attributing
                # new releases by summary guesswork until a restart.
                self.release_ledger = None
            return self._report

    def record(self, record_id: str) -> dict[str, Any] | None:
        if not _RECORD_ID.fullmatch(record_id):
            return None
        path = self.source.records_dir / record_id / "record.json"
        if path.is_symlink() or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def loop_state(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.source.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        releases = payload.get("releases")
        return {
            "last_sweep": payload.get("last_sweep")
            if isinstance(payload.get("last_sweep"), str)
            else None,
            "releases": {
                str(repo): str(tag)
                for repo, tag in (
                    releases.items() if isinstance(releases, dict) else ()
                )
            },
            "releases_initialized": bool(payload.get("releases_initialized")),
        }


class Timeline:
    """The ledger's completed wakes, answering "how far along was the campaign".

    ``at(iso)`` counts the wakes that had finished before a reading started;
    a bisect over one sorted list, so a series of any length costs one query.
    """

    def __init__(self, rows: list[tuple[str, str | None, float]]) -> None:
        self._finished: list[datetime] = []
        self._worked: list[int] = []
        self._cost: list[float] = []
        worked = 0
        cost = 0.0
        for finished, outcome, cost_usd in rows:
            moment = _moment(finished)
            if moment is None:
                continue
            worked += 1 if outcome == "worked" else 0
            cost += cost_usd
            self._finished.append(moment)
            self._worked.append(worked)
            self._cost.append(cost)

    def at_step(self, step: int) -> dict[str, Any]:
        """The campaign's position after its ``step``-th completed wake.

        A script-labelled reading measured the world *as of* that wake, so it
        belongs there — not at the wake count of the moment the script ran,
        which may be thousands later. ``world_at`` is when that wake finished.
        """

        index = max(0, min(step, len(self._finished)))
        return {
            "wakes_completed": step,
            "wakes_worked": self._worked[index - 1] if index else 0,
            "cost_usd": self._cost[index - 1] if index else 0.0,
            "world_at": self._finished[index - 1].isoformat() if index else None,
        }

    def at(self, iso: str | None) -> dict[str, Any]:
        moment = _moment(iso)
        if moment is None or not self._finished:
            return {"wakes_completed": None, "wakes_worked": None, "cost_usd": None}
        index = bisect.bisect_right(self._finished, moment)
        return {
            "wakes_completed": index,
            "wakes_worked": self._worked[index - 1] if index else 0,
            "cost_usd": self._cost[index - 1] if index else 0.0,
        }


def _moment(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def cadence_evaluation(report: dict[str, Any]) -> str | None:
    """The battery the sweeps run on: most ``world@latest`` readings wins."""

    best: str | None = None
    most = -1
    for name, summary in report["evaluations"].items():
        count = summary["readings"]["latest"] + summary["readings"]["other"]
        if count > most:
            best, most = name, count
    return best


def _latest_point(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The campaign's newest reading: the last sweep of the heads.

    A script-labelled cutoff (a backfill of an early wake) may well be the
    most recently *measured* record; it is never the latest world.
    """

    if not points:
        return None
    sweeps = [point for point in points if point["kind"] == "latest"]
    point = (sweeps or points)[-1]
    return {
        "id": point["id"],
        "started": point["started"],
        "complete": point["complete"],
        # Consumers, not tasks: cachecontrol's three red tasks are one gate.
        "gates_red": len({ref.partition("@")[0] for ref in point["gated_tasks"]}),
        "geomean": point["aggregates"]["geomean"],
        "geomean_exposed": point["aggregates"]["geomean_exposed"],
        "geomean_adoption": point["aggregates"]["geomean_adoption"],
        "geomean_exposed_adoption": point["aggregates"]["geomean_exposed_adoption"],
        "movers_up": len(point["movers_up"]),
        "movers_down": len(point["movers_down"]),
    }


def summary_view(reader: EvaluationReader, state: StateStore) -> dict[str, Any]:
    report = reader.report()
    floors = [
        task["floor"] for task in report["tasks"].values() if task["floor"] is not None
    ]
    return {
        "manifest": str(reader.source.manifest_path),
        "records_dir": str(reader.source.records_dir),
        "cadence_evaluation": cadence_evaluation(report),
        "evaluations": [
            {
                "name": summary["name"],
                "metric": summary["metric"],
                "tasks": len(summary["tasks"]),
                "exposed_tasks": (
                    len(summary["exposed_tasks"])
                    if summary["exposed_tasks"] is not None
                    else None
                ),
                "exposure_source": summary["exposure_source"],
                "ambient_subjects": summary["ambient_subjects"],
                "downstream_of": summary["downstream_of"],
                "reference": summary["reference"],
                "reference_complete": summary["reference_complete"],
                "readings": summary["readings"],
                "latest": _latest_point(report["series"][summary["name"]]),
            }
            for summary in report["evaluations"].values()
        ],
        "tasks": len(report["tasks"]),
        "floors": {
            "tasks": len(report["tasks"]),
            "known": len(floors),
            "max": max(floors) if floors else None,
            "median": statistics.median(floors) if floors else None,
        },
        "releases": {repo: len(chain) for repo, chain in report["releases"].items()},
        "additivity": [
            {
                "evaluation": name,
                "reading": entry["reading"],
                "tasks": len(entry["tasks"]),
                "observed_geomean": entry["observed_geomean"],
                "predicted_geomean": entry["predicted_geomean"],
                "residual": entry["residual"],
            }
            for name, entry in report["additivity"].items()
        ],
        "health": report["health"],
        "loop": reader.loop_state(),
        "ledger": {
            "completed_wakes": state.completed_wakes(),
            "evaluation_marker": state.evaluation_marker(),
        },
    }


def _resolve_evaluation(report: dict[str, Any], evaluation: str | None) -> str | None:
    if evaluation is None:
        return cadence_evaluation(report)
    return evaluation if evaluation in report["evaluations"] else None


def _positioned(
    report: dict[str, Any], name: str, timeline: Timeline
) -> list[dict[str, Any]]:
    """The evaluation's readings, placed and ordered by campaign position.

    A cadence sweep sits where the campaign was when it started; a
    script-labelled cutoff sits at the wake it snapshotted. Ordering by that
    position — not by measurement time — is what lets a backfill measured
    last night draw at the start of the curve, on every route that walks
    the readings.
    """

    points = []
    for point in report["series"][name]:
        if point["step"] is not None:
            position = timeline.at_step(point["step"])
        else:
            position = {**timeline.at(point["started"]), "world_at": point["started"]}
        points.append({**point, **position})
    points.sort(
        key=lambda point: (
            point["wakes_completed"] if point["wakes_completed"] is not None else -1,
            point["world_at"] or point["started"] or "",
            point["id"],
        )
    )
    return points


def series_view(
    reader: EvaluationReader,
    config: CoordinatorConfig,
    state: StateStore,
    evaluation: str | None,
) -> dict[str, Any] | None:
    report = reader.report()
    name = _resolve_evaluation(report, evaluation)
    if name is None:
        return None
    timeline = Timeline(state.completed_timeline())
    summary = report["evaluations"][name]
    points = _positioned(report, name, timeline)
    # A release is marked where it *happened* — the end of the wake that cut
    # the tag — not where the evaluator got round to measuring it. The
    # release lane measures its backlog in bursts after each boundary, so
    # measurement times bunch up and say nothing about when the society
    # shipped. Only an unmatched release falls back to its measurement time.
    markers = []
    for chain in report["releases"].values():
        for entry in chain:
            _, wake = _cached_release_wake(reader, config, state, entry)
            cut_at = (wake.get("finished") or wake.get("started")) if wake else None
            markers.append(
                {
                    "repo": entry["repo"],
                    "tag": entry["tag"],
                    "id": entry["id"],
                    "started": entry["started"],
                    "cut_at": cut_at,
                    "run_id": wake.get("run_id") if wake else None,
                    "geomean": entry["aggregates"]["geomean"],
                    "control_leak": entry["aggregates"]["control_leak"],
                    "complete": entry["complete"],
                    "wakes_completed": timeline.at(entry["started"])["wakes_completed"],
                    "wakes_at_cut": timeline.at(cut_at)["wakes_completed"],
                }
            )
    markers.sort(
        key=lambda marker: (marker["cut_at"] or marker["started"] or "", marker["id"])
    )
    return {
        "evaluation": name,
        "metric": summary["metric"],
        "tasks": len(summary["tasks"]),
        "exposed_tasks": (
            len(summary["exposed_tasks"])
            if summary["exposed_tasks"] is not None
            else None
        ),
        "points": points,
        "markers": markers,
    }


def tasks_view(
    reader: EvaluationReader, state: StateStore, evaluation: str | None
) -> dict[str, Any] | None:
    report = reader.report()
    name = _resolve_evaluation(report, evaluation)
    if name is None:
        return None
    summary = report["evaluations"][name]
    points = _positioned(report, name, Timeline(state.completed_timeline()))
    # `exposed: []` is a statement — no task is reachable — and must not
    # collapse into "every task" through truthiness.
    listed = summary["exposed_tasks"]
    exposed = set(summary["tasks"]) if listed is None else set(listed)
    tasks = []
    for ref in summary["tasks"]:
        values = [point["speedups"].get(ref) for point in points]
        latest = next((value for value in reversed(values) if value is not None), None)
        evaluator, _, task = ref.partition("@")
        owner = report["owners"].get(ref)
        tasks.append(
            {
                "ref": ref,
                "evaluator": evaluator,
                "task": task,
                "exposed": ref in exposed,
                "floor": report["tasks"].get(ref)
                or {"floor": None, "pairs": 0, "assumed": True, "threshold": None},
                "owner": owner,
                "latest": latest,
                "values": values,
            }
        )
    return {
        "evaluation": name,
        "readings": [point["id"] for point in points],
        "tasks": tasks,
    }


def _tag_patterns(tag: str) -> list[re.Pattern[str]]:
    """The tag as a bounded token: `v1.1` must not match inside `v1.10`."""

    needles = {needle for needle in (tag, tag.lstrip("vV")) if needle}
    return [
        re.compile(
            r"(?<![A-Za-z0-9._-])" + re.escape(needle) + r"(?![A-Za-z0-9_-])(?!\.\d)"
        )
        for needle in needles
    ]


def _ledger_release_index(runs_dir: Path) -> dict[tuple[str, str], str]:
    """`(repo, tag) -> run_id`, from the recorded forge action ledger.

    The ledger is authoritative: the runner writes one `release.create` row per
    real release with the tag it carried, so which wake cut a tag is a recorded
    fact and not something to infer from an agent's prose. Only the summary
    fallback below needs guessing, and only where the ledger is silent.
    """

    index: dict[tuple[str, str], str] = {}
    if not runs_dir.is_dir():
        return index
    for actions in sorted(runs_dir.glob("*/output/forge-actions.jsonl")):
        run_id = actions.parent.parent.name
        try:
            lines = actions.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("action") != "release.create":
                continue
            repo = str(row.get("repo") or "").rsplit("/", 1)[-1]
            tag = (row.get("metadata") or {}).get("tag")
            if not repo or not isinstance(tag, str) or not tag:
                continue
            # A tag is a short token; anything long is a mis-extracted body
            # (the pre-fix `-b <notes>` bug) and must not shadow a real row.
            if len(tag) > 64 or "\n" in tag:
                continue
            index.setdefault((repo, tag), run_id)
    return index


def _release_wake(
    config: CoordinatorConfig,
    state: StateStore,
    repo: str,
    tag: str | None,
    started: str | None,
    ledger: dict[tuple[str, str], str] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """The agents bound to ``repo``, and the wake that cut ``tag``.

    The recorded action ledger answers this exactly when it has the release
    (``match: "ledger"``). The summary fallback below is for releases the
    ledger missed: the agent's own summary names the tag ("released
    Starlette 1.6.44"). A candidate must name the tag as a bounded token,
    must have *finished* before the release vector was measured — the tag
    existed by then — and among the candidates of every bound agent the
    newest finished one wins. It is a summary match and says so.
    """

    agents = [
        profile.name
        for profile in config.agents
        if any(owned.rsplit("/", 1)[-1] == repo for owned in profile.write_repos)
    ]
    if not tag:
        return agents, None
    run_id = (ledger or {}).get((repo, tag)) or (ledger or {}).get(
        (repo, tag.lstrip("vV"))
    )
    if run_id:
        for wake in state.list_wakes(run_id=run_id, limit=1):
            return agents, {
                "id": wake.id,
                "run_id": wake.run_id,
                "agent": wake.agent,
                "started": wake.started,
                "finished": wake.finished,
                "summary": wake.summary,
                "match": "ledger",
            }
    patterns = _tag_patterns(tag)
    cutoff = _moment(started)
    best: Any = None
    best_finished = None
    for agent in agents:
        wakes = state.list_wakes(
            agent=agent, status="completed", until=started, limit=_SUMMARY_WAKE_LIMIT
        )
        for wake in wakes:
            finished = _moment(wake.finished)
            if finished is None or (cutoff is not None and finished > cutoff):
                continue
            summary = wake.summary or ""
            if not any(pattern.search(summary) for pattern in patterns):
                continue
            if best_finished is None or finished > best_finished:
                best, best_finished = wake, finished
    if best is None:
        return agents, None
    return agents, {
        "id": best.id,
        "run_id": best.run_id,
        "agent": best.agent,
        "started": best.started,
        "finished": best.finished,
        "summary": best.summary,
        "match": "summary",
    }


def _cached_release_wake(
    reader: EvaluationReader,
    config: CoordinatorConfig,
    state: StateStore,
    entry: dict[str, Any],
) -> tuple[list[str], dict[str, Any] | None]:
    found = reader.wakes.get(entry["id"])
    if found is not None:
        return found["agents"], found["wake"]
    if reader.release_ledger is None:
        reader.release_ledger = _ledger_release_index(config.runs_dir)
    agents, wake = _release_wake(
        config,
        state,
        entry["repo"],
        entry["tag"],
        entry["started"],
        reader.release_ledger,
    )
    if wake is not None:
        reader.wakes[entry["id"]] = {"agents": agents, "wake": wake}
    return agents, wake


def releases_view(
    reader: EvaluationReader,
    config: CoordinatorConfig,
    state: StateStore,
    repo: str | None = None,
) -> dict[str, Any] | None:
    report = reader.report()
    chains = report["releases"]
    if repo is not None and repo not in chains:
        return None
    repos = []
    for name, chain in chains.items():
        if repo is not None and name != repo:
            continue
        entries = []
        for entry in chain:
            agents, wake = _cached_release_wake(reader, config, state, entry)
            entries.append({**entry, "wake": wake})
        repos.append({"repo": name, "agents": agents, "releases": entries})
    return {"repos": repos}


def matrix_view(
    reader: EvaluationReader, evaluation: str | None
) -> dict[str, Any] | None:
    report = reader.report()
    if evaluation is not None and evaluation not in report["evaluations"]:
        return None
    name = evaluation if evaluation is not None else cadence_evaluation(report)
    columns = (
        list(report["evaluations"][name]["tasks"]) if name else sorted(report["tasks"])
    )
    rows = []
    values = []
    for chain in report["releases"].values():
        for entry in chain:
            row = [entry["speedups"].get(ref) for ref in columns]
            if all(value is None for value in row):
                continue
            rows.append(
                {
                    "repo": entry["repo"],
                    "tag": entry["tag"],
                    "id": entry["id"],
                    "started": entry["started"],
                    "complete": entry["complete"],
                }
            )
            values.append(row)
    return {"evaluation": name, "tasks": columns, "rows": rows, "values": values}


def record_view(reader: EvaluationReader, record_id: str) -> dict[str, Any] | None:
    record = reader.record(record_id)
    return {"record": record} if record is not None else None


class _ClosingReadOnlyConnection(sqlite3.Connection):
    def __exit__(self, *args: object) -> bool | None:
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class ReadOnlyStateStore(StateStore):
    """State reader for hub-api manifests that never creates coordinator state."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri, timeout=30, uri=True, factory=_ClosingReadOnlyConnection
        )
        connection.row_factory = sqlite3.Row
        return connection
