# Runner image

`services/runner-image` builds the Docker image agent runs execute in:
`node:22-trixie-slim` (fj needs glibc 2.39+, newer than bookworm's) plus
`git`, `curl`, `jq`, `ripgrep`, a pinned and checksum-verified
[`fj`](https://codeberg.org/forgejo-contrib/forgejo-cli) CLI (v0.6.0), and
small tools for forge access. It is the image the one-shot runner
launches — not related to `forgejo-runner`, the Actions CI runner. Building
requires BuildKit/buildx (the default in current Docker) for `TARGETARCH`.
The image carries **no agent CLI**, deliberately — see
[Profile images](#profile-images).

```bash
just runner-image-build          # tags swecraft-runner-image:0.1
just runner-image-smoke
```

Point the runner at it with `--image swecraft-runner-image:0.1`, or the
coordinator with its (per-agent) `image` setting.

## Forge access from inside a run

The tooling is zero-ceremony for the agent — the host side provisions the
credential, the image consumes it:

- `fj` is a small shim over the real binary (`fj-bin`): it injects the baked
  forge URL and self-authenticates from the per-run token on first use,
  so agents run plain `fj issue view "owner/name#7"` with no `--host` and no
  login step. The shim exists because fj has no environment-variable
  configuration today (no `FJ_HOST`/`FJ_TOKEN`; verified against v0.6.0
  source) — it should be deleted once fj grows env support or the checkout
  remote is rewritten for native host inference.
- `forge` — a curl wrapper (`forge GET /repos/<owner>/<name>/pulls/3/reviews`)
  for endpoints fj does not cover, chiefly PR reviews. The cheat sheet ships
  at `/usr/local/share/swecraft/forge-api.md`.
- `forge-react` — a helper for issue, pull request, and comment reactions. It
  discovers the live allowlist with `forge-react allowed`, validates reaction
  values before writes, and delegates API calls to `forge` so authentication
  and wake action recording stay on the same path.
- `forge-login` — manual fallback only (debugging, cleared key stores).
- `swecraft-memory` — structured access to the durable memory workspace when a
  run mounts one at `/app/memory`: `show`, `search <query>`,
  `add [--section <name>] <text>`, `replace --match <old> --with <new>`,
  `remove --match <old>`, and `daily <text>`. Keeps bullet edits in a
  consistent format; `replace`/`remove` resolve `--match` to exactly one
  bullet and refuse ambiguous or missing matches with a nonzero exit.

The image bakes `SWECRAFT_FORGE_URL=http://host.docker.internal:3400` — the forge
as reachable from inside containers. The token arrives per run through the
runner's agent-env mechanism, under one of two names the tools check in
order: `SWECRAFT_FORGE_TOKEN` (a write-capable identity token; supplied by the
[coordinator](coordinator.md) for direct-action wakes) and
`SWECRAFT_FORGE_READ_TOKEN` (read-scoped, for runs that should only read the
forge):

```bash
# Mint a read-only token (scopes cap the user's rights server-side; a
# read-scoped token on the bot user cannot write even though the user can):
docker compose -p swecraft-forge -f services/forge/compose.yaml exec -T forgejo \
  forgejo admin user generate-access-token --username demo-bot \
  --token-name runner-read --scopes read:repository,read:issue,read:user --raw

export SWECRAFT_FORGE_READ_TOKEN=<that token>
# then forward it into the run: swecraft-runner run ... --agent-env SWECRAFT_FORGE_READ_TOKEN
```

## Recording what a run did to the forge

Both wrappers write one JSON file per invocation into
`/app/.swecraft/forge-calls/`: the argument vector, the exit code, and captured
output. They classify nothing and interpret nothing. The runner collects the
directory and derives the action ledger on the host — see
[What the wake did to the forge](runner.md#what-the-wake-did-to-the-forge)
for the record shape and for why the split is there.

What this means for the image:

- Reads keep the untouched `exec` fast path. Everything else is captured, so
  an unfamiliar subcommand is recorded rather than dropped. `fj auth` is never
  recorded, because its argument is the credential.
- No redaction happens here, and no secret list is shipped in. `argv` reaches
  the host as it was, and is redacted there against the run's real secrets.
- Recording is best effort and swallows its own failures: it must never change
  what the agent saw, what the caller got back, or the exit code. A run whose
  recording could not be written is not a failed run.
- One file per call rather than appended lines, so an agent running these in
  the background cannot interleave two records. Captured output is capped at
  `SWECRAFT_FORGE_CALL_MAX_BYTES` (8 KiB per stream) — the agent still receives
  every byte.

## Trust model

- A run given `SWECRAFT_FORGE_READ_TOKEN` gains outbound access to the forge
  with a **read-scoped** token; enforcement is server-side (token scopes),
  not client-side. In the coordinator's lane the run deliberately carries
  the agent's own write-capable token; isolation there is permission-based
  (write on the agent's one repository only) — see
  [coordinator.md](coordinator.md).
- Everything the agent fetches through fj/`forge` is untrusted user data;
  the prompts say so explicitly.

## Caveats

- `host.docker.internal` resolves out of the box on macOS and Windows
  Docker; on Linux, task containers need a `host-gateway` extra host, which
  Harbor does not currently expose — revisit when the runner runs on Linux.
- Without a forge token in the run's agent environment, the image behaves
  like plain `node:22-slim` plus git: the tools exist but stay
  unauthenticated, and prompts direct the agent to the staged context only.
- Pin discipline: bump `FJ_VERSION` deliberately and recompute both
  `FJ_SHA256_*` args; fj is pre-1.0 and its CLI surface can change between
  versions.

## Profile images

This image is **agent-agnostic and toolchain-free**, and both are deliberate.

Harbor picks its agent per run and installs the CLI itself when one is
absent — its agents begin with `command -v claude` / `command -v codex` and
skip the install when the binary is already there. So an agent baked in here
is a *cache*, not a requirement, and it is a cache every lane inherits
whether or not it uses that agent. The same argument applies to language
toolchains: the stock image cannot build a Rust extension or run a suite
across six interpreters, and most repositories need neither.

Both belong in a **profile image** derived from this one — built for a
particular world, paying only for what that world uses:

```dockerfile
FROM swecraft-runner-image:0.1

# Toolchains this world's repositories need to build and test themselves.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential valgrind \
    && rm -rf /var/lib/apt/lists/*

# The agent, pinned. Harbor installs whatever npm calls latest when the CLI
# is missing, so baking is currently the only way to hold an agent version
# steady across runs — which a comparison between runs needs. Bump it
# deliberately, the way FJ_VERSION is bumped.
ARG CLAUDE_CODE_VERSION=2.1.220
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && claude --version
```

Point a run at it with the runner's `--image` or the coordinator's
per-agent `image`.

Two caveats worth knowing before choosing:

- **Not baking costs an npm install per run**, and it needs npm reachable
  from inside the sandbox. Where egress is restricted, an unbaked agent
  fails per run rather than once at build time.
- **Baking is a pin, and the absence of one is not neutral.** An unbaked
  agent means each run takes whatever the registry served that minute, and
  the run receipt does not record which — so two runs are not comparable
  after the fact. That gap is tracked separately; until it closes, a profile
  image is where an experiment holds its agent version still.

`templates/experiment/images/agent-pi/Dockerfile` is the profile
for [pi](runner.md#pi): the world's agent image plus a pinned pi. The pi
extensions and settings are not baked into it — the runner writes them into
the sandbox per trial — so the image changes only when pi is bumped.

`templates/experiment/images/agent-antigravity/Dockerfile` is the
profile for the [Antigravity CLI](runner.md#antigravity-cli): the agent
image plus the `agy` binary its installer places under `~/.local/bin`.
The installer takes no version and Harbor's stock agent runs it at every
trial, so the baked binary is the only pin a run can have:
`SwecraftAntigravityCli` skips the installer when it finds the binary, and
Harbor records the version it finds. Build the image once per experiment
and record `agy --version` in its run log.
