"""pi as a SWECraft agent: the pure parts of ``harbor_agents.SwecraftPi``.

Harbor's stock pi agent installs the CLI from the network on every trial
(under a package name pi has since left behind), sources an nvm SWECraft's
images do not carry, and cannot choose which extensions a run loads. The
SWECraft's agent expects pi pinned in the task image, ships its own extensions
and settings into the sandbox once per trial, and runs pi with exactly
those loaded. This module holds everything that does not need Harbor
importable — the files shipped, the shell executed, the environment set —
so the contract is unit-tested outside Harbor's process.
"""

from __future__ import annotations

import base64
import json
import shlex
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from importlib import resources

# The agent class the coordinator names in ``agent_import_path``.
PI_AGENT_IMPORT = "swecraft_runner.harbor_agents:SwecraftPi"
# Where the shipped extensions and settings are written inside the sandbox.
HARNESS_DIR = "/opt/swecraft-pi/harness"
SESSION_DIR = "/logs/agent/pi/sessions"
OUTPUT_FILENAME = "pi.txt"
# Extensions shipped with the runner and loaded, in this order, by every run.
EXTENSIONS = ("bash-timeout.ts", "long-wake.ts")
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
BASH_TIMEOUT_ENV = "SWECRAFT_BASH_TIMEOUT"
BASH_TIMEOUT_MAX_ENV = "SWECRAFT_BASH_TIMEOUT_MAX"
RESULT_PATH = "/app/result.json"
# The coordinator states the wake mode and budget in the agent env under this
# prefix (``SWECRAFT_WAKE_MODE``, ``SWECRAFT_WAKE_TURNS``, ...); the agent forwards them
# to the pi process, where the long-wake extension reads them.
WAKE_ENV_PREFIX = "SWECRAFT_WAKE_"
# Room pi keeps for the compaction summary at least; pi's 16k default left a
# thinking model's summaries hitting the token cap, so they were discarded.
MIN_COMPACTION_RESERVE = 65_536
# How far below the model window a long wake is finalized as a last resort
# when no hard context budget is set: above pi's compaction threshold, so a
# wake is never cut where compaction would have saved it, and below the
# window, so a wake still ends cleanly when compaction cannot keep up.
HARD_STOP_HEADROOM = 32_768
# The context management method of a pi turn, single-task or long:
# ``native-summary`` leaves it to pi's compaction, which summarizes older
# messages.
CONTEXT_MODES = ("native-summary",)
# A Codex subscription login: the coordinator names the codex CLI's auth.json
# snapshot in the agent env, as the codex arms do, and the agent converts it
# into pi's credential for this provider and uploads it as a file.
CODEX_PROVIDER = "openai-codex"
# Google: the Gemini API (``google/...`` models, an API key) and Vertex AI
# (``google-vertex/...``, an API key or Application Default Credentials
# named by a service-account file, plus the project and location).
GOOGLE_PROVIDER = "google"
VERTEX_PROVIDER = "google-vertex"
VERTEX_API_KEY_ENV = "GOOGLE_CLOUD_API_KEY"
# The Gemini CLI reads the same Vertex key as GOOGLE_API_KEY; accepted here so
# one declaration serves both harnesses.
VERTEX_API_KEY_ALIAS = "GOOGLE_API_KEY"
GOOGLE_CREDENTIALS_PATH_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
VERTEX_PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"
VERTEX_LOCATION_ENV = "GOOGLE_CLOUD_LOCATION"
GOOGLE_CREDENTIALS_UPLOAD_PATH = "/tmp/swecraft-pi-secrets/google-credentials.json"
# Declared names that are not API keys: pi resolves them itself (Application
# Default Credentials from the file, plus the project and location), so a
# models.json entry must not name them as its ``apiKey``.
_NOT_API_KEYS = frozenset(
    (GOOGLE_CREDENTIALS_PATH_ENV, VERTEX_PROJECT_ENV, VERTEX_LOCATION_ENV)
)
CODEX_AUTH_PATH_ENV = "CODEX_AUTH_JSON_PATH"
AUTH_UPLOAD_PATH = "/tmp/swecraft-pi-secrets/auth.json"
_JWT_AUTH_CLAIM = "https://api.openai.com/auth"
# Context windows as pi's built-in model table (0.85.1) has them, for the
# models SWECraft runs; anything else needs an explicit ``context_window``.
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "anthropic/claude-opus-5": 1_000_000,
    "openai-codex/gpt-5.6-sol": 272_000,
    "openai-codex/gpt-5.6-terra": 272_000,
    "openai-codex/gpt-6-astra": 272_000,
    "google/gemini-3.8-flash": 1_048_576,
    "google/gemini-3.1-pro-preview": 1_048_576,
    "google-vertex/gemini-3.8-flash": 1_048_576,
    "google-vertex/gemini-3.1-pro-preview": 1_048_576,
}

