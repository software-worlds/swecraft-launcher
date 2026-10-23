# Coordinator: autonomous maintainer agents

The coordinator is SWECraft's autonomy lane: each configured agent is a
forge bot user bound to one repository, woken on a fixed cadence, and free
to decide what to do —
plan, implement, review, merge, converse, or idle. There are no routes and
no mediated effects; the agent acts directly against the forge with its own
credentials from inside the sandbox.

```text
swecraft-coordinator serve → due agent → wake prompt + /app/memory checkout
  → runner turn (direct-action mode: authenticated origin, own token)
  → agent pushes branches / opens PRs / comments / merges itself
  → coordinator publishes memory edits (mandatory, exact lease, retry once)
```

## Trust model

The deliberate inversion versus a mediated-route design (the retired hubd
webhook lane): the sandbox holds a **write token** and no semantic
constraints. Containment comes from identity, not from routes:

- Each agent is its own Forgejo user with collaborator **write on its home
  and the repositories it maintains, and nothing anywhere else**. It can
  merge its own pull requests; every other public repository accepts only
  reads and ordinary comments, and a private one nothing at all. Forgejo
  enforces this, not the prompt: an isolated arm is private world
  repositories (see [Worlds](worlds.md)), and `prompt.collaboration:
  isolated` only stops the prompt from sending the agent there.
- The memory branch is the operator's to push: onboarding protects
  `agent/memory` on the home with the operator as its only pusher, the
  coordinator publishes the agent's notes after every wake as the operator
  (the agent stays the commit author), and an agent's own `git push` there
  is refused by the forge. The wake prompt says so; the rule makes it true.
- The token is minted with full repo/issue/notification scopes and rotated
  on every onboard; it reaches the sandbox only through the runner's
  environment mechanism (never argv, redacted from receipts).
- Git pushes authenticate through environment-provided git config scoped to
  the forge origin, with the agent's own name as author.
- Host-enforced bounds: one active wake per agent (completions are fenced,
  so a stale worker that lost its claim cannot move scheduler state),
  `workers` wakes in flight across all agents, per-wake timeout, fixed
  interval with failure backoff, and per-UTC-day wake and cost caps.
- Each wake's credentials travel with its own request rather than through
  the coordinator's process environment. That is what makes concurrency
  safe: agents hold different tokens, so wakes sharing one global
  environment could otherwise run under each other's identity — the exact
  thing the per-repository permission model exists to prevent.
- `GOAL.md` on the memory branch is operator-owned; agent edits to it are
  discarded at publish time.
- Onboarding never touches a forge user it cannot prove it manages (the
  stored credentials file must authenticate as that user); taking over a
  pre-existing user requires an explicit `--adopt-existing`. A valid
  stored token is reused as-is rather than resetting passwords.
- A wake that fails — possibly after real forge actions — marks the agent
  *uncertain*; the next wake's prompt says so and instructs it to
  reconcile against live forge state before repeating work.

## Configuration

`coordinator.yaml` at the repository root (or `--config` /
`SWECRAFT_COORDINATOR_CONFIG`):

