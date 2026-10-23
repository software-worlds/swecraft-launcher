# Worlds: intent and seed provenance

A **world** is the set of Forge repositories a society can see and maintain,
together with the exact state from which those repositories began evolving.
The world protocol makes that initial condition reproducible and
machine-checkable.

Two documents deliberately have different authors:

| Document | Authority | Meaning |
| --- | --- | --- |
| `world.yaml` | Experiment operator | Repository names, upstreams, pins, branches, and seed policy |
| `world.lock.json` | Seeder | Resolved pin objects and the baseline commits and trees actually created |

The manifest is intent and is never rewritten by tooling. The lock is an
observation and is always written atomically. Keeping them separate makes both
operator edits and changes to the seeded world visible.

## Manifest

Pins state their type explicitly:

```yaml
schema_version: 1
world_id: experiment-2
org: swecraft
repos:
  - name: pydantic
    upstream: https://github.com/pydantic/pydantic.git
    pin:
      type: tag
      value: v2.13.4
    visibility: public
    default_branch: main
    seed_protocol: relocate-github-actions-v1
```

`seed_protocol` names the versioned transform that neutralizes upstream
GitHub workflows (the forge reads `.github/workflows` too, so left in place
they fire uselessly against a runner that cannot reach github.com):

- `relocate-github-actions-v1` (default) — moves them byte-identical to
  `.github/workflows-upstream/` with a short README. Agents keep the
  upstream CI as reference — test matrices, lint steps, release flows —
  instead of spending early wakes rebuilding it from nothing, which the
  first experiments showed they otherwise do.
- `strip-github-actions-v1` — deletes them outright; kept for reproducing
  worlds seeded before the relocate transform existed.

Either way the transform is one commit on top of the pin, its exact paths
are recorded in the lock, and `verify_transform` proves the live tree
matches the declaration (for relocation, including blob-identity of every
moved file).

A commit pin uses `type: commit` with a full lowercase commit ID. Repository
names are unique. If two entries deliberately resolve to the same commit, the
later one declares `duplicate_pin_of` and names the primary entry that appears
earlier in the manifest.

Repositories default to `visibility: public`. Set `visibility: private` on
each repository in an isolated/no-collaboration arm; the seeder creates private
Forgejo repositories, records that intent in the lock, and verification fails if
the live repository visibility drifts.

## Lock

Each lock entry repeats the manifest intent and records:

- the full pin commit and tree IDs;
- the full seeded baseline commit and tree IDs;
- the exact `.github/workflows/` paths removed by the seed protocol;
- the intended Forgejo repository visibility;
- whether Forgejo Actions was enabled;
- optional adoption-time current HEAD provenance.

The lock also binds to the SHA-256 of the manifest's exact bytes, the SWECraft
revision that produced it, and exactly one creation mode (`seeded_at` or
`adopted_at`). A resumable `partial` lock contains a prefix of manifest
repositories; a `complete` lock contains all of them.

The protocol layer checks document shape and cross-document identity. The
seeder engine supplies the live evidence: it resolves every ref before mutation,
proves trees and the exact transformation diff, checks baseline ancestry, and
checks Forgejo repository state. A complete lock is therefore evidence from the
tool that created or adopted the world, not a collection of hand-copied SHAs.

## Seed, adopt, and verify

Start from the committed example, preferably beside the experiment it defines:

```bash
cp examples/world.yaml experiments/my-experiment/world.yaml
just world-seed experiments/my-experiment/world.yaml
```

`world-seed` resolves every upstream pin before changing Forgejo, rejects
undeclared duplicate commits, removes only the upstream GitHub workflows,
pushes the upstream's tag history without the pin's future (the pin tag is
the version anchor agents inherit for their own releases; any tag that
descends from the pin is withheld, so a world never carries releases
upstream cut later), holds the Actions unit disabled for the whole seed,
verifies the result, and atomically journals each repository to
`world.lock.json`.

The forge is one of two places a later upstream release can reach a world from.
The other is the package index: point the world's agent images and CI at the
[capped package index](index-cap.md) for the same date.

**Seeding never enables Actions.** That is a separate command:

```bash
uv run --package swecraft-world swecraft-world --manifest experiments/my-experiment/world.yaml enable-actions
```