# Credentials the CLI reads, per pi provider. Only these are forwarded to the
# pi process; the runner's agent env may carry more (forge tokens for the
# sandbox's wrappers), which pi has no reason to see.
PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY",),
    "google-vertex": (
        VERTEX_API_KEY_ENV,
        VERTEX_API_KEY_ALIAS,
        GOOGLE_CREDENTIALS_PATH_ENV,
        VERTEX_PROJECT_ENV,
        VERTEX_LOCATION_ENV,
    ),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "xai": ("XAI_API_KEY",),
}


# The wire API each built-in provider speaks, read from pi 0.85.1's model
# catalogs (pi-ai ``providers/data/<provider>.json``); a models.json entry
# that overrides a provider's endpoint must name it, or pi's config loader
# rejects the entry. openrouter is absent on purpose: its catalog mixes two APIs, so
# an override there would have to list models, which this does not do.
PROVIDER_API: dict[str, str] = {
    "anthropic": "anthropic-messages",
    "openai": "openai-responses",
    "google": "google-generative-ai",
    "google-vertex": "google-vertex",
    "groq": "openai-completions",
    "mistral": "mistral-conversations",
    "xai": "openai-responses",
}


def models_json(
    provider: str,
    base_url: str,
    credentials: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """pi's ``models.json`` sending ``provider`` to ``base_url`` instead.

    The provider keeps its built-in model list and pricing; only the endpoint,
    the wire API and the credential reference are stated. The credential is
    named by environment variable (pi resolves ``$NAME``), so the URL is the
    only thing the file carries. ``credentials`` is what the run declared
    (see ``credentials()``): the first API-key name among them is the one
    named, and a run that declared no key at all, a Vertex service-account
    file with its project and location, gets no ``apiKey`` entry, since a
    configured key would short-circuit pi's own Vertex resolver before it
    could fall back to Application Default Credentials.
    """

    if provider not in PROVIDER_KEYS:
        raise ValueError(f"SwecraftPi is not wired for the {provider!r} provider")
    if provider not in PROVIDER_API:
        raise ValueError(
            f"the {provider!r} provider has no single wire API "
            "to route through a gateway"
        )
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must be an http(s) URL")
    key_names = [name for name in PROVIDER_KEYS[provider] if name not in _NOT_API_KEYS]
    if credentials is None:
        key_name: str | None = key_names[0]
    else:
        key_name = next((name for name in key_names if credentials.get(name)), None)
    entry: dict[str, object] = {
        "baseUrl": base_url.rstrip("/"),
        "api": PROVIDER_API[provider],
    }
    if key_name is not None:
        entry["apiKey"] = f"${key_name}"
    return {"providers": {provider: entry}}


def credentials(provider: str, declared: Mapping[str, str]) -> dict[str, str]:
    """The provider credentials a run declared, and nothing from the host.

    Harbor's ``_get_env`` falls back to Harbor's own process environment,
    which the runner starts from the host's, so an ambient key would reach
    a run that never declared it — past per-run credential selection and
    the runner's redaction of declared values. Only the declared agent env
    (Harbor's ``BaseAgent.extra_env``) is consulted.
    """

    if provider not in PROVIDER_KEYS:
        raise ValueError(f"SwecraftPi is not wired for the {provider!r} provider")
    found = {
        name: declared[name] for name in PROVIDER_KEYS[provider] if declared.get(name)
    }
    if not found:
        raise RuntimeError(
            f"no {provider} credential in the agent env (expected one of: "
            + ", ".join(PROVIDER_KEYS[provider])
            + ")"
        )
    if provider == VERTEX_PROVIDER:
        return vertex_credentials(found)
    return found


def vertex_credentials(declared: Mapping[str, str]) -> dict[str, str]:
    """The Vertex AI credential set as pi reads it.

    pi's ``google-vertex`` provider takes an API key as ``GOOGLE_CLOUD_API_KEY``
    or Application Default Credentials from the service-account file named by
    ``GOOGLE_APPLICATION_CREDENTIALS``, and needs the project and location
    either way. A key declared under the Gemini CLI's name for it,
    ``GOOGLE_API_KEY``, is forwarded under pi's. The file path is the host's;
    the agent replaces it with the sandbox copy it uploads.
    """

    credentials = {
        name: value
        for name, value in declared.items()
        if name in PROVIDER_KEYS[VERTEX_PROVIDER] and value
    }
    alias = credentials.pop(VERTEX_API_KEY_ALIAS, None)
    if alias and not credentials.get(VERTEX_API_KEY_ENV):
        credentials[VERTEX_API_KEY_ENV] = alias
    if not (
        credentials.get(VERTEX_API_KEY_ENV)
        or credentials.get(GOOGLE_CREDENTIALS_PATH_ENV)
    ):
        raise RuntimeError(
            "no google-vertex credential in the agent env (expected "
            f"{VERTEX_API_KEY_ENV}, {VERTEX_API_KEY_ALIAS} or "
            f"{GOOGLE_CREDENTIALS_PATH_ENV})"
        )
    missing = [
        name
        for name in (VERTEX_PROJECT_ENV, VERTEX_LOCATION_ENV)
        if not credentials.get(name)
    ]
    if missing:
        raise RuntimeError(
            "google-vertex needs " + " and ".join(missing) + " in the agent env"
        )
    return credentials


def google_credentials_path(declared: Mapping[str, str]) -> str | None:
    """The host path of a declared Google service-account file, if any."""

    return declared.get(GOOGLE_CREDENTIALS_PATH_ENV) or None


@dataclass(frozen=True)
class LongWakePolicy:
    """How the long-wake extension behaves inside the budget the coordinator set.

    Every field is exported as a ``SWECRAFT_*`` variable (see ``environment``);
    the extension's header documents the behaviour behind each one.
    """

    # Consecutive runs that ended without a tool call before finalizing: a
    # model that only talks must not burn the budget on empty turns.
    idle_continuations: int = 20
    # A run ended by a provider error pi retries itself is pi's to resume; one
    # pi gives up on is resumed after ``error_delay_seconds``, and after
    # ``error_runs`` such runs in a row the wake ends as ``blocked``.
    error_runs: int = 60
    error_delay_seconds: int = 60
    # A budget-used line steered in every N turns; 0 disables.
    progress_every: int = 100
    # A model call with no response for this many minutes is a call that will
    # never return (a Codex call twice hung like this); 0 disables.
    hang_minutes: int = 20
    # How many such calls are aborted and the run resumed, session intact,
    # before a hang ends the wake as blocked; 0 ends it on the first.
    hang_aborts: int = 0
    # Budget forcing: an envelope written before the finalize notice is set
    # aside and the model sent back to work, this many times at most; the
    # last envelope wins. 0 lets the first envelope end the wake. One long
    # session of ours ended itself at $14 of its $103.
    force_continues: int = 0
    # A plan or quota refusal is waited out for up to this many seconds
    # (polled every five minutes) before it ends the wake; 0 refuses at once,
    # which is right for short wakes since the coordinator then backs off.
    # For sessions meant to last days, where a usage burst must not cost the
    # session. A login the provider no longer accepts is never waited out.
    limit_wait_seconds: int = 0
    # Context policy in tokens, measured on the context the provider reports.
    # The defaults use the longest context the model has: ``soft`` 0 leaves
    # pi's compaction at its own threshold, the window minus its reserve, and
    # a positive value pulls that threshold in. ``hard`` is where the
    # wake is finalized as a last resort: 0 derives it from the window (see
    # ``resolved``), a positive value sets it outright.
    context_soft_tokens: int = 0
    context_hard_tokens: int = 0
    # Turns the model gets after the hard stop to write its final files
    # before the harness writes the envelope itself. The notice asks for the
    # envelope and the response first; a model that reconciles its notes
    # edit by edit before them used six turns on notes alone.
    final_turns: int = 10

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{item.name} must be a non-negative integer")
        if (
            self.context_soft_tokens
            and self.context_hard_tokens
            and self.context_hard_tokens <= self.context_soft_tokens
        ):
            raise ValueError("context_hard_tokens must exceed context_soft_tokens")
        if self.final_turns < 1:
            raise ValueError("final_turns must be at least 1")

    def resolved(self, context_window: int) -> LongWakePolicy:
        """The policy with the window-derived default filled in."""

        if self.context_hard_tokens or context_window <= HARD_STOP_HEADROOM:
            return self
        return replace(self, context_hard_tokens=context_window - HARD_STOP_HEADROOM)

    def environment(self) -> dict[str, str]:
        return {
            "SWECRAFT_IDLE_CONTINUATIONS": str(self.idle_continuations),
            "SWECRAFT_ERROR_RUNS": str(self.error_runs),
            "SWECRAFT_ERROR_DELAY_S": str(self.error_delay_seconds),
            "SWECRAFT_PROGRESS_EVERY": str(self.progress_every),
            "SWECRAFT_HANG_MINUTES": str(self.hang_minutes),
            "SWECRAFT_HANG_ABORTS": str(self.hang_aborts),
            "SWECRAFT_FORCE_CONTINUES": str(self.force_continues),
            "SWECRAFT_LIMIT_WAIT_S": str(self.limit_wait_seconds),
            "SWECRAFT_CONTEXT_SOFT_TOKENS": str(self.context_soft_tokens),
            "SWECRAFT_CONTEXT_HARD_TOKENS": str(self.context_hard_tokens),
            "SWECRAFT_FINAL_TURNS": str(self.final_turns),
        }


def policy_from_kwargs(kwargs: dict[str, object]) -> LongWakePolicy:
    """Pop the policy's fields out of Harbor agent kwargs (YAML values)."""

    values: dict[str, int] = {}
    for item in fields(LongWakePolicy):
        if item.name not in kwargs:
            continue
        raw = kwargs.pop(item.name)
        if isinstance(raw, bool) or not isinstance(raw, int | str):
            raise ValueError(f"{item.name} must be an integer")
        try:
            values[item.name] = int(raw)
        except ValueError:
            raise ValueError(f"{item.name} must be an integer") from None
    return LongWakePolicy(**values)


def context_window_for(model: str) -> int | None:
    """The model's context window as pi knows it, when SWECraft has it on file."""

    return KNOWN_CONTEXT_WINDOWS.get(model)


def codex_snapshot_path(declared: Mapping[str, str]) -> str:
    """Where the codex CLI auth snapshot is, from the declared agent env only."""

    path = declared.get(CODEX_AUTH_PATH_ENV)
    if not path:
        raise RuntimeError(
            f"no Codex login in the agent env (expected {CODEX_AUTH_PATH_ENV}, "
            "the codex CLI auth.json snapshot to use)"
        )
    return path


def _jwt_payload(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("access_token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("access_token payload is not decodable") from error
    if not isinstance(decoded, dict):
        raise ValueError("access_token payload is not an object")
    return decoded


def codex_credential(snapshot: Mapping[str, object]) -> dict[str, object]:
    """pi's ``openai-codex`` credential from a codex CLI ``auth.json`` snapshot.

    The codex CLI keeps the OAuth tokens under ``tokens``; pi keeps them as
    an ``oauth`` credential with the access token's expiry in epoch
    milliseconds and the ChatGPT account id, both of which pi reads from
    the access token's claims at login. The same refresh token serves both
    CLIs, with the caveat the codex arms already live with: it is single
    use, so a snapshot is good for as long as its access token lasts unless
    exactly one client refreshes it.
    """

    tokens = snapshot.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("codex auth.json has no tokens")
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        raise ValueError("codex auth.json has no access_token")
    if not isinstance(refresh, str) or not refresh:
        raise ValueError("codex auth.json has no refresh_token")
    payload = _jwt_payload(access)
    expiry = payload.get("exp")
    if not isinstance(expiry, int | float) or isinstance(expiry, bool):
        raise ValueError("access_token has no exp claim")
    claim = payload.get(_JWT_AUTH_CLAIM)
    account = claim.get("chatgpt_account_id") if isinstance(claim, dict) else None
    if not isinstance(account, str) or not account:
        account = tokens.get("account_id")
    if not isinstance(account, str) or not account:
        raise ValueError("codex auth.json has no account id")
    return {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": int(expiry * 1000),
        "accountId": account,
    }


def codex_credential_expired(credential: Mapping[str, object]) -> bool:
    expires = credential.get("expires")
    return isinstance(expires, int) and expires <= int(time.time() * 1000)


def pi_auth_file(credential: Mapping[str, object]) -> bytes:
    """pi's ``auth.json`` holding one ``openai-codex`` credential."""

    return (json.dumps({CODEX_PROVIDER: dict(credential)}, indent=2) + "\n").encode(
        "utf-8"
    )


def pi_settings(
    *, context_window: int, policy: LongWakePolicy, context: str = "native-summary"
) -> dict[str, object]:
    """pi's global settings for a run: quiet, trust nothing, retry outages.

    pi's automatic compaction is the CLI's own context management, the
    counterpart of what claude-code and codex do by default. Under
    ``native-summary`` it stays on: pi compacts once the context exceeds the model
    window minus its reserve, so by default a long wake uses the whole
    window and compacts ``MIN_COMPACTION_RESERVE`` below its edge; a
    positive ``context_soft_tokens`` pulls that threshold in by raising the
    reserve. (An extension-triggered compaction would abort the running
    turn first; the threshold path keeps the turn.)
    """

    if context not in CONTEXT_MODES:
        raise ValueError("context must be one of: " + ", ".join(CONTEXT_MODES))
    reserve = MIN_COMPACTION_RESERVE
    if policy.context_soft_tokens and context_window > policy.context_soft_tokens:
        reserve = max(reserve, context_window - policy.context_soft_tokens)
    return {
        "quietStartup": True,
        "defaultProjectTrust": "never",
        "compaction": {
            "enabled": True,
            "reserveTokens": reserve,
            "keepRecentTokens": 20000,
        },
        "retry": {"enabled": True, "maxRetries": 6, "baseDelayMs": 20000},
    }


def shipped_files() -> dict[str, bytes]:
    """The extension sources that travel with the runner package."""

    package = resources.files(__name__)
    return {name: (package / name).read_bytes() for name in EXTENSIONS}


def extension_paths(context: str = "native-summary") -> tuple[str, ...]:
    """Extensions a run loads: the shipped ones."""

    if context not in CONTEXT_MODES:
        raise ValueError("context must be one of: " + ", ".join(CONTEXT_MODES))
    return tuple(f"{HARNESS_DIR}/{name}" for name in EXTENSIONS)


def install_command(
    *,
    context_window: int,
    policy: LongWakePolicy,
    context: str = "native-summary",
    auth_file: str | None = None,
    provider: str = "",
    base_url: str = "",
    credentials: Mapping[str, str] | None = None,
) -> str:
    """Shell that writes the extensions and pi's settings into the sandbox.

    Runs once per trial before the run, as the agent user. The files are
    embedded base64 so no quoting rule of the sandbox's shell can alter them;
    pi reads its settings from ``$HOME/.pi/agent/settings.json``. A
    credential file is never embedded: ``auth_file`` names one the agent
    uploaded separately, and only the copy into pi's directory happens here. When a
    ``base_url`` is given (a proxy or gateway in front of the provider), pi's
    ``models.json`` is written into the same directory.
    """

    files = dict(shipped_files())
    settings = pi_settings(
        context_window=context_window, policy=policy, context=context
    )
    files["settings.json"] = (json.dumps(settings, indent=2) + "\n").encode("utf-8")
    if base_url:
        models = models_json(provider, base_url, credentials)
        files["models.json"] = (json.dumps(models, indent=2) + "\n").encode("utf-8")
    parts = [
        "command -v pi >/dev/null"
        " || { echo 'pi is not installed in the image' >&2; exit 1; }",
        f'mkdir -p {HARNESS_DIR} "$HOME/.pi/agent"',
    ]
    for name, data in files.items():
        encoded = base64.b64encode(data).decode("ascii")
        target = shlex.quote(f"{HARNESS_DIR}/{name}")
        parts.append(f"printf '%s' {encoded} | base64 -d > {target}")
    parts.append(f'cp {HARNESS_DIR}/settings.json "$HOME/.pi/agent/settings.json"')
    if auth_file is not None:
        parts.append(
            f'cp {shlex.quote(auth_file)} "$HOME/.pi/agent/auth.json"'
            ' && chmod 600 "$HOME/.pi/agent/auth.json"'
        )
    if base_url:
        parts.append(f'cp {HARNESS_DIR}/models.json "$HOME/.pi/agent/models.json"')
    return " && ".join(parts)


def run_command(
    *,
    provider: str,
    model: str,
    thinking: str,
    instruction: str,
    context: str = "native-summary",
) -> str:
    """The pi invocation for one run, as Harbor's stock agent shapes it.

    ``--print --mode json`` streams events to ``/logs/agent/pi.txt`` for
    Harbor's usage parsing; ``--no-extensions -e ...`` loads exactly the
    shipped extensions and nothing the image happens to carry.
    """

    if thinking not in THINKING_LEVELS:
        raise ValueError("thinking must be one of: " + ", ".join(THINKING_LEVELS))
    flags = " ".join(f"-e {shlex.quote(path)}" for path in extension_paths(context))
    return (
        f"mkdir -p {SESSION_DIR} && cd /app && "
        "pi --print --mode json --no-extensions --no-prompt-templates --no-themes "
        f"{flags} --session-dir {SESSION_DIR} "
        f"--provider {shlex.quote(provider)} --model {shlex.quote(model)} "
        f"--thinking {shlex.quote(thinking)} {shlex.quote(instruction)} "
        '2>&1 </dev/null | grep --line-buffered -v \'"type":"message_update"\' '
        f"| stdbuf -oL tee /logs/agent/{OUTPUT_FILENAME}"
    )


def agent_environment(
    *,
    credentials: dict[str, str],
    bash_timeout: int,
    bash_timeout_max: int = 0,
    policy: LongWakePolicy,
    wake: dict[str, str],
    context: str = "native-summary",
) -> dict[str, str]:
    """Environment of the pi process.

    Credentials, the bash timeout, the long-wake policy, and the wake mode
    and budget the coordinator stated (``wake``: the ``SWECRAFT_WAKE_*`` values
    from the agent env, forwarded as they are). No update checks.
    """

    return {
        **credentials,
        **{
            name: value
            for name, value in wake.items()
            if name.startswith(WAKE_ENV_PREFIX)
        },
        **policy.environment(),
        BASH_TIMEOUT_ENV: str(bash_timeout),
        BASH_TIMEOUT_MAX_ENV: str(bash_timeout_max),
        "SWECRAFT_RESULT_PATH": RESULT_PATH,
        # No release checks or package fetches from inside a run.
        "PI_OFFLINE": "1",
    }
