"""Clone one repository, run one Harbor trial, and collect its changes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from swecraft_runner import forge_actions
from swecraft_runner.pricing import ModelPricing, agent_usage

HARBOR_VERSION = "0.22.0"
_RUNNER_PROJECT = Path(__file__).resolve().parents[2]
# The default Harbor invocation: the pinned Harbor's own CLI behind the
# runner's `swecraft-harbor` entry point (swecraft_runner.harbor_cli), run from the
# runner's project. That puts two things inside Harbor's process that plain
# `uvx harbor` cannot: the in-process fixes, applied before Harbor
# starts, and the agent classes in swecraft_runner.harbor_agents, which Harbor
# loads by import path. An explicit ``harbor_command`` (a site's own
# launcher) replaces it verbatim and is then responsible for both; the
# version check applies either way.
DEFAULT_HARBOR_COMMAND = (
    "uv",
    "run",
    "--project",
    str(_RUNNER_PROJECT),
    "--with",
    f"harbor=={HARBOR_VERSION}",
    "swecraft-harbor",
)
GIT_TIMEOUT_SECONDS = 600
MIN_REDACTABLE_SECRET_LENGTH = 8
# What every sandbox gets unless the caller sets the same name in ``agent_env``.
# A task container sees all of the host's cores, so a tool that sizes itself by
# the core count overruns the container's memory cap: `pytest -n auto` started
# 256 xdist workers on a 256-core host and the turn was OOM-killed at 16 GiB,
# twice in a row for the same maintainer (CI jobs taught the same lesson).
# pytest-xdist reads this variable for `-n auto` and
# `-n logical` only; an explicit `-n 32` is still the agent's own choice.
SANDBOX_DEFAULT_ENV = {"PYTEST_XDIST_AUTO_NUM_WORKERS": "8"}
STRUCTURED_ARTIFACT = "/app/result.json"
# What the wake did to the forge. The image's wrappers write one file
# per recorded invocation here; the runner reads the directory and derives the
# ledger on the way out. Optional and never required: a wake that only read the
# forge records nothing, and a run whose agent never shelled out to `fj` is not
# a failed run.
FORGE_CALLS_ARTIFACT = "/app/.swecraft/forge-calls"
MEMORY_ARTIFACT = "/app/memory"
# Cross-run session resume: prior transcripts mount here, and the resuming
# agent subclass (loaded via Harbor's import_path, which the default Harbor
# command makes possible) turns them into `claude --continue`.
PRIOR_SESSION_MOUNT = "/app/prior-session"
RESUME_AGENT_IMPORT = "swecraft_runner.harbor_agents:ResumingClaudeCode"
RESUME_ENV = "SWECRAFT_RESUME"
# In-container transcript location, mirrored host-side under the trial dir.
_SESSION_RELATIVE = ("agent", "sessions", "projects", "-app")
# Extra wall-clock allowance beyond the agent timeout for image pulls,
# environment builds, and artifact collection before the process backstop.
HARBOR_OVERHEAD_SECONDS = 900
_SENSITIVE_AGENT_KWARG = re.compile(
    r"(?:api_?key|secret|token|password|credential)", re.IGNORECASE
)
_PINNED_IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_IMPORT_PATH = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*"
)
DEFAULT_DIND_IMAGE = (
    "docker:29.1.3-dind-rootless@"
    "sha256:0d57aab2a0ae0f891f021bce7902828f839aa324d41e82028e51e19759dc28ed"
)


class RunnerError(RuntimeError):
    """Raised when a one-shot run cannot produce a trustworthy result.

    When the failure happened inside an identified run, ``run_id`` and
    ``run_dir`` locate its directory so callers can still account for what
    the failed run consumed (Harbor's trial receipts record token usage and
    cost even for failed and timed-out trials).
    """

    run_id: str | None = None
    run_dir: Path | None = None


@dataclass(frozen=True)
class DockerDaemonConfig:
    """One disposable Docker daemon reachable only from this agent turn.

    Harbor starts the digest-pinned rootless daemon in a privileged sidecar
    (required by the official rootless DinD image). The agent receives its
    private TCP endpoint through ``DOCKER_HOST``; the
    host Docker socket is never mounted into either service.

    ``preload_images`` names host images copied read-only into the fresh
    daemon before the agent starts, so turns do not rebuild large base images
    inside an empty cache. Each image is exported once into
    ``preload_cache_dir`` keyed by its image ID and re-exported only when the
    host image changes. Seeding is a one-way copy at startup: no writable
    state is shared between turns.
    """

    image: str = DEFAULT_DIND_IMAGE
    preload_images: tuple[str, ...] = ()
    preload_cache_dir: Path = Path(".swecraft/cache/images")


@dataclass(frozen=True)
class WorkRepository:
    """A further repository staged beside the home checkout, at ``/app/repos/<name>``.

    ``/app/repository`` is the turn's home: its changes are collected and
    compared against the input tree. A work repository is staged for the
    agent to work in the same way, a real checkout with history, but nothing
    is collected from it: a direct-action run points its ``origin`` at
    ``push_remote_url``, authenticated like the home's, so work there reaches
    the forge only by being pushed; a run without direct action discards
    whatever happened there. The push remote must share the origin of the
    request's ``push_remote_url``, since git's authentication is scoped to
    that origin alone.
    """

    name: str
    repository: str
    revision: str = "HEAD"
    push_remote_url: str | None = None


@dataclass(frozen=True)
class RunRequest:
    repository: str
    revision: str
    prompt: str
    model: str
    output_dir: Path = Path(".swecraft/runs")
    agent: str = "claude-code"
    image: str = "node:22-slim"
    tool_directory: Path | None = None
    # Optional per-turn Docker authority. This is deliberately not a host
    # socket mount: Harbor creates a fresh rootless DinD compose sidecar and
    # destroys its containers, images, and volume with the trial.
    docker_daemon: DockerDaemonConfig | None = None
    # Repository path prefixes always included in change collection even
    # when the worktree's ignore rules hide them. Needed by callers whose
    # agent contract lives under an ignored path (e.g. a repository whose
    # .gitignore starts with `.*` would otherwise make .swecraft-eval/ edits
    # invisible and the turn look like it changed nothing).
    collect_prefixes: tuple[str, ...] = ()
    agent_env_names: tuple[str, ...] = ()
    # Agent-scoped variables supplied by value rather than read from this
    # process's environment. Callers running several turns concurrently must
    # use this: names are resolved from a single global environment, so two
    # in-flight turns would otherwise race over the same variable — and with
    # per-agent credentials the loser would run under another agent's token.
    agent_env: Mapping[str, str] = field(default_factory=dict)
    # Non-secret constructor options forwarded through Harbor AgentConfig.kwargs.
    # Credentials belong in agent_env, which has dedicated redaction semantics.
    agent_kwargs: Mapping[str, Any] = field(default_factory=dict)
    # Optional fallback for Harbor agents that report token counters but leave
    # cost_usd unset. Provider-reported cost always takes precedence.
    model_pricing: ModelPricing | None = None
    harbor_command: tuple[str, ...] = DEFAULT_HARBOR_COMMAND
    harbor_version: str = HARBOR_VERSION
    timeout_seconds: int = 3_600
    structured_output: bool = False
    # With lenient_structured, a present-but-malformed /app/result.json is
    # treated as absent instead of failing the run — for callers whose
    # agents perform external side effects before writing the result, where
    # a formatting slip must not turn a completed turn into a failure.
    lenient_structured: bool = False
    # 0 fetches full history; a positive value produces a shallow fetch.
    fetch_depth: int = 0
    extra_refs: tuple[str, ...] = ()
    # Optional durable memory workspace: a second checkout mounted at
    # /app/memory whose post-turn changes are collected separately.
    memory_repository: str | None = None
    memory_revision: str = "agent/memory"
    # Further repositories staged as real checkouts under /app/repos/<name>
    # (see WorkRepository): the agent works in them the way it works in
    # /app/repository, but nothing is collected from them.
    work_repositories: tuple[WorkRepository, ...] = ()
    # Directory of prior claude-code transcripts (*.jsonl); when set, the
    # turn starts with that conversation natively loaded (`--continue`).
    resume_bundle: Path | None = None
    # Launch a Harbor agent class by import path (``module.path:ClassName``)
    # instead of the stock agent named by ``agent``, which then only labels
    # the run. The class must be importable inside Harbor's process: the
    # default Harbor command runs from the runner's project, so a class
    # shipped in ``swecraft_runner`` (``harbor_agents:SwecraftPi``) needs
    # nothing else; an explicit ``harbor_command`` has to provide it itself.
    agent_import_path: str | None = None
    # Direct-action mode: the agent may push to its origin remote. The
    # checkout's origin is rewritten to the container-reachable
    # push_remote_url, and git authenticates through environment-provided
    # config carrying the token named by git_auth_token_env (which must be
    # one of agent_env_names). Off by default: the standard contract
    # forbids pushing entirely.
    allow_remote_writes: bool = False
    push_remote_url: str | None = None
    git_auth_token_env: str | None = None
    git_author_name: str | None = None
    git_author_email: str | None = None
    # A variable in agent_env_names or agent_env whose value authenticates
    # the runner's own host-side fetches (the repository, the memory branch,
    # the work repositories), as an ``Authorization: token`` header scoped
    # to each fetched URL's origin. A private repository needs it; it is
    # independent of allow_remote_writes, since an orientation turn reads
    # without pushing. The value travels in the git subprocesses'
    # environment only, never on a command line or in a log.
    fetch_auth_token_env: str | None = None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    source_sha: str
    response_path: Path
    changes_path: Path | None
    changed_files: tuple[str, ...]
    result_path: Path
    structured: dict[str, Any] | None = None
    structured_path: Path | None = None
    repository_path: Path | None = None
    extra_ref_shas: dict[str, str] = field(default_factory=dict)
    memory_source_sha: str | None = None
    # The collected final memory worktree. memory_source_sha is its trusted
    # starting point; publishers must not trust the agent-accessible .git state.
    memory_workspace: Path | None = None
    memory_changed_files: tuple[str, ...] = ()
    # This turn's claude-code transcripts, usable as a later resume_bundle.
    session_path: Path | None = None
    session_files: tuple[str, ...] = ()
    # Resolved commit of each staged work repository, by name.
    work_repository_shas: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _FileState:
    kind: str
    digest: str | None = None
    target: str | None = None
    executable: bool = False

    def as_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind}
        if self.digest is not None:
            value["sha256"] = self.digest
        if self.target is not None:
            value["target"] = self.target
        if self.executable:
            value["executable"] = True
        return value


def run_once(request: RunRequest) -> RunResult:
    """Execute one isolated Harbor agent turn.

    The source checkout is never modified: Harbor receives a copy, and the
    collected copy is compared against the exact input tree after the trial.
    """

    _validate_request(request)
    run_id = _run_id()
    run_dir = request.output_dir.resolve() / run_id
    input_dir = run_dir / "input"
    output_dir = run_dir / "output"
    harbor_dir = run_dir / "harbor"
    checkout = input_dir / "repository"
    prompt_path = input_dir / "prompt.md"
    output_dir.mkdir(parents=True)

    result_path = output_dir / "result.json"
    secrets: tuple[str, ...] = ()
    # Bound before the try so the failure handler can still reach Harbor's
    # artifacts when the run breaks after the trial returned.
    trial_dir: Path | None = None
    try:
        fetch_token = _fetch_token(request)
        repository = _normalized_repository(request.repository)
        source_sha, extra_ref_shas = _checkout(
            repository,
            request.revision,
            checkout,
            run_dir,
            fetch_depth=request.fetch_depth,
            extra_refs=request.extra_refs,
            env=_fetch_environment(repository, fetch_token),
        )
        if request.allow_remote_writes:
            assert request.push_remote_url is not None
            _set_origin_url(checkout, request.push_remote_url)
        memory_checkout, memory_source_sha = None, None
        if request.memory_repository is not None:
            memory_checkout = input_dir / "memory"
            memory_repository = _normalized_repository(request.memory_repository)
            memory_source_sha, _ = _checkout(
                memory_repository,
                request.memory_revision,
                memory_checkout,
                run_dir,
                fetch_depth=1,
                extra_refs=(),
                log_name="git-memory.log",
                env=_fetch_environment(memory_repository, fetch_token),
            )
        repos_dir, work_shas = _stage_work_repositories(
            request, input_dir, run_dir, fetch_token
        )
        tools = _stage_tools(request.tool_directory, input_dir)
        prior_session = _stage_resume_bundle(request.resume_bundle, input_dir)
        agent_environment = _stage_agent_environment(
            request,
            input_dir,
            checkout=checkout,
            tools=tools,
            memory=memory_checkout,
            repos=repos_dir,
            prior_session=prior_session,
        )
        prompt_path.write_text(
            _compiled_prompt(
                request.prompt,
                tools,
                request.structured_output,
                request.extra_refs,
                memory_checkout is not None,
                request.allow_remote_writes,
                docker_daemon=request.docker_daemon is not None,
                work_repositories=request.work_repositories,
            ),
            encoding="utf-8",
        )
        env_values = {
            **SANDBOX_DEFAULT_ENV,
            **_agent_environment(request.agent_env_names),
            **request.agent_env,
        }
        # Only caller-supplied values are secrets; derived git config entries
        # embed the token but are otherwise plain configuration.
        secrets = _redactable_secrets(env_values)
        if request.allow_remote_writes:
            env_values = {**env_values, **_direct_git_env(request, env_values)}
        _write_json(
            input_dir / "request.json",
            {
                "repository": request.repository,
                "requested_ref": request.revision,
                "resolved_sha": source_sha,
                "fetch_depth": request.fetch_depth,
                "extra_refs": extra_ref_shas,
                "agent": request.agent,
                "model": request.model,
                "model_pricing": (
                    request.model_pricing.as_dict() if request.model_pricing else None
                ),
                "agent_kwargs": dict(request.agent_kwargs),
                "agent_import_path": request.agent_import_path,
                "image": request.image,
                "harbor_version": request.harbor_version,
                "docker_daemon": (
                    {
                        "image": request.docker_daemon.image,
                        "preload_images": list(request.docker_daemon.preload_images),
                    }
                    if request.docker_daemon is not None
                    else None
                ),
                "agent_environment": list(request.agent_env_names),
                "tools": tools is not None,
                "memory_repository": request.memory_repository,
                "memory_resolved_sha": memory_source_sha,
                "work_repositories": _work_repository_records(request, work_shas),
                "fetch_auth_token_env": request.fetch_auth_token_env,
                "resume": prior_session is not None,
                "direct_action": request.allow_remote_writes,
            },
        )
        trial_dir, trial_result = _invoke_harbor(
            request=request,
            run_id=run_id,
            checkout=checkout,
            tools=tools,
            memory=memory_checkout,
            repos=repos_dir,
            prior_session=prior_session,
            prompt_path=prompt_path,
            harbor_dir=harbor_dir,
            agent_environment=agent_environment,
            env_values=env_values,
            secrets=secrets,
        )
        (
            response_path,
            collected_repository,
            structured,
            structured_path,
            collected_memory,
        ) = _collected_artifacts(
            trial_dir,
            output_dir,
            collect_structured=request.structured_output,
            collect_memory=memory_checkout is not None,
            lenient_structured=request.lenient_structured,
        )
        changes_path, changed_files, changes = _collect_changes(
            checkout,
            collected_repository,
            output_dir,
            collect_prefixes=request.collect_prefixes,
        )
        memory_changed_files: tuple[str, ...] = ()
        if collected_memory is not None:
            assert memory_checkout is not None
            memory_changed_files = _changed_paths(memory_checkout, collected_memory)
        forge_actions_path, forge_calls_path = _collect_forge_actions(
            trial_dir / "artifacts" / "app",
            output_dir,
            repository=request.repository,
            secrets=secrets,
        )
        session_path, session_files = _collect_session(trial_dir, output_dir)
        agent_result = trial_result.get("agent_result") or {}
        receipt = {
            "run_id": run_id,
            "status": "completed",
            "repository": request.repository,
            "requested_ref": request.revision,
            "resolved_sha": source_sha,
            "fetch_depth": request.fetch_depth,
            "extra_refs": extra_ref_shas,
            "harbor_version": request.harbor_version,
            "agent": request.agent,
            "model": request.model,
            "model_pricing": (
                request.model_pricing.as_dict() if request.model_pricing else None
            ),
            "agent_kwargs": dict(request.agent_kwargs),
            "agent_import_path": request.agent_import_path,
            "image": request.image,
            "docker_daemon": (
                {
                    "image": request.docker_daemon.image,
                    "preload_images": list(request.docker_daemon.preload_images),
                }
                if request.docker_daemon is not None
                else None
            ),
            "changed": bool(changed_files),
            "changed_files": list(changed_files),
            "response": str(response_path.relative_to(run_dir)),
            "changes": str(changes.relative_to(run_dir)),
            "forge_actions": (
                str(forge_actions_path.relative_to(run_dir))
                if forge_actions_path is not None
                else None
            ),
            "forge_calls": (
                str(forge_calls_path.relative_to(run_dir))
                if forge_calls_path is not None
                else None
            ),
            "structured": structured is not None,
            "memory_resolved_sha": memory_source_sha,
            "memory_changed_files": list(memory_changed_files),
            "work_repositories": _work_repository_records(request, work_shas),
            "resume": prior_session is not None,
            "direct_action": request.allow_remote_writes,
            "session_files": list(session_files),
            "usage": agent_usage(agent_result, request.model_pricing),
        }
        _write_json(result_path, receipt)
        return RunResult(
            run_id=run_id,
            run_dir=run_dir,
            source_sha=source_sha,
            response_path=response_path,
            changes_path=changes_path,
            changed_files=changed_files,
            result_path=result_path,
            repository_path=collected_repository,
            structured=structured,
            structured_path=structured_path,
            extra_ref_shas=extra_ref_shas,
            memory_source_sha=memory_source_sha,
            memory_workspace=collected_memory,
            memory_changed_files=memory_changed_files,
            session_path=session_path,
            session_files=session_files,
            work_repository_shas=work_shas,
        )
    except Exception as error:
        message = _redact(str(error), secrets)
        # A failed wake is exactly where the ledger matters most: the run broke
        # after Harbor returned, and `failed` already means "the agent may
        # already have acted on the forge". Dropping the record here would lose
        # the side effects that outlived the run.
        #
        # Best effort by construction — this is the failure path, and a second
        # exception raised while writing the receipt would mask the first.
        failed_forge_actions: Path | None = None
        failed_forge_calls: Path | None = None
        if trial_dir is not None:
            try:
                failed_forge_actions, failed_forge_calls = _collect_forge_actions(
                    trial_dir / "artifacts" / "app",
                    output_dir,
                    repository=request.repository,
                    secrets=secrets,
                )
            except Exception:  # noqa: BLE001 - never mask the original failure
                failed_forge_actions = None
                failed_forge_calls = None
        _write_json(
            result_path,
            {
                "run_id": run_id,
                "status": "failed",
                "error": message,
                "forge_actions": (
                    str(failed_forge_actions.relative_to(run_dir))
                    if failed_forge_actions is not None
                    else None
                ),
                "forge_calls": (
                    str(failed_forge_calls.relative_to(run_dir))
                    if failed_forge_calls is not None
                    else None
                ),
            },
        )
        if isinstance(error, RunnerError) and message == str(error):
            error.run_id = run_id
            error.run_dir = run_dir
            raise
        failure = RunnerError(message)
        failure.run_id = run_id
        failure.run_dir = run_dir
        raise failure from error


def _validate_request(request: RunRequest) -> None:
    if not request.repository.strip():
        raise RunnerError("repository must not be empty")
    if request.repository.strip().startswith("-"):
        raise RunnerError("repository must not start with '-'")
    if not request.revision.strip():
        raise RunnerError("revision must not be empty")
    if request.revision.strip().startswith("-"):
        raise RunnerError("revision must not start with '-'")
    if not request.prompt.strip():
        raise RunnerError("prompt must not be empty")
    if not request.model.strip():
        raise RunnerError("model must not be empty")
    if not request.agent.strip():
        raise RunnerError("agent must not be empty")
    if not request.harbor_command:
        raise RunnerError("Harbor command must not be empty")
    if request.timeout_seconds < 1:
        raise RunnerError("timeout must be at least one second")
    if request.docker_daemon is not None and not _PINNED_IMAGE.fullmatch(
        request.docker_daemon.image
    ):
        raise RunnerError(
            "docker daemon image must be pinned as NAME@sha256:<64 lowercase hex>"
        )
    if request.fetch_depth < 0:
        raise RunnerError("fetch depth must be zero (full) or positive (shallow)")
    if len(set(request.extra_refs)) != len(request.extra_refs):
        raise RunnerError("extra refs must be unique")
    for ref in request.extra_refs:
        if not _valid_branch_name(ref):
            raise RunnerError(f"invalid extra ref name: {ref!r}")
    if request.memory_repository is not None:
        if not request.memory_repository.strip():
            raise RunnerError("memory repository must not be empty")
        if request.memory_repository.strip().startswith("-"):
            raise RunnerError("memory repository must not start with '-'")
        if not request.memory_revision.strip():
            raise RunnerError("memory revision must not be empty")
        if request.memory_revision.strip().startswith("-"):
            raise RunnerError("memory revision must not start with '-'")
    if request.resume_bundle is not None and request.agent != "claude-code":
        raise RunnerError("session resume is only supported for claude-code")
    if request.agent_import_path is not None:
        if not _IMPORT_PATH.fullmatch(request.agent_import_path):
            raise RunnerError(
                "agent_import_path must be of the form module.path:ClassName"
            )
        if request.resume_bundle is not None:
            raise RunnerError(
                "session resume cannot be combined with agent_import_path"
            )
    if request.lenient_structured and not request.structured_output:
        raise RunnerError("lenient_structured requires structured_output")
    if len(set(request.agent_env_names)) != len(request.agent_env_names):
        raise RunnerError("agent environment variable names must be unique")
    overlap = set(request.agent_env_names) & set(request.agent_env)
    if overlap:
        raise RunnerError(
            "agent environment variable is given both by name and by value: "
            f"{', '.join(sorted(overlap))}"
        )
    for name in (*request.agent_env_names, *request.agent_env):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise RunnerError(f"invalid agent environment variable name: {name}")
        if name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            raise RunnerError(
                f"agent environment variable name is reserved for git: {name}"
            )
    for name in request.agent_kwargs:
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", name
        ):
            raise RunnerError(f"invalid agent kwarg name: {name!r}")
        if _SENSITIVE_AGENT_KWARG.search(name):
            raise RunnerError(
                f"secret-like agent kwarg {name!r} must be passed through agent_env"
            )
    try:
        json.dumps(dict(request.agent_kwargs), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RunnerError(f"agent kwargs must be JSON values: {error}") from None
    if request.fetch_auth_token_env is not None and (
        request.fetch_auth_token_env
        not in (*request.agent_env_names, *request.agent_env)
    ):
        raise RunnerError(
            "fetch_auth_token_env must be supplied in agent_env_names or agent_env"
        )
    _validate_work_repositories(request)
    direct_fields = (
        request.push_remote_url,
        request.git_auth_token_env,
        request.git_author_name,
        request.git_author_email,
    )
    if not request.allow_remote_writes:
        if any(value is not None for value in direct_fields):
            raise RunnerError("direct-action fields require allow_remote_writes=True")
        return
    if any(value is None for value in direct_fields):
        raise RunnerError(
            "allow_remote_writes requires push_remote_url, git_auth_token_env, "
            "git_author_name, and git_author_email"
        )
    assert request.push_remote_url is not None
    if not request.push_remote_url.startswith(("http://", "https://")):
        raise RunnerError("push_remote_url must be an http(s) URL")
    if request.git_auth_token_env not in (
        *request.agent_env_names,
        *request.agent_env,
    ):
        raise RunnerError(
            "git_auth_token_env must be supplied in agent_env_names or agent_env"
        )
    origin = _url_origin(request.push_remote_url)
    for work in request.work_repositories:
        if work.push_remote_url is None:
            continue
        if _url_origin(work.push_remote_url) != origin:
            raise RunnerError(
                f"work repository push remote for {work.name!r} must share "
                "push_remote_url's origin: git authenticates only there"
            )


_WORK_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _validate_work_repositories(request: RunRequest) -> None:
    names = [work.name for work in request.work_repositories]
    if len(set(names)) != len(names):
        raise RunnerError("work repository names must be unique")
    for work in request.work_repositories:
        if not _WORK_REPOSITORY_NAME.fullmatch(work.name) or work.name in (
            ".",
            "..",
        ):
            raise RunnerError(f"invalid work repository name: {work.name!r}")
        source = work.repository.strip()
        if not source or source.startswith("-"):
            raise RunnerError(
                f"invalid work repository source for {work.name!r}: {source!r}"
            )
        revision = work.revision.strip()
        if not revision or revision.startswith("-"):
            raise RunnerError(
                f"invalid work repository revision for {work.name!r}: {revision!r}"
            )
        if work.push_remote_url is None:
            continue
        if not request.allow_remote_writes:
            raise RunnerError(
                "work repository push remotes require allow_remote_writes=True"
            )
        if not work.push_remote_url.startswith(("http://", "https://")):
            raise RunnerError(
                f"work repository push remote for {work.name!r} must be an http(s) URL"
            )


def _url_origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def _valid_branch_name(name: str) -> bool:
    """Defer to git's own branch-name rules instead of approximating them.

    Rejects names like foo.lock and foo//bar that a permissive regex lets
    through, and accepts valid names like release@2026 that it rejects.
    """

    if not name or name.startswith("-") or "@{" in name:
        return False
    try:
        result = subprocess.run(
            ("git", "check-ref-format", "--branch", name),
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _normalized_repository(repository: str) -> str:
    candidate = Path(repository).expanduser()
    return str(candidate.resolve()) if candidate.exists() else repository


def _checkout(
    repository: str,
    revision: str,
    destination: Path,
    run_dir: Path,
    *,
    fetch_depth: int,
    extra_refs: tuple[str, ...],
    log_name: str = "git.log",
    env: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / log_name
    depth = (f"--depth={fetch_depth}",) if fetch_depth > 0 else ()
    commands = [
        ("git", "init", "--initial-branch=main", str(destination)),
        ("git", "-C", str(destination), "remote", "add", "origin", repository),
        (
            "git",
            "-C",
            str(destination),
            "fetch",
            *depth,
            "--no-tags",
            # Stop option parsing so an option-like revision can never be
            # interpreted as a flag such as --upload-pack.
            "--end-of-options",
            "origin",
            revision,
        ),
        (
            "git",
            "-C",
            str(destination),
            "-c",
            "advice.detachedHead=false",
            "checkout",
            "--detach",
            "FETCH_HEAD",
        ),
    ]
    commands.extend(
        (
            "git",
            "-C",
            str(destination),
            "fetch",
            *depth,
            "--no-tags",
            "origin",
            f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
        )
        for ref in extra_refs
    )
    logs: list[str] = []
    for command in commands:
        _run_git(command, revision, logs, log_path, env=env)
    log_path.write_text("".join(logs), encoding="utf-8")
    source_sha = _rev_parse(destination, "HEAD")
    extra_ref_shas = {
        ref: _rev_parse(destination, f"refs/remotes/origin/{ref}") for ref in extra_refs
    }
    return source_sha, extra_ref_shas


def _run_git(
    command: tuple[str, ...],
    revision: str,
    logs: list[str],
    log_path: Path,
    env: dict[str, str] | None = None,
) -> None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log_path.write_text("".join(logs), encoding="utf-8")
        raise RunnerError(
            f"Git timed out after {GIT_TIMEOUT_SECONDS} seconds while "
            f"resolving {revision!r}; see {log_path}"
        ) from None
    logs.extend((result.stdout, result.stderr))
    if result.returncode:
        log_path.write_text("".join(logs), encoding="utf-8")
        raise RunnerError(f"Git failed while resolving {revision!r}; see {log_path}")


def _rev_parse(destination: Path, ref: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(destination), "rev-parse", "--verify", f"{ref}^{{commit}}"),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    sha = result.stdout.strip().lower()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RunnerError(f"Git could not resolve {ref!r} to a commit")
    return sha


def _stage_tools(tool_directory: Path | None, input_dir: Path) -> Path | None:
    if tool_directory is None:
        return None
    source = tool_directory.expanduser().resolve()
    if not source.is_dir():
        raise RunnerError(f"tool directory does not exist: {source}")
    destination = input_dir / "tools"
    shutil.copytree(source, destination, symlinks=True)
    return destination


def _ensure_image_tarball(image: str, cache_dir: Path) -> Path:
    """Export one host image to a cache tarball keyed by its image ID."""

    inspect = subprocess.run(
        ("docker", "image", "inspect", "--format", "{{.Id}}", image),
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode:
        raise RunnerError(
            f"preload image is not present on the host: {image}: "
            f"{inspect.stderr.strip()}"
        )
    image_id = inspect.stdout.strip().removeprefix("sha256:")[:16]
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", image)
    path = cache_dir / f"{safe_name}-{image_id}.tar"
    # The seed service reads the staged copy as the rootless DinD image's
    # uid-1000 user; docker save writes 0600, which is unreadable there
    # whenever the invoking host user is not uid 1000. The image content is
    # a public base image, so world-readable is safe.
    if path.is_file():
        path.chmod(0o644)
        return path
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    save = subprocess.run(
        ("docker", "save", "--output", str(temporary), image),
        capture_output=True,
        text=True,
        check=False,
    )
    if save.returncode:
        temporary.unlink(missing_ok=True)
        raise RunnerError(f"docker save failed for {image}: {save.stderr.strip()}")
    temporary.chmod(0o644)
    temporary.replace(path)
    return path


def _stage_agent_environment(
    request: RunRequest,
    input_dir: Path,
    *,
    checkout: Path,
    tools: Path | None,
    memory: Path | None,
    repos: Path | None,
    prior_session: Path | None,
) -> Path | None:
    """Build Harbor's trusted compose input for a per-turn DinD daemon.

    The ordinary runner lets Harbor copy each staged path into a prebuilt
    single-service task. Compose tasks are different: Harbor does not upload
    ``environment/`` when a compose file exists, so the generated file binds
    fresh *compiled copies* of the repository and optional workspaces into the
    main service. The original input checkouts remain untouched.
    """

    if request.docker_daemon is None:
        return None
    root = input_dir / "agent-environment"
    root.mkdir()
    mounts = [_stage_compose_mount(checkout, root / "repository", "/app/repository")]
    if tools is not None:
        mounts.append(
            _stage_compose_mount(tools, root / "tools", "/app/tools", read_only=True)
        )
    if memory is not None:
        mounts.append(_stage_compose_mount(memory, root / "memory", "/app/memory"))
    if repos is not None:
        mounts.append(_stage_compose_mount(repos, root / "repos", "/app/repos"))
    if prior_session is not None:
        mounts.append(
            _stage_compose_mount(
                prior_session,
                root / "prior-session",
                PRIOR_SESSION_MOUNT,
                read_only=True,
            )
        )

    main_depends: dict[str, dict[str, str]] = {"dind": {"condition": "service_healthy"}}
    seed_service: dict[str, object] | None = None
    if request.docker_daemon.preload_images:
        preload_dir = root / "preload"
        preload_dir.mkdir()
        for image_name in request.docker_daemon.preload_images:
            tarball = _ensure_image_tarball(
                image_name, request.docker_daemon.preload_cache_dir
            )
            shutil.copy2(tarball, preload_dir / tarball.name)
        seed_service = {
            "image": request.docker_daemon.image,
            "entrypoint": [
                "sh",
                "-c",
                # $$ keeps the variable literal through compose interpolation.
                'for archive in /preload/*.tar; do docker load -i "$$archive"; done',
            ],
            "environment": {
                "DOCKER_HOST": "tcp://dind:2375",
                "DOCKER_TLS_CERTDIR": "",
            },
            "depends_on": {"dind": {"condition": "service_healthy"}},
            "volumes": [
                {
                    "type": "bind",
                    "source": "./preload",
                    "target": "/preload",
                    "read_only": True,
                }
            ],
        }
        main_depends["seed"] = {"condition": "service_completed_successfully"}

    deadline = _turn_deadline_seconds(request)
    compose = {
        "services": {
            "main": {
                "depends_on": main_depends,
                # PID 1 is the turn's clock. Harbor's prebuilt compose runs
                # `sleep infinity` and enforces the agent timeout from the
                # host, so a turn whose supervisor dies (operator kill,
                # host wedge) keeps its agent and everything it spawned
                # running forever (sixty-odd orphaned turns once drove a
                # 128-core host to load 88k). A finite sleep as PID 1 means
                # the kernel reaps the whole container namespace on
                # schedule with no dependency on anything outside it.
                "command": ["sh", "-c", f"sleep {deadline}"],
                "environment": {
                    "DOCKER_HOST": "tcp://dind:2375",
                    "DOCKER_TLS_CERTDIR": "",
                },
                "volumes": mounts,
            },
            "dind": {
                "image": request.docker_daemon.image,
                "privileged": True,
                # The sidecar gets the same finite clock as main. Its own
                # daemon hosts every nested workload container the agent
                # or its shakeout creates, so when the sidecar's PID 1
                # exits, that daemon and everything it runs die with it —
                # nested `sleep infinity` runtimes included. Without this,
                # main expiring left the sidecar's workloads running with
                # no owner. busybox `timeout` ships in the
                # dind image; the entrypoint stays the image's own.
                "entrypoint": [
                    "timeout",
                    "-s",
                    "KILL",
                    str(deadline),
                    "dockerd-entrypoint.sh",
                ],
                "command": ["--host=tcp://0.0.0.0:2375", "--tls=false"],
                "environment": {
                    "DOCKER_HOST": "tcp://127.0.0.1:2375",
                    "DOCKER_TLS_CERTDIR": "",
                },
                "expose": ["2375"],
                "healthcheck": {
                    "test": ["CMD", "docker", "info"],
                    "interval": "1s",
                    "timeout": "5s",
                    "retries": 60,
                    "start_period": "2s",
                },
                "volumes": ["dind-data:/home/rootless/.local/share/docker"],
            },
        },
        "volumes": {"dind-data": {}},
    }
    if seed_service is not None:
        compose["services"]["seed"] = seed_service
    _write_json(root / "docker-compose.yaml", compose)
    return root


def _turn_deadline_seconds(request: RunRequest) -> int:
    """Wall-clock ceiling for one turn's container, enforced by PID 1.

    Harbor's own agent timeout normally fires first; this is the backstop
    that holds when Harbor does not. It leaves the same overhead allowance
    Harbor gets for environment build and artifact collection, so a turn
    that legitimately runs to its agent timeout still finishes collecting.
    """

    return int(request.timeout_seconds) + HARBOR_OVERHEAD_SECONDS


def _stage_compose_mount(
    source: Path, destination: Path, target: str, *, read_only: bool = False
) -> dict[str, object]:
    shutil.copytree(source, destination, symlinks=True)
    mount: dict[str, object] = {
        "type": "bind",
        "source": f"./{destination.name}",
        "target": target,
    }
    if read_only:
        mount["read_only"] = True
    return mount


def _stage_resume_bundle(bundle: Path | None, input_dir: Path) -> Path | None:
    """Copy prior transcripts into the mount that becomes /app/prior-session."""

    if bundle is None:
        return None
    source = bundle.expanduser().resolve()
    if not source.is_dir():
        raise RunnerError(f"resume bundle does not exist: {source}")
    destination = input_dir / "prior-session"
    destination.mkdir(parents=True)
    staged = _copy_transcript_tree(source, destination)
    if not staged:
        raise RunnerError(f"resume bundle has no transcripts (*.jsonl): {source}")
    return destination


def _fetch_token(request: RunRequest) -> str | None:
    name = request.fetch_auth_token_env
    if name is None:
        return None
    value = (
        request.agent_env[name] if name in request.agent_env else os.environ.get(name)
    )
    if not value:
        raise RunnerError(f"fetch_auth_token_env names an unset variable: {name}")
    return value


def _fetch_environment(repository: str, token: str | None) -> dict[str, str]:
    """The environment of a host-side git fetch.

    Never prompts, so a missing credential fails instead of hanging. With a
    token and an http(s) URL, the token rides as an ``Authorization``
    header scoped to that URL's origin through git's environment-provided
    configuration, the same shape the container gets for direct action:
    nothing on the command line, nothing in the log, and no other host
    the fetch might reach sees it. Any git configuration the host process
    carried in its environment is dropped first, so the numbered entries
    are exactly ours.
    """

    env = {
        name: value
        for name, value in os.environ.items()
        if name != "GIT_CONFIG_COUNT"
        and not name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    parts = urlsplit(repository)
    if token and parts.scheme in ("http", "https") and parts.netloc:
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{_url_origin(repository)}.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: token {token}",
            }
        )
    return env


def _stage_work_repositories(
    request: RunRequest, input_dir: Path, run_dir: Path, fetch_token: str | None
) -> tuple[Path | None, dict[str, str]]:
    """Check out every work repository under ``input/repos/<name>``.

    The directory is staged whole at ``/app/repos``, one checkout per name.
    A direct-action run points each ``origin`` at its push remote; the
    authenticated git configuration the home checkout gets is scoped to
    that remote's origin, which every work repository shares (validated),
    so one identity pushes to all of them.
    """

    if not request.work_repositories:
        return None, {}
    repos_dir = input_dir / "repos"
    repos_dir.mkdir()
    shas: dict[str, str] = {}
    for work in request.work_repositories:
        destination = repos_dir / work.name
        source = _normalized_repository(work.repository)
        sha, _ = _checkout(
            source,
            work.revision,
            destination,
            run_dir,
            fetch_depth=request.fetch_depth,
            extra_refs=(),
            log_name=f"git-repos-{work.name}.log",
            env=_fetch_environment(source, fetch_token),
        )
        if request.allow_remote_writes and work.push_remote_url is not None:
            _set_origin_url(destination, work.push_remote_url)
        shas[work.name] = sha
    return repos_dir, shas


def _work_repository_records(
    request: RunRequest, shas: dict[str, str]
) -> list[dict[str, str | None]]:
    return [
        {
            "name": work.name,
            "repository": work.repository,
            "requested_ref": work.revision,
            "resolved_sha": shas.get(work.name),
            "push_remote_url": work.push_remote_url,
        }
        for work in request.work_repositories
    ]


def _collect_session(
    trial_dir: Path, output_dir: Path
) -> tuple[Path | None, tuple[str, ...]]:
    """Copy this turn's claude-code transcripts out of the trial directory."""

    source = trial_dir
    for component in _SESSION_RELATIVE:
        source /= component
        if source.is_symlink():
            return None, ()
    if not source.is_dir():
        return None, ()
    destination = output_dir / "session"
    names = _copy_transcript_tree(source, destination)
    if not names:
        return None, ()
    return destination, names


