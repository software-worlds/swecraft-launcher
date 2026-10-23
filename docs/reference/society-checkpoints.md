# Society checkpoints

A society checkpoint is a self-describing research dataset describing an
experiment at one exact point. Its canonical representation is a directory.
A local `tar.gz` and a private Hugging Face dataset are transports for that
same directory, not separate formats.

V1 separates **checkpoint and pure load** from mutable **resume**. Load never
imports or starts services; an explicit resume command is the only transition
from a verified research dataset to a live mutable society.

## Evidence and authority

The checkpoint keeps four evidence roles separate:

```text
apparatus/   author intent and exact lock files
state/       opaque service-owned state captured consistently
runs/        agent observations and traces
evaluations/ measurement output
reviews/     end-of-run survey batches (peer and society reviews)
```

It joins existing authorities rather than restating them:

| Artifact | Authority |
| --- | --- |
| `world.yaml` | experiment author |
| `world.lock.json` | world seeder: exact pins and seed baselines |
| `evaluation.yaml` | experiment author |
| coordinator config, charters, images | experiment author |
| service-state bytes | owning service checkpoint adapter |
| checkpoint manifest | captured inventory, current heads, and provenance |
| HF commit | exact published checkpoint revision |

The inventory and captured bytes are authoritative. Apparatus Git metadata is
provenance; checkpointing does not require a clean working tree.

## Canonical directory

```text
<checkpoint>/
├── manifest.json
├── README.md
├── apparatus/
│   ├── world.yaml
│   ├── world.lock.json
│   ├── evaluation.yaml
│   ├── coordinator.yaml
│   ├── charters/
│   └── images/
├── state/
│   ├── forge/
│   │   ├── forgejo-data.tar
│   │   └── host/
│   │       ├── admin_token
│   │       ├── admin_password
│   │       ├── forgejo_internal_token
│   │       ├── forgejo_secret_key
│   │       └── runner/
│   │           ├── .runner
│   │           ├── config.yml
│   │           └── labels
│   ├── coordinator/
│   │   ├── coordinator.db
│   │   ├── api_token
│   │   └── credentials/
│   └── evaluator/
│       └── state.json
├── runs/
├── evaluations/
└── reviews/          # present when the experiment ran review batches
```

Coordinator wakes are projected rather than copied byte-for-byte. The live
`.swecraft/runs/<run-id>/` remains untouched; its checkpoint representation is:

```text
runs/<run-id>/
├── harbor.tar.gz              # raw Harbor tree, only with `harbor: include`
├── input/                     # failed wakes only
│   ├── prompt.md
│   └── request.json
└── output/
    ├── result.json            # terminal receipt plus resolved request fields
    ├── response.md            # when produced
    ├── changes.json           # inventory only, when produced
    ├── trajectory.json        # stable ATIF path, when produced
    ├── agent-stdout.txt       # selected agent stdout, when produced (pi: pi.txt)
    └── session/               # selected native session files
```

Run traces are always captured — they are the irreplaceable half of the
record. Only run directories with a complete terminal `completed` or `failed`
receipt are projected. Partial directories left by a hard-killed wake and
non-directory entries under `.swecraft/runs/` are reported and excluded rather than
blocking recovery of the society. Projections are independent and are
exported on a bounded thread pool. The bulky raw Harbor archive is the one
policy choice (`checkpoint.harbor: include|omit`, default `omit`); when
included it is archived without following symlinks, omits checkout and
changed-file payload copies outside that archive, and is assembled atomically
in checkpoint staging. Projection never renames, removes, or rewrites the
live run.

Session projection is CLI-specific. Codex keeps rollout JSONL files, Claude
keeps JSONL files plus tool-result payloads, and Kimi keeps only its native
`context.jsonl` and `wire.jsonl` files, including nested subagent sessions.
Kimi's live `kimi-cli.txt` JSON-RPC stream is an adapter-side intermediate used
to derive trajectory and usage, not a checkpoint artifact.

The Forge runner contributes exactly the three files above. Its act cache
(`state/forge/host/runner/.cache/` in the checkpoint tree) is excluded: it is ephemeral, rebuilt on demand,
and on a society whose agents write CI it reaches thousands of files.
Capturing it also made Hugging Face publication fail outright, because HF
rejects any path beneath a `.cache/` folder — the local checkpoint would
succeed while the dataset silently never updated.