The split is not tidiness. Tags' trees still carry upstream
`.github/workflows` — the seed transform commit sits *on top of* the pin,
so it cannot reach them — and `git push` returns as soon as refs are
accepted while Forgejo evaluates the ref-update events asynchronously.
Enabling Actions at the end of each repository's seed raced that
evaluation: the pushed tags were evaluated against a repository that had
just become enabled, and every upstream workflow fired. A 26-repository
world produced **18,331 queued jobs** this way against a `capacity: 1`
runner, which then blocked society checkpointing entirely because capture
waits for CI to quiesce. The seed-time no-tasks assertion could not catch
it either: at the instant it ran the queue was legitimately empty.

Running the enabling step as its own command puts an unbounded gap
between the pushes and the switch, so the events are long since evaluated — while
Actions was still off — before anything is turned on. The command also
enforces the quiet window in code rather than relying on operator timing:
unevaluated ref-update events are invisible to every API read, so
`enable-actions` waits a settle window before enabling, proves each
repository quiet, enables, waits a second settle window, and re-checks.
If runs surface after the switch, it disables Actions again and fails
without rewriting the lock — so a world that would have been contaminated
fails loudly at that point rather than hours into a campaign. The lock
records what was observed: `actions_enabled` is `false` after seeding and
becomes `true` when `enable-actions` rewrites it.

If `enable-actions` fails partway — a contaminated repository is detected
after earlier repositories were already enabled — the lock is not
rewritten, so it still records `actions_enabled: false` for repositories
whose Actions unit is now on. Re-running `enable-actions` after resolving
the failure converges: enabling is idempotent, and each repository is
re-checked. Re-running `seed` in that state fails, because the seed
asserts Actions is off; to re-seed such a world, either disable Actions on
the affected repositories first or use `--replace`.

`swecraft-world actions-status` reports runs in the `waiting`, `running`, or
`blocked` status per repository at any time — before enabling, to prove
the seed was clean; afterwards, to see what the society has queued against
the runner. The contamination gate inside `enable-actions` is stricter: it
counts runs in every status, because a correctly seeded repository has no
run history at all. Seed and adopt hold a non-waiting
operation lock beside that file, so concurrent mutations fail instead of racing
Forge changes. A failure partway through leaves a valid `partial` lock;
rerunning verifies the completed prefix and resumes. A no-op rerun preserves
the complete lock byte-for-byte, including its original `seeded_at`.

For a world created before the protocol existed:

```bash
just world-adopt experiments/my-experiment/world.yaml
```

Adoption never mutates live repositories. It locates the direct seed-transform
child of the pin on first-parent history, verifies that its diff is exactly the
declared workflow deletions, and records a later current HEAD separately.

Before a run, verify the complete live world:

```bash
just world-verify experiments/my-experiment/world.yaml
```

Verification resolves upstream again, proves that pin and baseline trees still
match the lock, requires every baseline to remain an ancestor of live HEAD, and
checks that repositories are nonempty, have the locked visibility,
Actions-enabled, and free of upstream `.github/workflows/`. Only the initial
seed constrains tags — exactly the upstream's tags minus any that descend
from the pin, nothing else — and requires no queued Actions: release
tags and completed agent-authored CI are legitimate
later evolution. Use `swecraft-world --help` for explicit Forge URL, lock,
token-file, and cache options. Tokens are read from a file and never placed on
command lines.

## Relationship to the rest of SWECraft

```text
world.yaml
  → seeder → world.lock.json
                  ├─ coordinator cross-checks its configured repository set
                  ├─ evaluator uses baseline commits as the society reference
                  └─ society snapshots preserve the lock plus later HEADs
```

The lock's `baseline_commit` remains the experiment's initial condition even
after agents move `main`. Snapshot-time current heads and evaluator candidate
heads are separate observations and never overwrite the seed baseline.

Hidden downstream evaluator repositories and workloads are not part of the
world: the world describes the agent-visible training environment, while the
evaluator describes external outcome measurement.

## Package boundary

[`services/world`](../../services/world/README.md) provides the versioned
Pydantic models, manifest/lock binding, atomic lock I/O, and the seed/adopt/check
engine. Git runs through `swecraft-common`'s isolated helper, Forgejo calls through
its bounded `httpx` transport, and service-specific mutation policy remains in
`swecraft-world` rather than expanding the common package.

```bash
just world-test
just world-check
```
