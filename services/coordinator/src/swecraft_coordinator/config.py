"""Coordinator configuration from one YAML file.

Secret-bearing values (`admin_token` and `agent_env` entries) support
indirection so the YAML stays committable: `file:<path>` reads a file,
`env:<NAME>` reads the environment, anything else is a literal.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import yaml
from swecraft_runner.pi import PI_AGENT_IMPORT
from swecraft_runner.pricing import ModelPricing

from swecraft_coordinator import CoordinatorError

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_AGENT_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_REPO_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_SENSITIVE_AGENT_KWARG = re.compile(
    r"(?:api_?key|secret|token|password|credential)", re.IGNORECASE
)

CollaborationMode = Literal["shared", "isolated"]
BudgetDisclosure = Literal["stated", "silent"]

DEFAULT_CONFIG_PATH = Path("coordinator.yaml")

# What one wake is. ``single-task``: one coherent unit of work, then stop —
# the agent decides when. ``long-wake``: the harness keeps the agent working
# until the wake's budget is spent, then has it finalize; the next wake
# starts from the notes as usual.
WAKE_MODE_SINGLE_TASK = "single-task"
WAKE_MODE_LONG_WAKE = "long-wake"
WAKE_MODES = (WAKE_MODE_SINGLE_TASK, WAKE_MODE_LONG_WAKE)
# Agent classes whose harness enforces a wake budget.
LONG_WAKE_IMPORTS = (PI_AGENT_IMPORT,)
WAKE_MODE_ENV = "SWECRAFT_WAKE_MODE"
# A long wake's harness is also told a deadline this far (at most) before
# the runner's timeout, which is a hard kill that would lose the wake's
# final files; the harness wraps the wake up before it whatever budget
# kinds are configured.
DEADLINE_MARGIN_SECONDS = 900
_IMPORT_PATH = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*"
)


def is_agent_name(value: str) -> bool:
    return _AGENT_NAME.fullmatch(value) is not None


_TOP_KEYS = (
    "forge_url",
    "push_url",
    "admin_token",
    # The read API's settings. The read API is not part of this release; the
    # keys are still accepted so older configurations, such as the ones a
    # checkpoint carries, keep loading.
    "api_token",
    "api_host",
    "api_port",
    "model",
    "model_pricing",
    "agent",
    "agent_env",
    "agent_kwargs",
    "agent_import_path",
    "prompt",
    "nudges",
    "budget",
    "wake_mode",
    "wake_budget",
    "image",
    "runs_dir",
    "database",
    "credentials_dir",
    "memory_branch",
    "timeout_seconds",
    "interval_seconds",
    "failure_backoff_seconds",
    "max_backoff_seconds",
    "workers",
    "daily_wake_limit",
    "daily_cost_limit_usd",
    "retention_days",
    "checkpoint_every_wakes",
    "checkpoint_keep",
    "checkpoint_config",
    "checkpoint_command",
    "checkpoint_timeout_seconds",
    "checkpoint_publish",
    "publish_command",
    "evaluate_every_wakes",
    "evaluate_command",
    "evaluate_timeout_seconds",
    "agents",
)

_AGENT_KEYS = (
    "name",
    "repo",
    "repos",
    "home_repo",
    "write_repos",
    "image",
    "model",
    "agent",
    "interval_seconds",
    "timeout_seconds",
    "agent_env",
    "agent_kwargs",
    "agent_import_path",
)


@dataclass(frozen=True)
class BelowRate:
    """The collaboration nudge's record condition (``docs/reference/nudges.md``).

    When fewer than ``target`` of the agent's last ``window`` completed
    wakes reached another repository, by the forge ledger, the wake being
    prepared carries the collaboration check with the rate stated. Nothing
    is judged before ``window`` completed wakes exist. 0 turns it off.
    """

    target: float = 0.0
    window: int = 10

    @property
    def enabled(self) -> bool:
        return bool(self.target)


@dataclass(frozen=True)
class CollaborationNudge:
    """Asking the agent whether it has worked with the others, and when.

    Two independent conditions put the collaboration check in the wake
    prompt: ``below_rate``, the forge's record of recent wakes, and
    ``every_wakes``, a cadence that fires whatever the record says. Either
    suffices; 0 disables either. The check asks and weighs; it orders
    nothing, so a no stays an honest answer and the forge record stays
    the measure.
    """

    below_rate: BelowRate = field(default_factory=BelowRate)
    every_wakes: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.every_wakes or self.below_rate.enabled)


@dataclass(frozen=True)
class Nudges:
    """Interventions with a trigger and a text.

    Unlike ``PromptPolicy``, which describes the standing situation the
    same way on every wake, a nudge is present on some wakes and absent
    on the others (``docs/reference/nudges.md``).
    ``finalize_at`` is the end-of-wake notice's place, as a fraction of
    the wake budget, when ``nudges.finalize.at`` sets it; the effective
    value always lives in ``WakeBudget.finalize_at``, the harness contract.
    """

    collaboration: CollaborationNudge = field(default_factory=CollaborationNudge)
    finalize_at: float | None = None


@dataclass(frozen=True)
class PromptPolicy:
    """Settings that change only what the agents are told.

    Everything here reaches the runtime as prompt-template variables and
    nothing else: no grant, token scope, subscription or environment
    variable depends on it (``docs/reference/prompts.md`` lists the axes).
    ``collaboration``: ``shared`` lets the wake prompt mention sibling
    repositories (reading them, asking their maintainers, adopting their
    releases) and the notifications step; ``isolated`` drops them and says
    only that other repositories may be private or inaccessible by design.
    It is paired with private world repositories, which are what actually
    keep siblings out of reach.
    """

    collaboration: CollaborationMode = "shared"
    # ``stated`` tells the agent its run budget in wakes and what it has
    # used; ``silent`` enforces the same cap without a word, which is the
    # disclosure ablation. Nothing without ``budget.wakes``.
    budget: BudgetDisclosure = "stated"


@dataclass(frozen=True)
class LedgerFile:
    """The budget file on the board: ``repo`` and the ``path`` inside it."""

    repo: str
    path: str

    def __str__(self) -> str:
        return f"{self.repo}/{self.path}"


@dataclass(frozen=True)
class RunBudget:
    """A run budget per agent, in wakes.

    ``wakes`` is how many wakes each agent may start over the whole run;
    failed and idle wakes count, since the wake happened. The scheduler
    does not wake an agent at its number again. 0 leaves agents uncapped.

    ``ledger`` lets wakes move between agents. It names a YAML file on the
    board, one line per agent (``name: wakes``), that the coordinator reads
    from the default branch and applies as each agent's number when it
    lists exactly the roster and its total does not exceed the roster's
    ``wakes`` summed: transfers move wakes, never create them. Agents move
    wakes by a pull request against the file and ask by an issue; the
    pull-request history is the record.
    """

    wakes: int = 0
    ledger: LedgerFile | None = None


@dataclass(frozen=True)
class WakeBudget:
    """What ends a long wake: turns, wall-clock seconds, or dollars.

    The first kind exceeded counts; 0 leaves a kind unlimited. Dollars are
    pi's own per-message cost, the number the ledger books. The fractions
    place the two harness interventions: at ``finalize_at`` the agent is told
    to wrap up, at ``hard_stop_at`` the harness limits it to writing its
    final files and then writes the envelope itself.
    """

    turns: int = 0
    seconds: int = 0
    usd: float = 0.0
    finalize_at: float = 0.97
    hard_stop_at: float = 1.05

    def __post_init__(self) -> None:
        for name in ("turns", "seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CoordinatorError(
                    f"wake_budget.{name} must be a non-negative integer"
                )
        if (
            isinstance(self.usd, bool)
            or not isinstance(self.usd, int | float)
            or self.usd < 0
        ):
            raise CoordinatorError("wake_budget.usd must be a non-negative number")
        if not 0 < self.finalize_at <= self.hard_stop_at:
            raise CoordinatorError(
                "finalize_at (nudges.finalize.at or wake_budget.finalize_at) must "
                "be positive and at most wake_budget.hard_stop_at"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.turns or self.seconds or self.usd)

    def describe(self) -> str:
        """The budget as the prompt states it, e.g. ``1,000 turns or $40``."""

        parts = []
        if self.turns:
            parts.append(f"{self.turns:,} turns")
        if self.seconds:
            parts.append(
                f"{self.seconds // 3600} hours"
                if self.seconds % 3600 == 0
                else f"{self.seconds:,} seconds"
            )
        if self.usd:
            parts.append(
                f"${self.usd:,.0f}"
                if self.usd == int(self.usd)
                else f"${self.usd:,.2f}"
            )
        return " or ".join(parts)

    def environment(self) -> dict[str, str]:
        return {
            "SWECRAFT_WAKE_TURNS": str(self.turns),
            "SWECRAFT_WAKE_SECONDS": str(self.seconds),
            "SWECRAFT_WAKE_USD": str(float(self.usd)),
            "SWECRAFT_WAKE_FINALIZE_AT": str(self.finalize_at),
            "SWECRAFT_WAKE_HARD_STOP_AT": str(self.hard_stop_at),
        }


@dataclass(frozen=True)
class AgentProfile:
    """One autonomous maintainer: an identity, its home, the repositories it maintains.

    ``home_repo`` is the agent's control repository: the checkout a wake
    stages at ``/app/repository``, the repository whose ``memory_branch``
    holds the charter and the notes, the anchor of the identity.
    ``write_repos`` is the complete set of repositories the agent
    maintains: collaborator write and a subscription on each, and a real
    checkout staged under ``/app/repos/<name>`` for every one that is not
    the home. The two overlap in the one-repository layout (``repo:`` in
    the configuration), where the home is the maintained repository and
    the notes are as visible as the code. A dedicated home
    (``avery-bot/home``, private, not in ``write_repos``) keeps the notes
    private and starts every wake in a neutral workspace, so the home
    no longer reads as an assignment.
    """

    name: str
    home_repo: str
    write_repos: tuple[str, ...]
    image: str
    model: str
    interval_seconds: int
    timeout_seconds: int
    # Harbor's stock agent for this wake. The model alone does not determine
    # it: `claude-code` and `codex` are different CLIs with different auth,
    # so an agent change usually travels with an `agent_env` change.
    agent: str = "claude-code"
    # A Harbor agent class in place of the stock agent (see the top-level
    # setting); per agent like ``agent`` itself, so a society can mix
    # harnesses the way it mixes models.
    agent_import_path: str = ""
    # Non-secret Harbor constructor options, layered over global options.
    agent_kwargs: dict[str, object] = field(default_factory=dict)
    # Overrides layered over the global `agent_env`, so agents can hold
    # distinct model credentials — several sharing one key is a rate-limit
    # ceiling for concurrent wakes.
    agent_env_sources: dict[str, str] = field(default_factory=dict)

    @property
    def dedicated_home(self) -> bool:
        """Whether the home is a control repository rather than a maintained one."""

        return self.home_repo not in self.write_repos

    @property
    def work_repos(self) -> tuple[str, ...]:
        """The maintained repositories staged beside the home, under /app/repos."""

        return tuple(repo for repo in self.write_repos if repo != self.home_repo)

    @property
    def grant_repos(self) -> tuple[str, ...]:
        """Every repository the identity must be able to write, the home first."""

        return (self.home_repo, *self.work_repos)

    def run_request_overrides(self) -> dict[str, object]:
        """RunRequest fields that swap Harbor's agent class, when configured."""

        if self.agent_import_path:
            return {"agent_import_path": self.agent_import_path}
        return {}


