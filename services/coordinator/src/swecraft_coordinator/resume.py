"""Coordinator-owned runtime relocation after checkpoint load."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import CoordinatorConfig


@dataclass(frozen=True)
class CoordinatorResumeReport:
    config_path: Path
    agents: tuple[str, ...]


@dataclass(frozen=True)
class CoordinatorResume:
    apparatus_config: Path
    destination: Path
    config: CoordinatorConfig

    @classmethod
    def load(cls, apparatus_config: Path, *, destination: Path) -> CoordinatorResume:
        return cls(
            apparatus_config=apparatus_config,
            destination=destination.resolve(),
            config=CoordinatorConfig.load(apparatus_config),
        )

    def activate(
        self, *, forge_url: str, port: int, checkpoint_config: Path
    ) -> CoordinatorResumeReport:
        """Write relocated runtime config without changing copied identities."""

        runtime_config = self.destination / "runtime/coordinator.yaml"
        self._write_runtime_config(
            runtime_config,
            forge_url=forge_url,
            port=port,
            checkpoint_config=checkpoint_config,
        )
        return CoordinatorResumeReport(
            config_path=runtime_config,
            agents=tuple(profile.name for profile in self.config.agents),
        )

    def _write_runtime_config(
        self,
        destination: Path,
        *,
        forge_url: str,
        port: int,
        checkpoint_config: Path,
    ) -> None:
        try:
            data = yaml.safe_load(self.apparatus_config.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise CoordinatorError(
                f"could not read coordinator config {self.apparatus_config}: {error}"
            ) from None
        if not isinstance(data, dict):
            raise CoordinatorError(
                f"coordinator config must be a mapping: {self.apparatus_config}"
            )

        root = self.destination
        admin_token = root / "state/forge/host/admin_token"
        if not admin_token.is_file():
            raise CoordinatorError(
                f"loaded Forge admin token is missing: {admin_token}"
            )
        data["forge_url"] = forge_url
        data["push_url"] = f"http://host.docker.internal:{port}"
        data["admin_token"] = f"file:{admin_token}"

        # A file-sourced Hub API token is checkpointed. Literal and env sources
        # remain apparatus declarations and continue to resolve at process start.
        api_token = root / "state/coordinator/api_token"
        if api_token.is_file():
            data["api_token"] = f"file:{api_token}"
        data["database"] = str(root / "state/coordinator/coordinator.db")
        data["runs_dir"] = str(root / "runs")
        data["credentials_dir"] = str(root / "state/coordinator/credentials")
        data["checkpoint_config"] = str(checkpoint_config.resolve())
        data.pop("checkpoint_command", None)
        # A resumed copy must not publish into the source society's dataset.
        data.pop("publish_command", None)
        data.pop("checkpoint_publish", None)
        (root / "runs").mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
