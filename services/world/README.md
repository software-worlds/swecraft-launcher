# swecraft-world

The versioned protocol defining a society's initial world. `world.yaml` records
operator intent; `world.lock.json` records the exact pin and seeded baseline
commits and trees observed by the seeder.

This service owns schema validation, manifest/lock binding, atomic lock I/O,
and the manifest-driven seed/adopt/check engine. Git execution and Forgejo
transport come from `swecraft-common`; the transformation and provenance policy stay
here. See [the world architecture](../../docs/reference/worlds.md) for operations and for
how these documents bind the coordinator, evaluator, and society snapshots to
one initial condition.

## Manifest

Pins are explicit rather than ambiguous strings:

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

A commit pin uses `type: commit` and a full lowercase object ID. If two entries
resolve to the same commit, the later entry must record
`duplicate_pin_of: <primary>`; the primary must appear first.
Repositories default to `visibility: public`; set `visibility: private` when a
world is meant to hide sibling repositories from non-collaborator agents.

## Lock

The machine-written JSON lock binds to the SHA-256 of the manifest's exact
bytes and repeats its repository intent alongside resolved provenance:

- full pin commit and tree IDs;
- full seeded baseline commit and tree IDs;
- exact removed `.github/workflows/` paths;
- repository visibility;
- Actions enablement;
- the seeder's SWECraft revision;
- either `seeded_at` or `adopted_at`.

A partial lock is a resumable prefix of manifest repositories. A complete lock
contains every repository. Structural validation happens before mutation; the
engine additionally proves the commits, trees, transform diff, and ancestry
against Git and the live Forge.

## Usage

```bash
cp examples/world.yaml world.yaml       # adjust and commit as experiment intent
just world-seed world.yaml             # writes world.lock.json beside it
just world-adopt world.yaml            # observe an existing world, no mutation
just world-verify world.yaml            # fail closed on live drift
```

The lower-level CLI exposes alternate paths and endpoints:

```bash
uv run --package swecraft-world swecraft-world --help
```