def _copy_transcript_tree(source: Path, destination: Path) -> tuple[str, ...]:
    """Mirror the `*.jsonl` tree, preserving relative paths.

    Claude Code treats nested transcripts (e.g. `subagents/*.jsonl`) as part
    of the session, so collection and staging must be recursive. Symlinks —
    as files or as path components — are never followed.
    """

    names = []
    for path in sorted(source.rglob("*.jsonl")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source)
        parents = [
            source.joinpath(*relative.parts[:index])
            for index in range(1, len(relative.parts))
        ]
        if any(parent.is_symlink() for parent in parents):
            continue
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target, follow_symlinks=False)
        names.append(relative.as_posix())
    return tuple(names)


def _compiled_prompt(
    prompt: str,
    tools: Path | None,
    structured: bool,
    extra_refs: tuple[str, ...],
    memory: bool,
    direct_action: bool = False,
    docker_daemon: bool = False,
    work_repositories: tuple[WorkRepository, ...] = (),
) -> str:
    tool_note = (
        "Additional trusted tools and reference files are available under "
        "`/app/tools`; changes there are not collected.\n"
        if tools is not None
        else "No additional tool directory was supplied.\n"
    )
    structured_note = (
        "When the task above defines a structured result, write it as exactly "
        "one JSON object to `/app/result.json`; Hub collects that file too.\n"
        if structured
        else ""
    )
    refs_note = (
        "These branches are pre-fetched and addressable as `origin/<name>`: "
        f"{', '.join(extra_refs)}.\n"
        if extra_refs
        else ""
    )
    memory_note = (
        "A durable memory workspace is mounted at `/app/memory`; edits there "
        "are collected separately from the repository. Follow the task's "
        "guidance on what to record.\n"
        if memory
        else ""
    )
    collected_scope = (
        "`/app/repository` and `/app/memory`" if memory else "only that directory"
    )
    docker_note = (
        "A fresh isolated Docker daemon is available through `DOCKER_HOST`. "
        "It is not the host daemon and exposes no host Docker socket. Images, "
        "containers, and volumes in it disappear with this turn. Use "
        "`--network none` for final workload runs whose network must be closed.\n"
        if docker_daemon
        else ""
    )
    if work_repositories:
        staged = ", ".join(f"`/app/repos/{work.name}`" for work in work_repositories)
        outcome = (
            "each has an authenticated `origin` like `/app/repository`, and Hub "
            "collects nothing from them: work there reaches the forge only by "
            "being pushed.\n"
            if direct_action
            else "Hub collects nothing from them; changes there are discarded.\n"
        )
        work_note = (
            "Further repositories are staged as real git checkouts under "
            f"`/app/repos/<name>`: {staged}; {outcome}"
        )
    else:
        work_note = ""
    context_notes = f"{refs_note}{memory_note}{work_note}{tool_note}{docker_note}"
    if direct_action:
        push_contract = (
            "Your `origin` remote is authenticated as your own identity; you "
            "may fetch from it, create branches, and push them. Never "
            "force-push and never rewrite branches you did not create."
        )
        history_note = (
            "Use local git commands (`diff`, `log`, `blame`, `show`) for history."
        )
    else:
        push_contract = "Do not push to any remote."
        history_note = (
            "Use local git commands (`diff`, `log`, `blame`, `show`) for "
            "history; do not\nfetch from the network."
        )
    return f"""{prompt.strip()}

---

## Hub execution contract

Work on the repository at `/app/repository`; it is a real git checkout.
{history_note}
{context_notes}You may inspect, edit, and test the repository.
{push_contract}
Leave all repository changes in `/app/repository`; Hub collects {collected_scope}
after the turn, so treat every other writable path as scratch space.
{structured_note}Before finishing, write your answer or a concise summary of your
changes to `/app/response.md`. The run fails if it is missing or empty.
"""


