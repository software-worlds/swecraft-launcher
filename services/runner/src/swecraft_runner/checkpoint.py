"""Read-only projection of live Runner directories into checkpoint bundles."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from swecraft_runner.pricing import ModelPricing, agent_usage
from swecraft_runner.runner import RunnerError

_TERMINAL_STATUSES = {"completed", "failed"}
_AGENT_STDOUT_NAMES = ("claude-code.txt", "codex.txt", "antigravity-cli.txt", "pi.txt")
# Harbor copies the Antigravity CLI's transcript beside its stdout.
_ANTIGRAVITY_SESSION_FILE_NAMES = (
    "antigravity-cli.trajectory.jsonl",
    "antigravity-cli.trajectory.json",
)
_KIMI_SESSION_FILE_NAMES = {"context.jsonl", "wire.jsonl"}


class RunNotTerminalError(RunnerError):
    """Raised when a run has no complete terminal receipt yet."""


def export_run(
    source: Path,
    destination: Path,
    *,
    include_harbor: bool = True,
    harbor_cache: Path | None = None,
) -> None:
    """Materialize one terminal run in the lean checkpoint representation.

    ``source`` is read only. The projection is assembled beside ``destination``
    and atomically installed, so a failed export changes neither the live run
    nor the visible checkpoint stage. Normalized evidence — receipt,
    response, trajectory, selected sessions, and selected stdout — is projected;
    the raw Harbor tree is a bulky forensic extra packed only on
    ``include_harbor``.

    ``harbor_cache`` skips the dominant cost of repeated captures. A terminal
    run is immutable and ``_pack_harbor`` is deterministic, so the archive is
    a pure function of the run id: the first export pays the gzip work and
    publishes the result into the cache, and every later capture of the same
    run links or copies the cached bytes instead of re-packing. The cache is
    advisory — absent, empty, or irregular entries fall back to packing and
    are atomically replaced.
    """

    source = source.expanduser()
    destination = destination.expanduser()
    if not source.is_dir() or source.is_symlink():
        raise RunnerError(f"run directory is not regular: {source}")
    source = source.resolve()
    if destination.exists() or destination.is_symlink():
        raise RunnerError(f"run checkpoint destination already exists: {destination}")
    destination = destination.resolve()
    if destination.is_relative_to(source):
        raise RunnerError("run checkpoint destination must be outside the live run")
    receipt_path = source / "output/result.json"
    receipt = _optional_json(receipt_path)
    if receipt is None or receipt.get("status") not in _TERMINAL_STATUSES:
        raise RunNotTerminalError(f"run is not terminal: {source.name}")
    status = receipt["status"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        normalized = _normalized_receipt(source, receipt)
        if status == "failed":
            _copy_optional(source / "input/prompt.md", stage / "input/prompt.md")
            _copy_optional(source / "input/request.json", stage / "input/request.json")

        response = _copy_optional(
            source / "output/response.md", stage / "output/response.md"
        )
        changes = _copy_optional(
            source / "output/changes.json", stage / "output/changes.json"
        )
        # The forge ledger travels with the run it belongs to. Without this the
        # receipt kept its `forge_actions` pointer while the file stayed behind,
        # so a checkpoint — the durable record — carried a dangling reference to
        # the provenance it was supposed to preserve.
        forge_actions = _copy_optional(
            source / "output/forge-actions.jsonl",
            stage / "output/forge-actions.jsonl",
        )
        # The recording travels too. The ledger is derived from it under one
        # version of the vocabulary, and a checkpoint that kept only the
        # derived rows could never be re-derived under a corrected one.
        forge_calls = _copy_optional(
            source / "output/forge-calls.jsonl",
            stage / "output/forge-calls.jsonl",
        )
        # The forge's copy of what was posted, fetched after the fact for
        # bodies the wrapper never held. It exists nowhere else in the
        # run, so a checkpoint that dropped it would lose the words again.
        forge_bodies = _copy_optional(
            source / "output/forge-bodies.jsonl",
            stage / "output/forge-bodies.jsonl",
        )
        structured = _optional_json(source / "output/agent-result.json")
        if structured is not None:
            normalized["structured_result"] = structured

        trial = _trial_directory(source)
        trajectory = _copy_optional(
            source / "output/trajectory.json", stage / "output/trajectory.json"
        )
        agent = normalized.get("agent")
        agent_stdout = False
        if agent != "kimi-cli":
            agent_stdout = _copy_optional(
                source / "output/agent-stdout.txt",
                stage / "output/agent-stdout.txt",
            )
        session_files: tuple[str, ...] = ()
        if trial is not None:
            if not trajectory:
                trajectory = _copy_optional(
                    trial / "agent/trajectory.json", stage / "output/trajectory.json"
                )
            if not agent_stdout and agent != "kimi-cli":
                agent_stdout = _copy_agent_stdout(
                    trial, stage / "output/agent-stdout.txt"
                )
            session_files = _copy_sessions(
                trial,
                stage / "output/session",
                agent=agent,
            )
        if not session_files:
            session_files = _copy_existing_session(
                source / "output/session",
                stage / "output/session",
                agent=agent,
                already_projected=receipt.get("schema_version") == 1,
            )

        harbor = source / "harbor"
        existing_archive = source / "harbor.tar.gz"
        harbor_archive = False
        if not include_harbor:
            pass
        elif harbor.is_dir() and not harbor.is_symlink():
            staged = stage / "harbor.tar.gz"
            cached = _cached_harbor(harbor_cache, source.name)
            if cached is not None:
                _install_link_or_copy(cached, staged)
            else:
                _pack_harbor(harbor, staged)
                _publish_harbor_cache(harbor_cache, source.name, staged)
            harbor_archive = True
        elif harbor.exists() or harbor.is_symlink():
            raise RunnerError(f"Harbor run tree is not a regular directory: {harbor}")
        elif existing_archive.exists() or existing_archive.is_symlink():
            harbor_archive = _copy_optional(existing_archive, stage / "harbor.tar.gz")

        _set_artifact_fields(
            normalized,
            response=response,
            changes=changes,
            forge_actions=forge_actions,
            forge_calls=forge_calls,
            forge_bodies=forge_bodies,
            trajectory=trajectory,
            agent_stdout=agent_stdout,
            session_files=session_files,
            harbor_archive=harbor_archive,
        )
        _write_json(stage / "output/result.json", normalized)
        _validate_projection(stage)
        os.rename(stage, destination)
    except RunnerError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(stage, ignore_errors=True)
        raise RunnerError(f"could not export run {source.name}: {error}") from None
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _normalized_receipt(source: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    request = _optional_json(source / "input/request.json") or {}
    normalized = {**request, **receipt, "schema_version": 1}
    if normalized.get("status") == "failed" and "usage" not in normalized:
        usage = _failed_usage(source)
        if usage is not None:
            normalized["usage"] = usage
    return normalized


def _failed_usage(source: Path) -> dict[str, int | float] | None:
    pricing = _request_pricing(source)
    totals: dict[str, int | float] = {
        "input_tokens": 0,
        "cache_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    found = False
    for path in sorted(source.glob("harbor/jobs/*/*/result.json")):
        document = _optional_json(path)
        agent_result = document.get("agent_result") if document else None
        if not isinstance(agent_result, dict):
            continue
        found = True
        usage = agent_usage(agent_result, pricing)
        for key in totals:
            totals[key] += usage[key]
    return totals if found else None


def _request_pricing(source: Path) -> ModelPricing | None:
    request = _optional_json(source / "input/request.json") or {}
    raw = request.get("model_pricing")
    if not isinstance(raw, dict):
        return None
    try:
        return ModelPricing.from_mapping(raw)
    except ValueError:
        return None


def _trial_directory(source: Path) -> Path | None:
    trials = sorted(
        path.parent
        for path in source.glob("harbor/jobs/*/*/result.json")
        if path.is_file() and not path.is_symlink()
    )
    if len(trials) > 1:
        raise RunnerError(f"run has more than one Harbor trial: {source.name}")
    return trials[0] if trials else None


def _copy_agent_stdout(trial: Path, destination: Path) -> bool:
    candidates = [
        trial / "agent" / name
        for name in _AGENT_STDOUT_NAMES
        if (trial / "agent" / name).is_file()
        and not (trial / "agent" / name).is_symlink()
    ]
    if len(candidates) > 1:
        raise RunnerError(f"trial has multiple agent stdout files: {trial}")
    return _copy_optional(candidates[0], destination) if candidates else False


def _copy_sessions(trial: Path, destination: Path, *, agent: object) -> tuple[str, ...]:
    # Each CLI keeps its session record in its own layout; kimi-cli's lives
    # under agent/kimi/share/sessions rather than agent/sessions, so the
    # root is resolved per agent before selection.
    selected: list[tuple[Path, Path]] = []
    if agent == "codex":
        sessions = trial / "agent/sessions"
        if sessions.is_dir() and not sessions.is_symlink():
            for path in sorted(sessions.rglob("rollout-*.jsonl")):
                relative = path.relative_to(sessions)
                selected.append((path, Path("sessions") / relative))
    elif agent == "claude-code":
        projects = trial / "agent/sessions/projects/-app"
        if projects.is_dir() and not projects.is_symlink():
            for path in sorted(projects.rglob("*")):
                relative = path.relative_to(projects)
                if path.suffix == ".jsonl" or "tool-results" in relative.parts:
                    selected.append((path, Path("projects/-app") / relative))
    elif agent == "antigravity-cli":
        agent_dir = trial / "agent"
        if agent_dir.is_dir() and not agent_dir.is_symlink():
            for name in _ANTIGRAVITY_SESSION_FILE_NAMES:
                path = agent_dir / name
                if path.is_file() and not path.is_symlink():
                    selected.append((path, Path("sessions") / name))
    elif agent == "pi":
        # pi's own session record (swecraft_runner.pi.SESSION_DIR); its stdout,
        # pi.txt, is the same events plus tool streaming, and trajectory.json
        # is derived from that stdout (swecraft_runner.pi.trajectory).
        sessions = trial / "agent/pi/sessions"
        if sessions.is_dir() and not sessions.is_symlink():
            for path in sorted(sessions.rglob("*.jsonl")):
                relative = path.relative_to(sessions)
                selected.append((path, Path("sessions") / relative))
    elif agent == "kimi-cli":
        sessions = trial / "agent/kimi/share/sessions"
        if sessions.is_dir() and not sessions.is_symlink():
            # Keep Kimi's native JSONL transcript files without the runtime
            # metadata or unrelated JSONL side logs that may share the tree.
            for path in sorted(sessions.rglob("*")):
                relative = path.relative_to(sessions)
                if path.name in _KIMI_SESSION_FILE_NAMES:
                    selected.append((path, Path("sessions") / relative))
    return _copy_selected(selected, destination)


def _copy_existing_session(
    source: Path,
    destination: Path,
    *,
    agent: object,
    already_projected: bool,
) -> tuple[str, ...]:
    if not source.is_dir() or source.is_symlink():
        return ()
    prefix = (
        Path()
        if already_projected
        else Path("projects/-app")
        if agent == "claude-code"
        else Path("sessions")
    )
    selected = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if agent == "kimi-cli":
            should_copy = path.name in _KIMI_SESSION_FILE_NAMES
        else:
            should_copy = (
                path.suffix == ".jsonl"
                or "tool-results" in relative.parts
                or path.name == "state.json"
            )
        if should_copy:
            selected.append((path, prefix / relative))
    return _copy_selected(selected, destination)


def _copy_selected(
    selected: list[tuple[Path, Path]], destination: Path
) -> tuple[str, ...]:
    copied: list[str] = []
    for source, relative in sorted(selected, key=lambda item: item[1].as_posix()):
        if not _regular_without_symlink_parent(source):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        copied.append(relative.as_posix())
    return tuple(copied)


def _regular_without_symlink_parent(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    current = path.parent
    while current != current.parent:
        if current.is_symlink():
            return False
        current = current.parent
    return True


def _cached_harbor(cache: Path | None, run_id: str) -> Path | None:
    """The cached archive for one run, when it is safely usable."""

    if cache is None:
        return None
    candidate = cache / f"{run_id}.harbor.tar.gz"
    if candidate.is_symlink() or not candidate.is_file():
        return None
    if candidate.stat().st_size == 0:
        return None
    return candidate


def _install_link_or_copy(source: Path, destination: Path) -> None:
    """Hardlink when the filesystem allows it, else copy."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _publish_harbor_cache(cache: Path | None, run_id: str, staged: Path) -> None:
    """Best-effort, atomic cache fill; capture never fails on cache trouble."""

    if cache is None:
        return
    try:
        cache.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{run_id}.", suffix=".tmp", dir=cache
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            os.link(staged, temporary)
        except OSError:
            shutil.copy2(staged, temporary)
        os.replace(temporary, cache / f"{run_id}.harbor.tar.gz")
    except OSError:
        return


