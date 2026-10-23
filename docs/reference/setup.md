# Setup

Configuration for the local SWECraft stack. All variables are optional — every
command works out of the box with the defaults below.

## Prerequisites

The forge scripts need Docker (with Compose), `just`, `uv`, `openssl`,
`curl`, and `python3` (used by the smoke test). Python development uses the
root `uv` workspace and lock:

```bash
uv sync --all-packages --all-groups
just python-check
just python-test
```

## Environment variables

| Variable               | Default     | Used by                                        | Description                                                                                          |
| ---------------------- | ----------- | ---------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `SWECRAFT_FORGE_PORT`       | `3400`      | `compose.yaml`, `bootstrap.sh`, `smoke.sh`     | Host port Forgejo is published on. Also sets Forgejo's `ROOT_URL`.                                    |
| `SWECRAFT_FORGE_HOST`       | `127.0.0.1` | `compose.yaml`, `bootstrap.sh`, `smoke.sh`     | Address the forge is published on, and the address the scripts use to reach it. On Linux, set this to the Docker bridge (see below) so task containers can reach the forge. |
| `SWECRAFT_FORGE_BIND`       | `SWECRAFT_FORGE_HOST` | `compose.yaml`                            | Interface the forge is published on, when it must differ from the address in `ROOT_URL`. `SWECRAFT_FORGE_BIND=0.0.0.0 just forge-up` makes the forge reachable from other machines while every advertised URL stays on `SWECRAFT_FORGE_HOST`. |
| `SWECRAFT_FORGE_ORG`        | `swecraft`  | `bootstrap.sh`, `smoke.sh`                     | Forgejo organization that bootstrapped repositories are created under.                               |
| `SWECRAFT_FORGE_REPOSITORY` | `demo`      | `smoke.sh`                                     | Repository name the smoke test checks for health and cloneability.                                   |
| `COMPOSE_PROJECT_NAME` | `swecraft-forge` | `compose.yaml`, `bootstrap.sh`, `justfile`     | Docker Compose project name. Change it (together with `SWECRAFT_FORGE_PORT`) to run an isolated instance. |
| `SWECRAFT_FORGE_RUNNER_CAPACITY` | `1` | `prepare.sh` | Jobs the Actions runner executes concurrently. Written into `state/runner/config.yml` only when that file is absent, so raising it on an existing state directory needs the file removed (or `just forge-reset`). One slot is safe for a quiet world; a society whose agents write CI will outrun it, and stale Actions runs can be neither cancelled nor deleted through the API. |
| `SWECRAFT_FORGE_JOB_MEMORY` / `SWECRAFT_FORGE_JOB_CPUS` | `8g` / `4` | `prepare.sh` | Per-CI-job container limits (`--memory`, swap-disabled, and `--cpus`). Same write-once caveat as capacity. Unbounded jobs can exhaust the host: one pytest-xdist suite auto-sizing to nproc inside DinD reached ~957 GiB RSS. Keep capacity × memory well under host RAM. |
| `SWECRAFT_FORGE_RUNNER_LABELS` | `docker:docker://node:22-bookworm,ubuntu-latest:docker://ghcr.io/catthehacker/ubuntu:act-22.04,python:docker://python:3.12-bookworm` | `bootstrap.sh` | Labels the Actions runner registers with, mapping a workflow's `runs-on` value to the image the job runs in. |

Example — run a second, isolated Forgejo instance:

```bash
SWECRAFT_FORGE_PORT=3401 COMPOSE_PROJECT_NAME=swecraft-forge-dev just forge-up
```

## Generated state

