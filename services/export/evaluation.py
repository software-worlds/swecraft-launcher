"""Export one run's held-out evaluation for the browser: ``evaluation.json``.

The source is a checkpoint whose records are in the current layout
(``apparatus/evaluation.yaml`` and ``evaluations/<id>/record.json``), read with
the read API's evaluation views (``evaluation_views.py``), called offline.
Checkpoints from the August evaluator store ``plan.json`` and ``result.json``
instead, which the views do not read.

The file holds:

- ``summary``, ``series`` and ``tasks``: the views' ``summary_view``,
  ``series_view`` and ``tasks_view``, unchanged.
- ``carry`` and ``strict``: the paper's two scores at each reading, over the
  tasks the seed measured: carry keeps each task's last measured speedup (1.0
  before any); strict scores every task of a package 1.0 when its gate is red
  or one of its tasks has no value.
- ``source``: where the numbers come from, stated on the page.

Readings are placed on the run's completed sessions with the views'
``Timeline``, from the coordinator's ledger. Run it in the workspace, so those
modules are importable::

    uv run python services/export/evaluation.py --checkpoint <checkpoint> --id <run-id>

The checkpoint's ledger is opened read-only and immutable. Nothing here reads
a credential: a coordinator configuration is parsed for its agents and their
repositories only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import evaluation_views as views
import yaml
from evaluation_views import ReadOnlyStateStore, _ClosingReadOnlyConnection
from swecraft_coordinator.config import _TOP_KEYS as COORDINATOR_KEYS
from swecraft_coordinator.config import CoordinatorConfig
from swecraft_evaluator.report import (
    reading_from_record,
)

SCHEMA = "sim-browser/evaluation-v1"

# ------------------------------------------------------------------ shared


def judge(ref: str) -> str:
    return ref.partition("@")[0]


def gmean(values: list[float]) -> float | None:
    return math.exp(sum(math.log(v) for v in values) / len(values)) if values else None


def paper_scores(points: list[dict[str, Any]], scored: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The paper's S_carry and S_strict at each reading, over the ``scored`` tasks."""

    last = dict.fromkeys(scored, 1.0)
    carry, strict = [], []
    for point in points:
        values = point["speedups"]
        present = {ref: values[ref] for ref in scored if (values.get(ref) or 0) > 0}
        last.update(present)
        red = {judge(ref) for ref in point["gated_tasks"]} | {judge(ref) for ref in scored if ref not in present}
        carry.append(gmean([last[ref] for ref in scored]))
        strict.append(gmean([1.0 if judge(ref) in red else present[ref] for ref in scored]))
    return {"tasks": scored, "values": carry}, {"tasks": scored, "values": strict}


def git_head(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def generated() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def local_paths(value: Any, needles: tuple[str, ...]) -> list[str]:
    """Strings in the export that name a directory on this machine."""

    if isinstance(value, str):
        return [value] if any(needle in value for needle in needles) else []
    if isinstance(value, dict):
        return [found for item in value.values() for found in local_paths(item, needles)]
    if isinstance(value, list):
        return [found for item in value for found in local_paths(item, needles)]
    return []


# -------------------------------------------------------------- checkpoint

MANIFEST = Path("apparatus/evaluation.yaml")
RECORDS = Path("evaluations")
COORDINATOR = Path("apparatus/coordinator.yaml")
LEDGER = Path("state/coordinator/coordinator.db")
LOOP_STATE = Path("state/evaluator/state.json")
RUNS = Path("runs")


class LedgerReader(ReadOnlyStateStore):
    """hub-api's read-only ledger, opened immutable so no journal file is created.

    A ledger written before the coordinator recorded evaluation boundaries
    has no ``evaluation_marker`` table; the exporter treats it as hub-api
    treats an empty one: 0.
    """

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self._path.resolve().as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, factory=_ClosingReadOnlyConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def evaluation_marker(self) -> int:
        try:
            return super().evaluation_marker()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error):
                raise
            return 0


def coordinator_config(checkpoint: Path) -> CoordinatorConfig:
    """The run's agents and repositories, with paths resolved into the checkpoint.

    Some runs were configured with keys of a coordinator that this one does
    not have (the open pull requests the paper's runs used: ``collaboration``,
    and the budget pool's ``budget`` keys). Those keys set how the scheduler
    behaved, not the agents and repositories read here, so they are left out
    instead of refused as ``CoordinatorConfig.load`` refuses them; the budget,
    which the views never read, is left out whole.
    """

    path = checkpoint / COORDINATOR
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a mapping")
    config = CoordinatorConfig._build({key: value for key, value in data.items() if key in COORDINATOR_KEYS and key != "budget"})
    return replace(
        config,
        runs_dir=checkpoint / RUNS,
        database=checkpoint / LEDGER,
        # Never read here; pointed inside the checkpoint so nothing resolves
        # against the working directory.
        credentials_dir=checkpoint / "state" / "coordinator" / "credentials",
    )


