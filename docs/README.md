# Documentation

New here? [Run your first society](tutorials/first-society.md) is the
shortest path from an empty checkout to agents working on a one-repository
forge. [Run an experiment](tutorials/run-an-experiment.md) is the full path:
a seeded world, an eval set, agents, a stop rule, a checkpoint, and a
browsable export. The [architecture page](reference/architecture.md) is the
one-page map of how the pieces fit.

`docs/tutorials/` holds task guides; `docs/reference/` explains how each
piece works and why. To capture, share, and reload a complete experiment,
follow [checkpoint a society and load it back](tutorials/checkpoint-and-restore.md);
the [society checkpoint protocol](reference/society-checkpoints.md) defines
how and why the format works.

Python development uses one root `uv` workspace and one root `uv.lock`.
Run `uv sync --all-packages --all-groups`, `just python-check`, and
`just python-test` from the repository root.

## Components

| Component | Path | Purpose | Reference |
| --- | --- | --- | --- |
| Forge | `services/forge` | Local Forgejo Git server with an Actions runner | [setup](reference/setup.md) |
| World | `services/world` | Versioned world manifest, seeder, and seed-lock protocol | [worlds](reference/worlds.md) |
| Index cap | `services/index-cap` | PyPI's simple index as it stood on a world's date | [index-cap](reference/index-cap.md) |
| Coordinator | `services/coordinator` | Autonomous per-repository maintainer loop | [coordinator](reference/coordinator.md), [prompts](reference/prompts.md), [nudges](reference/nudges.md) |
| Runner | `services/runner` | One sandboxed agent turn through Harbor | [runner](reference/runner.md) |
| Runner image | `services/runner-image` | Task image with pinned fj and forge tooling | [runner-image](reference/runner-image.md) |
| Evaluator | `services/evaluator` | Downstream evaluation: the dependents' own benchmarks, gated | [evaluator](reference/evaluator.md) |
| Society | `services/society` | Checkpoint, load, resume, and publish a whole experiment | [checkpoints](reference/society-checkpoints.md) |
| Analyzer | `services/analyzer` | One checkpoint as a queryable evidence store | [analyzer](reference/analyzer.md) |
| Export | `services/export` | One checkpoint as a sim-browser bundle | [export](reference/export.md) |
| `swecraft-common` | `packages/swecraft-common` | Shared host-Git, Forgejo, and run-ID primitives | [package README](../packages/swecraft-common/README.md) |
| Launcher | `packages/swecraft-launch` | `swecraft-launch <step> <folder>`: up, seed, shakeout, onboard, run, finish, export | [run an experiment](tutorials/run-an-experiment.md) |
