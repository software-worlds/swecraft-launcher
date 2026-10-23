"""Execute one wake: claim, run the sandboxed turn, publish memory, record."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from swecraft_common.forge import ForgeClient
from swecraft_runner.pricing import ModelPricing, agent_usage
from swecraft_runner.runner import RunRequest, RunResult, run_once

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.budget import budget_variables
from swecraft_coordinator.collaboration import collaboration_nudge
from swecraft_coordinator.config import (
    AgentProfile,
    CoordinatorConfig,
    resolve_agent_env,
    resolve_secret,
)
from swecraft_coordinator.memory import publish_memory
from swecraft_coordinator.prompts import render_long_wake, render_wake
from swecraft_coordinator.repositories import prompt_variables, work_repositories
from swecraft_coordinator.retention import slim_run
from swecraft_coordinator.state import StateStore

logger = logging.getLogger(__name__)

OUTCOMES = ("worked", "idle", "blocked")
TOKEN_ENV = "SWECRAFT_FORGE_TOKEN"
FORGE_URL_ENV = "SWECRAFT_FORGE_URL"
# Claims older than the agent timeout plus this margin are abandoned.
STALE_CLAIM_MARGIN_SECONDS = 1_800


@dataclass(frozen=True)
class WakeReport:
    agent: str
    run_id: str
    outcome: str | None
    summary: str | None
    cost_usd: float
    memory_commit: str | None


def wake_agent(
    config: CoordinatorConfig,
    profile: AgentProfile,
    state: StateStore,
    *,
    run: Callable[[RunRequest], RunResult] = run_once,
) -> WakeReport:
    # Claim before any preflight so that preflight failures (missing
    # credentials, missing bootstrap, forge trouble) are recorded and back
    # off instead of hot-looping in the scheduler.
    wake_id = state.claim(
        profile.name,
        stale_after_seconds=profile.timeout_seconds + STALE_CLAIM_MARGIN_SECONDS,
    )
    run_id: str | None = None
    cost = 0.0
    secrets: list[str] = []
    try:
        token = _agent_token(config, profile)
        secrets.append(token)
        # The memory branch is the operator's to push (see onboarding): the
        # coordinator publishes the agent's notes as the operator, with the
        # agent as author. Resolved before the turn so a missing setting is a
        # recorded preflight failure, not a lost wake's worth of notes.
        publisher = config.resolved_admin_token()
        secrets.append(publisher)
        if not state.is_bootstrapped(profile.name):
            raise CoordinatorError(
                f"agent {profile.name} has not been bootstrapped; run "
                f"`swecraft-coordinator bootstrap {profile.name}` first"
            )
        forge = ForgeClient(config.forge_url, token)
        repository = forge.get_repository(profile.home_repo) or {}
        default_branch = repository.get("default_branch") or "main"
        if not forge.branch_exists(profile.home_repo, config.memory_branch):
            raise CoordinatorError(
                f"memory branch {config.memory_branch} is missing on "
                f"{profile.home_repo}; "
                f"run `swecraft-coordinator onboard {profile.name}`"
            )
        status = state.status(profile.name)
        staged, described = work_repositories(config, forge, profile, push=True)
        variables = {
            **prompt_variables(
                profile,
                default_branch,
                described,
                collaboration=config.prompt.collaboration,
            ),
            "MEMORY_BRANCH": config.memory_branch,
            "LAST_OUTCOME": (status.last_outcome if status else None) or "",
            "LAST_SUMMARY": (status.last_summary if status else None) or "",
            "LAST_FINISHED": (status.last_finished if status else None) or "",
            "LAST_UNCERTAIN": bool(status and status.last_uncertain),
            "LAST_ERROR": (status.last_error if status else None) or "",
            "WAKE_BUDGET": config.wake_budget.describe(),
            **collaboration_nudge(config, state, profile, wake_id=wake_id),
            **budget_variables(config, state, profile),
        }
        prompt = (
            render_long_wake(variables) if config.long_wake else render_wake(variables)
        )
        clone_url = f"{config.forge_url}/{profile.home_repo}.git"
        environment = resolve_agent_env(config, profile)
        secrets.extend(environment.values())
        environment[TOKEN_ENV] = token
        environment[FORGE_URL_ENV] = config.push_url
        # The wake's mode, budget and deadline, for a harness that enforces them.
        environment.update(config.wake_environment(profile.timeout_seconds))
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
                # By value, not by name: several wakes may be in flight, and
                # each agent has its own token. Resolving names from this
                # process's environment would let concurrent wakes overwrite
                # one another's credentials.
                agent_env=environment,
                timeout_seconds=profile.timeout_seconds,
                structured_output=True,
                # The agent may act on the forge before writing the result
                # envelope; a formatting slip must not fail a wake whose
                # external actions already happened.
                lenient_structured=True,
                memory_repository=clone_url,
                memory_revision=config.memory_branch,
                allow_remote_writes=True,
                push_remote_url=f"{config.push_url}/{profile.home_repo}.git",
                work_repositories=staged,
                git_auth_token_env=TOKEN_ENV,
                # The home may be private: the runner's own fetches carry the
                # agent's token too, scoped to the forge.
                fetch_auth_token_env=TOKEN_ENV,
                git_author_name=profile.name,
                git_author_email=f"{profile.name}@localhost",
            )
        )
        run_id = result.run_id
        cost = _run_cost(result)
        outcome, summary = _envelope(result)
        memory_commit = None
        if result.memory_workspace is not None and result.memory_source_sha:
            memory_commit = publish_memory(
                clone_url=clone_url,
                branch=config.memory_branch,
                token=publisher,
                author=profile.name,
                workspace=result.memory_workspace,
                input_memory=result.run_dir / "input" / "memory",
                source_sha=result.memory_source_sha,
                changed_files=result.memory_changed_files,
                commit_message=(f"{profile.name}: wake notes (run {result.run_id})"),
            )
        if memory_commit is None and not result.memory_changed_files:
            logger.warning(
                "wake %s: agent made no memory edits (notebook not updated)",
                result.run_id,
            )
        state.finish(
            profile.name,
            wake_id,
            run_id=result.run_id,
            outcome=outcome,
            summary=summary,
            cost_usd=cost,
            next_due_seconds=profile.interval_seconds,
        )
        _slim(result)
        return WakeReport(
            agent=profile.name,
            run_id=result.run_id,
            outcome=outcome,
            summary=summary,
            cost_usd=cost,
            memory_commit=memory_commit,
        )
    except Exception as error:
        status = state.status(profile.name)
        failures = (status.consecutive_failures if status else 0) + 1
        backoff = min(
            config.failure_backoff_seconds * 2 ** (failures - 1),
            config.max_backoff_seconds,
        )
        # A failed run still spent real money: Harbor's trial receipts
        # survive timeouts and errors, and the runner attaches the run
        # location to its exception precisely so the ledger stays honest.
        if run_id is None:
            run_id = getattr(error, "run_id", None)
        if not cost:
            cost = _trial_receipt_cost(
                getattr(error, "run_dir", None), config.pricing_for(profile.model)
            )
        state.fail(
            profile.name,
            wake_id,
            error=_redacted(str(error), secrets),
            run_id=run_id,
            cost_usd=cost,
            next_due_seconds=backoff,
        )
        raise


def _slim(result: RunResult) -> None:
    try:
        freed = slim_run(result.run_dir)
    except OSError:
        logger.exception("could not slim run %s", result.run_id)
    else:
        if freed:
            logger.info(
                "slimmed run %s (%.1f MB freed)", result.run_id, freed / 1_000_000
            )


def _agent_token(config: CoordinatorConfig, profile: AgentProfile) -> str:
    path = config.credentials_path(profile.name)
    if not path.is_file():
        raise CoordinatorError(
            f"no credentials for agent {profile.name}; run "
            f"`swecraft-coordinator onboard {profile.name}` first"
        )
    return resolve_secret(f"file:{path}")


def _envelope(result: RunResult) -> tuple[str | None, str | None]:
    """The end-of-wake envelope is advisory: absent or malformed is fine."""

    structured = result.structured
    if not isinstance(structured, dict):
        return None, None
    outcome = structured.get("outcome")
    summary = structured.get("summary")
    outcome = outcome if outcome in OUTCOMES else None
    summary = summary.strip()[:500] if isinstance(summary, str) else None
    return outcome, summary


def _run_cost(result: RunResult) -> float:
    try:
        receipt = json.loads(result.result_path.read_text(encoding="utf-8"))
        return float((receipt.get("usage") or {}).get("cost_usd") or 0.0)
    except (OSError, ValueError):
        return 0.0


def _trial_receipt_cost(run_dir: object, pricing: ModelPricing | None = None) -> float:
    """Sum reported or configured-fallback cost for a failed Harbor run."""

    if not isinstance(run_dir, Path) or not run_dir.is_dir():
        return 0.0
    total = 0.0
    for receipt in run_dir.glob("harbor/jobs/*/*/result.json"):
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            agent_result = data.get("agent_result") or {}
            if isinstance(agent_result, dict):
                total += float(agent_usage(agent_result, pricing)["cost_usd"])
    return total


MIN_REDACTABLE_SECRET_LENGTH = 8


def _redacted(message: str, secrets: list[str]) -> str:
    """Secret-bearing failure text must not be persisted to the state DB."""

    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= MIN_REDACTABLE_SECRET_LENGTH:
            message = message.replace(secret, "[REDACTED]")
    return message
