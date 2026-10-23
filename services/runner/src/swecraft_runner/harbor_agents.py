"""Harbor agent subclasses loaded inside Harbor's own process.

This module is referenced through Harbor's ``import_path`` mechanism
(``swecraft_runner.harbor_agents:ResumingClaudeCode``,
``swecraft_runner.harbor_agents:SwecraftPi``,
``swecraft_runner.harbor_agents:SwecraftAntigravityCli``)
and therefore imports harbor lazily: the
runner package does not depend on harbor, the invocation environment
supplies it (``uv run --project services/runner --with harbor==<pin>
swecraft-harbor``, the runner's default Harbor command).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from swecraft_runner import antigravity as hub_antigravity
from swecraft_runner import pi as swecraft_pi
from swecraft_runner.harbor_cli import apply_process_fixes
from swecraft_runner.pi import trajectory as pi_trajectory

try:
    from harbor.agents.installed.antigravity_cli import AntigravityCli
    from harbor.agents.installed.base import CliFlag
    from harbor.agents.installed.claude_code import ClaudeCode
    from harbor.agents.installed.pi import Pi
    from harbor.models.trial.paths import EnvironmentPaths
except ImportError as error:  # pragma: no cover - only hit outside harbor env
    raise ImportError(
        "swecraft_runner.harbor_agents requires harbor; invoke through "
        "'uv run --project services/runner --with harbor==<pin> swecraft-harbor' "
        "so both are importable"
    ) from error

logger = logging.getLogger(__name__)

# The launcher that loads a class from here by import path may not be the
# runner's own swecraft-harbor; make sure the in-process fixes are in place either
# way (idempotent).
apply_process_fixes()

if TYPE_CHECKING:  # pragma: no cover
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

PRIOR_SESSION_DIR = "/app/prior-session"
RESUME_ENV = "SWECRAFT_RESUME"
# Where SwecraftAntigravityCli puts an uploaded Google credentials file.
ANTIGRAVITY_CREDENTIALS_UPLOAD_PATH = (
    "/tmp/swecraft-agy-secrets/google-credentials.json"
)
# The Antigravity CLI as its installer places it; a profile image bakes it here.
ANTIGRAVITY_BINARY = "$HOME/.local/bin/agy"


async def _upload_google_credentials(
    agent, environment: BaseEnvironment, remote_path: str
) -> bool:
    """Upload a declared service-account file and repoint the declared env at it.

    Harbor merges an exec's environment as persistent < per-exec < the
    agent's declared env (``AgentConfig.env``), and the trial overlays a copy
    of that declared env around setup and again around run. A per-exec
    override therefore loses to the host path the run declared; rewriting
    the declared env here, during setup, is what the run phase then copies.
    Returns whether a file was declared. Coupling note: writes the
    installed-agent base's private ``_extra_env`` — pinned to the runner's
    Harbor version like the other private members this module relies on.
    """

    host_path = swecraft_pi.google_credentials_path(agent.extra_env)
    if host_path is None:
        return False
    await _upload_host_file(
        agent,
        environment,
        Path(host_path),
        remote_path,
        what="the Google credentials file",
    )
    agent._extra_env[swecraft_pi.GOOGLE_CREDENTIALS_PATH_ENV] = remote_path
    return True


async def _upload_host_file(
    agent,
    environment: BaseEnvironment,
    host_path: Path,
    remote_path: str,
    *,
    what: str,
) -> None:
    """Copy a host file into the sandbox as a file, readable by the agent user.

    Harbor's ``upload_file`` moves the bytes without them touching a command
    line, an environment variable, or a log, which is the only way a
    credential file may enter a run.
    """

    if not host_path.is_file():
        raise RuntimeError(f"{what} at {host_path} is not a readable file")
    remote_dir = str(PurePosixPath(remote_path).parent)
    await agent.exec_as_agent(
        environment, command=f"mkdir -p {shlex.quote(remote_dir)}"
    )
    await environment.upload_file(host_path, remote_path)
    if environment.default_user is not None:
        await agent.exec_as_root(
            environment,
            command=f"chown {environment.default_user} {shlex.quote(remote_path)}",
        )
    await agent.exec_as_agent(
        environment, command=f"chmod 600 {shlex.quote(remote_path)}"
    )


class ResumingClaudeCode(ClaudeCode):
    """claude-code with cross-run session resume.

    Harbor's own resume only spans steps of one multi-step trial. When the
    runner stages prior transcripts at ``/app/prior-session`` and sets
    ``SWECRAFT_RESUME`` in the agent environment, this subclass copies them into
    ``$CLAUDE_CONFIG_DIR/projects/-app`` before the turn and invokes the CLI
    with ``--continue``, so the new turn starts with the prior conversation
    natively loaded.

    Coupling note: this relies on Harbor's session layout
    (``agent_dir / "sessions"``) and the ``_resume``/``_get_env`` members of
    the installed-agent base — private surface pinned by the runner's Harbor
    version. Revisit on any Harbor bump.
    """

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if (self._get_env(RESUME_ENV) or "").strip():
            sessions = (EnvironmentPaths.agent_dir / "sessions").as_posix()
            await self.exec_as_agent(
                environment,
                command=(
                    # The staged bundle is a vetted *.jsonl tree; copy it
                    # whole so nested transcripts (subagents/*.jsonl) land.
                    f"mkdir -p {sessions}/projects/-app && "
                    f"cp -R {PRIOR_SESSION_DIR}/. {sessions}/projects/-app/"
                ),
            )
            self._resume = True
        await super().run(instruction, environment, context)


class SwecraftPi(Pi):
    """pi as a SWECraft agent: pinned in the task image, run with SWECraft's extensions.

    Harbor's stock ``Pi`` installs the CLI from the network on every trial
    and runs one prompt with whatever extensions the sandbox happens to
    carry. This subclass expects pi pre-installed and pinned in the task
    image (like claude-code; see the profile image template), writes the
    runner's extensions and pi settings into the sandbox once per trial, and
    runs pi with exactly those loaded. The shell and the files live in
    ``swecraft_runner.pi``; credentials come from the declared agent env only,
    never the host's (``swecraft_runner.pi.credentials``); usage and cost come
    from pi's own event stream through the stock
    ``populate_context_post_run``.

    Two kinds of login. API-key providers (``anthropic/...``) take their key
    from the agent env. The Codex subscription (``openai-codex/...``) takes
    the codex CLI's ``auth.json`` snapshot named by ``CODEX_AUTH_JSON_PATH``
    in the agent env, as the codex arms do; the snapshot is converted to
    pi's credential and uploaded as a file, so the tokens never pass through
    a command line or an environment variable.

    Constructor kwargs (Harbor ``agent_kwargs``):

    * ``thinking``: pi's thinking level for the model;
    * ``bash_timeout``: default timeout in seconds for bash calls the model
      issues without one (0 disables);
    * ``context_window``: the model's context window as pi knows it, taken
      from ``swecraft_runner.pi.KNOWN_CONTEXT_WINDOWS`` when omitted; by default
      a long wake uses all of it, with pi compacting near its edge and a
      last-resort finalize just below it;
    * ``context``: the context management method of any pi turn,
      ``native-summary`` (pi's own compaction, the only one);
    * ``bash_timeout_max``: a ceiling the model's own bash timeout is clamped
      to (0: none) — a session waiting for work sleeps in a bash call, and
      hours of sleep keep every harness notice from reaching the model;
    * ``base_url``: send the provider's calls to this endpoint instead of its
      default (a gateway or proxy in front of it, as a site without direct
      provider access has); written into the sandbox as pi's ``models.json``,
      with the provider's built-in models and pricing kept and its credential
      still taken from the agent env;
    * the ``swecraft_runner.pi.LongWakePolicy`` fields, which shape a long wake
      inside the budget the coordinator states in the agent env
      (``SWECRAFT_WAKE_*``); they are inert in a single-task wake.
    """

    SUPPORTS_RESUME = False

    def __init__(
        self,
        *args,
        thinking: str = "medium",
        bash_timeout: int = 900,
        context_window: int | None = None,
        context: str = "native-summary",
        bash_timeout_max: int = 0,
        base_url: str = "",
        **kwargs,
    ) -> None:
        policy = swecraft_pi.policy_from_kwargs(kwargs)
        super().__init__(*args, **kwargs)
        if not isinstance(base_url, str):
            raise ValueError("base_url must be a string")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        self._base_url = base_url
        if thinking not in swecraft_pi.THINKING_LEVELS:
            raise ValueError(
                "thinking must be one of: " + ", ".join(swecraft_pi.THINKING_LEVELS)
            )
        if (
            isinstance(bash_timeout, bool)
            or not isinstance(bash_timeout, int)
            or bash_timeout < 0
        ):
            raise ValueError("bash_timeout must be a non-negative integer")
        if context_window is not None and (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window < 1
        ):
            raise ValueError("context_window must be a positive integer")
        if context not in swecraft_pi.CONTEXT_MODES:
            raise ValueError(
                "context must be one of: " + ", ".join(swecraft_pi.CONTEXT_MODES)
            )
        if (
            isinstance(bash_timeout_max, bool)
            or not isinstance(bash_timeout_max, int)
            or bash_timeout_max < 0
        ):
            raise ValueError("bash_timeout_max must be a non-negative integer")
        self._bash_timeout_max = bash_timeout_max
        self._thinking = thinking
        self._bash_timeout = bash_timeout
        self._context_window_kwarg = context_window
        self._unresolved_policy = policy
        self._context = context

    def get_version_command(self) -> str | None:
        return "pi --version"

    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    def _provider_and_model(self) -> tuple[str, str]:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")
        provider, model = self.model_name.split("/", 1)
        return provider, model

    def _context_window(self) -> int:
        if self._context_window_kwarg is not None:
            return self._context_window_kwarg
        window = swecraft_pi.context_window_for(self.model_name or "")
        if window is None:
            raise ValueError(
                f"context_window is not on file for {self.model_name!r}; "
                "pass it as an agent kwarg"
            )
        return window

    def _policy(self) -> swecraft_pi.LongWakePolicy:
        return self._unresolved_policy.resolved(self._context_window())

    async def install(self, environment: BaseEnvironment) -> None:
        provider, _ = self._provider_and_model()
        auth_file = None
        if provider == swecraft_pi.CODEX_PROVIDER:
            auth_file = swecraft_pi.AUTH_UPLOAD_PATH
            await self._upload_codex_login(environment, auth_file)
        if provider == swecraft_pi.VERTEX_PROVIDER:
            # Application Default Credentials travel as a file: the declared
            # value names it on the host, pi reads the sandbox copy.
            await self._upload_google_credentials(
                environment, swecraft_pi.GOOGLE_CREDENTIALS_UPLOAD_PATH
            )
        credentials = (
            {}
            if provider == swecraft_pi.CODEX_PROVIDER
            else swecraft_pi.credentials(provider, self.extra_env)
        )
        await self.exec_as_agent(
            environment,
            command=swecraft_pi.install_command(
                context_window=self._context_window(),
                policy=self._policy(),
                context=self._context,
                auth_file=auth_file,
                provider=provider,
                base_url=self._base_url,
                credentials=credentials,
            ),
        )

    async def _upload_google_credentials(
        self, environment: BaseEnvironment, remote_path: str
    ) -> None:
        await _upload_google_credentials(self, environment, remote_path)

    async def _upload_codex_login(
        self, environment: BaseEnvironment, remote_path: str
    ) -> None:
        """Convert the codex snapshot and put it in the sandbox as a file.

        Harbor's ``upload_file`` copies host to sandbox without the content
        touching a command line, an environment variable, or a log; the host
        side is a 0600 temporary file removed as soon as the upload is done.
        """

        snapshot_path = Path(swecraft_pi.codex_snapshot_path(self.extra_env))
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"could not read the Codex login at {snapshot_path}: {error}"
            ) from error
        credential = swecraft_pi.codex_credential(snapshot)
        if swecraft_pi.codex_credential_expired(credential):
            self.logger.warning(
                "Codex login at %s has an expired access token; pi will try "
                "the refresh token, which the codex CLI may already have used",
                snapshot_path,
            )
        descriptor, host_path = tempfile.mkstemp(
            prefix="swecraft-pi-auth-", suffix=".json"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(swecraft_pi.pi_auth_file(credential))
            os.chmod(host_path, 0o600)
            await _upload_host_file(
                self,
                environment,
                Path(host_path),
                remote_path,
                what="the converted Codex login",
            )
        finally:
            os.unlink(host_path)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        # pi's raw session log stays as it is; Harbor's trajectory.json is
        # derived beside it so the run packaging and the console read a pi run
        # like any other harness (swecraft_runner.pi.trajectory). Never fail a run
        # over the transcript.
        try:
            pi_trajectory.write_trajectory(self.logs_dir, force=True)
        except Exception:  # noqa: BLE001
            logger.warning(
                "pi trajectory not written for %s", self.logs_dir, exc_info=True
            )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        provider, model = self._provider_and_model()
        credentials = (
            {}
            if provider == swecraft_pi.CODEX_PROVIDER
            else swecraft_pi.credentials(provider, self.extra_env)
        )
        await self.exec_as_agent(
            environment,
            command=swecraft_pi.run_command(
                provider=provider,
                model=model,
                thinking=self._thinking,
                instruction=instruction,
                context=self._context,
            ),
            env=swecraft_pi.agent_environment(
                credentials=credentials,
                bash_timeout=self._bash_timeout,
                policy=self._policy(),
                bash_timeout_max=self._bash_timeout_max,
                wake=self.extra_env,
                context=self._context,
            ),
        )


class SwecraftAntigravityCli(AntigravityCli):
    """Harbor's antigravity-cli agent with a Google credentials file uploaded by path.

    Harbor 0.22's stock agent runs the Antigravity CLI headlessly in two ways:
    a Gemini API key (``GEMINI_API_KEY``), or ``AGY_ADC_AUTH=true``, the
    enterprise path, where the CLI authenticates from Application Default
    Credentials already present in the container. This subclass supplies the
    second form the way SwecraftPi does for pi: the service-account file named by
    ``GOOGLE_APPLICATION_CREDENTIALS`` on the host is uploaded once per trial
    (through Harbor's ``upload_file``, so the key never passes through a
    command line, an environment variable value, or a log), the declared
    variable is repointed at the sandbox copy before the run, and the ADC
    switch is set when a file is declared. ``agent`` stays ``antigravity-cli``
    as the ledger label; ``agent_import_path`` selects this class.

    Pinning: the CLI's installer has no version argument and Harbor's stock
    install runs it at every trial, so a profile image that bakes the binary
    is the only way to hold the version still. When the image carries the
    binary this class skips the installer, and Harbor records the version it
    finds through ``agy --version``.

    Cost: the CLI's step transcript carries no token counters (agy 1.2.6,
    either credential form), so Harbor's stock agent books nothing for a
    run, zero tokens and zero dollars. The CLI's run summary does carry
    them, so this class asks for the JSON summary on stdout
    (``--output-format json``; the ``output_format`` kwarg overrides it)
    and reads the counters back from the CLI's stdout capture after the
    run, mapping the CLI's disjoint sent-and-cached counts onto Harbor's
    whole-prompt convention (see ``swecraft_runner.antigravity.CliUsage``),
    priced from LiteLLM's table as the stock agent would. Worth selecting
    for the API-key form too.

    Coupling note: writes the installed-agent base's private ``_extra_env``
    (see ``_upload_google_credentials``) — pinned to the runner's Harbor
    version like the other private members this module relies on.
    """

    CLI_FLAGS = [
        *AntigravityCli.CLI_FLAGS,
        CliFlag(
            "output_format",
            cli="--output-format",
            type="enum",
            choices=list(hub_antigravity.OUTPUT_FORMATS),
            default=hub_antigravity.DEFAULT_OUTPUT_FORMAT,
        ),
    ]

    async def install(self, environment: BaseEnvironment) -> None:
        present = await environment.exec(
            command=f"test -x {ANTIGRAVITY_BINARY}",
            user=environment.default_user,
        )
        if present.return_code == 0:
            self.logger.debug("Antigravity CLI present in the image; installer skipped")
        else:
            await super().install(environment)
        if await _upload_google_credentials(
            self, environment, ANTIGRAVITY_CREDENTIALS_UPLOAD_PATH
        ):
            # A credentials file means the enterprise ADC path; the stock agent
            # reads the switch through the declared env and canonicalizes it.
            self._extra_env.setdefault("AGY_ADC_AUTH", "true")

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        # The stock hook leaves the counters unknown when the transcript has
        # none; a run cannot have used zero tokens, so zeros are unknown too.
        if context.n_input_tokens or context.n_output_tokens:
            return
        try:
            captured = (self.logs_dir / hub_antigravity.CLI_OUTPUT_NAME).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return
        usage = hub_antigravity.usage_from_cli_output(captured)
        if usage is None:
            return
        context.n_input_tokens = usage.prompt_tokens
        context.n_cache_tokens = usage.cache_read_tokens
        context.n_output_tokens = usage.output_tokens
        if context.cost_usd is None:
            context.cost_usd = self._compute_cost_from_pricing(
                usage.prompt_tokens, usage.output_tokens, usage.cache_read_tokens
            )
