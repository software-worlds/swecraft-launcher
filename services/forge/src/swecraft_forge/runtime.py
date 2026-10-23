"""Standard Forge Compose lifecycle and checkpoint capture."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from swecraft_common.fs import copy_regular_tree

from swecraft_forge.errors import ForgeRuntimeError

Runner = Callable[..., subprocess.CompletedProcess]
VOLUME = "forgejo-data"


@dataclass(frozen=True)
class LiveForge:
    url: str


class ForgeRuntime:
    """Own Docker/Compose details for one standard Hub Forge project."""

    def __init__(
        self,
        *,
        project: str,
        compose_file: Path,
        state_dir: Path,
        run: Runner = subprocess.run,
    ) -> None:
        self.project = project
        self.compose_file = compose_file.resolve()
        self.state_dir = state_dir.resolve()
        self._run = run

    @property
    def image(self) -> str:
        try:
            data = yaml.safe_load(self.compose_file.read_text(encoding="utf-8"))
            image = data["services"]["forgejo"]["image"]
        except (OSError, yaml.YAMLError, KeyError, TypeError) as error:
            raise ForgeRuntimeError(
                f"could not read Forge image from {self.compose_file}: {error}"
            ) from None
        if not isinstance(image, str) or not image.strip():
            raise ForgeRuntimeError(f"{self.compose_file} pins no Forge image")
        return image.strip()

    @property
    def volume(self) -> str:
        return f"{self.project}_{VOLUME}"

    def running_services(self) -> tuple[str, ...]:
        completed = self._invoke(
            "compose",
            "-p",
            self.project,
            "-f",
            str(self.compose_file),
            "ps",
            "--services",
            "--status",
            "running",
        )
        return tuple(
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        )

    def stop(self, services: Sequence[str]) -> None:
        if services:
            self._invoke(
                "compose",
                "-p",
                self.project,
                "-f",
                str(self.compose_file),
                "stop",
                *services,
            )

    def start(self, services: Sequence[str]) -> None:
        if services:
            self._invoke(
                "compose",
                "-p",
                self.project,
                "-f",
                str(self.compose_file),
                "start",
                *services,
            )

    def capture(self, destination: Path) -> None:
        """Capture host state and the stopped Forge volume."""

        if not self.state_dir.is_dir():
            raise ForgeRuntimeError(
                f"Forge state directory does not exist: {self.state_dir}"
            )
        host_destination = destination / "host"
        copy_regular_tree(
            self.state_dir,
            host_destination,
            label="Forge host state",
            # `.cache` is the Actions runner's act cache: ephemeral, rebuilt
            # on demand, and no part of the documented host state (which is
            # `.runner`, `config.yml`, and `labels`). A society whose agents
            # write CI fills it with thousands of files -- 1,020 MB / 13,310
            # files on a 26-repository campaign -- and every one of them
            # lands under `state/forge/host/runner/.cache/` in the
            # checkpoint. Hugging Face rejects any path beneath a `.cache/`
            # folder, so capturing it made publication fail outright while
            # the local checkpoint still succeeded: the worst arrangement,
            # since the coordinator sees a healthy capture and the dataset
            # silently never updates.
            ignore={".gitkeep", ".cache"},
        )
        volume_tar = destination / "forgejo-data.tar"
        volume_tar.parent.mkdir(parents=True, exist_ok=True)
        self._invoke(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "tar",
            "-v",
            f"{self.volume}:/volume:ro",
            "-v",
            f"{volume_tar.parent.resolve()}:/backup",
            self.image,
            "-C",
            "/volume",
            "-cf",
            f"/backup/{volume_tar.name}",
            ".",
        )

    def inspect_image(self, reference: str) -> tuple[str, str]:
        completed = self._invoke(
            "image", "inspect", "--format", "{{json .}}", reference
        )
        try:
            payload = json.loads(completed.stdout)
            image_id = str(payload["Id"])
            platform = f"{payload['Os']}/{payload['Architecture']}"
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ForgeRuntimeError(
                f"docker returned invalid image metadata for {reference}: {error}"
            ) from None
        return image_id, platform

    def resume(
        self,
        *,
        volume_tar: Path,
        image: str,
        host: str,
        port: int,
    ) -> LiveForge:
        """Import one loaded volume and start it with copied host state."""

        if self._volume_exists():
            raise ForgeRuntimeError(
                f"Docker volume {self.volume} already exists; choose another project"
            )
        environment = {
            "SWECRAFT_FORGE_HOST": host,
            "SWECRAFT_FORGE_PORT": str(port),
            "SWECRAFT_FORGE_STATE_DIR": str(self.state_dir),
        }
        self._invoke("volume", "create", self.volume)
        try:
            self._import_volume(volume_tar, image)
            self._compose_up(("forgejo", "dind", "runner"), environment)
        except Exception:
            self._cleanup_failed_resume(environment)
            raise
        return LiveForge(url=f"http://{host}:{port}")

    def _import_volume(self, input_tar: Path, image: str) -> None:
        self._invoke(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "sh",
            "-v",
            f"{self.volume}:/volume",
            "-v",
            f"{input_tar.parent.resolve()}:/backup:ro",
            image,
            "-c",
            f"tar -C /volume -xf /backup/{input_tar.name} "
            "&& chown -R 1000:1000 /volume",
        )

    def _compose_up(
        self, services: Sequence[str], environment: Mapping[str, str]
    ) -> None:
        self._invoke(
            "compose",
            "-p",
            self.project,
            "-f",
            str(self.compose_file),
            "up",
            "-d",
            "--wait",
            *services,
            env=environment,
        )

    def _volume_exists(self) -> bool:
        completed = self._run(
            ["docker", "volume", "inspect", self.volume],
            capture_output=True,
            text=True,
        )
        return completed.returncode == 0

    def _cleanup_failed_resume(self, environment: Mapping[str, str]) -> None:
        try:
            self._invoke(
                "compose",
                "-p",
                self.project,
                "-f",
                str(self.compose_file),
                "down",
                "--remove-orphans",
                env=environment,
            )
        except ForgeRuntimeError:
            pass
        try:
            self._invoke("volume", "rm", self.volume)
        except ForgeRuntimeError:
            pass

    def _invoke(
        self,
        *arguments: str,
        timeout: int = 3_600,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            completed = self._run(
                ["docker", *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **env} if env else os.environ.copy(),
            )
        except FileNotFoundError:
            raise ForgeRuntimeError("docker is not installed or not on PATH") from None
        except subprocess.TimeoutExpired:
            raise ForgeRuntimeError(
                f"docker command timed out: docker {' '.join(arguments)}"
            ) from None
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise ForgeRuntimeError(
                f"docker command failed: docker {' '.join(arguments)}: {detail}"
            )
        return completed


def standard_compose_file() -> Path:
    path = Path(__file__).resolve().parents[2] / "compose.yaml"
    if not path.is_file():
        raise ForgeRuntimeError(f"standard Forge Compose file is missing: {path}")
    return path
