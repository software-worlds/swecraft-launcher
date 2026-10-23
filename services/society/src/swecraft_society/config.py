"""Local `.swecraft/checkpoint.yaml` operational defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from swecraft_society.errors import SocietyError

DEFAULT_CONFIG_PATH = Path(".swecraft/checkpoint.yaml")


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApparatusConfig(ConfigModel):
    root: Path = Path(".")
    world: Path = Path("world.yaml")
    world_lock: Path = Path("world.lock.json")
    evaluation: Path = Path("evaluation.yaml")
    coordinator: Path = Path("coordinator.yaml")
    charters: Path = Path("charters")
    images: Path = Path("images")


class CheckpointDefaults(ConfigModel):
    output_dir: Path = Path(".swecraft/checkpoints")
    # Run traces are the irreplaceable half of the record and are always
    # captured. Only the bulky raw Harbor archive is a policy choice.
    harbor: Literal["include", "omit"] = "omit"
    # Deprecated and ignored: traces are no longer optional. Accepted
    # so checkpoint configs written before the harbor policy keep loading.
    include_runs: bool | None = None
    compose_project: str = "swecraft-forge"
    # Let Forge-side CI finish at the coordinator's quiet wake boundary.
    # Zero preserves advisory-only capture for deployments that prefer it.
    actions_wait_seconds: int = Field(default=300, ge=0)
    evaluator_records_dir: Path = Path(".swecraft/evaluator/records")
    evaluator_state: Path = Path(".swecraft/evaluator/state.json")
    # End-of-run review batches (swecraft-coordinator review); captured when present.
    reviews_dir: Path = Path(".swecraft/reviews")
    # The standard Hub file is the default; explicit paths support isolated tests.
    forge_compose_file: Path | None = None
    forge_state_dir: Path | None = None

    @field_validator("compose_project")
    @classmethod
    def valid_project(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("compose_project must not be empty or contain whitespace")
        return value


class LoadDefaults(ConfigModel):
    output_dir: Path = Path(".swecraft/loaded")


class ResumeDefaults(ConfigModel):
    compose_project_prefix: str = "swecraft-resumed"

    @field_validator("compose_project_prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError(
                "compose_project_prefix must not be empty or contain whitespace"
            )
        return value


class HuggingFaceConfig(ConfigModel):
    repo_id: str
    private: bool = True

    @field_validator("repo_id")
    @classmethod
    def valid_repo_id(cls, value: str) -> str:
        value = value.strip()
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repo_id must be an owner/name dataset identifier")
        if any(character.isspace() for character in value):
            raise ValueError("repo_id must not contain whitespace")
        return value


class CheckpointConfig(ConfigModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    apparatus: ApparatusConfig = Field(default_factory=ApparatusConfig)
    checkpoint: CheckpointDefaults = Field(default_factory=CheckpointDefaults)
    load: LoadDefaults = Field(default_factory=LoadDefaults)
    resume: ResumeDefaults = Field(default_factory=ResumeDefaults)
    huggingface: HuggingFaceConfig | None = None


@dataclass(frozen=True)
class ConfigDocument:
    path: Path
    root: Path
    config: CheckpointConfig


def load_checkpoint_config(
    path: Path = DEFAULT_CONFIG_PATH, *, root: Path = Path(".")
) -> ConfigDocument:
    """Load local defaults, using schema defaults when the file is absent."""

    root = root.resolve()
    source = path if path.is_absolute() else root / path
    if not source.exists():
        return ConfigDocument(path=source, root=root, config=CheckpointConfig())
    if not source.is_file():
        raise SocietyError(f"checkpoint config is not a regular file: {source}")
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SocietyError(
            f"could not read checkpoint config {source}: {error}"
        ) from None
    if payload is None:
        payload = {}
    try:
        config = CheckpointConfig.model_validate(payload)
    except ValidationError as error:
        raise SocietyError(f"invalid checkpoint config {source}: {error}") from None
    return ConfigDocument(path=source, root=root, config=config)