def seed_tasks(reader: views.EvaluationReader, reference: str | None) -> list[str] | None:
    """The tasks the evaluation's seed reading measured, in the battery's order."""

    if reference is None:
        return None
    record = reader.record(reference)
    reading = reading_from_record(reader.manifest(), record) if record else None
    if reading is None:
        return None
    return [ref for ref, sample in reading.samples.items() if sample is not None]


def checkpoint_name(checkpoint: Path) -> str:
    try:
        manifest = json.loads((checkpoint / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError):
        return checkpoint.name
    name = manifest.get("checkpoint_id") if isinstance(manifest, dict) else None
    return name if isinstance(name, str) and name else checkpoint.name


def relative(path: str, checkpoint: Path) -> str:
    """A path the views report, relative to the checkpoint when it is inside it.

    Symbolic links are kept: a dataset snapshot links its files into a blob
    store, and the path inside the checkpoint is the one a reader can look up.
    """

    try:
        return Path(path).relative_to(checkpoint).as_posix()
    except ValueError:
        return Path(path).name


def from_checkpoint(checkpoint: Path, bundle_id: str, evaluation: str | None) -> dict[str, Any]:
    """hub-api's views over a checkpoint in hub-api's current layout."""

    source = views.EvaluationSource(
        manifest_path=checkpoint / MANIFEST,
        records_dir=checkpoint / RECORDS,
        state_path=checkpoint / LOOP_STATE,
    )
    if not source.manifest_path.is_file():
        raise SystemExit(f"no evaluation manifest at {source.manifest_path}")
    if not any(source.records_dir.glob("*/record.json")):
        if any(source.records_dir.glob("*/plan.json")):
            raise SystemExit(
                f"{source.records_dir} holds the August evaluator's plan.json and result.json, "
                "which the evaluation views do not read"
            )
        raise SystemExit(f"no records with a record.json under {source.records_dir}")
    reader = views.EvaluationReader(source)
    state = LedgerReader(checkpoint / LEDGER)
    config = coordinator_config(checkpoint)

    summary = views.summary_view(reader, state)
    series = views.series_view(reader, config, state, evaluation)
    tasks = views.tasks_view(reader, state, evaluation)
    summary["manifest"] = relative(summary["manifest"], checkpoint)
    summary["records_dir"] = relative(summary["records_dir"], checkpoint)

    carry = strict = None
    if series is not None:
        battery = next((e for e in summary["evaluations"] if e["name"] == series["evaluation"]), None)
        scored = seed_tasks(reader, battery["reference"] if battery else None)
        if scored is not None:
            carry, strict = paper_scores(series["points"], scored)

    hub = git_head(Path(views.__file__).resolve().parents[2])
    readings = len(series["points"]) if series else 0
    releases = len(series["markers"]) if series else 0
    return {
        "schema": SCHEMA,
        "source": {
            "kind": "checkpoint",
            "id": bundle_id,
            "checkpoint": checkpoint_name(checkpoint),
            "hub_commit": hub,
            "summary": (
                f"Measured by the run's own evaluator: {readings} readings of the world's state and "
                f"{releases} releases, read with hub-api's evaluation views"
                + (f" at {hub[:8]}." if hub else ".")
            ),
            "generated": generated(),
        },
        "summary": summary,
        "series": series,
        "tasks": tasks,
        "carry": carry,
        "strict": strict,
    }


# -------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--checkpoint", type=Path, required=True, help="a checkpoint whose records are in the current layout")
    parser.add_argument("--id", required=True, help="the bundle id, for example my-run")
    parser.add_argument("--evaluation", help="the evaluation to export (default: the cadence evaluation)")
    parser.add_argument("--out", type=Path, help="default: <id>/evaluation.json in the bundles, BUNDLES_DIR or bundles/")
    args = parser.parse_args(argv)

    checkpoint = args.checkpoint.absolute()
    bundle = from_checkpoint(checkpoint, args.id, args.evaluation)
    needles = (str(checkpoint), str(checkpoint.resolve()), str(Path.home()))
    leaked = local_paths(bundle, needles)
    if leaked:
        raise SystemExit(f"the export names local paths, for example {leaked[0]!r}")

    out = args.out or Path(os.environ.get("BUNDLES_DIR") or "bundles") / args.id / "evaluation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    series = bundle["series"]
    points = len(series["points"]) if series else 0
    last = (bundle["carry"] or {}).get("values") or [None]
    print(
        f"wrote {out} ({out.stat().st_size:,} bytes): {points} readings, "
        f"{len(series['markers']) if series else 0} releases; last carry {last[-1]}, "
        f"last strict {((bundle['strict'] or {}).get('values') or [None])[-1]}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