def _pack_harbor(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        # Deterministic bytes: identical trees must produce identical
        # archives, or every checkpoint re-uploads and re-stores every
        # (immutable) run's archive and per-path provenance in the published
        # dataset is scrambled -- observed at ~817MB of harbor
        # archives duplicated per checkpoint. Plain `w:gz` breaks this two
        # ways: gzip stamps the current time in its header, and it embeds
        # the output *filename* -- here a randomized tempfile name. Fixing
        # the gzip layer suffices because the tar layer is already stable:
        # TarFile.add walks directories in sorted order and member metadata
        # comes from the immutable run tree itself.
        with open(temporary, "wb") as stream:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=stream, mtime=0
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", dereference=False
                ) as archive:
                    archive.add(source, arcname="harbor", recursive=True)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _set_artifact_fields(
    receipt: dict[str, Any],
    *,
    response: bool,
    changes: bool,
    forge_actions: bool,
    forge_calls: bool,
    forge_bodies: bool,
    trajectory: bool,
    agent_stdout: bool,
    session_files: tuple[str, ...],
    harbor_archive: bool,
) -> None:
    fields = {
        "response": "output/response.md" if response else None,
        "changes": "output/changes.json" if changes else None,
        "forge_actions": ("output/forge-actions.jsonl" if forge_actions else None),
        "forge_calls": ("output/forge-calls.jsonl" if forge_calls else None),
        "forge_bodies": ("output/forge-bodies.jsonl" if forge_bodies else None),
        "trajectory": "output/trajectory.json" if trajectory else None,
        "agent_stdout": "output/agent-stdout.txt" if agent_stdout else None,
        "harbor_archive": "harbor.tar.gz" if harbor_archive else None,
    }
    for name, value in fields.items():
        if value is None:
            receipt.pop(name, None)
        else:
            receipt[name] = value
    receipt["session_files"] = list(session_files)


def _copy_optional(source: Path, destination: Path) -> bool:
    if not source.exists() and not source.is_symlink():
        return False
    if not source.is_file() or source.is_symlink():
        raise RunnerError(f"run artifact is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def _optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _validate_projection(root: Path) -> None:
    allowed_roots = {"harbor.tar.gz", "input", "output"}
    if any(path.name not in allowed_roots for path in root.iterdir()):
        raise RunnerError(f"run projection has an unexpected root entry: {root}")
    allowed_input = {"prompt.md", "request.json"}
    input_dir = root / "input"
    if input_dir.is_dir() and any(
        path.name not in allowed_input for path in input_dir.iterdir()
    ):
        raise RunnerError(f"run projection has an unexpected input entry: {root}")
    allowed_output = {
        "result.json",
        "response.md",
        "changes.json",
        "forge-actions.jsonl",
        "forge-calls.jsonl",
        "forge-bodies.jsonl",
        "trajectory.json",
        "session",
        "agent-stdout.txt",
    }
    output = root / "output"
    if not output.is_dir() or any(
        path.name not in allowed_output for path in output.iterdir()
    ):
        raise RunnerError(f"run projection has an unexpected output entry: {root}")