def _set_origin_url(checkout: Path, url: str) -> None:
    """Point origin at a container-reachable URL for direct-action pushes."""

    result = subprocess.run(
        ("git", "-C", str(checkout), "remote", "set-url", "origin", url),
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise RunnerError(f"could not set the push remote URL: {result.stderr.strip()}")


def _direct_git_env(request: RunRequest, env_values: dict[str, str]) -> dict[str, str]:
    """Environment-provided git config for authenticated in-container pushes.

    The auth header is scoped to the push URL's origin so the token is never
    sent to any other host the agent might reach.
    """

    assert request.push_remote_url is not None
    assert request.git_auth_token_env is not None
    token = env_values[request.git_auth_token_env]
    parts = urlsplit(request.push_remote_url)
    scope = f"{parts.scheme}://{parts.netloc}/"
    entries = (
        (f"http.{scope}.extraheader", f"Authorization: token {token}"),
        ("user.name", request.git_author_name or ""),
        ("user.email", request.git_author_email or ""),
    )
    values: dict[str, str] = {"GIT_CONFIG_COUNT": str(len(entries))}
    for index, (key, value) in enumerate(entries):
        values[f"GIT_CONFIG_KEY_{index}"] = key
        values[f"GIT_CONFIG_VALUE_{index}"] = value
    return values


def _agent_environment(names: tuple[str, ...]) -> dict[str, str]:
    values = {}
    for name in names:
        value = os.environ.get(name)
        if value is None:
            raise RunnerError(f"agent environment variable is not set: {name}")
        values[name] = value
    return values


def _invoke_harbor(
    *,
    request: RunRequest,
    run_id: str,
    checkout: Path,
    tools: Path | None,
    memory: Path | None,
    repos: Path | None,
    prior_session: Path | None,
    prompt_path: Path,
    harbor_dir: Path,
    agent_environment: Path | None,
    env_values: dict[str, str],
    secrets: tuple[str, ...],
) -> tuple[Path, dict[str, Any]]:
    harbor_dir.mkdir(parents=True)
    command = request.harbor_command
    _check_harbor_version(command, request.harbor_version)
    paths = [str(checkout)]
    if tools is not None:
        paths.append(str(tools))
    if memory is not None:
        paths.append(str(memory))
    if repos is not None:
        paths.append(str(repos))
    if prior_session is not None:
        paths.append(str(prior_session))
    compile_environment: dict[str, object]
    if agent_environment is None:
        compile_environment = {
            "paths": paths,
            "docker_image": request.image,
            "workdir": "/app",
        }
    else:
        compile_environment = {
            "path": str(agent_environment),
            "docker_image": request.image,
            "workdir": "/app",
        }
    artifacts = ["/app/response.md", "/app/repository", FORGE_CALLS_ARTIFACT]
    if request.structured_output:
        artifacts.append(STRUCTURED_ARTIFACT)
    if memory is not None:
        artifacts.append(MEMORY_ARTIFACT)
    agent_config: dict[str, Any] = {
        "name": request.agent,
        "model_name": request.model,
        "env": env_values,
        "override_timeout_sec": request.timeout_seconds,
    }
    if request.agent_kwargs:
        agent_config["kwargs"] = dict(request.agent_kwargs)
    import_path = (
        RESUME_AGENT_IMPORT if prior_session is not None else request.agent_import_path
    )
    if import_path is not None:
        # Harbor's factory prefers `name` over `import_path`; only the
        # import path may be present or the stock agent silently runs.
        del agent_config["name"]
        agent_config["import_path"] = import_path
    if prior_session is not None:
        agent_config["env"] = {**env_values, RESUME_ENV: "1"}
    exec_config = {
        "map": {
            "compile": {
                "task_name_prefix": run_id,
                "output_dir": str(harbor_dir / "tasks"),
                "instructions": [{"path": str(prompt_path)}],
                "artifacts": artifacts,
                "environments": [compile_environment],
            },
            "job": {
                "job_name": run_id,
                "jobs_dir": str(harbor_dir / "jobs"),
                "n_attempts": 1,
                "n_concurrent_trials": 1,
                "quiet": True,
                "retry": {"max_retries": 0},
                "environment": {"type": "docker", **_sandbox_overrides()},
                "verifier": {"disable": True},
                "agents": [agent_config],
            },
        },
    }
    # Secrets travel in this file, never on the command line, so they are
    # invisible to `ps`; Harbor still copies them into the job and trial
    # config.json receipts, which _scrub_harbor_receipts rewrites afterwards.
    config_path = harbor_dir / "exec-config.json"
    config_path.touch(mode=0o600)
    config_path.write_text(json.dumps(exec_config, indent=2) + "\n", encoding="utf-8")
    arguments = (*command, "exec", "--config", str(config_path))
    # Harbor treats any env name matching KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|
    # AUTH as sensitive: it emits `${NAME}` when its own environment holds the
    # same value, and otherwise writes a REDACTED placeholder into the trial
    # config -- which the container then receives. `GIT_CONFIG_KEY_*` matches
    # that pattern, so direct-action runs reached the agent with corrupted git
    # config keys and every git command failed. Handing Harbor the same values
    # keeps them resolvable, and they stay out of this process's own git calls.
    environment = {**os.environ, **env_values}
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            env=environment,
            timeout=request.timeout_seconds + HARBOR_OVERHEAD_SECONDS,
        )
    except FileNotFoundError:
        raise RunnerError(
            f"could not execute Harbor: {command[0]!r} not found"
        ) from None
    except subprocess.TimeoutExpired as error:
        _write_harbor_logs(harbor_dir, error.stdout, error.stderr, secrets)
        raise RunnerError(
            f"Harbor exceeded the {request.timeout_seconds}s agent timeout "
            f"plus {HARBOR_OVERHEAD_SECONDS}s overhead allowance; "
            f"see {harbor_dir}"
        ) from None
    finally:
        _scrub_harbor_receipts(harbor_dir, config_path, secrets)
    _write_harbor_logs(harbor_dir, result.stdout, result.stderr, secrets)
    if result.returncode:
        raise RunnerError(f"Harbor exited with {result.returncode}; see {harbor_dir}")
    return _harbor_receipt(harbor_dir / "jobs" / run_id)


