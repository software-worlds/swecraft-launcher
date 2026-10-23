# swecraft-evaluator

`swecraft-evaluator` measures whether a society's changes to the world
repositories made real downstream software faster or slower. Frozen
downstream projects (evaluators), baked into container images, run their own
benchmarks against a chosen world **environment** — a complete map of every
world repository to an exact commit — and every candidate is scored against
the `world@seed` baseline from
[`world.lock.json`](../world/README.md).

Three environment shortcuts cover every use: `world@seed`,
`world@latest`, and `world@seed+repo:tag` (one repository at a release).
The manifest declares subjects (how each repository installs), evaluators
(image, correctness gate, tasks with `walltime`, `codspeed-walltime`, and
`codspeed-cpu-sim` metrics), and named evaluations — the task selections
that produce one score series.
Containers run offline with a single build per environment; a failed gate
voids measurement, so a broken world scores `null`, never a number. Each
run appends one immutable `record.json` under the records directory.
For Slurm capacity, `export` writes a credential-free bundle with one cell
workdir per evaluator and a generated array script. Each array task runs
`swecraft-evaluator --executor apptainer worker ...` and writes one terminal cell
result; `collect` writes the aggregate `record.json` only after every
expected cell is terminal. Set `SWECRAFT_EVALUATOR_WORKER_CLASS` to a calibrated
homogeneous partition name before comparing seed and candidate records across
nodes; otherwise worker classes default to host-specific fingerprints.

```bash
swecraft-evaluator --manifest evaluation.yaml validate
swecraft-evaluator run society-general --env world@latest
swecraft-evaluator run downstream-attrs --env world@seed+attrs:26.3.0
swecraft-evaluator score
swecraft-evaluator loop society-general --once
swecraft-evaluator export society-general --env world@seed --bundle /shared/eval/b1
sbatch /shared/eval/b1/slurm/swecraft-evaluator-worker.sbatch
swecraft-evaluator collect /shared/eval/b1
```

`loop` sweeps `world@latest` on a time cadence and evaluates every new
release oldest-first; wake-based cadence belongs to the coordinator
(`evaluate_every_wakes`), which invokes the evaluation at a drained step
boundary. The evaluator image must provide Python 3 for the small stdlib
container harness. It must also keep the frozen ruler checkout at
`/study/ruler`; `codspeed-*` metrics additionally require `pytest-codspeed`,
and `codspeed-cpu-sim` requires stock `valgrind` with Callgrind.

See [the evaluator reference](../../docs/reference/evaluator.md) for the
manifest schema, execution model, and scoring rules.