A checkpoint is private and secret-bearing. It preserves society-local state,
including Forge and coordinator credentials, but capture never follows
external model-provider, cloud, or Hugging Face credential references.
Unresolved declarations such as `env:CLAUDE_CODE_OAUTH_TOKEN` remain apparatus
text rather than checkpoint inputs. The coordinator API token is copied into
state only when configured as `file:`; an `env:` source is never resolved, and
a literal remains only in the captured apparatus text.

## Manifest and verification

`schema_version: 2` records:

- checkpoint ID and UTC capture time;
- producer `hub_commit` as software provenance, not a compatibility version;
- apparatus Git provenance and exact inventory digest;
- exact world lock digest;
- seed baseline and checkpoint-time head for every world repository;
- Forge project, image, and volume provenance;
- observed agent image identity and platform;
- sorted contributor inventories of regular-file sizes and SHA-256 digests;
- explicit `omissions`: expected paths this capture could not include.

A checkpoint is a recovery artifact first and a research dataset second.
The world manifest, world lock, and coordinator config are recovery
essentials whose absence aborts capture; the evaluation manifest is
regenerable measurement metadata, and when it is missing, capture proceeds
**partial** — it is recorded under `omissions`, the dataset card says so,
and `resume` warns. A present manifest is always captured: society treats
it as opaque hashed bytes and never parses it. Nothing else may be
omitted.

The manifest's agent image is the image observed at checkpoint time, not a
claim that every historical run used it. Per-run apparatus/image epochs remain
owned by run and evaluation records and are deferred from this checkpoint
schema.

Verification proves:

1. every listed regular file has the recorded size and digest;
2. no unlisted file, symlink, or unsafe path is accepted;
3. the embedded world lock digest and world identity agree with the manifest;
4. manifest baselines agree with `world.lock.json`;
5. every baseline exists in its captured bare repository and is an ancestor of
   the captured head;
6. heads re-derived from the frozen Forge volume agree with the manifest;
7. the generated dataset card agrees with the manifest.

For a partial checkpoint the invariant is *everything listed is verified*:
evaluation-bound checks are skipped exactly when the manifest omits the
evaluation manifest, and an omission that contradicts the inventory (a listed
file also claimed omitted, a stray digest) fails verification.

The immutable baseline and current-head vectors are deliberately separate so
the loaded dataset can later become an evaluator subject.

## Ownership boundaries

Society owns the protocol, transaction ordering, inventory, and transports.
It does not parse another service's database or configuration schema and does
not implement Forge Docker details.

- `swecraft_coordinator.checkpoint` loads authoritative coordinator configuration,
  checks quiescence, and snapshots coordinator state and runs.
- `swecraft_evaluator.checkpoint` copies complete evaluation records and the
  evaluator's loop state.
- `swecraft_forge` owns Compose lifecycle, host-state capture, volume export, and
  frozen-repository validation.
- `swecraft_society` sequences those adapters and builds the manifest.

Participant packages never depend on Society.

The checkpoint deliberately does not carry a Compose file. V1 pure load does
not relaunch services, so `hub_commit` and the recorded Forge image are
provenance rather than a portability guarantee. Any later resume implementation
must compare its pinned Forge image with the manifest before importing the
Forge database.

## Checkpoint transaction

The quiescent boundary — lifecycle lock held, no active wakes — makes the
apparatus, coordinator ledger, run traces, and evaluator results static, so
they are all copied **while the Forge keeps serving**. Only the Forge
volume needs stopped services: Forgejo keeps SQLite and Git repositories in
one Docker volume with no synchronized live-snapshot guarantee, so shutdown
is its consistency boundary. The Forge outage therefore covers exactly the
volume tar. The **finalization** phase runs after the lock is released:
heads, ancestry proofs, hashing, and verification all read the
already-copied staging directory.

```text
validate world apparatus; degrade on absent evaluation metadata
        ↓
resolve participant-owned checkpoint adapters
        ↓
assert coordinator/evaluator state share one lifecycle lock
        ↓
acquire that lock and refuse active coordinator wakes
        ↓
copy apparatus, coordinator state, runs, evaluations    (Forge still up)
        ↓
wait for the Forgejo Actions tail to drain
        ↓
stop Forge services → tar the volume → restart them     (the only outage)
        ↓  — society is live again; the lock is released —
derive and validate locked baselines/current heads      (finalization)
        ↓
write and verify manifest/card/inventory
        ↓
atomically expose the canonical directory
```

