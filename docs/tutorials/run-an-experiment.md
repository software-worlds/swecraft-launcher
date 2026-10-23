# Tutorial: run an experiment

An experiment puts one agent per repository on a seeded world of real Python
libraries, lets them maintain it, measures their released code with the
benchmarks of downstream projects they never see, and ends with a checkpoint
of everything and a browsable export. This page is the path, step by step;
each step is one `swecraft-launch <step> experiments/<name>` command
(`packages/swecraft-launch`), and the reference pages explain the pieces.

![How an experiment runs](../assets/run-flow.svg)

## What you need

- A Linux host with Docker, [`just`](https://github.com/casey/just) and
  [`uv`](https://docs.astral.sh/uv/), and room for one container per wake and
  per CI job at once.
- Model credentials for the agents in your shell, named the way
  `coordinator.yaml`'s `agent_env` forwards them (for example
  `CLAUDE_CODE_OAUTH_TOKEN`). Never write them into the experiment folder.
- `uv sync --all-packages --all-groups` once, then `source .venv/bin/activate`
  (or run each command as `just exp <step> experiments/<name>`).

One checkout runs one experiment at a time: the forge's credentials live in
`services/forge/state/` and the run's state in `.swecraft/`. Run a second
experiment from a second checkout (or `git worktree`), with its own forge
port and Compose project.

## 1. Decide, and write the experiment folder

```bash
cp -r templates/experiment experiments/<name>
```

Every value to decide carries a `FILL` marker. Decide them before writing:

- **The question**, in one falsifiable sentence.
- **The world** (`world.yaml`): the repositories and the release each starts
  from. A repository belongs in the world only if some judge actually runs its
  code, which the shakeout proves, or if it is a deliberate control.
- **The judges** (`evaluation.yaml`): downstream projects that import the
  world's repositories, each with a correctness gate and benchmark tasks; the
  timeouts come from measured durations, not guesses.
- **The treatment** (`coordinator.yaml` and `charters/`): model and harness,
  shared or private repositories, wake budget, cadence, daily caps. Generate
  the charters with `charters/generate.py` and commit the output.
- **Spend and the stop rule** (`experiment.env`): `STOP_WAKES` and/or
  `STOP_DOLLARS`, the daily caps, `workers` (wakes in flight), CI capacity
  and per-job limits, wake container size. Never leave these at defaults.
- **The date** of the world, for the capped package index
  (`SWECRAFT_FORGE_INDEX_URL`, `SWECRAFT_FORGE_EXCLUDE_NEWER`): installs see PyPI as it
  stood that day ([index-cap](../reference/index-cap.md)).

Before seeding, read every repository's `AGENTS.md`, `CLAUDE.md`,
`CONTRIBUTING*` and similar files for rules addressed to coding agents ("ask
the user to approve before opening a pull request", "never create a
release"). Agents follow them literally and stall; say in that repository's
charter that upstream's agent rules do not apply on the society's forge.

```bash
swecraft-launch check experiments/<name>     # files, charters, FILL markers, settings
```

## 2. Build the images

```bash
just benchmark-base-image-build                      # benchmark-python:1
docker build -t swecraft-experiment-base:1 experiments/<name>/images/base
docker build -f experiments/<name>/images/ruler/Dockerfile -t <judge>:1 <judge checkout>   # one per judge
just runner-image-build                              # swecraft-runner-image:0.1
docker build -t <name>-agent:1 experiments/<name>/images/agent
```

Then layer the capped index onto the agent image
(`images/index-cap/Dockerfile`), and for pi agents add `images/agent-pi`.
A baked tag is an epoch: never rebuild a tag in place; change the tag and the
manifest.

## 3. Start the forge

```bash
swecraft-launch up experiments/<name>
```

Starts the capped index if nothing answers at `SWECRAFT_FORGE_INDEX_URL`, then the
forge with its CI runner (capacity, per-job memory and CPUs, and the capped
index for CI jobs).

## 4. Seed the world

```bash
swecraft-launch seed experiments/<name>
```

Seeds every repository at its pinned release, proves the seed queued no CI
run (a queued run means a contaminated world: reset the forge and seed
again), enables CI, and checks the forge against the new
`world.lock.json`. Commit the lock with the experiment.

## 5. Shakeout

```bash
swecraft-launch shakeout experiments/<name>
```

Mints the evaluator's read-only forge token, validates the eval set, and
measures the untouched seed against itself (an A/A run). Every gate must
pass; a red gate means the measurement is broken, and no later number is
believable until it is fixed. The A/A deltas are the pipeline's noise floor.

## 6. Onboard the agents

```bash
swecraft-launch onboard experiments/<name>
```

Creates each agent's forge user, home and token, writes its charter as the
operator-owned `GOAL.md` on its memory branch, and runs one bootstrap turn
each. Model spending starts here.

## 7. Run

```bash
swecraft-launch run experiments/<name>      # inside tmux or screen: it runs until the stop rule
swecraft-launch status experiments/<name>   # from another terminal
```

`run` serves the coordinator and sweeps the world's heads every
`MEASURE_INTERVAL_SECONDS` (the first sweep at once). When the stop rule is
met it drains the coordinator (one interrupt: wakes in flight finish), waits
for the sweep in flight, and finishes. Ctrl-C drains the same way and pauses;
`run` again continues where the ledger left off.

What to watch: the forge's web UI (branches, pull requests, reviews,
releases), `.swecraft/runs/<run-id>/` (each wake's receipt, response and
trajectory), `.swecraft/coordinator-serve.log`, `.swecraft/measure-loop.log`, and
`uv run swecraft-coordinator --config experiments/<name>/coordinator.yaml agents`.

## 8. Finish

`run` finishes by itself at the stop rule; `swecraft-launch finish experiments/<name>`
does the same after a manual stop. It runs a final sweep, prints the score,
and takes one verified checkpoint of everything (apparatus, frozen forge,
ledger, traces, measurements) with an archive under `archives/`. Checkpoints
carry the forge's live credentials: keep them local or publish them only to a
private dataset (`uv run swecraft-society publish <checkpoint>`; see
[checkpoint and restore](checkpoint-and-restore.md)).

## 9. Export

```bash
swecraft-launch export experiments/<name> --known-secrets <file>
```

Collects the newest checkpoint and writes a bundle under `bundles/<name>` that
sim-browser shows. Prepare the known-secrets file outside the checkpoint
(model keys and any other value that could appear in an agent's output) so
it is redacted as well ([export](../reference/export.md)).

## Rules that hold throughout

- No standing service gets the forge admin token; the evaluator reads with a
  read-scoped token.
- Take a checkpoint before any `just forge-reset` of a world that matters:
  reset is unrecoverable.
- Wake-container and CI limits are part of the design. One unbounded test job
  can exhaust a host.