Bootstrap credentials are generated on first run under the gitignored
`services/forge/state/` directory. The two Forgejo secrets and the runner
config are written `0644`, and `state/runner` `0777`, because the rootless
container reads and writes them as UID 1000 (see
[Running on Linux](#running-on-linux)); the admin password and token stay
`0600`:

| File                     | Created by     | Description                                        |
| ------------------------ | -------------- | -------------------------------------------------- |
| `forgejo_secret_key`     | `prepare.sh`   | Forgejo `SECRET_KEY`                               |
| `forgejo_internal_token` | `prepare.sh`   | Forgejo internal API token                         |
| `admin_password`         | `bootstrap.sh` | Password for the `swecraft-admin` user                  |
| `admin_token`            | `bootstrap.sh` | API access token for the `swecraft-admin` user (all scopes) |
| `runner/config.yml`      | `prepare.sh`   | Actions runner configuration                       |
| `runner/.runner`         | `bootstrap.sh` | Actions runner registration (its identity and token) |

## Actions

Forgejo Actions is enabled, and `forge-bootstrap` registers one
`forgejo-runner` against a Docker-in-Docker daemon, so workflow jobs run in
containers on an isolated daemon rather than the host's. The runner idles
until registration completes, then serves jobs with `capacity: 1`.

Workflows live in `.forgejo/workflows/` (Forgejo also reads
`.github/workflows/`). `runs-on` accepts the labels in
`SWECRAFT_FORGE_RUNNER_LABELS` above — `docker`, `ubuntu-latest`, and `python`
by default:

```yaml
jobs:
  test:
    runs-on: python
    steps:
      - run: python3 --version
```

Two constraints worth knowing before writing workflows:

- **Jobs cannot reach github.com**, so `uses: actions/checkout@v...` and
  other marketplace actions fail. Clone from the forge instead — the job's
  `$GITHUB_SERVER_URL` and `$GITHUB_REPOSITORY` point at it.
- **Repositories created before Actions was enabled** keep the unit
  disabled and ignore their workflows. Turn it on per repository with
  `PATCH /api/v1/repos/{owner}/{repo}` and `{"has_actions": true}`.

## Seeding worlds from upstream repositories

For a reproducible multi-repository experiment, use the manifest-driven world
engine. It resolves every pin before mutation and writes the exact seeded
baselines to a resumable lock:

```bash
cp examples/world.yaml experiments/my-experiment/world.yaml
just world-seed experiments/my-experiment/world.yaml
uv run --package swecraft-world swecraft-world \
  --manifest experiments/my-experiment/world.yaml actions-status   # must report zero
uv run --package swecraft-world swecraft-world \
  --manifest experiments/my-experiment/world.yaml enable-actions
uv run --package swecraft-world swecraft-world \
  --manifest experiments/my-experiment/world.yaml actions-status   # must still report zero
just world-verify experiments/my-experiment/world.yaml
```

Seeding deliberately leaves Forgejo Actions **off** and `enable-actions`
turns it on afterwards. Enabling inside the seed raced Forgejo's
asynchronous evaluation of the pushed refs, so every upstream workflow
carried in a pushed tag tree fired; see [Worlds](worlds.md). The
`actions-status` checks bracket the enabling step: zero runs before
proves the seed was clean, zero runs after proves enabling surfaced
nothing.
`enable-actions` itself waits a settle window before and after the switch
and refuses a world where runs appear, so a contaminated seed fails at
this step rather than mid-campaign.

See [Worlds](worlds.md) for the manifest, lock, adoption, and verification
contracts. For isolated/no-collaboration experiments, set each manifest
repository to `visibility: private` and configure the coordinator with
`collaboration: isolated`; the world check verifies the live Forgejo
visibility. For a one-off repository that does not define an experiment world,
the lower-level helper remains available:

```bash
just forge-seed https://github.com/python/typing_extensions.git 4.16.0 typing-extensions
```

The one-off helper clones upstream with full history, checks out the tag as
`main`, removes
`.github/workflows/` in a commit whose message tells the agent what to use
instead, creates the repository with `auto_init: false`, pushes `main`,
enables the Actions unit, and verifies the result. Each step prevents a
specific failure:

- **Upstream CI fires on the mirror push.** Forgejo Actions reads
  `.github/workflows/` too, so one push queues the entire upstream matrix —
  jobs that cannot work here, against a `capacity: 1` runner.
- **Pushing the pinned tag re-fires it even when `main` is stripped**, because
  the strip commit sits *on top of* the tag and the tag's tree still carries
  the workflows. `forge-seed` pushes no tags at all and prints the pinned
  commit instead. This one is repository-dependent — most repositories ship no
  tag-triggered workflows, so a seeding script can test clean and jam on the
  next repository.
- **`auto_init` orphans the mirrored history** by adding an unrelated first
  commit.
- **Repositories created before Actions was enabled ignore their workflows**
  until `has_actions` is patched on.
- **A push can leave the repository flagged `empty` in Forgejo's database**
  while its refs are already present, so onboarding sees an empty repository.
  The flag settles on its own, but not synchronously with the push, so
  `forge-seed` waits for it rather than trusting the push's exit status.

Stale Actions runs cannot be deleted or cancelled through the API, so the only
way back from a mis-seeded repository is to delete and recreate it — which is
why the verification is part of seeding rather than an afterthought.

Verify the full path (registration → dispatch → green run) with:

```bash
just forge-actions-smoke
```

Changing `SWECRAFT_FORGE_RUNNER_LABELS` re-registers the runner on the next
`forge-bootstrap`; the labels used are recorded in `state/runner/labels`.

**Trust boundary (intentional, local-research only):** the DinD daemon
listens unauthenticated on its compose-internal network, and job
containers share that daemon. Any workflow — including agent-authored
ones — can therefore control the CI daemon: start containers, read other
jobs' workspaces, interfere with concurrent runs. The boundary that holds
is between CI and the host (jobs never touch the host Docker daemon or
credentials), not between jobs. Treat CI results as advisory when
multiple agents share the runner, and revisit (rootless DinD, per-job
daemons) before any shared deployment.

## Running on Linux

Two things differ from macOS/Windows Docker Desktop, and both are total
losses of function rather than caveats if left unaddressed.

**Task containers cannot reach a loopback forge.** `host.docker.internal`
does not resolve inside them on Linux (Harbor passes no `host-gateway`
extra host, per [runner-image](runner-image.md)), and the forge publishes
on `127.0.0.1` by default, which a container cannot route to either. So an
agent in the sandbox sees a forge that does not exist. Publish on the
Docker bridge instead, which is reachable from containers on that bridge
and from the host, and from nowhere else:

```bash
export SWECRAFT_FORGE_HOST=$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)  # usually 172.17.0.1
just forge-up && just forge-bootstrap demo
```

Point the coordinator at the same address — **both** `forge_url` (its own
API calls and checkouts, from the host) and `push_url` (the push remote
and `SWECRAFT_FORGE_URL` the sandboxed agent sees, from the container):

```yaml
# coordinator.yaml
forge_url: http://172.17.0.1:3400
push_url:  http://172.17.0.1:3400
```

[coordinator.md](coordinator.md) explains why both are needed and what
fails when either is left at its default. Anything else that runs
sandboxed turns against this forge needs the same treatment — the rule is
that the address baked into a container's environment has to be routable
*from the container*, not from the host.

**`services/forge/state/runner` is created by the host user**, while the
runner container writes its registration as UID 1000. On Linux hosts whose
checkout owner is not UID 1000, first bootstrap can fail with a permission
error — `chmod 0777` the directory, `chown -R 1000` it, or run the compose
services with your UID. (The two generated secrets have the same
constraint and are written `0644` for exactly this reason: the rootless
image must be able to read them, and they are machine-local secrets for a
forge that is not published beyond this host.)

`just forge-reset` deletes the Docker volume and all generated credentials;
the next `forge-up`/`forge-bootstrap` regenerates them.

## Docker network pools under concurrent turns

Every sandboxed agent turn creates its own Docker Compose network (and a
DinD-equipped drafting turn keeps it for the whole turn). Docker's default
address pools allow only about **30 concurrent networks**; the standing
forge stacks each hold one too. Past the limit, every new turn fails at
environment bring-up with:

```text
Error response from daemon: all predefined address pools have been fully subnetted
```

The failure is easy to misread at scale because it surfaces as Harbor
trial errors, not as a Docker message in your terminal — one large
sweep of ours lost its entire first 64-worker launch to it (harmlessly: turns die
before the agent phase, so nothing is spent). A 32-way workload sits just
under the wall and works by accident.

Before running more than ~25 concurrent turns on a host, widen the pools
in `/etc/docker/daemon.json` and restart the daemon:

```json
{
  "default-address-pools": [
    {"base": "10.200.0.0/13", "size": 24}
  ]
}
```

That yields ~2,048 `/24` networks. Pick a base range that does not collide
with your host's real networks — **and always pin `"bip"` (for example
`"bip": "172.17.0.1/16"`) in the same change**: without it the pool
reassigns the default bridge itself, silently moving `docker0` off
`172.17.0.1` at the restart. Every service published or advertised on the
old bridge address — the standing forges, any evaluator loop pointed at
`172.17.0.1` — goes dark until reconfigured (this took down all three
forges in one incident of ours). The restart bounces every container on the
host: the forge stacks recover on their own (`restart: unless-stopped`),
and in-flight turns die and must be retried — pause turn-launching
workloads first.