Only **coordinator wakes** gate a checkpoint. The evaluator never does: it
writes neither the Forge volume nor the coordinator ledger, and its
evaluations can run for hours on their own cadence. Its records directory
is append-only and `record.json` is the last file a run writes, atomically,
so `evaluations/` receives exactly the record directories that contain
`record.json` — an in-flight record directory is skipped and logged, never
captured half-written.

An evaluation fetching sources when the frozen window begins can fail that
fetch; the failure lands in its record and the loop's next reconcile retries
release evaluations whose cursor has not advanced. The hours-long
measurement phase runs network-isolated and cannot observe the pause.

Forgejo Actions jobs are the one writer no lifecycle lock covers. A wake's
final push can leave a short asynchronous CI tail after the wake itself has
finished, so capture polls `/admin/actions/runners/jobs` and waits up to
`checkpoint.actions_wait_seconds` for queued/running jobs to finish. On
timeout the checkpoint is skipped and CI continues — checkpointing never
cancels an Actions job. Set the wait to `0` for the former advisory behavior,
which warns and proceeds with a crash-consistent frozen volume. If the admin
token or probe is unavailable, capture warns and retains that best-effort
crash-consistent behavior.

Claims block behind the lifecycle lock for the copy window only and resume
during finalization. Catchable failures restore observed Forge services and
remove staging. No process can perform cleanup after `SIGKILL`, host power
loss, or a runtime crash; restart the same Forge project/state directory
with `just forge-up` if an interrupted checkpoint leaves it stopped.

## Boundary checkpoints from the coordinator

Frequent checkpointing belongs inside the step loop, in the training-loop
sense: the coordinator owns the wake cadence, so it owns the checkpoint
cadence too. With

```yaml
# coordinator.yaml
checkpoint_every_wakes: 20   # 0 disables the cadence
checkpoint_keep: 5           # boundary checkpoints to retain; 0 keeps all
checkpoint_publish: true     # publish each boundary to HF, out-of-band
```

the serve loop counts completed wakes; when the cadence is reached it stops
dispatching, lets in-flight wakes finish, and invokes
`swecraft-society checkpoint` at the quiet boundary it created — then resumes.
The checkpoint command's quiescence refusal passes by construction and acts
as an assertion that the loop drained correctly.

`evaluate_every_wakes` (with an explicit `evaluate_command` argv) shares the
same boundary mechanism for evaluator sweeps; when both are due, the
evaluation runs first so its record enters the checkpoint.

`swecraft-coordinator checkpoint-now` requests the same boundary path
off-cadence: it writes a flag file the serve loop polls, so the loop
drains, captures, and resumes without any process stop — and works even
with the cadence disabled. The request is consumed by the attempt, success
or failure, and a failed request restores the previous cadence marker so
it never delays the next scheduled boundary.

With `checkpoint_publish: true`, a successful capture is uploaded by a
detached `swecraft-society publish` process; dispatch never waits on the
network, a still-running upload skips the next one instead of racing it,
and any checkpoint can be (re)published later by hand. `publish_command`
overrides the upload argv (the checkpoint path is appended as the final
argument) for deployments where the standard derivation does not fit.

The step marker is recorded **before** capture, so the checkpointed
coordinator database always carries its own boundary — a resumed copy
never believes another checkpoint is immediately due. Dispatch stays
paused for the whole synchronous capture invocation, including
finalization: the Forge (and every claim) resumes after the volume tar,
but the next wake dispatch waits until verification completes. That pause
lands between steps, where agents are asleep anyway — and publication is
explicitly not part of it.

A failed boundary is skipped loudly (the next attempt comes one full
cadence later). Retention prunes where Society actually writes: the
output directory is read from `.swecraft/checkpoint.yaml` — the same file the
subprocess reads — and only `manifest.json`-bearing directories are
candidates. `checkpoint_command` overrides the invocation argv when the
standard root-workspace `uv run --package swecraft-society swecraft-society checkpoint`
derivation does not fit the deployment.

## Pure load

