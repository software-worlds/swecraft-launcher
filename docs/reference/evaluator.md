# Evaluator

The evaluator answers one question: did the society's changes to the world
repositories make real downstream software faster or slower? It measures
frozen downstream projects (evaluators) running their own benchmarks against
a chosen world state, and scores every candidate against the seed baseline.

## Concepts

- **Environment** — a complete map of every world repository to an exact
  commit. Three shortcuts cover every use:
  - `world@seed` — the locked initial state (`baseline_commit` per repo)
  - `world@latest` — current default-branch heads from the Forge
  - `world@seed+repo:tag` — the seed with one repository at a release tag
- **Evaluator** — a frozen downstream project baked into a container image
  (its checkout lives at `/study/ruler`). It declares which subjects it
  exercises, a correctness **gate**, and named **tasks**.
- **Task** — one benchmark command plus the **metrics** it produces:
  - `walltime` — N timed repeats, raw seconds
  - `codspeed-walltime` — pytest-codspeed in-process benchmark-body timing,
    stored as full per-benchmark stats plus a task-level `seconds` total
  - `codspeed-cpu-sim` — pytest-codspeed simulation markers under stock
    Callgrind, stored as a task-level `instructions` total plus URI-keyed
    per-benchmark instruction diagnostics
- **Evaluation** — a named shortcut selecting `evaluator@task` pairs, the
  metric that scores them, and the aggregates (`geomean`, `average`).
- **Subject** — how one world repository installs into an evaluator
  container (explicit commands; sources are fetched from the Forge at the
  environment's commits and mounted at `/eval/sources/<repo>`).

## Manifest

```yaml
world_lock: world.lock.json
subjects:
  h11:
    install:
      - argv: [python, -m, pip, install, --no-deps, --no-build-isolation, /eval/sources/h11]
evaluators:
  wsproto:
    image: eval-wsproto:1
    subjects: [h11]
    gate: {argv: [python, -m, pytest, -q, tests/test_client.py]}
    tasks:
      connection:
        command: {argv: [python, -m, pytest, bench/test_connection.py, -q]}
        metrics: [walltime, codspeed-walltime, codspeed-cpu-sim]
evaluations:
  society-general:
    tasks: [wsproto@connection]
    metric: codspeed-cpu-sim
    aggregates: [geomean]
```

Subjects that derive their version from git metadata (hatch-vcs,
setuptools-scm) need `env: {SETUPTOOLS_SCM_PRETEND_VERSION: 0.0.0}` on their
install command — sources arrive as archives without `.git`.

Task commands are workload commands, not instrument commands. For
`codspeed-*` metrics the command must be direct pytest argv (`pytest ...` or
`python -m pytest ...`) and must not include `--codspeed`,
`--codspeed-mode`, or pytest-codspeed plugin flags. The harness appends the
right flags, loads pytest-codspeed, and stages the `min_time=0.001` pin
plugin itself.

## Execution model

One offline container (`--network none`) per evaluator per environment,
single build: fetch subject sources, run installs, run the gate, run the
tasks. Docker is the default executor. The Apptainer executor runs the same
mounted `/eval` workdir with `apptainer exec --containall --cleanenv
--writable-tmpfs --net --network none --bind ...`; if a site does not permit
network namespaces, the cell fails closed instead of silently running
online. `--writable-tmpfs` restores Docker's writable-container-layer
semantics: a SIF rootfs is read-only, so an image whose gate or benchmark
writes inside its own tree (caches, scratch files under `/study/ruler`)
would otherwise fail with `EROFS` under Apptainer while passing under
Docker. The overlay is ephemeral — nothing written there survives the
cell. A failed gate
voids every task — a broken world scores `null`, never a number. Each run
appends one `record.json` (environment, digest, per-task samples) under the
records directory; the surrounding workdir keeps the plan and all command
logs.

For Slurm handoff, the trusted host can export an offline bundle instead of
running the containers itself:

```bash
swecraft-evaluator export society-general --env world@seed --bundle /shared/eval/b1
sbatch /shared/eval/b1/slurm/swecraft-evaluator-worker.sbatch
swecraft-evaluator collect /shared/eval/b1
```

The bundle contains `bundle.json`, `cells.txt`, one immutable
`cells/<cell>/work` directory per evaluator, the staged harness, `plan.json`,
cell metadata with the budget and local SIF SHA-256 when the image reference
is a file, and subject sources fetched from the Forge at exact commits.
Forge tokens, coordinator state, and society checkpoint credentials are not copied; a
top-level `.swecraft` tree in an input archive is discarded. The generated Slurm
script submits a 1-based array where each task runs
`swecraft-evaluator --executor apptainer worker "$BUNDLE" --array-index "$SLURM_ARRAY_TASK_ID"`.
The script bakes in the exported bundle's absolute path (`sbatch` runs a
spooled copy, so it cannot locate the bundle relative to itself);
`SWECRAFT_EVALUATOR_BUNDLE` relocates a moved bundle. It also carries a
`#SBATCH --time` derived from the largest cell budget plus staging margin,
and commented site-bootstrap lines (module load, virtualenv activation) to
adjust before submitting — `swecraft-evaluator` and `apptainer` must be on the
job's `PATH`.
Workers are stateless: they copy one cell workdir into the results directory,
run it, remove copied sources from the result, and atomically write
`cell-result.json`. The collector is idempotent and writes the aggregate
`record.json` only after every expected cell result is terminal.

