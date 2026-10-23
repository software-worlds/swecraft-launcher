# Architecture: how the pieces fit

Each service documents its own boundaries; this page is the one map of what
runs where, what owns what, and where the parts meet. Python development is
one root `uv` workspace and lock, but the runtime is a set of independently
owned components. The short version: **the long-running processes never
speak to each other directly**. The coordinator and the evaluator meet at the
forge's API and at the filesystem under `.swecraft/`; after the run, the
checkpoint, the analyzer and the exporter read that filesystem and nothing
else.

```text
                       ┌─────────────────────────────┐
  docker compose ────▶ │   forge (Forgejo, :3400)    │ ◀── agents act directly
                       └─────────────────────────────┘     (fj/forge + own token,
                           ▲ REST + git      ▲              from inside sandboxes)
                           │                 │ read-only token
 swecraft-coordinator serve ─ wake ─▶ swecraft-runner (library, run_once)
        │                             └─▶ Harbor task container (runner image)
        │ writes                                        │ artifacts
        ▼                                               ▼
   .swecraft/coordinator.db   +   .swecraft/runs/<run-id>/{output,harbor}
                                                 swecraft-evaluator sweeps (timed)
                                                   └─▶ .swecraft/evaluator/records/
 after the run:
   swecraft-society checkpoint ─▶ .swecraft/checkpoints/<id>/ ─▶ swecraft-analyzer collect
        ─▶ evidence.db + unpacked forge ─▶ services/export ─▶ bundle ─▶ sim-browser
```

`swecraft-launch <step> <folder>` (`packages/swecraft-launch`) sequences these for one
experiment: it starts the forge and the capped index, seeds the world, runs
the A/A shakeout, onboards the agents, then supervises `swecraft-coordinator
serve` and the timed sweeps until the stop rule, and finishes with a final
sweep and a checkpoint.

## Processes and libraries

| Runs as a process | What it owns |
| --- | --- |
| forge (`just forge-up`) | The world: repositories, identities, permissions, CI |
| `swecraft-index-cap serve` | PyPI's index as it stood on the world's date, for CI and wakes |
| `swecraft-coordinator serve` | The wake loop: claims, scheduling, caps, memory publish, boundary checkpoints |
| `swecraft-evaluator run` (one per sweep) | Measurements of the released code, as append-only records |

| Imported as a library | By whom |
| --- | --- |
| `swecraft-runner` (`run_once`) | The coordinator: one sandboxed agent turn per call; the `swecraft-runner` CLI is a debug wrapper around the same function |
| `swecraft-world` | Evaluator and society: manifest and lock protocol |
| `swecraft-evaluator` | Society: checkpoint and measurement apparatus capture |
| `swecraft-coordinator` | Society: the ledger and run artifacts a checkpoint captures |
| `swecraft-forge` | Coordinator and society: Forge runtime adapters |
| `swecraft-common` | Forge, world, coordinator, evaluator and society: shared *mechanisms* (see boundary rule) |

The package metadata stays per component so namespaces, entry points,
stores, CLIs, and service-owned policy do not collapse into one package.
The root workspace is only the host-side installation and lock surface. The
analyzer and the exporter use only the standard library, so both also run
outside the workspace.

## The filesystem is the internal API

There is no RPC between the processes. From the coordinator, readers share
exactly three things:

- **`.swecraft/coordinator.db`**: the wake ledger and scheduler state, SQLite in
  WAL mode. The scheduler writes; readers (the launcher's stop
  rule, the checkpoint) open it read-only.
- **`.swecraft/runs/<run-id>/`**: one directory per runner turn: the decision
  record (`output/result.json`), the agent's `response.md`, and Harbor's full
  trajectory. Written once, slimmed after success, swept whole by retention,
  so every reader treats absence as normal (a ledger run id legitimately
  outlives its artifacts) and validates run ids against the grammar in
  `swecraft_common.runid` before joining them onto a path.
- **`coordinator.yaml`**: the agent→repository binding lives here, resolved
  at read time; the wakes table stores only agent names.

Measurements add a fourth surface, read the same way: the evaluation
manifest and the evaluator's append-only records directory
(`evaluation.yaml`, `.swecraft/evaluator/records`, or a checkpoint's `apparatus/`
and `evaluations/`).

The forge is the other meeting point, with two trust postures: the
coordinator talks to it from the host with per-agent tokens it provisions,
while the agent inside the sandbox talks to it directly with its own
write-capable token. Containment is Forgejo's permission model (write on the
agent's own repositories, nothing anywhere else). The evaluator reads with a
separate read-scoped token. See [coordinator.md](coordinator.md).

## Boundary rules

- **`packages/swecraft-common` holds mechanisms, not policy.** Something graduates
  there when a *second service needs the same mechanism* (isolated git
  execution, Forgejo transport, the run-id grammar), not when two things look
  similar. Single-owner policy stays with its owner: the trusted memory
  applier and run retention live in the coordinator, and each service's
  SQLite store stays its own.
- **Control is CLI-only, and secrets cross no wire.** Configuration carries
  `env:` / `file:` *sources*, never values. Checkpoints do carry the forge's
  live credentials, so they stay local or in a private dataset; exported
  bundles are scrubbed ([export](export.md)).
- **Everything the agent produced is untrusted input.** Bounded reads, capped
  content fields, id validation before path joins.

## Where measurement fits

The evaluator is a deliberate outlier: it measures the society's released
code against downstream consumers the agents never see, so it runs its own
containers (no network), keeps its own records, and shares only the forge
API and `swecraft-common` with the rest of the system. A sweep measures every
world repository's default-branch head (`world@latest`); per-release
evaluations replay each release tag against the seed. See
[evaluator.md](evaluator.md).
