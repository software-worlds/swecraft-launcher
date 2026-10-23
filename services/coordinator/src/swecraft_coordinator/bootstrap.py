"""One-time orientation turn: survey the repositories, produce initial memory.

Runs after `onboard` and before the first wake, under the runner's standard
no-push contract — the agent only reads its repositories (the home at
/app/repository, the rest staged under /app/repos/<name>, nothing to fetch)
and writes memory.
Unlike wakes, the turn exists solely to produce memory, so writing nothing
is an error rather than a warning.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from swecraft_common.forge import ForgeClient
from swecraft_runner.runner import RunRequest, RunResult, run_once

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import (
    WAKE_MODE_ENV,
    WAKE_MODE_SINGLE_TASK,
    AgentProfile,
    CoordinatorConfig,
    resolve_agent_env,
)
from swecraft_coordinator.memory import GOAL_FILE, publish_memory
from swecraft_coordinator.prompts import render
from swecraft_coordinator.repositories import prompt_variables, work_repositories
from swecraft_coordinator.state import StateStore
from swecraft_coordinator.wake import FORGE_URL_ENV, TOKEN_ENV, _agent_token, _slim

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BootstrapReport:
    agent: str
    run_id: str
    memory_commit: str
    changed_files: tuple[str, ...]


def bootstrap_agent(
    config: CoordinatorConfig,
    profile: AgentProfile,
    state: StateStore,
    *,
    run: Callable[[RunRequest], RunResult] = run_once,
) -> BootstrapReport:
    token = _agent_token(config, profile)
    # The memory branch is pushed by the operator only (see onboarding).
    publisher = config.resolved_admin_token()
    forge = ForgeClient(config.forge_url, token)
    repository = forge.get_repository(profile.home_repo) or {}
    default_branch = repository.get("default_branch") or "main"
    if not forge.branch_exists(profile.home_repo, config.memory_branch):
        raise CoordinatorError(
            f"memory branch {config.memory_branch} is missing on "
            f"{profile.home_repo}; "
            f"run `swecraft-coordinator onboard {profile.name}` first"
        )
    staged, described = work_repositories(config, forge, profile, push=False)
    prompt = render(
        "bootstrap.md",
        {
            **prompt_variables(
                profile,
                default_branch,
                described,
                collaboration=config.prompt.collaboration,
            ),
            "MEMORY_BRANCH": config.memory_branch,
        },
    )
    clone_url = f"{config.forge_url}/{profile.home_repo}.git"
    environment = resolve_agent_env(config, profile)
    environment[TOKEN_ENV] = token
    environment[FORGE_URL_ENV] = config.push_url
    # An orientation turn is one task whatever the society's wake mode.
    environment[WAKE_MODE_ENV] = WAKE_MODE_SINGLE_TASK
    result = run(
        RunRequest(
            repository=clone_url,
            revision=default_branch,
            prompt=prompt,
            model=profile.model,
            agent=profile.agent,
            agent_kwargs=profile.agent_kwargs,
            **profile.run_request_overrides(),
            model_pricing=config.pricing_for(profile.model),
            output_dir=config.runs_dir,
            image=profile.image,
            # By value, so the orientation turn's credentials never enter
            # this process's environment (see wake.py).
            agent_env=environment,
            timeout_seconds=profile.timeout_seconds,
            memory_repository=clone_url,
            memory_revision=config.memory_branch,
            work_repositories=staged,
            # No direct action, but the fetches still need the agent's token:
            # a dedicated home is private.
            fetch_auth_token_env=TOKEN_ENV,
        )
    )
    if result.memory_workspace is None or result.memory_source_sha is None:
        raise CoordinatorError(
            f"bootstrap run {result.run_id} returned no memory workspace"
        )
    meaningful = tuple(
        path for path in result.memory_changed_files if path != GOAL_FILE
    )
    if not meaningful:
        raise CoordinatorError(
            f"bootstrap run {result.run_id} wrote no memory; re-run "
            f"`swecraft-coordinator bootstrap {profile.name}`"
        )
    commit = publish_memory(
        clone_url=clone_url,
        branch=config.memory_branch,
        token=publisher,
        author=profile.name,
        workspace=result.memory_workspace,
        input_memory=result.run_dir / "input" / "memory",
        source_sha=result.memory_source_sha,
        changed_files=result.memory_changed_files,
        commit_message=f"{profile.name}: bootstrap memory (run {result.run_id})",
    )
    if commit is None:
        raise CoordinatorError(
            f"bootstrap run {result.run_id} produced nothing publishable"
        )
    state.mark_bootstrapped(profile.name)
    _slim(result)
    logger.info(
        "bootstrapped %s at %s (run %s, %d file(s))",
        profile.name,
        commit[:12],
        result.run_id,
        len(meaningful),
    )
    return BootstrapReport(
        agent=profile.name,
        run_id=result.run_id,
        memory_commit=commit,
        changed_files=meaningful,
    )