All transports cross one materialization boundary:

```text
local directory ─┐
local tar.gz     ─┼─▶ materialize staging ─▶ verify ─▶ atomic loaded directory
HF dataset root ─┘
```

`load` performs filesystem operations only. It does not create a Docker
volume, start Forge, rotate credentials, or mutate the source checkpoint. An
empty destination is replaced atomically only after the copied directory
verifies. The loaded directory remains a verifiable research dataset suitable
for inspecting apparatus, repository vectors, traces, and evaluations.

Loading itself never starts a Forge preview or mutable society.

## Mutable resume

`resume` accepts only a pristine loaded checkpoint and then deliberately
crosses the mutable boundary:

1. warn when the producer SWECraft commit differs, but refuse a Forge image
   mismatch before importing the database;
2. create a fresh project-scoped volume and import the frozen Forge tar;
3. start Forge and its runner with the loaded host-state directory;
4. write `runtime/coordinator.yaml` and `runtime/checkpoint.yaml` with relocated
   URLs and absolute destination paths. The captured wake cadence continues
   against the resumed society rather than the checkout that launched it.

Resume continues the same private society: copied Forge admin, agent, read API,
and runner credentials remain unchanged because their matching server-side
records were imported with the Forge database. Credential rotation belongs to
a future fork operation, not V1 continuation.

Resume does not mutate `apparatus/coordinator.yaml` and leaves coordinator and
evaluator startup under explicit operator control. If a post-start step fails,
the error reports the exact Compose project and teardown command. After resume,
the loaded destination is mutable and is no longer expected to verify as the
original checkpoint; the source directory, archive, or HF commit remains
immutable.

## Local configuration

Git-ignored operational defaults live in `.swecraft/checkpoint.yaml`:

```yaml
schema_version: 1

apparatus:
  root: .
  world: world.yaml
  world_lock: world.lock.json
  evaluation: evaluation.yaml
  coordinator: coordinator.yaml
  charters: charters
  images: images

checkpoint:
  output_dir: .swecraft/checkpoints
  harbor: omit  # run traces are always captured; 'include' adds raw Harbor
  compose_project: swecraft-forge
  actions_wait_seconds: 300  # timeout skips checkpoint; it never cancels CI
  evaluator_records_dir: .swecraft/evaluator/records
  evaluator_state: .swecraft/evaluator/state.json
  # Defaults to SWECraft's standard Compose; overrides support isolated apparatus.
  # forge_compose_file: /absolute/or/relative/compose.yaml
  # forge_state_dir: /absolute/or/relative/state

load:
  output_dir: .swecraft/loaded

resume:
  compose_project_prefix: swecraft-resumed

huggingface:
  repo_id: software-worlds/experiment-3
  private: true
```

Configured evaluator paths must match the evaluator service's actual paths.
Capture refuses if the evaluator state file resolves to a different
lifecycle lock from the coordinator database.

## Commands

```bash
swecraft-society checkpoint
swecraft-society checkpoint --archive ./experiment.society.tar.gz
swecraft-society checkpoint --harbor include
swecraft-society checkpoint --publish
swecraft-society publish <directory-or-tar.gz> [--repo-id owner/dataset]
swecraft-society inspect <directory-or-tar.gz>
swecraft-society load <directory-or-tar.gz> [destination]
swecraft-society load hf://software-worlds/<dataset>[@revision] [destination]
swecraft-society resume [loaded-destination]
```

`publish` uploads an existing checkpoint: it re-verifies the inventory and
needs no quiescence, lifecycle lock, or Forge interaction, so it can run —
and be retried — while the society is live.

HF publication mirrors the canonical directory at the private dataset root,
refuses upload unless the repository is confirmed private, and reads the exact
uploaded commit's remote file inventory before tagging or reporting success.
Every local checkpoint file must be present; transport-added files such as LFS
metadata are allowed. Fetch resolves
a mutable branch or tag to an exact HF commit, materializes the cache's blob
symlinks into regular files, and records the resolved remote commit beside the
loaded copy; full semantic verification runs once on load's final staging
directory.

`README.md` is generated protocol content and must not be edited in the HF UI.
Changing it makes the dataset differ from its manifest and causes load
verification to fail. A credentialed HF smoke should cover two successive
publishes so LFS tracking survives wholesale replacement.
