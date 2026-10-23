# Tutorial: checkpoint a society and load it back

This is the what-to-type path from a running society to a verified,
shareable checkpoint — and from that checkpoint back to a materialized
copy on disk. It protects a run from losing its world with no recovery
path: a checkpoint is the recovery path.

A checkpoint is a plain directory: your experiment's exact apparatus,
the frozen Forge world, coordinator and evaluator state, agent traces,
and completed measurements, all bound together by a checksummed
manifest. See the
[society checkpoint protocol](../reference/society-checkpoints.md) for
how and why it works; this page only tells you what to do.

> **Checkpoints contain live credentials** (Forge secrets, admin and
> agent tokens). Treat every checkpoint as a secret: keep it local or in
> a **private** dataset repository, never anywhere public.

## Prerequisites

You need a society that was seeded from a world manifest — the
`world.yaml` path of
[run your first society](first-society.md) — so that `world.yaml` and
`world.lock.json` exist at your checkout root, next to
`coordinator.yaml`.

Checkpoints capture the *complete* experiment apparatus, which also
includes the evaluation manifest (`evaluation.yaml`). If you have not
defined an evaluation yet, create a minimal manifest — replace `demo`
with a repository name from your `world.yaml`:

```yaml
# evaluation.yaml
world_lock: world.lock.json
subjects:
  demo:
    install:
      - argv: ["true"]
evaluators:
  smoke:
    image: benchmark-python:1
    subjects: [demo]
    gate: {argv: ["true"]}
    tasks:
      noop:
        command: {argv: ["true"]}
        metrics: [walltime]
evaluations:
  society-general:
    tasks: [smoke@noop]
    metric: walltime
```

```bash
just benchmark-base-image-build   # builds benchmark-python:1
uv run --package swecraft-evaluator swecraft-evaluator validate
```

When `evaluation.yaml` is missing, capture still succeeds: it is
recorded under `omissions`, the dataset card notes the partial capture,
and `resume` warns. A real suite with meaningful benchmarks is described
in the [evaluator reference](../reference/evaluator.md).

## 1. Take a checkpoint

The society must be **quiet at the boundary**: no agent wake in flight.
Either let the coordinator idle between wakes or stop `coordinator-serve`
for a moment — the checkpoint refuses (loudly, harmlessly) while a wake
is active, and waits briefly for any running Forgejo Actions jobs rather
than interrupting them.

```bash
just society-checkpoint
```

The Forge pauses only for the seconds it takes to copy raw bytes, then
everything resumes while hashing and verification finish in the
background of your terminal. The result lands under
`.swecraft/checkpoints/<world>-<timestamp>/` as a fully verified directory.

Useful variants:

```bash
just society-checkpoint --archive backups/run.tar.gz   # also emit one tar.gz
just society-checkpoint --harbor include               # add raw Harbor archives
                                                       # (traces are always kept)
```

## 2. Look at what you captured

```bash
just society-inspect .swecraft/checkpoints/<checkpoint-id>
```

`inspect` re-proves every file against the manifest — sizes, digests,
that the repository heads in the manifest match the frozen Forge bytes,
and that every seed baseline is an ancestor of its captured head. The
generated `README.md` inside the checkpoint is its dataset card; do not
edit it (verification would then fail by design).

## 3. Checkpoint on a cadence, automatically

For a long campaign, let the coordinator checkpoint at wake boundaries —
the training-loop pattern, `save_every_n_steps` for societies:

```yaml
# coordinator.yaml
checkpoint_every_wakes: 20   # checkpoint after every 20 completed wakes
checkpoint_keep: 5           # prune older boundary checkpoints
```

The serve loop drains in-flight wakes, checkpoints at the quiet boundary
it created, and resumes. Agents never notice. A failed boundary is
logged and skipped; the next attempt comes one cadence later.

Evaluation shares the same boundary mechanism: `evaluate_every_wakes`
with a full `evaluate_command` argv runs the evaluator at the drained
boundary, before the checkpoint, so its record enters the capture. See
the commented block in `examples/coordinator.yaml` and the
[evaluator reference](../reference/evaluator.md).

## 4. Share it (optional, private only)

Configure a **private** Hugging Face dataset in `.swecraft/checkpoint.yaml`:

```yaml
# .swecraft/checkpoint.yaml
huggingface:
  repo_id: software-worlds/my-experiment
  private: true
```

Then, with `HF_TOKEN` set in your environment:

```bash
just society-checkpoint --publish

# or publish an existing checkpoint later — retryable, no quiescence needed:
uv run --package swecraft-society swecraft-society publish .swecraft/checkpoints/<id>
```

Each publish is one commit in the dataset repository, tagged with the
checkpoint id. Publishing refuses non-private repositories and verifies
the dataset is actually private before uploading a single byte.

## 5. Load a checkpoint back

Loading is a **pure filesystem operation**: it verifies the source and
materializes a pristine copy. It starts nothing, imports nothing into
Docker, and cannot disturb a running society.

```bash
just society-load .swecraft/checkpoints/<checkpoint-id>      # a directory
just society-load backups/run.tar.gz                    # a tar.gz
just society-load hf://software-worlds/my-experiment           # latest published
just society-load hf://software-worlds/my-experiment@<rev>     # exact commit or tag
```

The copy appears under `.swecraft/loaded/<checkpoint-id>/`, re-verified byte
for byte; HF loads also record the exact resolved revision beside the
copy. What you have now is the complete experiment as a dataset: the
apparatus to read, the traces and measurements to analyze, and per-repo
baseline/head vectors an evaluator can use as a subject.

## 6. Resume the loaded society

Resume a pristine loaded copy into a new Compose project and unused host port:

```bash
just society-resume .swecraft/loaded/<checkpoint-id> \
  --project my-resumed-society --port 3410
```

Resume imports the Forge volume, starts Forge/Dind/the copied runner, preserves
its private credentials, and writes relocated `runtime/coordinator.yaml` and
`runtime/checkpoint.yaml`. Automatic wake-boundary checkpointing keeps its
configured cadence but now targets the resumed society.

Coordinator and evaluator startup remain explicit. Start the coordinator with
the generated runtime configuration when you are ready:

```bash
uv run --package swecraft-coordinator swecraft-coordinator \
  --config .swecraft/loaded/<checkpoint-id>/runtime/coordinator.yaml serve
```

The loaded destination is mutable after resume and no longer verifies as the
original dataset; the source checkpoint, archive, or HF commit remains
immutable.

## When to checkpoint

- **Before `just forge-reset`, always.** Reset destroys the Forge volume;
  a checkpoint is the only way back. If your clean slate also
  deletes `.swecraft/`, use `--archive` to write the artifact outside it —
  default checkpoints live in `.swecraft/checkpoints/` and would be deleted
  with it.
- At the end of every experiment phase you may want to compare against.
- On a cadence during long campaigns (step 3), so no more than N wakes
  of society history are ever at risk.
