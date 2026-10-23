"""Boundary checkpointing owned by the wake loop, training-checkpoint style.

The scheduler is the step loop and completed wakes are the steps: when
``checkpoint_every_wakes`` is reached — or an operator files an on-demand
request — the loop stops dispatching, lets in-flight wakes finish, and
invokes the society checkpoint command at the quiet boundary it created.
The checkpoint command's own quiescence checks then pass by construction —
its strictness becomes an assertion that this loop did its job, not an
operational obstacle.

Publication is deliberately not part of the boundary: capture is local
and bounded, and when ``checkpoint_publish`` is on the upload runs in a
detached ``swecraft-society publish`` process, so dispatch resumes without
waiting on the network and a failed upload stays retryable.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

import yaml

from swecraft_coordinator.config import CoordinatorConfig
from swecraft_coordinator.state import StateStore

logger = logging.getLogger(__name__)

_HUB_ROOT = Path(__file__).resolve().parents[4]

# One in-flight publish at a time: overlapping uploads of the same dataset
# race on remote commits, and a skipped checkpoint can always be published
# later by hand — that is the point of the standalone command.
_publish_lock = threading.Lock()
_publish_active: subprocess.Popen | None = None
_publish_target: Path | None = None


def _active_publish_target() -> Path | None:
    """The checkpoint a still-running detached publish is reading, if any."""

    with _publish_lock:
        if _publish_active is not None and _publish_active.poll() is None:
            return _publish_target
    return None


def checkpoint_request_path(config: CoordinatorConfig) -> Path:
    """Flag file an operator writes to request an off-cadence boundary."""

    return config.database.parent / "checkpoint-request"


def warn_if_publish_misconfigured(
    config: CoordinatorConfig, *, root: Path = Path(".")
) -> None:
    """Surface a dead publish lane at startup, not after the first capture.

    The detached publisher would only fail hours later at the first
    boundary, in a log line nobody is tailing. This reads Society's config as loosely as
    ``checkpoint_output_dir`` does; a custom ``publish_command`` is the
    operator's own contract and is not second-guessed.
    """

    if not config.checkpoint_publish or config.publish_command:
        return
    source = (
        config.checkpoint_config
        if config.checkpoint_config.is_absolute()
        else root / config.checkpoint_config
    )
    repo_id = None
    if source.is_file():
        try:
            payload = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            logger.warning(
                "could not read %s to validate publishing: %s", source, error
            )
            return
        if isinstance(payload, dict) and isinstance(payload.get("huggingface"), dict):
            repo_id = payload["huggingface"].get("repo_id")
    if not repo_id:
        logger.warning(
            "checkpoint_publish is on but %s has no huggingface.repo_id; "
            "boundary publishes will fail until it is configured",
            source,
        )


def checkpoint_due(config: CoordinatorConfig, state: StateStore) -> bool:
    if checkpoint_request_path(config).exists():
        return True
    if config.checkpoint_every_wakes <= 0:
        return False
    completed = state.completed_wakes()
    return completed - state.checkpoint_marker() >= config.checkpoint_every_wakes


def evaluation_due(config: CoordinatorConfig, state: StateStore) -> bool:
    if config.evaluate_every_wakes <= 0:
        return False
    completed = state.completed_wakes()
    return completed - state.evaluation_marker() >= config.evaluate_every_wakes


def run_boundary_evaluation(config: CoordinatorConfig, state: StateStore) -> bool:
    """Invoke the evaluation command at an idle boundary; returns success.

    Same marker-before-command shape as the checkpoint: the marker means
    "boundary attempted", so a failing evaluation skips one cadence loudly
    instead of hot-looping against a healthy society, and the next boundary
    measures again anyway.
    """

    steps = state.completed_wakes()
    state.record_evaluation_marker(steps)
    command = list(config.evaluate_command)
    logger.info(
        "boundary evaluation at %d completed wake(s): %s",
        steps,
        " ".join(command),
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.evaluate_timeout_seconds,
        )
    except FileNotFoundError:
        logger.error("evaluation command not found: %s", command[0])
        return False
    except subprocess.TimeoutExpired:
        logger.error("evaluation timed out after %ds", config.evaluate_timeout_seconds)
        return False
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()
        logger.error(
            "evaluation failed (exit %d): %s", completed.returncode, detail[-2000:]
        )
        return False
    logger.info("boundary evaluation succeeded at step %d", steps)
    return True


def run_boundary_checkpoint(
    config: CoordinatorConfig, state: StateStore, *, root: Path = Path(".")
) -> bool:
    """Invoke the checkpoint command at an idle boundary; returns success.

    The marker is recorded **before** the command runs, for two reasons:
    the checkpoint copies the coordinator database, so the captured state
    must already carry its own boundary (a resumed copy would otherwise
    believe another checkpoint is immediately due); and for a scheduled
    boundary the marker means "boundary attempted", so a failure skips one
    full cadence loudly instead of hot-looping a broken pipeline against a
    healthy society. A failed attempt that only an operator request made
    due restores the previous marker: an off-cadence failure must not
    steal the scheduled boundary it did not consume.
    """

    steps = state.completed_wakes()
    previous_marker = state.checkpoint_marker()
    cadence_due = (
        config.checkpoint_every_wakes > 0
        and steps - previous_marker >= config.checkpoint_every_wakes
    )
    state.record_checkpoint_marker(steps)
    # The request is consumed by this attempt, success or not — a broken
    # pipeline must not turn one request into a hot loop.
    checkpoint_request_path(config).unlink(missing_ok=True)

    def failed() -> bool:
        if not cadence_due:
            state.record_checkpoint_marker(previous_marker)
        return False

    command = list(config.checkpoint_command) or _default_command(
        config.checkpoint_config
    )
    logger.info(
        "boundary checkpoint at %d completed wake(s): %s",
        steps,
        " ".join(command),
    )
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.checkpoint_timeout_seconds,
        )
    except FileNotFoundError:
        logger.error("checkpoint command not found: %s", command[0])
        return failed()
    except subprocess.TimeoutExpired:
        logger.error(
            "checkpoint timed out after %ds", config.checkpoint_timeout_seconds
        )
        return failed()
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()
        logger.error(
            "checkpoint failed (exit %d): %s", completed.returncode, detail[-2000:]
        )
        return failed()
    logger.info("boundary checkpoint succeeded at step %d", steps)
    if config.checkpoint_publish:
        output = _parse_checkpoint_path(completed.stdout or "")
        if output is None:
            logger.error(
                "checkpoint_publish is on but the checkpoint command did not "
                "report its output path; publish it manually with swecraft-society"
            )
        else:
            _spawn_publish(config, output)
    _prune(
        checkpoint_output_dir(root, config.checkpoint_config),
        config.checkpoint_keep,
        active_publish=_active_publish_target(),
    )
    return True


def _parse_checkpoint_path(stdout: str) -> Path | None:
    """Find the ``checkpoint: <path>`` line the society CLI prints."""

    for line in stdout.splitlines():
        if line.startswith("checkpoint:"):
            value = line.removeprefix("checkpoint:").strip()
            if value:
                return Path(value)
    return None


def _spawn_publish(config: CoordinatorConfig, output: Path) -> None:
    """Upload the finished checkpoint out-of-band; dispatch never waits.

    The checkpoint directory is immutable and self-verifying, so the
    upload needs nothing from the wake loop. A still-running previous
    publish skips this one rather than racing it — the checkpoint stays
    on disk and remains publishable by hand.
    """

    global _publish_active, _publish_target
    command = (
        [*config.publish_command, str(output)]
        if config.publish_command
        else _default_publish_command(config.checkpoint_config, output)
    )
    with _publish_lock:
        if _publish_active is not None and _publish_active.poll() is None:
            logger.warning(
                "previous checkpoint publish still running; not publishing %s "
                "(publish it later with: swecraft-society publish %s)",
                output.name,
                output,
            )
            return
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as error:
            logger.error("could not start checkpoint publish: %s", error)
            return
        _publish_active = process
        _publish_target = output
    logger.info("publishing checkpoint %s in the background", output.name)
    threading.Thread(
        target=_log_publish_outcome,
        args=(process, output.name),
        name="checkpoint-publish",
        daemon=True,
    ).start()


def _log_publish_outcome(process: subprocess.Popen, name: str) -> None:
    stdout, _ = process.communicate()
    if process.returncode:
        logger.error(
            "checkpoint publish for %s failed (exit %d): %s",
            name,
            process.returncode,
            (stdout or "").strip()[-2000:],
        )
    else:
        logger.info("checkpoint publish for %s succeeded", name)


def checkpoint_output_dir(root: Path, config_path: Path) -> Path:
    """The directory checkpoints land in, read from Society's own defaults.

    Retention must prune where `swecraft-society checkpoint` actually writes, so
    the answer comes from `.swecraft/checkpoint.yaml` — the same file the
    subprocess reads — never from a second coordinator-owned setting that
    could silently diverge.
    """

    configured: object = None
    source = config_path if config_path.is_absolute() else root / config_path
    if source.is_file():
        try:
            payload = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            logger.warning("could not read %s for pruning: %s", source, error)
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("checkpoint"), dict):
            configured = payload["checkpoint"].get("output_dir")
    directory = Path(str(configured)) if configured else Path(".swecraft/checkpoints")
    return directory if directory.is_absolute() else (root / directory).resolve()


def _default_command(config_path: Path) -> list[str]:
    return [
        "uv",
        "run",
        "--project",
        str(_HUB_ROOT / "services/society"),
        "swecraft-society",
        "checkpoint",
        "--config",
        str(config_path),
    ]


def _default_publish_command(config_path: Path, output: Path) -> list[str]:
    return [
        "uv",
        "run",
        "--project",
        str(_HUB_ROOT / "services/society"),
        "swecraft-society",
        "publish",
        str(output),
        "--config",
        str(config_path),
    ]


def _prune(directory: Path, keep: int, *, active_publish: Path | None = None) -> None:
    """Delete the oldest boundary checkpoints beyond ``keep``.

    Only directories carrying a ``manifest.json`` are candidates, so an
    unrelated file or an in-progress staging directory is never touched.
    Checkpoint ids embed a UTC stamp, so name order is age order. A
    checkpoint a detached publish is still uploading is spared until that
    upload finishes — deleting bytes mid-read would fail the publish for
    nothing.
    """

    if keep <= 0 or not directory.is_dir():
        return
    candidates = sorted(
        path
        for path in directory.iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and (path / "manifest.json").is_file()
    )
    for stale in candidates[: max(0, len(candidates) - keep)]:
        if active_publish is not None and stale.resolve() == active_publish.resolve():
            logger.info("not pruning %s while its publish is still running", stale.name)
            continue
        logger.info("pruning boundary checkpoint %s", stale.name)
        shutil.rmtree(stale, ignore_errors=True)