@dataclass(frozen=True)
class CoordinatorConfig:
    forge_url: str
    model: str
    agents: tuple[AgentProfile, ...]
    # What the agents are told, as opposed to what the runtime enforces.
    prompt: PromptPolicy = field(default_factory=lambda: PromptPolicy())
    # Interventions with a trigger and a channel; see Nudges.
    nudges: Nudges = field(default_factory=lambda: Nudges())
    # A run budget per agent in wakes, enforced by the scheduler; see RunBudget.
    budget: RunBudget = field(default_factory=lambda: RunBudget())
    # Unresolved source for the forge admin token; onboarding resolves it.
    admin_token_source: str = ""
    # Unresolved source for the read API's bearer token (see _TOP_KEYS).
    api_token_source: str = ""
    # The read API is an operator convenience with no container to reach it,
    # so it binds loopback by default.
    api_host: str = "127.0.0.1"
    api_port: int = 3_420
    # Container-reachable forge base URL for in-sandbox pushes and API calls.
    push_url: str = ""
    agent_env_sources: dict[str, str] = field(default_factory=dict)
    agent_kwargs: dict[str, object] = field(default_factory=dict)
    # Launch each turn's agent from a Harbor import path
    # (``module.path:ClassName``) instead of the stock agent named by
    # ``agent``, which then only labels the ledger; the default for every
    # agent, overridable per agent. The runner makes classes shipped in
    # ``swecraft_runner`` importable inside Harbor's process; see the runner
    # reference for ``swecraft_runner.harbor_agents:SwecraftPi``.
    agent_import_path: str = ""
    wake_mode: str = WAKE_MODE_SINGLE_TASK
    wake_budget: WakeBudget = field(default_factory=WakeBudget)
    # Optional per-model fallback rates for Harbor agents that expose token
    # counters but not provider cost. Keys are exact Harbor model identifiers.
    model_pricing: dict[str, ModelPricing] = field(default_factory=dict)
    image: str = "swecraft-runner-image:0.1"
    runs_dir: Path = Path(".swecraft/runs")
    database: Path = Path(".swecraft/coordinator.db")
    credentials_dir: Path = Path(".swecraft/coordinator/credentials")
    memory_branch: str = "agent/memory"
    timeout_seconds: int = 1_800
    interval_seconds: int = 1_800
    failure_backoff_seconds: int = 300
    max_backoff_seconds: int = 21_600
    # Per agent, per UTC day; 0 disables the corresponding cap.
    # Wakes allowed in flight at once, across all agents. Each one is a
    # sandboxed turn with its own container, so raise it only as far as
    # the host's CPU, memory, and disk actually stretch.
    workers: int = 1
    daily_wake_limit: int = 48
    daily_cost_limit_usd: float = 0.0
    # Days to keep run directories; 0 disables the sweep entirely.
    retention_days: int = 14
    # Society checkpoint cadence, in the training-loop sense: after every N
    # completed wakes the scheduler drains in-flight work and invokes the
    # checkpoint command at the step boundary. 0 disables the cadence.
    checkpoint_every_wakes: int = 0
    # Boundary checkpoints to retain in Society's configured output
    # directory (read from .swecraft/checkpoint.yaml at prune time); 0 keeps all.
    checkpoint_keep: int = 0
    # Society configuration used by the default checkpoint command and
    # retention. Resume replaces this with an absolute destination-local path.
    checkpoint_config: Path = Path(".swecraft/checkpoint.yaml")
    # Override argv for the checkpoint invocation; empty derives the standard
    # Society command with ``checkpoint_config`` from this checkout.
    checkpoint_command: tuple[str, ...] = ()
    checkpoint_timeout_seconds: int = 3_600
    # Publish each successful boundary checkpoint to the configured HF
    # dataset in a detached process; dispatch never waits on the upload.
    checkpoint_publish: bool = False
    # Override argv for that publish invocation; the checkpoint path is
    # appended as the final argument. Empty derives the standard
    # `swecraft-society publish <path>` command from this checkout.
    publish_command: tuple[str, ...] = ()
    # Boundary evaluation cadence: after every N completed wakes the
    # scheduler drains in-flight work and invokes the evaluation command at
    # the quiet step boundary, so a world@latest snapshot is well defined.
    # 0 disables the cadence.
    evaluate_every_wakes: int = 0
    # Argv for the evaluation invocation; required when the cadence is on,
    # because the evaluator needs its manifest/records/forge arguments.
    evaluate_command: tuple[str, ...] = ()
    evaluate_timeout_seconds: int = 3_600

    @classmethod
    def load(cls, config_path: Path | None = None) -> CoordinatorConfig:
        explicit = config_path is not None or bool(
            os.environ.get("SWECRAFT_COORDINATOR_CONFIG")
        )
        path = config_path or Path(
            os.environ.get("SWECRAFT_COORDINATOR_CONFIG") or DEFAULT_CONFIG_PATH
        )
        if not path.is_file():
            if explicit:
                raise CoordinatorError(f"config file does not exist: {path}")
            raise CoordinatorError(
                f"config file not found: {path} (create it or set "
                "SWECRAFT_COORDINATOR_CONFIG)"
            )
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise CoordinatorError(f"invalid config file {path}: {error}") from None
        if not isinstance(data, dict):
            raise CoordinatorError(f"config file must be a mapping: {path}")
        unknown = set(data) - set(_TOP_KEYS)
        if unknown:
            raise CoordinatorError(
                f"unknown config keys in {path}: {', '.join(sorted(unknown))}"
            )
        return cls._build(data)

    @classmethod
    def _build(cls, data: dict) -> CoordinatorConfig:
        def text(key: str, default: str = "") -> str:
            value = data.get(key)
            return default if value is None else str(value).strip()

        def integer(key: str, default: int, minimum: int = 0) -> int:
            raw = data.get(key)
            if raw is None:
                return default
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise CoordinatorError(f"{key} must be an integer") from None
            if value < minimum:
                raise CoordinatorError(f"{key} must be at least {minimum}")
            return value

        forge_url = text("forge_url", "http://127.0.0.1:3400").rstrip("/")
        model = text("model")
        if not model:
            raise CoordinatorError("required setting is missing: model")
        interval = integer("interval_seconds", 1_800, minimum=60)
        timeout = integer("timeout_seconds", 1_800, minimum=1)
        image = text("image", "swecraft-runner-image:0.1")
        agent = text("agent", "claude-code")
        if not agent:
            raise CoordinatorError("agent must not be empty")
        agent_kwargs = _agent_kwargs(data)
        agent_import_path = text("agent_import_path")
        _validate_import_path(agent_import_path)
        prompt = _prompt(data)
        nudges = _nudges(data, prompt)
        config = cls(
            forge_url=forge_url,
            push_url=text("push_url") or _default_push_url(forge_url),
            admin_token_source=text("admin_token"),
            api_token_source=text("api_token"),
            api_host=text("api_host", "127.0.0.1"),
            api_port=integer("api_port", 3_420, minimum=1),
            model=model,
            prompt=prompt,
            nudges=nudges,
            budget=_budget(data),
            model_pricing=_model_pricing(data),
            agent_kwargs=agent_kwargs,
            agent_import_path=agent_import_path,
            wake_mode=text("wake_mode", WAKE_MODE_SINGLE_TASK),
            wake_budget=_wake_budget(data, nudges),
            agent_env_sources=_agent_env(
                data, default={"ANTHROPIC_API_KEY": "env:ANTHROPIC_API_KEY"}
            ),
            image=image,
            runs_dir=Path(text("runs_dir", ".swecraft/runs")),
            database=Path(text("database", ".swecraft/coordinator.db")),
            credentials_dir=Path(
                text("credentials_dir", ".swecraft/coordinator/credentials")
            ),
            memory_branch=text("memory_branch", "agent/memory"),
            timeout_seconds=timeout,
            interval_seconds=interval,
            failure_backoff_seconds=integer("failure_backoff_seconds", 300, minimum=1),
            max_backoff_seconds=integer("max_backoff_seconds", 21_600, minimum=1),
            workers=integer("workers", 1, minimum=1),
            daily_wake_limit=integer("daily_wake_limit", 48),
            daily_cost_limit_usd=float(data.get("daily_cost_limit_usd") or 0.0),
            retention_days=integer("retention_days", 14),
            checkpoint_every_wakes=integer("checkpoint_every_wakes", 0),
            checkpoint_keep=integer("checkpoint_keep", 0),
            checkpoint_config=Path(
                text("checkpoint_config", ".swecraft/checkpoint.yaml")
            ),
            checkpoint_command=_command_list(data, "checkpoint_command"),
            checkpoint_timeout_seconds=integer(
                "checkpoint_timeout_seconds", 3_600, minimum=1
            ),
            checkpoint_publish=_boolean(data, "checkpoint_publish"),
            publish_command=_command_list(data, "publish_command"),
            evaluate_every_wakes=integer("evaluate_every_wakes", 0),
            evaluate_command=_command_list(data, "evaluate_command"),
            evaluate_timeout_seconds=integer(
                "evaluate_timeout_seconds", 3_600, minimum=1
            ),
            agents=_agents(
                data,
                interval,
                timeout,
                image,
                model,
                agent,
                agent_kwargs,
                agent_import_path,
            ),
        )
        if not config.memory_branch:
            raise CoordinatorError("memory_branch must not be empty")
        if not config.push_url.startswith(("http://", "https://")):
            raise CoordinatorError("push_url must be an http(s) URL")
        if config.evaluate_every_wakes > 0 and not config.evaluate_command:
            raise CoordinatorError("evaluate_every_wakes requires evaluate_command")
        if config.budget.ledger is not None:
            outside = [
                profile.name
                for profile in config.agents
                if config.budget.ledger.repo not in profile.write_repos
            ]
            if outside:
                raise CoordinatorError(
                    f"budget.ledger lives on {config.budget.ledger.repo}, which "
                    "every agent must maintain to read it and move wakes; not in "
                    "write_repos of: " + ", ".join(outside)
                )
        if config.wake_mode not in WAKE_MODES:
            raise CoordinatorError("wake_mode must be one of: " + ", ".join(WAKE_MODES))
        if config.long_wake:
            if not config.wake_budget.enabled:
                raise CoordinatorError(
                    "wake_mode long-wake requires a wake_budget (turns, seconds or usd)"
                )
            unsupported = sorted(
                profile.name
                for profile in config.agents
                if profile.agent_import_path not in LONG_WAKE_IMPORTS
            )
            if unsupported:
                raise CoordinatorError(
                    "wake_mode long-wake requires a harness that enforces the "
                    "budget (agent_import_path one of "
                    + ", ".join(LONG_WAKE_IMPORTS)
                    + ") on every agent; missing on: "
                    + ", ".join(unsupported)
                )
            covered = config.wake_budget.seconds * config.wake_budget.hard_stop_at
            short = sorted(
                profile.name
                for profile in config.agents
                if covered and profile.timeout_seconds < covered
            )
            if short:
                raise CoordinatorError(
                    "timeout_seconds must cover wake_budget.seconds times "
                    "hard_stop_at, or the runner kills the wake before the "
                    "harness ends it; too short on: " + ", ".join(short)
                )
        elif config.wake_budget.enabled:
            raise CoordinatorError("wake_budget requires wake_mode: long-wake")
        return config

    @property
    def long_wake(self) -> bool:
        return self.wake_mode == WAKE_MODE_LONG_WAKE

    def wake_environment(self, timeout_seconds: int) -> dict[str, str]:
        """What a wake's harness is told: the mode, the budget, and the deadline."""

        environment = {WAKE_MODE_ENV: self.wake_mode}
        if self.long_wake:
            environment.update(self.wake_budget.environment())
            environment["SWECRAFT_WAKE_DEADLINE_SECONDS"] = str(
                wake_deadline(timeout_seconds)
            )
        return environment

    def agent(self, name: str) -> AgentProfile:
        for profile in self.agents:
            if profile.name == name:
                return profile
        raise CoordinatorError(f"unknown agent: {name}")

    def pricing_for(self, model: str) -> ModelPricing | None:
        return self.model_pricing.get(model)

    def credentials_path(self, name: str) -> Path:
        return self.credentials_dir / f"{name}.token"

    def resolved_admin_token(self) -> str:
        if not self.admin_token_source:
            raise CoordinatorError(
                "required setting is missing: admin_token (needed for onboarding "
                "and to publish memory as the operator)"
            )
        return resolve_secret(self.admin_token_source)

    def resolved_api_token(self) -> str:
        if not self.api_token_source:
            raise CoordinatorError(
                "required setting is missing: api_token (needed for the read API)"
            )
        return resolve_secret(self.api_token_source)


