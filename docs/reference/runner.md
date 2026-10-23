# One-shot runner

The runner checks out one Git repository at an exact resolved commit, gives it
and a caller-provided prompt to one stock Harbor coding agent, and returns the
agent response plus a changed-file bundle. It does not modify or push the source
repository.

## Prerequisites

- Python 3.12
- `uv`
- Git
- Docker
- a model-provider credential

Harbor is invoked through the runner's own launcher by default
(`uv run --project services/runner --with harbor==0.22.0 swecraft-harbor`, see
[Harbor invocation](#harbor-invocation)), so no separate Harbor installation
is required.

## Run a task

Use either `--prompt` or `--prompt-file`:

```bash
uv run --package swecraft-runner swecraft-runner run \
  --repo http://127.0.0.1:3400/swecraft/demo.git \
  --ref main \
  --prompt "Inspect the repository and improve its README." \
  --model anthropic/claude-sonnet-4-6 \
  --agent-env ANTHROPIC_API_KEY
```

```bash
uv run --package swecraft-runner swecraft-runner run \
  --repo ./path/to/repository \
  --ref HEAD \
  --prompt-file prompt.md \
  --model anthropic/claude-sonnet-4-6 \
  --agent-env ANTHROPIC_API_KEY
```

`--agent-env` takes an environment-variable name, not its value. The variable
must already be present in the environment. Values are passed to Harbor through
a `0600` config file rather than command-line arguments, omitted from run
receipts, redacted from captured Harbor output, and scrubbed from the metadata
Harbor persists under `harbor/jobs/`.

The default Harbor agent is `claude-code`. To run Codex instead, pass
`--agent codex` and expose the auth variable that matches your Codex login
mode. For API-key auth, Harbor's Codex agent reads `OPENAI_API_KEY`:

```bash
uv run --package swecraft-runner swecraft-runner run \
  --repo ./path/to/repository \
  --ref HEAD \
  --prompt-file prompt.md \
  --agent codex \
  --model gpt-5.6-sol \
  --agent-env OPENAI_API_KEY
```

For ChatGPT login auth, point `CODEX_AUTH_JSON_PATH` at a host-side
`auth.json` created by `codex login` or by piping an enterprise Codex access
token into `codex login --with-access-token`. If your local config stores
credentials in a keyring, set `cli_auth_credentials_store = "file"` in
`~/.codex/config.toml` before creating the login file:

```bash
export CODEX_AUTH_JSON_PATH="$HOME/.codex/auth.json"
uv run --package swecraft-runner swecraft-runner run \
  --repo ./path/to/repository \
  --ref HEAD \
  --prompt-file prompt.md \
  --agent codex \
  --model gpt-5.6-sol \
  --agent-env CODEX_AUTH_JSON_PATH
```

Harbor uploads the auth file into a private container `CODEX_HOME` for each
run, so concurrent Codex runs do not share writable local Codex state. Token
refreshes inside a run do not update the host auth file; refresh the host
login periodically with `codex login` or the access-token flow.

## Cloudflare Kimi CLI

Harbor's model-native `kimi-cli` agent can use Cloudflare-hosted
[Kimi K2.7 Code](https://developers.cloudflare.com/workers-ai/models/kimi-k2.7-code/)
through its OpenAI-compatible Chat Completions endpoint. Prefix Cloudflare's
model ID with `openai/` so Kimi CLI selects its legacy OpenAI transport, then
pass the non-secret endpoint as Harbor's `base_url` constructor option:

```bash
export OPENAI_API_KEY="$CLOUDFLARE_API_TOKEN"
export CLOUDFLARE_OPENAI_BASE_URL="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1"

uv run --package swecraft-runner swecraft-runner run \
  --repo ./path/to/repository \
  --ref HEAD \
  --prompt-file prompt.md \
  --agent kimi-cli \
  --model 'openai/@cf/moonshotai/kimi-k2.7-code' \
  --agent-env OPENAI_API_KEY \
  --agent-kwarg "base_url=$CLOUDFLARE_OPENAI_BASE_URL" \
  --input-cost-per-million 0.95 \
  --cached-input-cost-per-million 0.19 \
  --output-cost-per-million 4.00
```

`--agent-kwarg` values are persisted in Harbor and SWECraft receipts, so they must
not contain credentials; pass secrets through `--agent-env`. The three pricing
flags are optional fallback accounting. Kimi CLI reports input/cache/output
counters but no dollar cost, so the runner uses these rates when configured;
a cost reported by Harbor always wins. Record current provider rates rather
than assuming the example remains current.

This integration currently covers Cloudflare-hosted Kimi K2.7 Code only. Its
file tools, function calling, artifact collection, and Harbor trajectory were
verified end to end with the pinned Harbor runner.

`--timeout-seconds` bounds agent execution and is enforced by Harbor inside the
trial, so the container is torn down on timeout; the runner process itself
allows a fixed extra margin for image pulls and artifact collection.

`--ref` accepts a branch, tag, or commit SHA. Fetching an exact SHA requires the
Git server to allow SHA fetches (`uploadpack.allowReachableSHA1InWant`); use a
branch or tag when the server does not.

The checkout fetches **full history by default** so the agent can use
`git diff`, `git log`, and `git blame`; pass `--fetch-depth N` for a shallow
fetch when history is not worth the copy cost. `--extra-ref <branch>`
(repeatable) fetches additional branches, addressable in the container as
`origin/<branch>` — used by PR-targeted routes to make the base branch
diffable. Resolved SHAs of the checkout and every extra ref are recorded in
the run receipt.

Run the command help for the complete option list:

```bash
just runner-help
# or: uv run --package swecraft-runner swecraft-runner run --help
```

Run these commands from the repository root. `uv run --project services/runner`
and package-local `uv run` remain compatibility paths because `services/runner`
is a member of the root workspace.

## Antigravity CLI

Harbor's `antigravity-cli` agent runs Google's Antigravity CLI (`agy`), the
CLI that knows the current Gemini models (Gemini CLI does not: asked for
`gemini-3.8-flash` it silently runs `gemini-3.5-flash`). The model is named
with a provider prefix Harbor strips (`google/gemini-3.8-flash`); the
`reasoning_effort` kwarg (`low`, `medium`, `high`) is required for the 3.x
flash models, since the CLI refuses a model selection without one rather
than substituting. Credentials go through `--agent-env`, in either of the
CLI's two headless forms:

- **Gemini API key**: `GEMINI_API_KEY`; Harbor writes the matching model
  provider into the CLI's settings. Works with the stock agent, but select
  `SwecraftAntigravityCli` here too for token counters (traces and cost, below).
- **Vertex AI with a service account** (the enterprise path): `AGY_ADC_AUTH=true`
  with `GOOGLE_APPLICATION_CREDENTIALS` naming the key file on the host,
  plus `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` and
  `ANTIGRAVITY_PROJECT_ID`. Harbor's stock agent expects the credentials to
  be inside the container already, so this form runs through
  `--agent-import-path swecraft_runner.harbor_agents:SwecraftAntigravityCli`, which
  uploads the file once per trial (Harbor's `upload_file`, so the key never
  passes through a command line, an environment variable value, or a log),
  points the declared variable at the sandbox copy and sets the ADC switch.
  Google limits the ADC path to Gemini 3 Flash and newer models, and it goes
  through the Gemini Enterprise Agent Platform rather than the plain Vertex
  endpoint pi uses, so confirm its billing basis in the console at a cell's
  first cost check.

```bash
uv run --package swecraft-runner swecraft-runner run \
  --repo ./path/to/repository \
  --ref HEAD \
  --prompt-file prompt.md \
  --agent antigravity-cli \
  --agent-import-path swecraft_runner.harbor_agents:SwecraftAntigravityCli \
  --model google/gemini-3.8-flash \
  --agent-kwarg reasoning_effort=medium \
  --agent-env AGY_ADC_AUTH \
  --agent-env GOOGLE_APPLICATION_CREDENTIALS \
  --agent-env GOOGLE_CLOUD_PROJECT \
  --agent-env GOOGLE_CLOUD_LOCATION \
  --agent-env ANTIGRAVITY_PROJECT_ID
```

**Pinning.** The CLI's installer takes no version and Harbor's stock agent
runs it at every trial, so a run would otherwise take whatever build the
release server serves that day. The `agent-antigravity` profile image (see
[runner-image](runner-image.md#profile-images)) bakes the binary, and
`SwecraftAntigravityCli` skips the installer when the binary is present; Harbor
records the version it finds (`agy --version`) in the trajectory.

**Traces and cost.** The CLI's stdout lands in `agent/antigravity-cli.txt`
and Harbor copies its step transcript (`USER_INPUT` and `PLANNER_RESPONSE`
records with thinking and tool calls) to `agent/antigravity-cli.trajectory.jsonl`
and converts it to an ATIF trajectory. That transcript carries no token
counters (agy 1.2.6, either credential form), so Harbor's stock agent books
nothing for a run: zero tokens, zero dollars. The CLI's run summary does
carry them, so `SwecraftAntigravityCli` asks for the JSON summary on stdout
(`--output-format json`; the `output_format` kwarg overrides it) and reads
the counters back from `antigravity-cli.txt` after the run. The CLI counts
a prompt as the tokens it sent plus the tokens served from cache, two
disjoint numbers; the class maps them onto Harbor's whole-prompt convention
(`n_input_tokens` is the whole prompt, `n_cache_tokens` its cached part) and
prices them from LiteLLM's table, `model_pricing` being the fallback for a
model that table does not know. A run the trial timeout cuts before the CLI
prints its summary keeps unknown counters rather than zero. Checkpoints keep
the stdout and the transcript. The transcript does not name the model that
answered; the CLI validates the selection instead of substituting, which is
what makes the arm honest.

## Tools

The stock `claude-code` Harbor agent already provides file reading, search,
editing, and shell execution.

Additional trusted scripts or reference files can be mounted at `/app/tools`:

```bash
uv run --package swecraft-runner swecraft-runner run \
  --repo ./repository \
  --prompt-file prompt.md \
  --model anthropic/claude-sonnet-4-6 \
  --agent-env ANTHROPIC_API_KEY \
  --tool-dir ./tools
```

Use `--image` when tools need system packages or language runtimes installed in
the task image.

## Disposable Docker authority

A specialized turn may request a digest-pinned Docker-in-Docker daemon:

```bash
uv run --package swecraft-runner swecraft-runner run \
  --repo ./repository \
  --ref HEAD \
  --prompt-file prompt.md \
  --model gpt-5.6-sol \
  --agent codex \
  --image <a profile image with your tooling> \
  --docker-daemon-image 'docker:29.1.3-dind-rootless@sha256:...'
```

Harbor starts the official rootless DinD image as a privileged sidecar on that
trial's private Compose network and puts `DOCKER_HOST=tcp://dind:2375` only in
the main agent container. (The official rootless image still requires outer
privilege to set up its user namespace.) The daemon itself runs as UID 1000 and
reports Docker's `rootless` security option. No daemon port is published and no
host Docker socket is mounted. The main container is not privileged. Harbor
teardown removes the sidecar's nested containers, images, and named storage
volume after the turn.

This confines Docker *authority* to a fresh rootless nested daemon, but its
privileged outer sidecar still shares the host kernel and is not VM isolation. Workload
containers whose execution must be offline need their own `--network none`;
the control connection to DinD and image builds necessarily use the private
Compose network.

## Execution contract

For each invocation, the runner:

1. fetches the requested ref (full history unless a depth is given) into a
   detached checkout, plus any extra refs as `origin/<branch>`;
2. records the exact resolved commit SHAs;
3. copies the checkout into one Harbor trial;
4. asks the agent to work under `/app/repository` and write
   `/app/response.md`; when disposable Docker was requested, a separate
   compiled repository copy is mounted there beside the private DinD service;
5. collects both the response and post-turn repository;
6. compares the post-turn tree with the untouched input tree.

Exactly one completed Harbor trial is required. Missing receipts, failed trials,
wrong Harbor versions, and missing artifacts fail the run.

With `structured_output` enabled on the request (used by coordinator routes),
the agent may additionally write exactly one JSON object to `/app/result.json`.
A missing structured result is not an error — callers apply their own
fallbacks — but a present, malformed one fails the run.

Claude-code runs collect the agent's session transcripts (the `*.jsonl`
conversation files) into `output/session/`, exposed on the result as a
potential `resume_bundle` for a later run. With `resume_bundle` set to such a
directory, the prior transcripts are staged at `/app/prior-session` and a
resuming agent subclass (`swecraft_runner.harbor_agents:ResumingClaudeCode`, loaded
through Harbor's `import_path`) copies them into the CLI's config directory
and invokes `claude --continue`: the new turn starts with the prior
conversation natively loaded. Harbor's own resume only spans steps inside one
trial, which is why the subclass exists. The default Harbor invocation runs
from the runner's project, so the subclass is importable inside Harbor's
process ([Harbor invocation](#harbor-invocation)); an explicit
`harbor_command` has to provide that itself. Session resume is
claude-code only. Transcripts can quote anything the prior turn saw, so treat
`output/session/` with the same secrecy as the rest of the run directory.

With `allow_remote_writes` enabled (library-only; used by the
[coordinator](coordinator.md) for autonomous wakes), the contract changes:
the checkout's `origin` is rewritten to the caller-supplied
container-reachable `push_remote_url`, git authenticates inside the
container through environment-provided config scoped to that origin
(`http.<origin>/.extraheader`, carrying the token named by
`git_auth_token_env`), commits are authored as the supplied identity, and
the compiled prompt permits pushing instead of forbidding it. All four
direct-action fields are required together, the token variable must be one
of `agent_env_names`, and standard runs keep the strict no-push contract.

With `work_repositories` set (library-only; the coordinator uses it for an
agent that maintains repositories besides its home), each entry is
checked out like the main repository and staged as a real git checkout at
`/app/repos/<name>`, its resolved commit recorded in the receipt
(`work_repositories`). Nothing is collected from them: in a direct-action
run each has its `origin` pointed at its own push remote, which must share
the origin of `push_remote_url` because git's authentication is scoped to
that origin, so work there reaches the forge only by being pushed; without
direct action, changes there are discarded, and the compiled prompt says
so either way. `fetch_auth_token_env` names a declared variable whose value
authenticates the runner's own host-side fetches (the repository, the
memory branch, the work repositories) as an `Authorization: token` header
scoped to each URL's origin, carried in the git subprocesses' environment
only; a private repository needs it, and it is independent of
`allow_remote_writes`.

With `memory_repository` set (used by coordinator routes for the durable
`agent/memory` branch), a second git checkout is mounted at `/app/memory`.
Post-turn, the agent's final memory worktree is exposed on the result;
`memory_source_sha` records its trusted starting point, and the receipt lists
paths whose final contents differ from the untouched input tree. Change
detection and publishers both ignore the collected `.git`, which is untrusted
sandbox output; callers publish the final tree from a copy using fresh trusted
Git metadata, with a non-fast-forward rejection providing the compare-and-swap.
The memory source checkout, like the repository checkout, is never modified.

## Harbor invocation

The runner drives Harbor as a subprocess. By default that subprocess is
`uv run --project services/runner --with harbor==0.22.0 swecraft-harbor`: the
pinned Harbor's own CLI behind the runner's `swecraft-harbor` entry point
(`swecraft_runner.harbor_cli`), run from the runner's project. Two things follow
from that shape, and they are why plain `uvx harbor` is not the default:

- **The scrub switch.** Since 0.20.0 Harbor scrubs the trial directory when
  a trial ends: it takes the value of every declared environment variable
  whose *name* looks sensitive (`KEY`, `SECRET`, `TOKEN`, `PASSWORD`,
  `CREDENTIAL`, `AUTH`) and replaces it wherever it appears in any text file
  there, collected artifacts included, with no regard for what the value
  is. A flag such as `CLAUDE_FORCE_OAUTH=1` or `AGY_ADC_AUTH=true` therefore
  erased every `1` or `true` in `result.json` and the trajectory, and the
  runner failed the run on invalid JSON. Worse, a direct-action wake passes
  its git configuration through git's numbered environment pairs, and
  `GIT_CONFIG_KEY_<n>` holds a configuration *key* (`user.name`,
  `user.email`, the scoped `http.<forge>/.extraheader`), so the scrub
  rewrote every `user.name` and `user.email` in the collected repository,
  which the runner then committed as the agent's work. Harbor has no way to
  turn the pass off, so `swecraft-harbor` does: it is **off by default**, and
  `SWECRAFT_HARBOR_SCRUB=on`, read from the environment Harbor's process inherits
  (the coordinator host's, or a site launcher's), restores the stock
  behaviour; any other value stops the launcher at start. With the pass
  off, the runner's own redaction still applies -- declared values of eight
  characters or more are redacted from receipts and captured output, as
  before -- so what is lost is only Harbor's extra pass over collected
  artifacts (a credential value an agent wrote into its own output).
- **Agent classes.** `swecraft_runner` is importable inside Harbor's process, so
  the classes in `swecraft_runner.harbor_agents` load by import path and session
  resume needs no second command shape.

`harbor_command` (`swecraft-runner run --harbor-command`) replaces the default verbatim,
for a site that runs Harbor from its own environment or with its own
patches. Such a launcher is then responsible for both points: make
`swecraft_runner` importable, call `swecraft_runner.harbor_cli.apply_process_fixes()`
before handing over to `harbor.cli.main.app`, and answer `--version` with
the pinned version, which the runner checks before every run. A launcher
that loads a class from `swecraft_runner.harbor_agents` gets the fixes on import
as well.

## Agent classes

Harbor runs a *stock* agent by name (`claude-code`, `codex`, `kimi-cli`, …).
The runner can also run an agent *class* shipped in `swecraft_runner`, selected
by `agent_import_path` (`module.path:ClassName`, `swecraft-runner run
--agent-import-path`); the stock name in `agent` then only labels the run.
Such a class must be importable inside Harbor's process, which the default
invocation provides ([Harbor invocation](#harbor-invocation)); an explicit
`harbor_command` has to provide it itself. Three classes ship today:
`ResumingClaudeCode`, which
the runner selects itself for session resume, `SwecraftPi`, and
`SwecraftAntigravityCli` ([Antigravity CLI](#antigravity-cli) with a
service-account file).

### pi

`swecraft_runner.harbor_agents:SwecraftPi` runs [pi](https://github.com/earendil-works/pi)
pinned in the task image (see [runner-image](runner-image.md#profile-images)):
once per trial it writes SWECraft's pi extensions and settings into the
sandbox, then runs `pi --print --mode json` with exactly those extensions
loaded. A harness change is therefore a runner change, never an image
rebuild. (Harbor's stock pi agent is not used: it installs the CLI from the
network on every trial, under a package name pi has since left behind, and
loads whatever extensions the sandbox happens to carry.)

- **Kwargs** (`agent_kwargs`): `thinking`, pi's thinking level (`medium`);
  `bash_timeout`, a default for bash calls the model issues without one
  (900 s; 0 disables), and `bash_timeout_max`, a ceiling the model's own
  timeout is clamped to (0: none — set it for long and giant wakes, whose
  idle sleeps otherwise grow to hours in which no notice reaches the model),
  both applied by the shipped `bash-timeout.ts` extension
  since pi's bash tool has no default.
- **Credentials** come from the declared agent env only, never from the
  host's environment, by provider: `ANTHROPIC_API_KEY` or
  `ANTHROPIC_OAUTH_TOKEN` for `anthropic/...` models, `GEMINI_API_KEY`
  for `google/...`, and for `google-vertex/...` the set described under
  [Google and Vertex AI](#google-and-vertex-ai).
- **Usage and cost** come from pi's own event stream
  (`/logs/agent/pi.txt`), like Harbor's stock agent.
- **Transcript**: pi writes no `trajectory.json` of its own, so after each
  run the agent derives Harbor's `agent/trajectory.json` from that event
  stream (`swecraft_runner.pi.trajectory`); the run packaging projects it to
  `output/trajectory.json` and the raw `pi.txt` to `output/agent-stdout.txt`,
  as for claude-code and codex, and pi's own session record under
  `output/session/`. Runs made before this derive it on demand with
  `uv run --project services/runner python -m swecraft_runner.pi.trajectory --runs .swecraft/runs`
  (finished runs only; `--force` rewrites existing files).
- **Turn shape**: pi answers the prompt and exits once the model replies
  without a tool call, so a pi turn is a single-task turn like any other.

The pure parts of the harness — the shell pi is run with, the files shipped
into the sandbox, the environment set — live in `swecraft_runner/pi/` next to
the extension sources, so they are unit-tested without Harbor; the agent
class itself imports Harbor and only runs inside Harbor's process.

#### Context management

`context` (a kwarg) names the context management method for any pi turn,
single-task or long. `native-summary`, the default and the only one, is pi's
own compaction,
the CLI's counterpart of what claude-code and codex do by default: older
messages are summarized once the context exceeds the model window minus
pi's reserve. A short wake rarely gets there; a long wake by default uses
the longest context the model has, so with `context_window` (a kwarg; 1M
for claude-opus-5) it runs to 64k below the edge of the window before pi
condenses it, the summary keeping that room for a thinking model, and is
finalized 32k below the window only as a last resort. Two knobs pull this
in for an experiment that wants a smaller budget: `context_soft_tokens`
moves pi's threshold (by raising its reserve) and
`context_hard_tokens` sets the finalize point outright. An
extension-triggered compaction would abort the running turn first, which
is why the threshold path is used.

#### Google and Vertex AI

Gemini models run through either of pi's Google providers, in any of
Google's three credential forms:
`google/gemini-3.8-flash` is the Gemini API, credential `GEMINI_API_KEY`;
`google-vertex/gemini-3.8-flash` is Vertex AI, which needs
`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` plus either an API
key (`GOOGLE_CLOUD_API_KEY`, pi's name for it; `GOOGLE_API_KEY`, the
CLI's, is accepted and forwarded under pi's so one declaration serves
both harnesses) or Application Default Credentials from the service
account file `GOOGLE_APPLICATION_CREDENTIALS` names on the host. The
agent uploads that file into the sandbox once per trial, the way the
Codex login travels, and hands pi the sandbox path; the key never enters
a command line, an environment variable value, or a log. Both
providers know `gemini-3.8-flash` and `gemini-3.1-pro-preview` with a
1,048,576-token window, on file for the long-wake defaults; pi prices
them from its own table, so no `model_pricing` entry is needed. A
gateway in front of Vertex takes `base_url` as for any provider; with a
service-account file and no key declared, the `models.json` the runner
writes names no `apiKey`, so pi's own Vertex resolver reaches the file.

#### Codex subscription

`openai-codex/...` models run on a ChatGPT subscription the way SWECraft's
codex arms do: `CODEX_AUTH_JSON_PATH` in the agent env names the codex
CLI's `auth.json` snapshot on the host. The agent converts the snapshot into
pi's own credential for the provider (the access and refresh tokens, the
access token's expiry and the ChatGPT account id, both read from the
token's claims exactly as pi's login does) and uploads it into the sandbox
as a file, so the tokens never pass through a command line, an environment
variable, or a log; the host side is a 0600 temporary file removed once
the upload is done. The refresh token is the same single-use token the
codex CLI holds, so a snapshot is good for as long as its access token
lasts unless exactly one client refreshes it, as with the codex arms. pi
prices these models from its own API-equivalent table, so the ledger books
a Codex wake at API-equivalent dollars without a `model_pricing` entry.

`context_window` is taken from `swecraft_runner.pi.KNOWN_CONTEXT_WINDOWS` when
omitted (1M for claude-opus-5, 272k for the Codex models SWECraft runs), and
must be passed for any other model.

#### Long wakes

With `SWECRAFT_WAKE_MODE=long-wake` and a budget in the agent env
(`SWECRAFT_WAKE_TURNS`, `SWECRAFT_WAKE_SECONDS`, `SWECRAFT_WAKE_USD`; the
[coordinator](coordinator.md#long-wakes) sets these from `wake_mode` and
`wake_budget`), the shipped `long-wake.ts` extension keeps the agent working
after every answer until the first budget kind is exceeded. Near the end
(`SWECRAFT_WAKE_FINALIZE_AT`, a fraction of the budget) it steers a finalize
notice, repeated every ten turns. At the hard stop (`SWECRAFT_WAKE_HARD_STOP_AT`)
it takes over: every tool but reading, writing `/app/result.json`,
`/app/response.md` and the notes, and the `swecraft-memory` CLI the daily log
goes through is refused, the model is told to write its final files, and
after `final_turns` turns (10) without them the harness writes a `blocked`
envelope itself and lets pi exit. The wake ends at the model's next reply
without a tool call once `/app/result.json` holds a valid envelope; a file
there that is not the envelope is sent back with the schema, twice, before
the wake is let go with whatever it holds — the first long wakes wrote
status reports of their own there, mostly after a compaction had summarized
the instruction away. Writing the envelope early does not pause the budget:
the finalize and hard stop still land. With `force_continues` (0 by
default) an envelope written before the finalize notice does not end the
wake either: it is set aside, the model is told how much of the budget is
unused and sent back to work, that many times at most, and the last
envelope written wins — budget forcing, for sessions meant to spend their
budget (one long session of ours ended itself at $14 of its $103). The runner's `timeout_seconds` is a hard
kill that would lose the wake's work, so the coordinator also passes a
deadline (`SWECRAFT_WAKE_DEADLINE_SECONDS`, the timeout less a margin) that the
extension treats as a time budget of its own: the same finalize and hard
stop happen before the kill, whatever budget kinds are configured.
A provider error pi retries itself (its retry settings and classifier are
mirrored, since extensions see neither the retry nor the decision) is left
to pi; one pi gives up on is resumed after a pause, and `error_runs` such
runs in a row end the wake as blocked. A plan or quota limit ("usage
limit", "quota exceeded") or a login the provider no longer accepts (a fresh
Codex login elsewhere invalidates the token a run started with) refuses
every call until an operator acts, so it ends the wake at once: as `blocked` with the envelope when the wake had billed
work, so its notes publish; without the final files when nothing was
billed, so the runner books the wake failed (it requires the response file)
and the coordinator backs off rather than dispatching a zero-cost wake
every interval (one of our experiments churned two hundred such
rows per run in two hours when its Codex account hit its plan limit). A
quota limit clears with time, so `limit_wait_seconds` (0 by default) waits
it out first, polling every five minutes with one refused, unbilled call,
while the wake's own budget and deadline keep running — for sessions meant
to last days, where a forty-minute usage burst must not cost the session; a
refused login is never waited out, since only a new login clears it. A
model call with no response for `hang_minutes` (20) is a call that will
never return (twice a Codex call never returned and the process sat
idle until the runner's timeout): `hang_aborts` (0) such calls
are aborted and the run resumed with its session intact, and past that a
hang ends the wake as blocked with its envelope, from inside pi. Should pi
exit without the final files on any path the harness did not decide, the
envelope is written on the way out so the wake still books. A
run without tool calls counts toward an idle cap, and the context policy
finalizes the wake at `context_hard_tokens` or after two failed compactions
in a row; an overflow pi cannot compact ends it at once. Those knobs
are the `swecraft_runner.pi.LongWakePolicy` fields, passed as agent kwargs. A
`base_url` kwarg sends the provider's calls to a gateway or proxy instead of
its default endpoint (a site without direct provider access): the install
step writes pi's `models.json` with the provider's `baseUrl`, its wire API
and its credential by environment-variable name, keeping the built-in model
list and pricing, so nothing site-specific lives in the image. Cost
is pi's own per-message figure, so a dollar-budgeted wake ends at the
number the coordinator books.

Long wakes are a pi feature today: the mechanics need a harness hook that
runs after the model's answer, which pi's extension API provides. The
contract is the agent env, not pi, so a claude-code long wake would be the
same `SWECRAFT_WAKE_*` variables enforced by a claude-code `Stop` hook; codex
has no such hook and would need a wrapper loop around `codex exec`.

The model's context window is pi's own (1M for claude-opus-5); the context
policy is a budget enforced by notices and compaction, never a smaller
window: telling pi a smaller window makes it clamp the model's output to
fit, which leaves a wake that overran it unable to recover.

## Outputs

Runs are immutable directories under `.swecraft/runs/` by default:

```text
.swecraft/runs/<run-id>/
  git.log
  input/
    prompt.md
    repository/
    memory/                # only when a memory repository is supplied
    prior-session/         # staged resume transcripts, when resuming
    request.json
    tools/                 # only when supplied
  harbor/
    exec-config.json       # secrets redacted after the run
    stdout.log
    stderr.log
    jobs/
    tasks/
  output/
    response.md
    changes.json
    changes/               # added/modified regular files, when any
    agent-result.json      # structured agent output, when requested and written
    forge-calls.jsonl      # recorded forge invocations, when the agent made any
    forge-actions.jsonl    # what those invocations did, under the vocabulary
    session/               # claude-code transcripts, when collected
    result.json
```

`changes.json` records added, modified, and deleted paths. Collection honors
the repository's own ignore rules: paths git would not commit (virtualenvs,
build trees, caches) are excluded, computed with the trusted input
checkout's metadata against the collected worktree, so one in-sandbox test
run does not turn the change bundle into gigabytes of debris. Tracked files
are always reported, even when an ignore pattern matches them. Agent-created
symlinks are recorded with their target but never followed or copied into
`changes/`.
`result.json` records the source SHA, Harbor/agent/model/image, changed paths, and
reported token/cost usage. `RunResult.repository_path` points to Harbor's
collected post-turn repository for deterministic publishers; its `.git`
metadata is agent-accessible and must not be trusted for host Git operations.

The collected changes are not applied or pushed automatically.

### What the wake did to the forge

A wake's effects on the forge do not show up in `changes.json`, because they
happen over the API rather than in the checkout. Two files record them,
and the split between them is deliberate:

- **`forge-calls.jsonl`** — what the image's `fj` and `forge` wrappers saw.
  One line per invocation: `argv`, exit code, captured output (capped),
  working directory, and the acting agent. The wrappers classify nothing.
- **`forge-actions.jsonl`** — what those invocations did, one row per
  successful repository-scoped call:
  `{ts, source, actor, repo, action, metadata, cmd}`. Derived here, on the
  host, from the recording above and the vocabulary in
  `services/runner/src/swecraft_runner/forge_actions.yaml`.

Deriving on the host is what makes the vocabulary revisable. A row classified
wrongly, or an action the table did not cover yet, can be derived again from a
recording already collected; a ledger classified inside the sandbox could only
be fixed for runs that had not happened yet. It also keeps the run's secret
list on the host: `cmd` is redacted against it at collection, so no secret has
to enter the container to be scrubbed there.

Both files travel into society checkpoints, so a checkpoint can be re-derived
under a corrected table.

Reads are not recorded. The wrappers keep an `exec` fast path for a list of
read-only subcommands and capture everything else, so a subcommand nobody has
classified is recorded and mapped to `other` rather than dropped. That list is
the one thing duplicated between the shell wrapper and the table, and
`services/runner/tests/test_forge_actions.py` asserts the two agree. Anything
reaching the forge without passing a wrapper — raw `curl`, `git push` — is not
recorded: this is evidence of what the wrappers saw, not a complete audit.

Society checkpoint capture does not copy this live layout directly. Once the
Coordinator has drained wakes and holds the lifecycle lock, Runner's read-only
checkpoint exporter requires a terminal receipt, copies the small stable
artifacts, exposes trajectory/stdout/native sessions under `output/`, and
stores the complete Harbor tree as `harbor.tar.gz`. Successful live input
staging and `output/changes/` payloads are omitted from the checkpoint view;
failed wakes retain only `input/prompt.md` and `input/request.json`. Export is
assembled in checkpoint staging and never changes `.swecraft/runs/`.

## Development

```bash
just runner-check
just runner-test
```

Tests use real temporary Git repositories and a realistic Harbor test double.
A model-driven run requires a provider credential and is intentionally separate
from the default test suite.