Every new evaluator outcome carries worker provenance: executor, image,
platform details, Slurm identifiers when present, a fingerprint, and a
comparison class. Without operator input the class includes the hostname, so
cross-host comparisons are blocked by default. After measuring a homogeneous
partition, set `SWECRAFT_EVALUATOR_WORKER_CLASS` on seed and candidate workers to
the same calibrated class name.

A complete record with all three metrics on one task (benchmark statistics
abbreviated to two entries; a real run carries one entry per benchmark URI):

```json
{
  "id": "20260811T054500Z-society-general-3f9a1c2b04de",
  "evaluation": "society-general",
  "environment_spec": "world@seed+ray:v2.9.1",
  "environment": {"msgpack": "9f2c41d0e8a7...", "ray": "b51e7a93c4f2..."},
  "environment_digest": "3f9a1c2b04de",
  "started": "2026-08-11T05:45:00+00:00",
  "finished": "2026-08-11T05:45:30+00:00",
  "evaluators": {
    "ray-bench": {
      "gate": true,
      "error": "",
      "tasks": {
        "serialization": {
          "error": "",
          "walltime": {"samples": [0.297, 0.194, 0.201]},
          "codspeed-walltime": {
            "seconds": 1.8e-05,
            "benchmarks": {
              "benches/test_bench.py::test_fib_bench": {
                "min_ns": 12769.48, "mean_ns": 13094.01, "median_ns": 12945.9,
                "max_ns": 19288.42, "q1_ns": 12856.6, "q3_ns": 13234.84,
                "stdev_ns": 373.06, "rounds": 2980, "iter_per_round": 77,
                "warmup_iters": 75857, "total_time": 3.000685,
                "iqr_outlier_rounds": 157, "stdev_outlier_rounds": 64
              },
              "benches/test_bench.py::test_fib_param[5]": {
                "min_ns": 429.84, "mean_ns": 451.7, "median_ns": 448.27,
                "max_ns": 626.52, "q1_ns": 442.64, "q3_ns": 457.25,
                "stdev_ns": 13.73, "rounds": 2998, "iter_per_round": 2028,
                "warmup_iters": 1650662, "total_time": 2.747278,
                "iqr_outlier_rounds": 127, "stdev_outlier_rounds": 47
              }
            }
          },
          "codspeed-cpu-sim": {
            "instructions": 578269,
            "benchmarks": {
              "benches/test_bench.py::test_fib_bench": {"instructions": 407680},
              "benches/test_bench.py::test_fib_param[5]": {"instructions": 14918}
            }
          }
        }
      }
    }
  }
}
```

Scores never appear in records: a record is measurements plus provenance,
and scoring recomputes speedups from a candidate/reference record pair at
read time. Per-benchmark `benchmarks` maps are diagnostics; scoring reads
only the task-level sample fields below.

Evaluator images must provide:

- `python3` on `PATH` for the stdlib harness
- the frozen evaluator checkout at `/study/ruler`
- `pytest-codspeed` for `codspeed-walltime` and `codspeed-cpu-sim`
- stock Valgrind with Callgrind for `codspeed-cpu-sim`

The CPU-sim path runs
`CODSPEED_ENV=1 valgrind --tool=callgrind --instr-atstart=no ... pytest ... --codspeed --codspeed-mode simulation`.
Scoring uses Valgrind's stderr `Collected` total. URI-tagged Callgrind part
dumps are parsed into the record's benchmark diagnostics and then removed;
`callgrind_annotate` is not part of measurement or scoring.

## Scoring

A task's sample field is explicit by metric:

| metric | scored task sample |
| --- | --- |
| `walltime` | mean of `samples` |
| `codspeed-walltime` | `seconds` |
| `codspeed-cpu-sim` | `instructions` |

Speedup is `seed / candidate` (>1 = faster). The evaluation's score
aggregates its task speedups; unscoreable tasks stay visible as `null`. The
seed reference is measured on demand and reused only when every declared task
carries the evaluation's current metric. A legacy `codspeed` record is
history, not a valid reference for `codspeed-walltime` or `codspeed-cpu-sim`;
rerunning `run <evaluation> --env world@seed` refreshes a baseline explicitly.