def _default_push_url(forge_url: str) -> str:
    """In-container pushes cannot reach a loopback forge address directly."""

    parts = urlsplit(forge_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"http://host.docker.internal:{port}"


def wake_deadline(timeout_seconds: int) -> int:
    """Seconds into a long wake by which the harness must have ended it."""

    return max(timeout_seconds - min(DEADLINE_MARGIN_SECONDS, timeout_seconds // 10), 1)


def _wake_budget(data: dict, nudges: Nudges) -> WakeBudget:
    raw = data.get("wake_budget")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CoordinatorError("wake_budget must be a mapping")
    unknown = set(raw) - {"turns", "seconds", "usd", "finalize_at", "hard_stop_at"}
    if unknown:
        raise CoordinatorError(
            "unknown wake_budget keys: " + ", ".join(sorted(map(str, unknown)))
        )
    values: dict[str, object] = {}
    for key in ("turns", "seconds"):
        if raw.get(key) is not None:
            try:
                values[key] = int(raw[key])
            except (TypeError, ValueError):
                raise CoordinatorError(
                    f"wake_budget.{key} must be an integer"
                ) from None
    for key in ("usd", "finalize_at", "hard_stop_at"):
        if raw.get(key) is not None:
            try:
                values[key] = float(raw[key])
            except (TypeError, ValueError):
                raise CoordinatorError(f"wake_budget.{key} must be a number") from None
    if nudges.finalize_at is not None:
        old = values.get("finalize_at")
        if old is not None and old != nudges.finalize_at:
            raise CoordinatorError(
                "nudges.finalize.at and wake_budget.finalize_at disagree; set one "
                "(nudges.finalize.at is the current spelling)"
            )
        values["finalize_at"] = nudges.finalize_at
    return WakeBudget(**values)


def _prompt(data: dict) -> PromptPolicy:
    raw = data.get("prompt")
    if raw is None:
        return PromptPolicy()
    if not isinstance(raw, dict):
        raise CoordinatorError("prompt must be a mapping")
    if "collaboration_check" in raw:
        raise CoordinatorError(
            "prompt.collaboration_check has moved: a nudge is not a prompt "
            "setting; write nudges.collaboration (docs/reference/nudges.md)"
        )
    unknown = set(raw) - {"collaboration", "budget"}
    if unknown:
        raise CoordinatorError(
            "unknown prompt keys: " + ", ".join(sorted(map(str, unknown)))
        )
    collaboration = str(raw.get("collaboration") or "shared").strip()
    if collaboration not in ("shared", "isolated"):
        raise CoordinatorError("prompt.collaboration must be 'shared' or 'isolated'")
    disclosure = str(raw.get("budget") or "stated").strip()
    if disclosure not in ("stated", "silent"):
        raise CoordinatorError("prompt.budget must be 'stated' or 'silent'")
    return PromptPolicy(
        collaboration=cast(CollaborationMode, collaboration),
        budget=cast(BudgetDisclosure, disclosure),
    )


def _nudges(data: dict, prompt: PromptPolicy) -> Nudges:
    raw = data.get("nudges")
    if raw is None:
        return Nudges()
    if not isinstance(raw, dict):
        raise CoordinatorError("nudges must be a mapping")
    unknown = set(raw) - {"collaboration", "finalize"}
    if unknown:
        raise CoordinatorError(
            "unknown nudges keys: " + ", ".join(sorted(map(str, unknown)))
        )
    collaboration = _collaboration_nudge(raw.get("collaboration"))
    if collaboration.enabled and prompt.collaboration == "isolated":
        raise CoordinatorError(
            "nudges.collaboration asks about work with other maintainers; "
            "it has no meaning under prompt.collaboration: isolated"
        )
    return Nudges(
        collaboration=collaboration, finalize_at=_finalize(raw.get("finalize"))
    )


def _collaboration_nudge(raw: object) -> CollaborationNudge:
    if raw is None:
        return CollaborationNudge()
    if not isinstance(raw, dict):
        raise CoordinatorError("nudges.collaboration must be a mapping")
    unknown = set(raw) - {"below_rate", "every_wakes"}
    if unknown:
        raise CoordinatorError(
            "unknown nudges.collaboration keys: " + ", ".join(sorted(map(str, unknown)))
        )
    every = raw.get("every_wakes", 0)
    if isinstance(every, bool) or not isinstance(every, int) or every < 0:
        raise CoordinatorError(
            "nudges.collaboration.every_wakes must be a non-negative whole number"
        )
    return CollaborationNudge(
        below_rate=_below_rate(raw.get("below_rate")), every_wakes=every
    )


def _below_rate(raw: object) -> BelowRate:
    if raw is None:
        return BelowRate()
    if not isinstance(raw, dict):
        raise CoordinatorError("nudges.collaboration.below_rate must be a mapping")
    unknown = set(raw) - {"target", "window"}
    if unknown:
        raise CoordinatorError(
            "unknown nudges.collaboration.below_rate keys: "
            + ", ".join(sorted(map(str, unknown)))
        )
    target = raw.get("target", 0)
    if (
        isinstance(target, bool)
        or not isinstance(target, int | float)
        or not 0 <= target <= 1
    ):
        raise CoordinatorError(
            "nudges.collaboration.below_rate.target must be a number between 0 and 1"
        )
    window = raw.get("window", 10)
    if isinstance(window, bool) or not isinstance(window, int) or window < 3:
        raise CoordinatorError(
            "nudges.collaboration.below_rate.window must be a whole number of "
            "wakes, at least 3"
        )
    return BelowRate(target=float(target), window=window)


def _finalize(raw: object) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CoordinatorError("nudges.finalize must be a mapping")
    unknown = set(raw) - {"at"}
    if unknown:
        raise CoordinatorError(
            "unknown nudges.finalize keys: " + ", ".join(sorted(map(str, unknown)))
        )
    if raw.get("at") is None:
        return None
    try:
        return float(raw["at"])
    except (TypeError, ValueError):
        raise CoordinatorError("nudges.finalize.at must be a number") from None


def _budget(data: dict) -> RunBudget:
    raw = data.get("budget")
    if raw is None:
        return RunBudget()
    if not isinstance(raw, dict):
        raise CoordinatorError("budget must be a mapping")
    unknown = set(raw) - {"wakes", "ledger"}
    if unknown:
        raise CoordinatorError(
            "unknown budget keys: " + ", ".join(sorted(map(str, unknown)))
        )
    wakes = raw.get("wakes")
    if wakes is None:
        wakes = 0
    if isinstance(wakes, bool) or not isinstance(wakes, int) or wakes < 0:
        raise CoordinatorError("budget.wakes must be a non-negative whole number")
    ledger = _ledger_file(raw.get("ledger"))
    if ledger is not None and wakes <= 0:
        raise CoordinatorError(
            "budget.ledger moves a budget every agent must have; set budget.wakes"
        )
    return RunBudget(wakes=wakes, ledger=ledger)


def _ledger_file(raw: object) -> LedgerFile | None:
    """``owner/repo/path/to/file.yaml``: the first two parts name the repository."""

    if raw is None:
        return None
    parts = [part for part in str(raw).strip().split("/") if part]
    if len(parts) < 3 or ".." in parts:
        raise CoordinatorError(
            "budget.ledger must be <owner>/<repo>/<path to the file>, e.g. "
            "swecraft/society/budget.yaml"
        )
    return LedgerFile(
        repo=_repo_slug("/".join(parts[:2]), "budget.ledger"), path="/".join(parts[2:])
    )


def _boolean(data: dict, key: str) -> bool:
    raw = data.get(key)
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise CoordinatorError(f"{key} must be a boolean")
    return raw


def _command_list(data: dict, key: str) -> tuple[str, ...]:
    raw = data.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise CoordinatorError(f"{key} must be a list of non-empty argv strings")
    return tuple(item.strip() for item in raw)


def _agent_kwargs(data: dict) -> dict[str, object]:
    raw = data.get("agent_kwargs")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CoordinatorError("agent_kwargs must be a mapping")
    result: dict[str, object] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise CoordinatorError(f"invalid agent kwarg name: {name!r}")
        if _SENSITIVE_AGENT_KWARG.search(name):
            raise CoordinatorError(
                f"secret-like agent kwarg {name!r} must use agent_env"
            )
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise CoordinatorError(
                f"agent kwarg {name!r} must be a JSON value: {error}"
            ) from None
        result[name] = value
    return result


def _model_pricing(data: dict) -> dict[str, ModelPricing]:
    raw = data.get("model_pricing")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CoordinatorError("model_pricing must be a mapping of model to rates")
    result: dict[str, ModelPricing] = {}
    for model, rates in raw.items():
        model_name = str(model).strip()
        if not model_name:
            raise CoordinatorError("model_pricing model names must not be empty")
        if not isinstance(rates, dict):
            raise CoordinatorError(
                f"model_pricing for {model_name!r} must be a mapping"
            )
        try:
            result[model_name] = ModelPricing.from_mapping(rates)
        except ValueError as error:
            raise CoordinatorError(
                f"invalid model_pricing for {model_name!r}: {error}"
            ) from None
    return result


def _agent_env(data: dict, default: dict[str, str] | None = None) -> dict[str, str]:
    """Parse an `agent_env` mapping; `default=None` means "no entries".

    The top-level call keeps the historical fallback; per-agent overrides
    default to empty so an agent without the key inherits the global map
    untouched.
    """

    mapping = data.get("agent_env")
    if mapping is None:
        if default is None:
            return {}
        return dict(default)
    if not isinstance(mapping, dict):
        raise CoordinatorError("agent_env must be a mapping of NAME to source")
    sources = {}
    for name, source in mapping.items():
        name = str(name)
        if not _ENV_NAME.fullmatch(name):
            raise CoordinatorError(f"invalid agent environment variable name: {name}")
        sources[name] = str(source)
    return sources


def _validate_import_path(value: str) -> None:
    if value and not _IMPORT_PATH.fullmatch(value):
        raise CoordinatorError(
            "agent_import_path must be of the form module.path:ClassName"
        )


def _agents(
    data: dict,
    interval: int,
    timeout: int,
    image: str,
    model: str,
    agent: str,
    agent_kwargs: dict[str, object],
    agent_import_path: str = "",
) -> tuple[AgentProfile, ...]:
    raw = data.get("agents")
    if not isinstance(raw, list) or not raw:
        raise CoordinatorError("agents must be a non-empty list")
    profiles = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise CoordinatorError("each agent must be a mapping")
        unknown = set(entry) - set(_AGENT_KEYS)
        if unknown:
            raise CoordinatorError(f"unknown agent keys: {', '.join(sorted(unknown))}")
        name = str(entry.get("name") or "").strip()
        if not is_agent_name(name):
            raise CoordinatorError(
                f"agent name must be a lowercase forge username: {name!r}"
            )
        home_repo, write_repos = _agent_repositories(entry, name)
        import_path = str(entry.get("agent_import_path") or agent_import_path).strip()
        _validate_import_path(import_path)
        profiles.append(
            AgentProfile(
                name=name,
                home_repo=home_repo,
                write_repos=write_repos,
                image=str(entry.get("image") or image),
                model=str(entry.get("model") or model),
                agent=str(entry.get("agent") or agent),
                agent_import_path=import_path,
                agent_kwargs={**agent_kwargs, **_agent_kwargs(entry)},
                interval_seconds=_agent_integer(
                    entry, "interval_seconds", interval, minimum=60
                ),
                timeout_seconds=_agent_integer(
                    entry, "timeout_seconds", timeout, minimum=1
                ),
                agent_env_sources=_agent_env(entry, default=None),
            )
        )
    names = [profile.name for profile in profiles]
    if len(set(names)) != len(names):
        raise CoordinatorError("agent names must be unique")
    homes = [profile.home_repo for profile in profiles]
    if len(set(homes)) != len(homes):
        raise CoordinatorError(
            "agent home_repo values must be distinct: two agents sharing a "
            "home would share one memory branch"
        )
    return tuple(profiles)


def _agent_repositories(entry: dict, name: str) -> tuple[str, tuple[str, ...]]:
    """The agent's home and the repositories it maintains, from either form.

    ``home_repo`` + ``write_repos`` is the explicit form. The legacy form,
    ``repo`` (optionally ``repos``), is the one-repository maintainer and
    normalizes to a home that is also maintained:
    ``write_repos = [repo, *repos]``.
    """

    explicit = "home_repo" in entry or "write_repos" in entry
    legacy = "repo" in entry or "repos" in entry
    if explicit and legacy:
        raise CoordinatorError(
            f"agent {name}: use home_repo + write_repos, or the legacy repo "
            "(+ repos), not both"
        )
    if explicit:
        home = _repo_slug(entry.get("home_repo"), "agent home_repo")
        write = _repo_slugs(entry.get("write_repos"), "agent write_repos")
        if not write:
            raise CoordinatorError(
                f"agent {name}: write_repos must name at least one repository"
            )
    else:
        home = _repo_slug(entry.get("repo"), "agent repo")
        further = _repo_slugs(entry.get("repos"), "agent repos")
        if home in further:
            raise CoordinatorError(f"agent {name}: repos must not repeat repo")
        write = (home, *further)
    if len(set(write)) != len(write):
        raise CoordinatorError(f"agent {name}: write_repos must be distinct")
    # Maintained repositories other than the home are staged at
    # /app/repos/<name>, so their names must not collide.
    staged = [repo.split("/", 1)[1] for repo in write if repo != home]
    if len(set(staged)) != len(staged):
        raise CoordinatorError(
            f"agent {name}: maintained repositories must have distinct names, "
            "each is staged at /app/repos/<name>"
        )
    return home, write


def _repo_slug(raw: object, what: str) -> str:
    value = str(raw or "").strip()
    if not _REPO_SLUG.fullmatch(value):
        raise CoordinatorError(f"{what} must be owner/name: {value!r}")
    return value


def _repo_slugs(raw: object, what: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise CoordinatorError(f"{what} must be a list of owner/name strings")
    return tuple(_repo_slug(item, what) for item in raw)


def _agent_integer(entry: dict, key: str, default: int, *, minimum: int) -> int:
    """Per-agent overrides get the same validation as the global settings."""

    raw = entry.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CoordinatorError(f"agent {key} must be an integer") from None
    if value < minimum:
        raise CoordinatorError(f"agent {key} must be at least {minimum}")
    return value


def resolve_secret(value: str) -> str:
    """Resolve `file:<path>` and `env:<NAME>` indirections; pass literals."""

    if value.startswith("file:"):
        path = Path(value[len("file:") :]).expanduser()
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise CoordinatorError(
                f"could not read secret file {path}: {error}"
            ) from None
        if not content:
            raise CoordinatorError(f"secret file is empty: {path}")
        return content
    if value.startswith("env:"):
        name = value[len("env:") :]
        resolved = os.environ.get(name)
        if resolved is None:
            raise CoordinatorError(f"environment variable is not set: {name}")
        return resolved
    return value


def resolve_agent_env(
    config: CoordinatorConfig, profile: AgentProfile | None = None
) -> dict[str, str]:
    """Global `agent_env`, with this agent's own entries layered on top."""

    sources = dict(config.agent_env_sources)
    if profile is not None:
        sources.update(profile.agent_env_sources)
    return {name: resolve_secret(source) for name, source in sources.items()}
