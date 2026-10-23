"""The collaboration nudge: asking an agent whether it has worked with the
others, on a cadence or when the forge record says it has not
(``docs/reference/nudges.md``).

Every wake leaves ``output/forge-actions.jsonl`` in its run directory, the
runner's ledger of the forge writes the agent made, each classified as an
action. A wake *reached out* when one of those rows is a communication act
on a repository outside the agent's own set. Over the agent's last
``window`` completed wakes that gives a rate; when it is below
``nudges.collaboration.below_rate.target``, this wake carries the check
with the rate stated. The measure stays the forge record, never the
agent's claim; the check asks and weighs, it orders nothing.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from swecraft_coordinator.config import AgentProfile, CoordinatorConfig
from swecraft_coordinator.state import StateStore

logger = logging.getLogger(__name__)

LEDGER_NAME = "forge-actions.jsonl"
# Ledger actions that are communication with another repository's people;
# tags, releases, CI dispatches and labels are housekeeping. Reactions are
# left out on purpose: once the prompt states the rate, a reaction is the
# cheapest way to satisfy it (measured on the opus six-maintainer cells, a
# reaction alone made a wake count in about one reached-out wake in ten).
COMMUNICATION_ACTIONS = frozenset(
    {
        "issue.create",
        "issue.comment",
        "issue.edit",
        "issue.close",
        "pr.create",
        "pr.comment",
        "pr.review",
        "pr.merge",
        "pr.edit",
        "pr.close",
        "comment.edit",
    }
)
# How a nudged wake is marked in the state database (``wake_treatments``).
TREATMENT = "collaboration-check"


@dataclass(frozen=True)
class Rate:
    reached: int
    window: int


def own_repositories(profile: AgentProfile) -> frozenset[str]:
    return frozenset(profile.write_repos) | {profile.home_repo}


def wake_reached_out(run_dir: Path, own: frozenset[str]) -> bool:
    """Whether the wake's ledger holds a communication act on another repository."""

    ledger = run_dir / "output" / LEDGER_NAME
    if not ledger.is_file():
        return False
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        repo = row.get("repo")
        if (
            row.get("action") in COMMUNICATION_ACTIONS
            and isinstance(repo, str)
            and repo not in own
        ):
            return True
    return False


def cross_repository_rate(
    config: CoordinatorConfig, state: StateStore, profile: AgentProfile
) -> Rate | None:
    """Reached-out wakes among the agent's last ``window`` completed wakes.

    None until the agent has ``window`` completed wakes to judge by: the
    rate is always over a full window, so the first wakes of a run are
    never judged on a handful.
    """

    window = config.nudges.collaboration.below_rate.window
    wakes = state.list_wakes(agent=profile.name, status="completed", limit=window)
    if len(wakes) < window:
        return None
    own = own_repositories(profile)
    reached = sum(
        wake_reached_out(config.runs_dir / wake.run_id, own)
        for wake in wakes
        if wake.run_id
    )
    return Rate(reached=reached, window=len(wakes))


def below_rate_due(
    config: CoordinatorConfig, state: StateStore, profile: AgentProfile
) -> Rate | None:
    """The rate when it is below the target, else None."""

    below = config.nudges.collaboration.below_rate
    if not below.enabled or config.prompt.collaboration == "isolated":
        return None
    rate = cross_repository_rate(config, state, profile)
    if rate is None or rate.reached / rate.window >= below.target:
        return None
    return rate


def cadence_due(
    config: CoordinatorConfig, state: StateStore, profile: AgentProfile
) -> bool:
    """Whether the cadence puts the check on this wake: every Nth wake the
    agent has started, this one included (the claim precedes the prompt).
    """

    every = config.nudges.collaboration.every_wakes
    return bool(every) and state.run_wakes(profile.name) % every == 0


def prompt_variables(
    rate: Rate | None, config: CoordinatorConfig, *, cadence: bool = False
) -> dict[str, object]:
    """The collaboration nudge's axis of the prompt (``docs/reference/prompts.md``).

    The check appears when either condition holds; the rate is stated when
    the record is what brought it.
    """

    below = config.nudges.collaboration.below_rate
    if rate is None:
        window = reached = target = 0
    else:
        window, reached = rate.window, rate.reached
        # The smallest count that satisfies the rate: the condition holds
        # exactly when fewer than this many of the window reached out.
        target = max(1, math.ceil(below.target * rate.window - 1e-9))
    return {
        "COLLABORATION_CHECK": cadence or rate is not None,
        "COLLABORATION_RATE": rate is not None,
        "COLLABORATION_WINDOW": window,
        "COLLABORATION_REACHED": reached,
        "COLLABORATION_TARGET": target,
    }


def collaboration_nudge(
    config: CoordinatorConfig, state: StateStore, profile: AgentProfile, *, wake_id: int
) -> dict[str, object]:
    """Decide this wake's collaboration check, record it, return its variables."""

    nudge = config.nudges.collaboration
    cadence = cadence_due(config, state, profile)
    rate = below_rate_due(config, state, profile)
    variables = prompt_variables(rate, config, cadence=cadence)
    if variables["COLLABORATION_CHECK"]:
        detail = (
            f"{rate.reached} of {rate.window} reached out"
            if rate is not None
            else f"every {nudge.every_wakes} wakes"
        )
        state.mark_treatment(wake_id, TREATMENT, detail)
        logger.info("%s: collaboration check (%s)", profile.name, detail)
    return variables