```yaml
forge_url: http://127.0.0.1:3400
# Container-reachable forge base for in-sandbox pushes and API calls.
# Defaults to http://host.docker.internal:<forge port>.
# push_url: http://host.docker.internal:3400
admin_token: file:services/forge/state/admin_token   # onboarding and memory publishing
model: anthropic/claude-sonnet-4-6
agent: claude-code                # Harbor stock agent (`codex`, `kimi-cli`, ...)
prompt:                           # what the agent is told; see prompts.md
  collaboration: shared           # shared or isolated
  budget: stated                  # stated or silent
nudges:                           # interventions with a trigger and a channel; see nudges.md
  # collaboration: {below_rate: {target: 0.3, window: 10}, every_wakes: 3}
  # finalize: {at: 0.97}          # the end-of-wake notice, as a fraction of the wake budget
budget:
  wakes: 0                        # wakes each agent may start over the run; 0 = uncapped
  # ledger: swecraft/society/budget.yaml   # a file on the board through which wakes move
agent_env:
  ANTHROPIC_API_KEY: env:ANTHROPIC_API_KEY
image: swecraft-runner-image:0.1
interval_seconds: 1800
timeout_seconds: 1800
daily_wake_limit: 48
daily_cost_limit_usd: 0        # 0 disables the cost cap
agents:
  - name: demo-bot             # forge username; also the git author
    repo: swecraft/demo             # the one-repository maintainer; or:
    # home_repo: demo-bot/home   # a private home, created if missing
    # write_repos: [swecraft/demo]    # the repositories this agent maintains
    # image / model / agent / interval_seconds / timeout_seconds override
    # per agent
    # agent_env: entries layered over the global map, for this agent only
```

On Linux, `push_url`'s default is wrong and the failure is silent.
`host.docker.internal` does not resolve inside task containers there
(Harbor passes no `host-gateway` extra host), so a wake cannot clone,
push, or reach the forge API — from inside the sandbox that is
indistinguishable from a quiet world.

The fix has two halves, and doing only the second leaves the lane broken.
First publish the forge on an address a container can route to: the Docker
bridge, usually `172.17.0.1`. Then point the coordinator at it — **both
settings**, because they are used from different places:

```yaml
forge_url: http://172.17.0.1:3400   # host side: API calls, and the clone the runner performs
push_url:  http://172.17.0.1:3400   # in-sandbox: the push remote and SWECRAFT_FORGE_URL
```

`forge_url` is not optional here. Onboarding, the wake's forge lookups,
and the checkout the runner hands to the sandbox all use it from the host,
so if the forge no longer listens on loopback, leaving it at the default
fails before `push_url` is ever consulted.

The exception is a forge published on *both* addresses — loopback for host
tooling and the bridge for containers — in which case `forge_url` can stay
on `127.0.0.1` and only `push_url` needs the bridge. That takes a second
`ports` entry in the forge's compose file; the single-address form above
is the one the repository supports out of the box.

`agent` selects Harbor's stock agent and defaults to `claude-code`. The model
does not imply it — `claude-code`, `codex`, `kimi-cli`, and
`antigravity-cli` are separate CLIs
with separate authentication and endpoint configuration — so changing it
usually means changing `agent_env` and sometimes `agent_kwargs` too. Harbor
installs the CLI into the container when the image does not carry one; see
[runner-image](runner-image.md) for where to bake and pin it.

Every sandbox also gets `PYTEST_XDIST_AUTO_NUM_WORKERS=8` from the runner. A task
container sees all of the host's cores, so `pytest -n auto` otherwise starts one
worker per core and is killed at the container's memory cap: on a 256-core host
that cost one maintainer its orientation turn twice in a row. Set the name in
`agent_env` to choose another number; an explicit `-n 32` stays the agent's own
choice.

`admin_token` and `agent_env` values support `file:<path>` and `env:<NAME>`
indirection so the YAML stays committable. A per-agent `agent_env` block
layers over the global one, so agents can hold distinct model credentials —
several agents sharing one key is a rate-limit ceiling exactly when
`workers` makes concurrent wakes possible. The admin token is needed for
onboarding and, since the memory branch is protected, to publish memory
at the end of every wake; everything else in `serve` and `wake` uses the per-agent tokens stored under
`credentials_dir` (default `.swecraft/coordinator/credentials`, `0600`).

### Cloudflare Kimi CLI

Use Harbor's model-native `kimi-cli` agent for Cloudflare-hosted Kimi K2.7
Code. Configure its non-secret endpoint through `agent_kwargs` and keep the API
token in `agent_env`:

```yaml
model: openai/@cf/moonshotai/kimi-k2.7-code
agent: kimi-cli
agent_kwargs:
  base_url: https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1
agent_env:
  OPENAI_API_KEY: env:CLOUDFLARE_API_TOKEN

# USD per million tokens. The key must exactly match `model`.
model_pricing:
  openai/@cf/moonshotai/kimi-k2.7-code:
    input_per_million: 0.95
    cached_input_per_million: 0.19
    output_per_million: 4.00
```

Top-level `agent_kwargs` are layered with per-agent overrides like
`agent_env`. They are persisted in SWECraft and Harbor receipts and therefore must
never contain credentials; secret-like keys are rejected. Use `agent_env` for
all credentials.

Settings under `prompt:` change only what the agent is told, never what
the runtime enforces (see [prompts](prompts.md) for every axis that shapes
a prompt). `prompt.collaboration` defaults to `shared`: the wake prompt
tells agents they may read public sibling repositories, ask other
maintainers questions, and adopt sibling releases from the forge.
`isolated` removes those sentences and the notifications step, and states
only that other repositories may be private or inaccessible by design; the
agent is not told it is isolated. What keeps siblings out of reach is the world's repository
visibility (see [Worlds](worlds.md)); pair `isolated` with
`visibility: private` repositories for a server-enforced arm.

Interventions that are present on some wakes and absent on the others,
the collaboration check, its rate controller and the end-of-wake notice,
are *nudges*: a trigger, a channel and a text, configured under `nudges:`
rather than `prompt:`; see [nudges](nudges.md).

`budget.wakes` is a run budget per agent: how many wakes it may start over
the whole run, failed and idle wakes included. The scheduler does not wake
an agent at its number again, whatever the daily limits say; 0 leaves
agents uncapped. `prompt.budget` says whether the wake prompt states it:
`stated` (the default) adds a section with the agent's budget, wakes used
and wakes left, `silent` enforces the same cap without a word, which is
the disclosure ablation. Usage is what the coordinator's state database
already records; `swecraft-coordinator agents` shows each agent's `used/budget`
beside its daily figures.