def _scrub_harbor_receipts(
    harbor_dir: Path, config_path: Path, secrets: tuple[str, ...]
) -> None:
    """Redact secrets from Harbor's persisted metadata.

    Harbor copies agent env values into config.json, lock.json, and trial
    result.json receipts under the jobs directory. Collected artifacts are
    left untouched so change collection stays byte-exact.
    """

    if not secrets:
        return
    candidates = [config_path]
    jobs_dir = harbor_dir / "jobs"
    if jobs_dir.is_dir():
        candidates.extend(
            path
            for path in jobs_dir.rglob("*")
            if "artifacts" not in path.relative_to(jobs_dir).parts
        )
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        redacted = _redact(text, secrets)
        if redacted != text:
            path.write_text(redacted, encoding="utf-8")


def _write_harbor_logs(
    harbor_dir: Path,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    secrets: tuple[str, ...],
) -> None:
    # subprocess attaches partial output to TimeoutExpired as bytes even in
    # text mode, so both types must be accepted here.
    for name, captured in (("stdout.log", stdout), ("stderr.log", stderr)):
        text = (
            captured.decode("utf-8", errors="replace")
            if isinstance(captured, bytes)
            else captured or ""
        )
        (harbor_dir / name).write_text(_redact(text, secrets), encoding="utf-8")


