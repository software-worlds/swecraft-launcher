"""Evaluator-owned checkpoint capture.

The evaluator never gates a checkpoint: it writes neither the Forge volume
nor the coordinator ledger, and its evaluations can run for hours on their
own cadence. Its records directory is append-only and ``record.json`` is
the last file a run writes (atomically), so a record directory containing
``record.json`` is complete. Capture copies exactly those directories plus
the loop-state file; an in-flight record directory is skipped and reported,
never captured half-written.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from swecraft_common.fs import copy_regular_tree

from swecraft_evaluator.errors import EvaluatorError

STATE_NAME = "state.json"
RECORD_NAME = "record.json"
# Per-cell injected source trees: a checkout of the commits the record's
# ``environment`` names, reproducible from the Forge volume the same
# checkpoint carries. Capturing them multiplied Exp10's checkpoints by
# ~1.6x (4.8 GB / 140k files per capture) and surfaced upstream test
# fixtures' symlinks to the checkpoint's regular-files-only rule.
PRUNED_RECORD_DIRS = ("sources",)


@dataclass(frozen=True)
class EvaluatorCheckpoint:
    records_dir: Path
    state_path: Path

    def capture(
        self, *, state_destination: Path, results_destination: Path
    ) -> tuple[str, ...]:
        """Copy complete records and loop state; return skipped record ids."""

        if self.state_path.is_symlink():
            raise EvaluatorError(f"evaluator state is a symlink: {self.state_path}")
        if self.state_path.is_file():
            state_destination.mkdir(parents=True, exist_ok=True)
            (state_destination / STATE_NAME).write_bytes(self.state_path.read_bytes())
        if not self.records_dir.is_dir():
            return ()
        skipped: list[str] = []
        for path in sorted(self.records_dir.iterdir()):
            if path.is_symlink():
                raise EvaluatorError(f"evaluator records contain a symlink: {path}")
            if not path.is_dir():
                continue
            if (path / RECORD_NAME).is_file():
                copy_regular_tree(
                    path,
                    results_destination / path.name,
                    label="evaluator records",
                    prune=PRUNED_RECORD_DIRS,
                )
            else:
                skipped.append(path.name)
        return tuple(skipped)
