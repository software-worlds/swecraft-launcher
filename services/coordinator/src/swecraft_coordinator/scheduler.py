"""Fixed-cadence serve loop: wake due agents, up to `workers` at a time.

When due agents outnumber free workers, slots go oldest-`next_due`
first — a ready queue in effect without a queue in state: an agent's
position is when it became due, so contention delays are shared in
arrival order instead of compounding by roster position.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime

from swecraft_common.forge import ForgeClient, ForgeError

from swecraft_coordinator.budget import agent_cap, refresh_ledger
from swecraft_coordinator.cadence import (
    checkpoint_due,
    evaluation_due,
    run_boundary_checkpoint,
    run_boundary_evaluation,
    warn_if_publish_misconfigured,
)
from swecraft_coordinator.config import AgentProfile, CoordinatorConfig, resolve_secret
from swecraft_coordinator.retention import sweep
from swecraft_coordinator.state import StateStore
from swecraft_coordinator.wake import WakeReport, wake_agent

logger = logging.getLogger(__name__)

POLL_SECONDS = 10.0
SWEEP_INTERVAL_SECONDS = 3_600.0
# How often the budget ledger on the board is re-read. A transfer is a
# merged pull request, so a minute's lag is invisible to the agents.
LEDGER_REFRESH_SECONDS = 60.0


def serve_forever(
    config: CoordinatorConfig,
    state: StateStore,
    stop: threading.Event,
    *,
    wake: Callable[..., WakeReport] = wake_agent,
) -> None:
    for profile in config.agents:
        state.ensure_agent(profile.name)
    logger.info(
        "serving %d agent(s) with %d worker(s): %s",
        len(config.agents),
        config.workers,
        ", ".join(profile.name for profile in config.agents),
    )
    warn_if_publish_misconfigured(config)
    budget_noticed: dict[str, str] = {}
    last_sweep = 0.0
    last_ledger = -LEDGER_REFRESH_SECONDS
    # One wake per agent at a time is already enforced by the store's claim;
    # tracking in-flight work here keeps the loop from stacking submissions
    # against an agent that is still awake.
    running: dict[str, Future[WakeReport]] = {}
    with ThreadPoolExecutor(
        max_workers=config.workers, thread_name_prefix="wake"
    ) as pool:
        while not stop.is_set():
            _reap(running)
            # A due boundary drains before it acts: stop dispatching, let
            # in-flight wakes finish, then evaluate and/or checkpoint at the
            # quiet step boundary and resume — the training-loop shape.
            # Evaluation runs first so its record enters the checkpoint.
            if checkpoint_due(config, state) or evaluation_due(config, state):
                if not running:
                    if evaluation_due(config, state):
                        run_boundary_evaluation(config, state)
                    if checkpoint_due(config, state):
                        run_boundary_checkpoint(config, state)
                    continue
                logger.info(
                    "boundary work due; draining %d in-flight wake(s)",
                    len(running),
                )
            else:
                if (
                    config.budget.ledger is not None
                    and time.monotonic() - last_ledger >= LEDGER_REFRESH_SECONDS
                ):
                    last_ledger = time.monotonic()
                    _refresh_ledger(config, state, budget_noticed)
                for profile in _due_order(config, state):
                    if stop.is_set() or len(running) >= config.workers:
                        break
                    if profile.name in running or not state.is_due(profile.name):
                        continue
                    if not _within_budget(config, state, profile, budget_noticed):
                        continue
                    running[profile.name] = pool.submit(wake, config, profile, state)
            if (
                config.retention_days
                and time.monotonic() - last_sweep >= SWEEP_INTERVAL_SECONDS
            ):
                last_sweep = time.monotonic()
                removed = sweep(config.runs_dir.resolve(), config.retention_days)
                if removed:
                    logger.info("swept %d old run(s)", len(removed))
            stop.wait(POLL_SECONDS)
        # Drain rather than orphan: a wake whose process dies leaves a claim
        # that stays valid until it goes stale, locking that agent out for
        # its timeout plus the stale margin.
        if running:
            logger.info("waiting for %d in-flight wake(s) to finish", len(running))
        while running:
            _reap(running)
            if running:
                time.sleep(1.0)


def _refresh_ledger(
    config: CoordinatorConfig, state: StateStore, noticed: dict[str, str]
) -> None:
    """Read the board's budget file as one of the agents, who all maintain it.

    The coordinator applies what the agents agreed on the board; it needs
    no operator token for that, since every agent has write on the board
    and its token is on disk after onboarding.
    """

    clients = [
        ForgeClient(config.forge_url, resolve_secret(f"file:{path}"))
        for path in (config.credentials_path(p.name) for p in config.agents)
        if path.is_file()
    ]
    if clients:
        refresh_ledger(config, state, _AnyOf(clients), noticed)
    elif noticed.get("ledger") != "no-credentials":
        logger.warning("budget ledger not read: no agent has credentials yet")
        noticed["ledger"] = "no-credentials"


class _AnyOf:
    """Read the board with whichever agent token works: a stale first token
    must not block a transfer that another agent's token could read."""

    def __init__(self, clients: list[ForgeClient]) -> None:
        self._clients = clients

    def get_file(self, repo: str, path: str) -> tuple[str, str] | None:
        error: ForgeError | None = None
        for client in self._clients:
            try:
                return client.get_file(repo, path)
            except ForgeError as failure:
                error = failure
        assert error is not None
        raise error


def _due_order(config: CoordinatorConfig, state: StateStore) -> list[AgentProfile]:
    """Roster sorted oldest `next_due` first; ties keep YAML order.

    Derived fresh each pass from the store, so fairness survives
    restarts with no state of its own.
    """

    def due_at(profile: AgentProfile) -> datetime:
        status = state.status(profile.name)
        if status is None:
            return datetime.min.replace(tzinfo=UTC)
        return datetime.fromisoformat(status.next_due)

    return sorted(config.agents, key=due_at)


def _reap(running: dict[str, Future[WakeReport]]) -> None:
    for name, future in list(running.items()):
        if not future.done():
            continue
        del running[name]
        try:
            report = future.result()
        except Exception:
            logger.exception("wake failed for %s; backing off", name)
        else:
            logger.info(
                "wake %s for %s finished: %s (%.4f USD)",
                report.run_id,
                name,
                report.outcome or "no envelope",
                report.cost_usd,
            )


def _within_budget(
    config: CoordinatorConfig,
    state: StateStore,
    profile: AgentProfile,
    noticed: dict[str, str] | None = None,
) -> bool:
    cap = agent_cap(config, state, profile.name)
    if cap is not None and state.run_wakes(profile.name) >= cap:
        # The run budget is final: no next UTC day brings it back, so the
        # notice is given once per run rather than once per day.
        if noticed is not None and noticed.get(profile.name) != "run":
            logger.warning(
                "%s has used its run budget of %d wakes; it will not wake again",
                profile.name,
                cap,
            )
            noticed[profile.name] = "run"
        return False
    wakes, cost = state.day_usage(profile.name)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if config.daily_wake_limit and wakes >= config.daily_wake_limit:
        reason = f"daily wake limit ({config.daily_wake_limit})"
    elif config.daily_cost_limit_usd and cost >= config.daily_cost_limit_usd:
        reason = f"daily cost limit ({config.daily_cost_limit_usd:.2f} USD)"
    else:
        return True
    if noticed is None or noticed.get(profile.name) != day:
        logger.warning(
            "%s hit the %s; pausing until the next UTC day", profile.name, reason
        )
        if noticed is not None:
            noticed[profile.name] = day
    return False
