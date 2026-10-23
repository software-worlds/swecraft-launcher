"""The run budget in wakes: each agent's number, the ledger that moves it."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import yaml
from swecraft_common.forge import ForgeClient, ForgeError

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import AgentProfile, CoordinatorConfig
from swecraft_coordinator.state import StateStore

logger = logging.getLogger(__name__)


def agent_cap(config: CoordinatorConfig, state: StateStore, agent: str) -> int | None:
    """The wakes this agent may start over the run, or None when uncapped.

    With a ledger applied it is the agent's line, which may be 0: an agent
    that gave everything away is stopped, not freed.
    """

    cap = config.budget.wakes
    if not cap:
        return None
    if config.budget.ledger is not None:
        _, allocation = state.ledger_allocation()
        return allocation.get(agent, cap)
    return cap


def parse_ledger(
    text: str, config: CoordinatorConfig, used: Mapping[str, int] | None = None
) -> dict[str, int]:
    """The ledger file as wakes per agent, or why it cannot be applied.

    Exactly the roster, whole non-negative numbers, a total no larger than
    the roster's ``budget.wakes`` summed, and no line below the wakes that
    agent has already started (``used``): transfers move wakes that are
    still unspent, they never create them and never hand on spent ones.
    """

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise CoordinatorError(f"not valid YAML: {error}") from None
    if not isinstance(data, dict):
        raise CoordinatorError("must be a mapping of agent to wakes")
    roster = [profile.name for profile in config.agents]
    unknown = sorted(str(key) for key in data if key not in roster)
    missing = [name for name in roster if name not in data]
    if unknown or missing:
        raise CoordinatorError(
            "must name exactly the roster"
            + (f"; not on it: {', '.join(unknown)}" if unknown else "")
            + (f"; missing: {', '.join(missing)}" if missing else "")
        )
    allocation: dict[str, int] = {}
    for name in roster:
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CoordinatorError(
                f"{name} must be a non-negative whole number of wakes"
            )
        allocation[name] = value
    total, society = sum(allocation.values()), config.budget.wakes * len(roster)
    if total > society:
        raise CoordinatorError(
            f"allots {total} wakes but the society has {society}; transfers move "
            "wakes, they do not create them"
        )
    for name, started in (used or {}).items():
        if name in allocation and allocation[name] < started:
            raise CoordinatorError(
                f"{name} has already started {started} wakes and cannot go below "
                f"that ({allocation[name]}); a maintainer gives only what it has left"
            )
    return allocation


def refresh_ledger(
    config: CoordinatorConfig,
    state: StateStore,
    forge: ForgeClient,
    noticed: dict[str, str] | None = None,
) -> bool:
    """Apply the ledger file when it changed and is valid; True when applied.

    A missing, unreadable or invalid file keeps the last applied allocation
    (or ``budget.wakes`` for everyone when none was ever applied), and is
    reported once per file version, so a bad edit on the board is loud
    without being noisy.
    """

    ledger = config.budget.ledger
    if ledger is None:
        return False
    try:
        found = forge.get_file(ledger.repo, ledger.path)
    except ForgeError as error:
        _notice(
            noticed, "read-error", f"budget ledger {ledger} could not be read: {error}"
        )
        return False
    if found is None:
        _notice(
            noticed,
            "missing",
            f"budget ledger {ledger} is missing; budgets stay as they are",
        )
        return False
    sha, text = found
    applied_sha, before = state.ledger_allocation()
    if sha == applied_sha:
        return False
    used = {profile.name: state.run_wakes(profile.name) for profile in config.agents}
    try:
        allocation = parse_ledger(text, config, used)
    except CoordinatorError as error:
        _notice(noticed, sha, f"budget ledger {ledger}@{sha[:8]} not applied: {error}")
        return False
    state.set_ledger_allocation(sha, allocation)
    if noticed is not None:
        noticed.pop("ledger", None)
    default = config.budget.wakes
    changes = ", ".join(
        f"{name} {before.get(name, default)} -> {wakes}"
        for name, wakes in allocation.items()
        if before.get(name, default) != wakes
    )
    logger.info(
        "budget ledger %s@%s applied: %s", ledger, sha[:8], changes or "no change"
    )
    return True


def render_ledger(config: CoordinatorConfig) -> str:
    """The seed file: every maintainer at ``budget.wakes``, the initial split."""

    lines = [
        "# Wakes each maintainer may start over this run. Give wakes you have not",
        "# used with a pull request that lowers your line and raises another's by",
        "# the same number; the total never grows. Ask for wakes by opening an",
        "# issue here. Merged pull requests are the record of who gave what.",
    ]
    for profile in config.agents:
        lines.append(f"{profile.name}: {config.budget.wakes}")
    return "\n".join(lines) + "\n"


def budget_variables(
    config: CoordinatorConfig, state: StateStore, profile: AgentProfile
) -> dict[str, object]:
    """The budget axis of the prompt (``docs/reference/prompts.md``).

    ``BUDGETED`` says whether the prompt states a budget at all: False when
    there is none or ``prompt.budget`` is ``silent``, and the section is not
    rendered. Otherwise ``BUDGET_WAKES`` is the agent's run budget (0 is a
    real number: an agent that gave everything away), ``WAKES_USED`` what it
    has started so far and ``WAKES_LEFT`` the rest. With a ledger,
    ``LEDGER_REPO`` and ``LEDGER_PATH`` say where wakes move; both empty
    otherwise. The scheduler enforces the cap either way.
    """

    silent = {
        "BUDGETED": False,
        "BUDGET_WAKES": 0,
        "WAKES_USED": 0,
        "WAKES_LEFT": 0,
        "LEDGER_REPO": "",
        "LEDGER_PATH": "",
    }
    cap = agent_cap(config, state, profile.name)
    if cap is None or config.prompt.budget == "silent":
        return silent
    used = state.run_wakes(profile.name)
    ledger = config.budget.ledger
    return {
        "BUDGETED": True,
        "BUDGET_WAKES": cap,
        "WAKES_USED": used,
        "WAKES_LEFT": max(cap - used, 0),
        "LEDGER_REPO": ledger.repo if ledger else "",
        "LEDGER_PATH": ledger.path if ledger else "",
    }


def _notice(noticed: dict[str, str] | None, key: str, message: str) -> None:
    if noticed is not None and noticed.get("ledger") == key:
        return
    logger.warning("%s", message)
    if noticed is not None:
        noticed["ledger"] = key