## CLI

```
swecraft-evaluator [--manifest evaluation.yaml] [--records DIR]
              [--forge-url URL] [--forge-token-file PATH] COMMAND

validate                 parse the manifest and world lock
run EVALUATION --env E   run one evaluation on one environment and score it
export E --env E --bundle DIR
                         write an offline bundle for Slurm/Apptainer workers
worker BUNDLE            run one cell selected by --cell, --array-index, or Slurm
collect BUNDLE           collect terminal cell results into record.json
score [EVALUATION]       score the newest candidate record(s) against seed
loop EVALUATION          cadence sweeps plus release evaluations, forever
```

`loop` runs the named evaluation on `world@latest` every
`--interval-seconds`, and evaluates every unseen release tag oldest-first on
`world@seed+repo:tag` using `downstream-<repo>` (falling back to the cadence
evaluation). A release's cursor advances only after its record exists, so an
infrastructure failure is retried on the next pass. State is one JSON cursor
file (`--state`). The first pass initializes release cursors without
replaying history; replaying an old release is an explicit `run`.

The complete state file — no measurements, only cursors:

```json
{
  "last_sweep": "2026-08-11T05:45:00+00:00",
  "releases": {"msgpack": "v1.2.1", "ray": "v2.9.1"},
  "releases_initialized": true
}
```

`last_sweep` times the next cadence sweep, `releases` holds the newest
release tag already evaluated per repository, and `releases_initialized`
makes the no-replay bootstrap durable for repositories that had no releases
yet. Deleting the file is safe: the loop re-initializes cursors from the
current release lists, at the cost of skipping releases published in the
gap. Measurements live only in records.

Wake-based cadence belongs to the coordinator: `evaluate_every_wakes` in
`coordinator.yaml` (with the full `evaluate_command` argv) fires the
evaluation at a drained step boundary, beside `checkpoint_every_wakes`, so
`world@latest` is a quiet, well-defined snapshot. The evaluator itself does
not read the coordinator ledger.

An evaluation may also list `exposed: [evaluator@task, ...]` — the subset of
its tasks a society release can reach, as the operator judges it; the
derived report aggregates over those beside every task.

## Derived report

`swecraft_evaluator.report.build_report(manifest, records)` turns the records
directory into one derived report, so every reader computes a number the
same way. Its vocabulary:

- A **reading** is one record, classified by `environment_spec`: `seed`
  (the reference), `latest` (a cadence sweep of the world's heads),
  `release` (`world@seed+repo:tag`), or `other` (an explicit `run` label).
  A label of the form `world@worked<N>`, `world@backfill-<N>`,
  `world@cutoff-<N>` names the world as of wake `<N>`; `<N>` becomes the
  reading's step, and the evaluation views (`services/export/evaluation_views.py`)
place such a reading at that wake —
  not at the wake count of the moment it was measured, which for a
  backfill can be thousands later.
- A **task** is one `evaluator@task`; its **speedup** is `seed / current`
  against the battery's reference — the newest complete seed reading —
  exactly as `score` reports it.
- A task's **noise floor** is the largest `|ln(a/b)|` between two complete
  readings of the same environment digest. Replicates cost nothing to find
  because the digest is part of every record id: the seed pair and every
  repeated sweep of an unchanged world are replicates by construction. A
  task **moves** when `|ln speedup|` clears `max(2 × floor, ln 1.005)`; with
  no replicate the floor is assumed at 0.5% and flagged.
- A repository's **release chain** is its release readings in order. Each
  step's **marginal** effect on a task is its speedup over the previous
  measured tag (over the seed for the first); the task's **owner** is the
  step with the largest marginal that cleared the floor.
- A release's **control leak** is the geomean over the tasks it was
  measured on that its `downstream-<repo>` battery does not name — tasks it
  cannot touch — and should read ~1.00. A repository with no battery of its
  own is all control.
- **Exposed tasks** are the ones a society release can reach at all. An
  evaluation's `exposed:` list is authoritative when present — the place
  for a consumer census the manifest's structure cannot express. Without
  it, the set is derived: every task some `downstream-*` battery names,
  leaving out **ambient** subjects (a test runner or packaging tool, whose
  battery is every task and so says nothing). Reports say which source
  they used.
- The **additivity residual** compares the newest sweep with the product of
  every repository's newest single-repo release vector on the tasks they
  share: the gap is interaction between repositories, or an unmeasured one.

The report is JSON-shaped (`readings`, `series`, `tasks`, `releases`,
`owners`, `additivity`, `health`).