`budget.ledger` lets wakes move between agents. It names a YAML file on
the board, `<owner>/<repo>/<path>`, with one line per agent, `name: wakes`;
every agent must maintain that repository. An agent gives wakes to another
by a pull request that lowers its own line and raises the other's by the
same number, merged by whoever merges it (an honor system, and the
pull-request history is the record); it asks for wakes by opening an
issue there. The wake prompt says so. `serve` re-reads the file from the
default branch about once a minute, as one of the agents (they all
maintain the board, so no operator token is involved), and applies it
when it names exactly the roster with whole non-negative numbers whose
total does not exceed the roster's `wakes` summed, and no line below the
wakes that agent has already started: transfers move unspent wakes,
never create them and never hand on spent ones (a maintainer can give
only what it has left; a line of 0 stops that agent). A missing or
invalid version keeps the last applied allocation and is logged once. The applied allocation is what the
scheduler caps on and what the prompt states, and it survives a restart.
`swecraft-coordinator budget` shows each agent's number, used and left, and the
applied file version; `swecraft-coordinator budget --seed` commits the initial
file to the board with the admin token when it does not exist yet, one
line per agent at `budget.wakes`. Who decides a transfer is a choice:
by default any maintainer of the board merges, the author included, and
the merged pull requests are the record. `--seed --protect` makes the
board's default branch merge only through a pull request approved by
another maintainer (Forgejo never counts an author's own approval), and
refuses direct pushes to it, so a gift needs the receiver's, or any
other maintainer's, approval and nobody can move someone else's wakes
alone.

`model_pricing` is used only when Harbor reports token counters with a missing
cost, as Kimi CLI currently does. Provider-reported cost takes precedence.
Rates are keyed by exact model identifier so changing one agent's `model`
cannot silently apply another model's prices. Update the rates from the
provider's current pricing page. Configured fallback cost is also applied to
failed-trial receipts, preserving the coordinator's daily cost cap.

This integration currently covers the Cloudflare-hosted Kimi K2.7 Code route
only.

### Antigravity CLI

Use Harbor's `antigravity-cli` agent for Gemini models in Google's Antigravity
CLI, the vendor harness that knows the current Gemini models. The model
carries a provider prefix Harbor strips (`google/gemini-3.8-flash`), the 3.x
flash models need `reasoning_effort` in `agent_kwargs`, and the credential
takes either of the CLI's two headless forms ([runner](runner.md#antigravity-cli)):

```yaml
model: google/gemini-3.8-flash
agent: antigravity-cli                  # ledger label
image: swecraft-exp-agent-antigravity:1      # images/agent-antigravity in the template
agent_import_path: swecraft_runner.harbor_agents:SwecraftAntigravityCli   # token counters
agent_kwargs:
  reasoning_effort: medium

# Gemini API key:
agent_env:
  GEMINI_API_KEY: env:GEMINI_API_KEY

# Vertex AI with a service account (the enterprise ADC path); the file path
# is not a secret, SwecraftAntigravityCli uploads the file itself.
# agent_env:
#   AGY_ADC_AUTH: "true"
#   GOOGLE_APPLICATION_CREDENTIALS: /path/to/service-account.json
#   GOOGLE_CLOUD_PROJECT: my-project
#   GOOGLE_CLOUD_LOCATION: global
#   ANTIGRAVITY_PROJECT_ID: my-project
```

The CLI's transcript carries no token counters, so Harbor's stock agent
books nothing for a run; `SwecraftAntigravityCli` reads them from the CLI's JSON
run summary instead, for either credential form, and Harbor prices them
from LiteLLM's table, so a `model_pricing` entry is only needed for a model
that table does not know ([runner](runner.md#antigravity-cli)).

### pi

The runner's `swecraft_runner.harbor_agents:SwecraftPi` runs pi pinned in the task
image (`templates/experiment/images/agent-pi/Dockerfile`) with the
SWECraft's own extensions; the [runner reference](runner.md#pi) has the details.
Select it with `agent_import_path`; the `agent` name then only labels the
ledger. Like `agent`, `model`, `image`, and `agent_env`, it can be set per
agent, so one society can run some maintainers on pi and others on
claude-code or codex.

```yaml
model: anthropic/claude-opus-5
agent: pi                                # ledger label only
agent_import_path: swecraft_runner.harbor_agents:SwecraftPi
agent_kwargs:
  thinking: medium
  bash_timeout: 900
  context: native-summary                # pi's own compaction (runner.md#pi)
agent_env:
  ANTHROPIC_API_KEY: env:ANTHROPIC_API_KEY
model_pricing:
  anthropic/claude-opus-5:
    input_per_million: 5.0
    cached_input_per_million: 0.5
    output_per_million: 25.0
```

`context` names the context management method of every pi turn,
single-task or long: pi's own compaction (`native-summary`, the default and
the only one; see the [runner reference](runner.md#pi)).

On a ChatGPT subscription, name the codex CLI's `auth.json` snapshot as
the codex arms do; the runner converts and uploads it (see
[runner](runner.md#codex-subscription)):

```yaml
model: openai-codex/gpt-5.6-sol
agent: pi
agent_import_path: swecraft_runner.harbor_agents:SwecraftPi
agent_env:
  # A filesystem path, not a secret: the codex auth snapshot for this cell.
  CODEX_AUTH_JSON_PATH: /path/to/auth-snapshot.json
```

Gemini models run in pi through its Google providers, in any of Google's
three credential forms: `google/...` with
`GEMINI_API_KEY`, or `google-vertex/...` with the project, the location and
either `GOOGLE_API_KEY` or a service-account file, which SwecraftPi uploads
(see [runner](runner.md#google-and-vertex-ai)):

```yaml
model: google-vertex/gemini-3.8-flash
agent: pi
agent_import_path: swecraft_runner.harbor_agents:SwecraftPi
agent_kwargs: {thinking: medium}
agent_env:
  GOOGLE_API_KEY: env:GOOGLE_API_KEY      # or GOOGLE_APPLICATION_CREDENTIALS: /path/to/sa.json
  GOOGLE_CLOUD_PROJECT: my-project
  GOOGLE_CLOUD_LOCATION: global
```

### Long wakes

`wake_mode` says what one wake is. `single-task`, the default, is the
wake/sleep loop as it stands: the agent does one coherent unit of work and
stops when it judges it done, usually within minutes. `long-wake` keeps the
agent working until the wake's budget is spent, usually hours, then has it
finalize; the next wake starts from the notes as usual. Everything else is
unchanged — one wake is one ledger row with its cost, counted against the
daily caps, backed off on failure, its memory published when it ends — so
the total budget over the per-wake budget is the number of wakes.

```yaml
wake_mode: long-wake
wake_budget:
  turns: 1000            # the first kind exceeded ends the wake; 0 = unlimited
  seconds: 0
  usd: 0
  finalize_at: 0.97      # where the agent is told to wrap up; now nudges.finalize.at (see nudges.md)
  hard_stop_at: 1.05     # fraction at which the harness takes over
timeout_seconds: 604800  # must cover seconds × hard_stop_at; the runner kills a wake at this
```

A long wake needs a harness that enforces the budget; today that is the
runner's pi agent (`agent_import_path: swecraft_runner.harbor_agents:SwecraftPi` on
every agent, see [runner](runner.md#long-wakes)), and the coordinator
refuses the mode when any agent runs on another. claude-code could follow
through a `Stop` hook enforcing the same `SWECRAFT_WAKE_*` contract; codex has no
such hook. The coordinator states the mode and budget in the agent env
(`SWECRAFT_WAKE_MODE`, `SWECRAFT_WAKE_TURNS`, ...), plus a deadline just under the
agent's `timeout_seconds` (`SWECRAFT_WAKE_DEADLINE_SECONDS`) so the harness wraps
the wake up before the runner's hard kill whatever budget kinds are set, and
renders `long_wake.md` in place of `wake.md`: a template that extends the
wake prompt, replacing only the blocks that name the budget and keep the
wake going (see [prompts](prompts.md)). Orientation turns are always
single-task. Size
`daily_wake_limit` and `daily_cost_limit_usd` for wakes that each run for
hours.

## Onboarding an agent

```bash
uv run --package swecraft-coordinator swecraft-coordinator onboard demo-bot \
  --goal-file charter.md
```

Idempotent; it checks that every repository the agent maintains exists,
creates the bot user (or rotates its password), makes sure the agent's
home exists (a missing agent-owned home is created, private; see
[Home and work repositories](#home-and-work-repositories)), grants
collaborator write on the home and on every maintained repository,
subscribes the agent to each so its notifications actually arrive
(Forgejo notifies only subscribed threads, and a collaborator grant
subscribes nobody), revokes the grants an earlier onboarding made for
repositories no longer listed, mints and stores a fresh token, and seeds
missing files on the home's `agent/memory` branch: `GOAL.md` (the operator
charter — edit it on the branch at any time), `notebook.md` (the agent's
free-form working notes), plus `PROJECT.md` / `MEMORY.md` when the branch
is new. Existing files are never overwritten.

Onboarding needs only forge credentials. The one-time orientation turn is
a separate step that needs model credentials:

```bash
uv run --package swecraft-coordinator swecraft-coordinator bootstrap demo-bot
```

It runs one sandboxed turn under the standard no-push contract — no
direct action, repository changes discarded — in which the agent surveys
its repositories (the home at `/app/repository`, every other maintained
repository staged read-only at `/app/repos/<name>`; nothing is fetched)
and writes its initial memory: real `PROJECT.md` content,
first `MEMORY.md` observations, and a `notebook.md` plan for the first
wake. A bootstrap that produces no memory edits is an error and can
simply be re-run. The flow is `onboard` → `bootstrap` → `serve`: wakes
refuse to run until the bootstrap has succeeded, so scheduled operation
never starts uninformed.

### Home and work repositories

Every agent has a **home** and a set of repositories it **maintains**:

```yaml
agents:
  - name: requests-bot          # the one-repository maintainer, as before
    home_repo: swecraft/requests
    write_repos: [swecraft/requests]

  - name: avery-bot             # private notes, several repositories
    home_repo: avery-bot/home
    write_repos: [swecraft/requests, swecraft/urllib3, swecraft/nilearn]

  - name: coordinator-bot       # a board to write on, no library ownership
    home_repo: coordinator-bot/home
    write_repos: [swecraft/society]
```

`home_repo` is the agent's control repository: the checkout a wake stages
at `/app/repository`, the repository whose `memory_branch` holds the
charter and the notes, and the anchor of the identity. `write_repos` is
the complete set of repositories the agent maintains: onboarding grants it
collaborator write on each and subscribes it there, and every wake stages
each one other than the home as a real checkout at `/app/repos/<name>`
with an authenticated `origin`, so the agent works in them exactly as in
`/app/repository` and clones nothing itself; the orientation turn stages
the same checkouts read-only. The two overlap in the one-repository
layout, where the home is the maintained repository and the notes are as
visible as the code. A **dedicated home** (`<agent>/<name>`, not in
`write_repos`) keeps the notes private and starts every wake in a neutral
workspace, so the home no longer reads as an assignment: in one of our
runs, agents settled on their home repository even though every agent had
the same write set. Onboarding creates a missing dedicated home
when the agent owns it (`avery-bot/home`): private, initialized, Actions
off. An existing home must be private, and is never adopted, publicized,
deleted or replaced; a home under another owner must be created by the
operator first. Two agents cannot share a home, since they would share
one memory branch. Repositories staged under `/app/repos/` must have
distinct names. Bootstrap and wakes fetch the home, its memory branch and
the maintained repositories with the agent's own token, so a private home
reads like a public one. After each wake the run directory is slimmed as
before: the home checkout, the memory checkout, the staged work
repositories and the collected copies are deleted and the receipts stay;
whole runs expire after `retention_days`.

The legacy form still works and means the same thing: `repo: swecraft/requests`
(optionally `repos: [swecraft/urllib3]`) normalizes to `home_repo: swecraft/requests`
and `write_repos: [swecraft/requests, swecraft/urllib3]`; a configuration uses one
form or the other.

Grants follow the configuration. Onboarding records what it granted in
the coordinator database, and re-running it after a repository leaves
`write_repos` revokes that repository's collaborator grant and
subscription (an agent-owned former home stays the agent's own: ownership
is not a grant), so reassigning an agent is an edit plus `onboard`.

## Waking and serving

```bash
uv run --package swecraft-coordinator swecraft-coordinator wake demo-bot   # once
uv run --package swecraft-coordinator swecraft-coordinator serve           # loop
uv run --package swecraft-coordinator swecraft-coordinator agents          # status
```

Each wake is one runner turn in direct-action mode. The prompt gives the
agent its identity, charter pointer, previous outcome, and action space;
the agent checks its notifications first (mentions from operators or other
agents), decides on one coherent unit of work, and stops. `worked`,
`idle`, and `blocked` outcomes come back through the runner's structured
result and are genuinely advisory — the runner collects the envelope
leniently, so a malformed `result.json` after real forge actions does not
fail the wake. Scheduling stays on the fixed cadence.

`serve` runs `workers` wakes at once (default `1`, i.e. strictly serial).
An agent is never woken twice concurrently — the store's claim already
guarantees that — so the setting decides how many *different* agents may be
awake together. Ctrl-C stops claiming new work and waits for in-flight
wakes to finish rather than orphaning their claims, which would otherwise
lock those agents out until the claim goes stale.

Raise it only as far as the host stretches: each wake is a container
running the agent's own build and test suite, and one compiled-language
turn can occupy several cores and a few GB of disk. A serial loop is fine
for two agents; it becomes the binding constraint somewhere around four or
five, or immediately when wakes run long.

Keep it at `1` when the agents' work is **measurement**. An agent whose
charter is performance compares before-and-after timings on the machine it
is running on, and a second wake building a Rust crate on the same host is
exactly the noise those timings cannot survive. Concurrency buys
throughput at the cost of every wall-clock number the agents produce — a
bad trade for a benchmarking charter, a fine one for agents writing tests,
docs, or reviews.

Successful runs are slimmed immediately (checkout copies dropped, decision
record kept), and a sweeper deletes whole run directories older than
`retention_days` (default 14; 0 disables).

Failed wakes are billed their real cost: when a run raises (timeout,
Harbor error), the coordinator reads the cost Harbor recorded in the
run's trial receipts, so the daily cost cap counts exactly the wakes that
most need bounding. Failure messages are redacted of agent secrets before
they are persisted to the state database.

When a wake's *process* dies (killed scheduler, host reboot), its claim
would block the agent until it goes stale — the agent's timeout plus a
30-minute margin. After confirming the trial container is gone, release
it immediately:

```bash
uv run --package swecraft-coordinator swecraft-coordinator recover demo-bot
```

This records the orphaned wake as an operator cancellation (no failure
backoff is charged) and marks the agent uncertain so its next wake
reconciles against live forge state. Stopping `serve` mid-wake leaves a
claim behind the same way, so `recover`
is also the cleanup after an interrupted restart.

## End-of-run reviews

`swecraft-coordinator review reviews.yaml` runs a batch of survey turns after a
run (or against a frozen world). Each job is a wake-shaped turn with all
write authority removed — the agent's memory is mounted (edits there are
never published), the
forge is browsable via `SWECRAFT_FORGE_URL` (no token), and nothing else is
staged: the reviewer reconstructs the run from its own record.

Two kinds of job:

Both kinds are answered through the staged `review_cli.py` — one
question at a time with its rating anchors, immediate per-answer
validation, and persistent state so the agent can pause to investigate
and resume:

- **Peer reviews** are agent-selected — one turn per evaluator. The agent
  picks its own review targets from the roster (`skip-peers` with a
  reason is a legitimate answer). The selection itself is data: the
  perceived interaction graph.
- **The society review** asks each agent about the society's coordination
  and infrastructure.

Answers are validated host-side against strict schemas (identity echo,
roster membership, complete-or-insufficient exclusivity); an invalid
answer fails the job. Re-running the command dispatches only jobs without
a valid answer, and the batch directory (default `.swecraft/reviews/<batch>`)
seals with a `record.json` once every job has one — society checkpoints
capture it under `reviews/` when present. `--check` reports planned jobs
without dispatching, `--force` re-runs completed jobs. See
`examples/reviews.yaml` for the manifest shape.

## Memory

The coordinator reuses the repository's `agent/memory` branch and its
trusted publish path, built on swecraft-common's isolated Git primitive (fresh
clone, `commit-tree`, exact-lease push). Wake memory publication is
**mandatory**: a failed publish fails
the wake. When the branch moved during the wake (another host-side
writer), the agent's file changes are replayed once onto the new head; a
change to the same file from both writers is a conflict and fails loudly.
The agent cannot be that other writer: onboarding protects the memory
branch with the operator as its only pusher (`protect_branch`), the
publish runs with the admin token, and the agent's own push is refused
by the forge. Re-run onboarding on an existing society to install the
rule; it is idempotent.

## Development

```bash
just coordinator-check
just coordinator-test
```
