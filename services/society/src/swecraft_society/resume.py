"""Resume orchestration over Forge- and coordinator-owned adapters."""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from swecraft_common import HubError
from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.resume import CoordinatorResume
from swecraft_forge import ForgeRuntime, standard_compose_file

from swecraft_society import layout
from swecraft_society.config import DEFAULT_CONFIG_PATH, load_checkpoint_config
from swecraft_society.errors import SocietyError
from swecraft_society.inventory import verify_checkpoint
from swecraft_society.provenance import repository_head

_PROJECT_CHARACTER = re.compile(r"[^a-z0-9_-]+")
WarningSink = Callable[[str], None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResumeOptions:
    root: Path = Path(".")
    config_path: Path = DEFAULT_CONFIG_PATH
    dest: Path | None = None
    compose_project: str | None = None
    port: int = 3400
    host: str = "127.0.0.1"


@dataclass(frozen=True)
class ResumeReport:
    dest: Path
    forge_url: str
    config_path: Path
    compose_project: str
    agents: tuple[str, ...]


def resume(
    options: ResumeOptions,
    *,
    forge_runtime: ForgeRuntime | None = None,
    warn: WarningSink | None = None,
) -> ResumeReport:
    """Turn a pristine loaded checkpoint into a mutable running society."""

    warn = warn or logger.warning
    root = options.root.expanduser().resolve()
    config = load_checkpoint_config(options.config_path, root=root).config
    dest = _resolve_dest(root, config.load.output_dir, options.dest)
    manifest = verify_checkpoint(dest)
    if manifest.omissions:
        warn(
            "resuming a partial checkpoint; it omits: " + ", ".join(manifest.omissions)
        )
    current_commit = repository_head()
    if current_commit != manifest.hub_commit:
        warn(
            f"checkpoint used Hub {manifest.hub_commit}, but resume is using "
            f"{current_commit}; V1 does not enforce commit compatibility"
        )
    project = options.compose_project or _project_name(
        config.resume.compose_project_prefix, manifest.checkpoint_id
    )
    state = dest / layout.FORGE_HOST_DIR
    apparatus_config = dest / layout.COORDINATOR_CONFIG
    if not state.is_dir() or not apparatus_config.is_file():
        raise SocietyError(f"{dest} is not a complete loaded checkpoint")
    try:
        forge = forge_runtime or ForgeRuntime(
            project=project,
            compose_file=standard_compose_file(),
            state_dir=state,
        )
        if forge.project != project:
            raise SocietyError(
                "injected Forge runtime does not match the resume project"
            )
        if forge.image != manifest.forge.image:
            raise SocietyError(
                f"checkpoint Forge image is {manifest.forge.image}, but current "
                f"Compose uses {forge.image}; refusing to import its database"
            )
    except SocietyError:
        raise
    except HubError as error:
        raise SocietyError(f"resume preflight failed: {error}") from None

    try:
        live = forge.resume(
            volume_tar=dest / layout.FORGE_VOLUME_TAR,
            image=manifest.forge.image,
            host=options.host,
            port=options.port,
        )
        checkpoint_config = _write_runtime_checkpoint_config(
            dest=dest,
            project=project,
            forge=forge,
            source=config,
        )
        coordinator = CoordinatorResume.load(apparatus_config, destination=dest)
        activated = coordinator.activate(
            forge_url=live.url,
            port=options.port,
            checkpoint_config=checkpoint_config,
        )
    except (CoordinatorError, HubError, SocietyError) as error:
        raise SocietyError(
            f"resume project {project} failed: {error}. Recovery handle: "
            f"{_recovery_command(project, forge.compose_file, state)}"
        ) from None
    return ResumeReport(
        dest=dest,
        forge_url=live.url,
        config_path=activated.config_path,
        compose_project=project,
        agents=activated.agents,
    )


def _write_runtime_checkpoint_config(
    *, dest: Path, project: str, forge, source
) -> Path:
    """Relocate automatic checkpointing to the mutable destination."""

    path = dest / "runtime/checkpoint.yaml"
    payload = {
        "schema_version": 1,
        "apparatus": {
            "root": str((dest / layout.APPARATUS_DIR).resolve()),
            "coordinator": str((dest / "runtime/coordinator.yaml").resolve()),
        },
        "checkpoint": {
            "output_dir": str((dest / ".swecraft/checkpoints").resolve()),
            "harbor": source.checkpoint.harbor,
            "compose_project": project,
            "actions_wait_seconds": source.checkpoint.actions_wait_seconds,
            "evaluator_records_dir": str((dest / layout.EVALUATIONS_DIR).resolve()),
            "evaluator_state": str((dest / layout.EVALUATOR_STATE).resolve()),
            "forge_compose_file": str(forge.compose_file),
            "forge_state_dir": str((dest / layout.FORGE_HOST_DIR).resolve()),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            yaml.safe_dump(payload, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
    except OSError as error:
        raise SocietyError(
            f"could not write runtime checkpoint config: {error}"
        ) from None
    return path


def _recovery_command(project: str, compose_file: Path, state: Path) -> str:
    command = shlex.join(
        (
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(compose_file),
            "down",
            "-v",
            "--remove-orphans",
        )
    )
    return f"SWECRAFT_FORGE_STATE_DIR={shlex.quote(str(state))} {command}"


def _resolve_dest(root: Path, output_dir: Path, dest: Path | None) -> Path:
    if dest is not None:
        return (
            dest.expanduser().resolve()
            if dest.is_absolute()
            else (root / dest).resolve()
        )
    loaded = output_dir if output_dir.is_absolute() else root / output_dir
    candidates = []
    if loaded.is_dir():
        for candidate in loaded.iterdir():
            if not candidate.is_dir():
                continue
            try:
                manifest = verify_checkpoint(candidate)
            except SocietyError:
                continue
            candidates.append((manifest.created_at, candidate))
    if not candidates:
        raise SocietyError(
            f"no pristine loaded checkpoints under {loaded}; pass one explicitly"
        )
    return max(candidates, key=lambda item: item[0])[1].resolve()


def _project_name(prefix: str, checkpoint_id: str) -> str:
    value = _PROJECT_CHARACTER.sub("-", f"{prefix}-{checkpoint_id}".lower()).strip("-_")
    if not value:
        raise SocietyError("could not derive a Compose project name")
    return value
