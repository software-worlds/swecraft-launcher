"""One hardened way to run Git on the host.

Host-side services run Git against repositories and files they do not fully
control. Every invocation therefore ignores host Git configuration, disables
hooks and fsmonitor, redirects HOME/XDG into a disposable directory, removes
inherited Git control and tracing variables, and scopes HTTP authentication to
the remote origin. Tokens stay in the child environment rather than argv.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from uuid import uuid4

from filelock import FileLock

from swecraft_common import HubError

GIT_TIMEOUT_SECONDS = 600
SAFE_GIT_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.protectHFS=true",
    "-c",
    "core.protectNTFS=true",
    "-c",
    "core.attributesFile=/dev/null",
)
_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_ENVIRONMENT_KEYS = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_ASKPASS",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_EXEC_PATH",
    "GIT_EXTERNAL_DIFF",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PROTOCOL_FROM_USER",
    "GIT_PROXY_COMMAND",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_SSH_VARIANT",
    "GIT_SSL_NO_VERIFY",
    "GIT_WORK_TREE",
    "SSH_ASKPASS",
}


@dataclass(frozen=True)
class CachedGitRevision:
    """A commit resolved through the shared mirror cache."""

    remote_url: str
    revision: str
    commit: str
    mirror_path: Path
    fetched: bool


_GIT_ENVIRONMENT_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_TRACE",
)


def git_environment(
    *,
    isolation_dir: Path,
    token: str | None = None,
    remote_url: str | None = None,
) -> dict[str, str]:
    """Return an environment isolated from the operator's Git setup.

    If ``token`` is supplied, ``remote_url`` is required. HTTP credentials are
    scoped to that URL's origin (for example ``http://forge:3400/``), so a Git
    process that also contacts an upstream host cannot forward the Forge token.
    Tokens for non-HTTP remotes are ignored.
    """

    home = isolation_dir / "home"
    xdg = isolation_dir / "xdg"
    home.mkdir(parents=True, exist_ok=True)
    xdg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for name in tuple(env):
        if (
            name in _GIT_ENVIRONMENT_KEYS
            or name == "GIT_CONFIG_COUNT"
            or name.startswith(_GIT_ENVIRONMENT_PREFIXES)
        ):
            env.pop(name, None)
    env.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg),
        }
    )
    if token:
        if remote_url is None:
            raise HubError("remote_url is required when configuring a Git token")
        parts = urlsplit(remote_url)
        if parts.scheme in ("http", "https") and parts.netloc:
            scope = f"{parts.scheme}://{parts.netloc}/"
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"http.{scope}.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: token {token}",
                }
            )
    return env


def run_git(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int = GIT_TIMEOUT_SECONDS,
) -> str:
    """Run Git and normalize execution, timeout, and command failures."""

    try:
        result = subprocess.run(
            ("git", *SAFE_GIT_CONFIG, *arguments),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        raise HubError("could not execute git") from None
    except subprocess.TimeoutExpired:
        command = " ".join(arguments[:2])
        raise HubError(
            f"git {command} exceeded its {timeout_seconds}s timeout"
        ) from None
    except OSError as error:
        raise HubError(f"could not execute git: {error}") from None
    if result.returncode:
        command = " ".join(arguments[:2])
        detail = (result.stderr or result.stdout).strip()[:300]
        raise HubError(f"git {command} failed: {detail}")
    return result.stdout


def ensure_git_revision(
    *,
    cache_root: Path,
    remote_url: str,
    revision: str,
) -> CachedGitRevision:
    """Ensure ``revision`` is present in a locked bare mirror and resolve it.

    The local mirror is tried first, so exact-SHA warm hits never contact the
    remote; the mirror is fetched only when the revision is not yet resolvable
    locally.
    """

    if not revision.strip():
        raise HubError("Git revision must be non-empty")
    cache_root = cache_root.expanduser().resolve()
    origin = canonical_git_remote(remote_url)
    mirror = git_mirror_path(cache_root, origin)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(mirror.with_name(f"{mirror.name}.lock")))
    with lock:
        environment = git_environment(isolation_dir=cache_root / ".git-environment")
        fetched = _ensure_mirror(mirror, origin, environment)
        commit = _try_resolve_revision(mirror, revision, environment)
        if commit is None:
            fetched = True
            _fetch_mirror(mirror, revision, environment)
            commit = _try_resolve_revision(mirror, revision, environment)
        if commit is None:
            raise HubError(f"Git revision {revision!r} is not available in {origin}")
        return CachedGitRevision(
            remote_url=origin,
            revision=revision,
            commit=commit,
            mirror_path=mirror,
            fetched=fetched,
        )


def materialize_git_revision(
    *,
    cache_root: Path,
    remote_url: str,
    revision: str,
    destination: Path,
) -> CachedGitRevision:
    """Create a self-contained detached checkout from the shared mirror cache.

    The destination must not already exist. The local clone may use filesystem
    hardlinks, but it never writes a Git alternates file, so the materialized
    checkout stays usable after the mirror is moved or garbage-collected.
    """

    cache_root = cache_root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    resolved = ensure_git_revision(
        cache_root=cache_root,
        remote_url=remote_url,
        revision=revision,
    )
    if destination.exists():
        raise HubError(f"checkout destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    environment = git_environment(isolation_dir=cache_root / ".git-environment")
    try:
        run_git(
            (
                "clone",
                "--no-checkout",
                "--local",
                "--",
                str(resolved.mirror_path),
                str(temporary),
            ),
            cwd=destination.parent,
            env=environment,
        )
        run_git(
            ("remote", "set-url", "origin", resolved.remote_url),
            cwd=temporary,
            env=environment,
        )
        run_git(
            (
                "-c",
                "advice.detachedHead=false",
                "checkout",
                "--detach",
                resolved.commit,
            ),
            cwd=temporary,
            env=environment,
        )
        alternates = temporary / ".git" / "objects" / "info" / "alternates"
        if alternates.exists():
            raise HubError(f"checkout unexpectedly borrows objects: {alternates}")
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return resolved


def canonical_git_remote(remote_url: str) -> str:
    """Return a credential-free URL used for cache identity and Git origin."""

    value = remote_url.strip()
    if not value:
        raise HubError("Git remote URL must be non-empty")
    parsed = urlsplit(value)
    if not parsed.scheme:
        return Path(value).expanduser().resolve().as_uri()
    if parsed.scheme == "file":
        path = unquote(parsed.path)
        return Path(path).expanduser().resolve().as_uri()
    hostname = parsed.hostname
    if not hostname:
        raise HubError(f"Git remote URL has no host: {remote_url!r}")
    netloc = hostname.lower()
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def git_mirror_path(cache_root: Path, remote_url: str) -> Path:
    """Return the deterministic bare-mirror path for ``remote_url``."""

    cache_root = cache_root.expanduser().resolve()
    origin = canonical_git_remote(remote_url)
    parsed = urlsplit(origin)
    scheme = _safe_path_part(parsed.scheme or "local")
    host = _safe_path_part(parsed.netloc or "local")
    raw_path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    raw_leaf = raw_path_parts.pop() if raw_path_parts else "repository"
    if raw_leaf.endswith(".git"):
        raw_leaf = raw_leaf[:-4]
    path_parts = [_safe_path_part(part) for part in raw_path_parts]
    leaf = _safe_path_part(raw_leaf or "repository")
    digest = hashlib.sha256(origin.encode()).hexdigest()[:12]
    mirror = cache_root.joinpath(
        "git", scheme, host, *path_parts, f"{leaf}-{digest}.git"
    )
    try:
        mirror.relative_to(cache_root)
    except ValueError:
        raise HubError(f"Git mirror cache path escapes cache root: {mirror}") from None
    return mirror


def hub_checkout_head(start: Path) -> str:
    """Return the HEAD commit for the Hub checkout containing ``start``."""

    cwd = start.expanduser().resolve()
    if not cwd.is_dir():
        cwd = cwd.parent
    with tempfile.TemporaryDirectory(prefix="swecraft-common-git-") as raw:
        environment = git_environment(isolation_dir=Path(raw) / ".git-environment")
        value = run_git(("rev-parse", "HEAD"), cwd=cwd, env=environment).strip()
    if not value:
        raise HubError(f"Hub checkout has an empty HEAD: {cwd}")
    return value


def _ensure_mirror(mirror: Path, origin: str, env: dict[str, str]) -> bool:
    if mirror.exists():
        if not mirror.joinpath("HEAD").is_file():
            raise HubError(f"Git mirror cache is not a bare repository: {mirror}")
        cached_origin = run_git(("remote", "get-url", "origin"), cwd=mirror, env=env)
        if canonical_git_remote(cached_origin.strip()) != origin:
            raise HubError(
                f"Git mirror cache origin mismatch for {mirror}: "
                f"{cached_origin.strip()!r} != {origin!r}"
            )
        return False

    temporary = mirror.parent / f".{mirror.stem}.{uuid4().hex}.tmp.git"
    try:
        run_git(
            ("clone", "--mirror", "--", origin, str(temporary)),
            cwd=mirror.parent,
            env=env,
        )
        temporary.replace(mirror)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return True


def _fetch_mirror(mirror: Path, revision: str, env: dict[str, str]) -> None:
    run_git(("fetch", "--prune", "--force", "--tags", "origin"), cwd=mirror, env=env)
    if _is_sha(revision) and _try_resolve_revision(mirror, revision, env) is None:
        run_git(("fetch", "--force", "origin", revision), cwd=mirror, env=env)


def _try_resolve_revision(
    mirror: Path, revision: str, env: dict[str, str]
) -> str | None:
    try:
        value = run_git(
            ("rev-parse", "--verify", f"{revision}^{{commit}}"),
            cwd=mirror,
            env=env,
        ).strip()
    except HubError:
        return None
    if not _is_sha(value):
        return None
    return value.lower()


def _is_sha(value: str) -> bool:
    return bool(_SHA.fullmatch(value.lower()))


def _safe_path_part(value: str) -> str:
    if value == ".":
        return "_dot"
    if value == "..":
        return "_dotdot"
    safe = quote(value, safe="-._")
    return safe or "_"