def _check_harbor_version(command: tuple[str, ...], required: str) -> None:
    try:
        result = subprocess.run(
            (*command, "--version"),
            capture_output=True,
            text=True,
            # Generous: a first `uv run --with harbor` invocation builds the
            # overlay environment before it can print anything.
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise RunnerError(f"could not execute Harbor: {error}") from error
    if result.returncode or result.stdout.strip() != required:
        actual = result.stdout.strip() or "unknown"
        raise RunnerError(f"Harbor {required} is required; found {actual}")


def _sandbox_overrides() -> dict[str, int]:
    """Size the wake container from the coordinator's environment.

    Harbor's per-task defaults are 4 CPUs / 16 GiB. A world whose members compile
    and run large suites needs more (pandas' tests with `-n 64` were OOM-killed
    at 16 GiB); the experiment's settings give SWECRAFT_SANDBOX_CPUS /
    SWECRAFT_SANDBOX_MEMORY_MB and every wake gets them.
    """
    out: dict[str, int] = {}
    if os.environ.get("SWECRAFT_SANDBOX_CPUS"):
        out["override_cpus"] = int(os.environ["SWECRAFT_SANDBOX_CPUS"])
    if os.environ.get("SWECRAFT_SANDBOX_MEMORY_MB"):
        out["override_memory_mb"] = int(os.environ["SWECRAFT_SANDBOX_MEMORY_MB"])
    return out


def _harbor_receipt(job_dir: Path) -> tuple[Path, dict[str, Any]]:
    job = _read_json(job_dir / "result.json")
    stats = job.get("stats") or {}
    if (
        job.get("finished_at") is None
        or job.get("n_total_trials") != 1
        or stats.get("n_completed_trials") != 1
        or stats.get("n_errored_trials", 0) != 0
    ):
        raise RunnerError(f"Harbor did not complete exactly one trial: {job_dir}")
    try:
        trial_dirs = [
            path
            for path in job_dir.iterdir()
            if path.is_dir() and (path / "result.json").is_file()
        ]
    except FileNotFoundError as error:
        raise RunnerError(f"Harbor job directory does not exist: {job_dir}") from error
    if len(trial_dirs) != 1:
        raise RunnerError(f"expected exactly one Harbor trial in {job_dir}")
    trial_dir = trial_dirs[0]
    trial = _read_json(trial_dir / "result.json")
    if trial.get("finished_at") is None or trial.get("exception_info") is not None:
        raise RunnerError(f"Harbor trial failed: {trial_dir}")
    return trial_dir, trial


def _collected_artifacts(
    trial_dir: Path,
    output_dir: Path,
    *,
    collect_structured: bool,
    collect_memory: bool,
    lenient_structured: bool = False,
) -> tuple[Path, Path, dict[str, Any] | None, Path | None, Path | None]:
    manifest = _read_json(trial_dir / "artifacts" / "manifest.json")
    if not isinstance(manifest, list):
        raise RunnerError("Harbor artifact manifest must be a list")
    statuses = {
        entry.get("source"): entry.get("status")
        for entry in manifest
        if isinstance(entry, dict)
    }
    required = ["/app/response.md", "/app/repository"]
    if collect_memory:
        required.append(MEMORY_ARTIFACT)
    for source in required:
        if statuses.get(source) != "ok":
            raise RunnerError(f"Harbor did not collect required artifact: {source}")
    collected = trial_dir / "artifacts" / "app"
    response = collected / "response.md"
    repository = collected / "repository"
    if not response.is_file() or response.is_symlink():
        raise RunnerError("collected response is not a regular file")
    try:
        text = response.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as error:
        raise RunnerError("collected response is not UTF-8") from error
    if not text:
        raise RunnerError("collected response is empty")
    if not repository.is_dir() or repository.is_symlink():
        raise RunnerError("collected repository is not a directory")
    memory = None
    if collect_memory:
        memory = collected / "memory"
        if not memory.is_dir() or memory.is_symlink():
            raise RunnerError("collected memory is not a directory")
    response_path = output_dir / "response.md"
    response_path.write_text(f"{text}\n", encoding="utf-8")
    structured, structured_path = None, None
    if collect_structured and statuses.get(STRUCTURED_ARTIFACT) == "ok":
        try:
            structured = _structured_result(collected / "result.json")
        except RunnerError:
            if not lenient_structured:
                raise
        else:
            structured_path = output_dir / "agent-result.json"
            _write_json(structured_path, structured)
    return response_path, repository, structured, structured_path, memory


def _collect_forge_actions(
    collected: Path,
    output_dir: Path,
    repository: str | None = None,
    secrets: tuple[str, ...] = (),
) -> tuple[Path | None, Path | None]:
    """The wake's forge ledger and the recording it was derived from.

    The sandbox records invocations and classifies nothing, so both files are
    written here: `forge-calls.jsonl` is what the wrappers saw, and
    `forge-actions.jsonl` is what that means under the current vocabulary. The
    recording is kept because the vocabulary will change — a table corrected
    later can be applied to runs already collected, which is not true of a
    ledger classified at the moment of capture.

    Both are redacted against the run's secret list on the way out. Deriving
    is best effort: the agent's actual work already happened, and losing a
    completed wake over its bookkeeping is the wrong trade.
    """

    source = collected / ".swecraft" / "forge-calls"
    calls = forge_actions.read_calls(source)
    if not calls:
        _remove_forge_call_artifact(source)
        return None, None

    try:
        calls_path = output_dir / "forge-calls.jsonl"
        calls_path.write_text(
            "".join(
                _redact(json.dumps(call, sort_keys=True), secrets) + "\n"
                for call in calls
            ),
            encoding="utf-8",
        )

        try:
            rows = forge_actions.derive(calls, repository=repository, secrets=secrets)
        except forge_actions.TableError:
            return None, calls_path
        if not rows:
            return None, calls_path
        actions_path = output_dir / "forge-actions.jsonl"
        actions_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        return actions_path, calls_path
    finally:
        _remove_forge_call_artifact(source)


def _remove_forge_call_artifact(source: Path) -> None:
    """Delete the unredacted Harbor copy once the host projection exists."""

    if source.is_dir() and not source.is_symlink():
        shutil.rmtree(source)
    else:
        source.unlink(missing_ok=True)


def _structured_result(path: Path) -> dict[str, Any]:
    """A structured result is optional, but if present it must be sound."""

    if not path.is_file() or path.is_symlink():
        raise RunnerError("collected structured result is not a regular file")
    value = _read_json(path)
    if not isinstance(value, dict):
        raise RunnerError("structured result must be a single JSON object")
    return value


def _changed_paths(before_root: Path, after_root: Path) -> tuple[str, ...]:
    """Compare worktree contents without consulting untrusted Git metadata."""

    before = _snapshot(before_root)
    after = _snapshot(after_root)
    return tuple(
        sorted(
            path
            for path in before.keys() | after.keys()
            if before.get(path) != after.get(path)
        )
    )


def _git_visible_paths(git_checkout: Path, worktree: Path) -> set[str] | None:
    """Paths git itself considers changed, honoring the worktree's ignore rules.

    Runs ``git status`` with the trusted input checkout's metadata as
    ``GIT_DIR`` and the collected (untrusted) tree attached only as
    ``GIT_WORK_TREE`` — the applier's established pattern. Without this
    filter, one test run's virtualenv or build tree lands in the change
    bundle by the thousands of files. Ignore rules come from the worktree,
    so collection reports exactly what ``git add --all`` would commit;
    tracked files are always reported regardless of ignore patterns.
    Returns None when git cannot answer, so callers fall back to the raw
    snapshot diff instead of silently reporting nothing.
    """

    git_dir = git_checkout / ".git"
    if not git_dir.is_dir():
        return None
    environment = {
        **os.environ,
        "GIT_DIR": str(git_dir),
        "GIT_WORK_TREE": str(worktree),
        "GIT_INDEX_FILE": str(git_dir / "swecraft-collect-index"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    try:
        result = subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "read-tree",
                "HEAD",
            ),
            capture_output=True,
            env=environment,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.returncode:
            return None
        result = subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain",
                "-z",
                "--untracked-files=all",
                "--no-renames",
            ),
            capture_output=True,
            env=environment,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        (git_dir / "swecraft-collect-index").unlink(missing_ok=True)
    if result.returncode:
        return None
    paths = set()
    for entry in result.stdout.decode("utf-8", errors="replace").split("\0"):
        if len(entry) > 3:
            paths.add(entry[3:])
    return paths


def _collect_changes(
    before_root: Path,
    after_root: Path,
    output_dir: Path,
    collect_prefixes: tuple[str, ...] = (),
) -> tuple[Path | None, tuple[str, ...], Path]:
    before = _snapshot(before_root)
    after = _snapshot(after_root)
    visible = _git_visible_paths(before_root, after_root)
    if visible is not None:
        if collect_prefixes:
            normalized = tuple(prefix.rstrip("/") + "/" for prefix in collect_prefixes)
            visible |= {
                path
                for path in {*before, *after}
                if any(
                    path.startswith(prefix) or path == prefix.rstrip("/")
                    for prefix in normalized
                )
            }
        before = {path: state for path, state in before.items() if path in visible}
        after = {path: state for path, state in after.items() if path in visible}
    added = sorted(after.keys() - before.keys())
    deleted = sorted(before.keys() - after.keys())
    modified = sorted(
        path for path in before.keys() & after.keys() if before[path] != after[path]
    )
    added, deleted, modified = _normalize_materialized_symlinks(
        before, after, before_root, after_root, added, deleted, modified
    )
    changes_dir = output_dir / "changes"
    records = []
    for status_name, paths in (
        ("added", added),
        ("modified", modified),
        ("deleted", deleted),
    ):
        for relative in paths:
            state = after.get(relative) or before[relative]
            record = {"path": relative, "status": status_name, **state.as_json()}
            records.append(record)
            if status_name != "deleted" and state.kind == "file":
                source = after_root.joinpath(*PurePosixPath(relative).parts)
                destination = changes_dir.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
    changes_json = output_dir / "changes.json"
    _write_json(
        changes_json,
        {
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "files": records,
        },
    )
    changed_files = tuple(sorted({*added, *modified, *deleted}))
    return (
        changes_dir if changes_dir.is_dir() else None,
        changed_files,
        changes_json,
    )


def _normalize_materialized_symlinks(
    before: dict[str, _FileState],
    after: dict[str, _FileState],
    before_root: Path,
    after_root: Path,
    added: list[str],
    deleted: list[str],
    modified: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Drop diff entries that are Harbor's symlink dereferencing, not changes.

    File symlinks come back as identical-content regular files (a modified
    entry); directory symlinks come back as real directories (the symlink
    path lands in deleted, and any snapshot-visible children land in added).
    Both are collection artifacts. Anything whose content differs from the
    resolved in-tree target stays reported.
    """

    added_set, deleted_set, modified_set = set(added), set(deleted), set(modified)
    for path in list(modified_set):
        if _is_materialized_symlink(before[path], after[path], before_root, path):
            modified_set.discard(path)
    root = before_root.resolve()
    for path in list(deleted_set):
        state = before[path]
        if state.kind != "symlink":
            continue
        link = before_root.joinpath(*PurePosixPath(path).parts)
        try:
            resolved = link.parent.joinpath(state.target or "").resolve()
        except OSError:
            continue
        if not resolved.is_dir() or root not in resolved.parents:
            continue
        if not after_root.joinpath(*PurePosixPath(path).parts).is_dir():
            continue
        prefix = path + "/"
        children = [entry for entry in added_set if entry.startswith(prefix)]

        def matches(
            entry: str, prefix: str = prefix, resolved: Path = resolved
        ) -> bool:
            rest = entry[len(prefix) :]
            target = resolved.joinpath(*PurePosixPath(rest).parts)
            try:
                collected = after[entry]
                return (
                    target.is_file()
                    and collected.kind == "file"
                    and _sha256(target) == collected.digest
                )
            except OSError:
                return False

        if all(matches(entry) for entry in children):
            deleted_set.discard(path)
            added_set.difference_update(children)
    return sorted(added_set), sorted(deleted_set), sorted(modified_set)


def _is_materialized_symlink(
    before_state: _FileState, after_state: _FileState, before_root: Path, relative: str
) -> bool:
    """True when a collected file is just a dereferenced upstream symlink.

    Harbor's docker-compose artifact download materializes symlinks into
    regular files, so a path that was a symlink before the turn and comes
    back as a regular file with exactly the linked content is a collection
    artifact, not an agent change. Dangling, absolute, or out-of-tree link
    targets stay reported as modifications.
    """

    if before_state.kind != "symlink" or after_state.kind != "file":
        return False
    link = before_root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = link.parent.joinpath(before_state.target or "").resolve()
        root = before_root.resolve()
        if not resolved.is_file() or root not in resolved.parents:
            return False
        return _sha256(resolved) == after_state.digest
    except OSError:
        return False


def _snapshot(root: Path) -> dict[str, _FileState]:
    snapshot: dict[str, _FileState] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept_directories = []
        for name in sorted(directories):
            path = current_path / name
            relative = path.relative_to(root)
            if ".git" in relative.parts:
                continue
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                snapshot[relative.as_posix()] = _FileState(
                    kind="symlink", target=os.readlink(path)
                )
            else:
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(root)
            if ".git" in relative.parts:
                continue
            metadata = path.lstat()
            key = relative.as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                snapshot[key] = _FileState(kind="symlink", target=os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                snapshot[key] = _FileState(
                    kind="file",
                    digest=_sha256(path),
                    executable=bool(metadata.st_mode & 0o111),
                )
            else:
                snapshot[key] = _FileState(kind="special")
    return snapshot


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redactable_secrets(env_values: dict[str, str]) -> tuple[str, ...]:
    """Keep only values long enough to redact safely.

    Redacting flag-like values such as "1" would replace every occurrence of
    that character in logs and Harbor receipts, corrupting them.
    """

    return tuple(
        value
        for value in env_values.values()
        if len(value) >= MIN_REDACTABLE_SECRET_LENGTH
    )


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    # Longest first, so a short secret cannot replace the prefix of a longer
    # one and leave its suffix exposed.
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise RunnerError(f"missing or invalid JSON: {path}") from error


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
