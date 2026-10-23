# Post-hoc checkpoint analyzer

`swecraft-analyzer` (`services/analyzer`) reads one finished experiment, as its
[society checkpoint](society-checkpoints.md), into evidence that can be
queried: one SQLite store of every wake, tool call, forge action, pull
request, diff, comment, review and release, with the frozen forge unpacked
beside it. No step uses a model. The [exporter](export.md) builds a run's
browsable bundle from this directory and the checkpoint.

```bash
uv run --package swecraft-analyzer swecraft-analyzer collect <checkpoint-dir> <analysis-dir>
uv run --package swecraft-analyzer swecraft-analyzer query   <analysis-dir> "SELECT ..." [--json]
```

A 600-wake run (35,000 tool calls, 600 pull requests) collects in about a
minute. Both commands use only the standard library, so they also run as
`python3 -m swecraft_analyzer` where the workspace is not installed.

## Input

The checkpoint directory, never a live run: `manifest.json`, `apparatus/`
(world lock, evaluation manifest), `state/coordinator/coordinator.db` (the wake
ledger), `state/forge/forgejo-data.tar` (Forgejo's database and the bare
repositories) and `runs/<run-id>/output/` (receipt, response, ATIF
`trajectory.json`, `forge-actions.jsonl`). Nothing in it is written: the one
thing unpacked, the forge volume, goes into the analysis directory, so the
checkpoint can sit on a read-only mount. The checkpoint's credential files
are never read.

A checkpoint whose pi runs keep no `trajectory.json` can be completed from the
run's live tree: `collect --runs-dir <checkout>/.swecraft/runs` reads each pi log
(`harbor/jobs/*/*/agent/pi.txt`) through the converter that writes pi's
`trajectory.json`, and records the source as `live-pi-log`. A run with no
transcript anywhere is counted under `transcripts.none` in `collect.json`.

## Outputs

| File | Contents |
| --- | --- |
| `evidence.db` | The tables below. |
| `collect.json` | Row counts per table, transcript sources, and every problem met (missing ledger rows, pull requests without a diff, a world that carries upstream's future). |
| `forge/` | The checkpoint's forge volume, unpacked: Forgejo's database and the bare repositories. |

## Evidence tables

| Table | One row per | Key columns |
| --- | --- | --- |
| `wakes` | captured run, joined to the ledger | `wake_id`, `run_id`, `agent`, `status`, `outcome` (`unledgered` for each agent's bootstrap), `cost_usd`, `harness`, `model`, `transcript`, `response` |
| `tool_calls` | tool call in a transcript | `run_id`, `step`, `tool`, `kind` (`shell`, `edit`, `write`, `read`, `web`, `other`), `text` (command, patch or path), `path`, `output` (head and tail) |
| `forge_actions` | forge write a wake made | `run_id`, `repo`, `action` (`pr.create`, `pr.merge`, `release.create`, …), `number` |
| `pulls` | pull request | `repo`, `number`, `author`, `state`, `merger`, `merge_base`, `head_commit`, size, `opened_wake_id` and `opened_run_id` (the wake that opened it) |
| `pull_files`, `pull_patches` | changed file, full diff | `category`: `src`, `test`, `bench`, `ci`, `config`, `docs`, `goal` |
| `comments` | forge timeline entry | `kind` (`comment`, `review`, `pull_push`, `merge_pull`, …), `force_push` |
| `reviews`, `content_edits` | review; edit or deletion of forge text | `reviewer`, `pr_author`, `kind`; `editor`, `content_author`, `is_deleted` |
| `releases`, `ci_runs`, `ci_schedules` | release or tag; CI run; scheduled workflow | |
| `future_tags`, `future_commits`, `future_lines` | upstream tag, commit and added Python line from after the world's pin that the forge carries | see contamination below |
| `meta`, `agents`, `repos` | checkpoint facts, agent→repository, seed and head per repository | `meta.wakes_completed`: the ledger's completed wakes |

The codex CLI's code mode writes JavaScript rather than tool calls
(`tools.exec_command({cmd})`, `tools.apply_patch(patch)` with the patch bound
to a variable). The collector resolves those calls; a call it cannot resolve is
still stored as shell text, so a parser gap never hides a command.

**Contamination.** A world is upstream frozen at a pin. Worlds seeded before
the seeder withheld later tags also carried the release tags upstream cut
after the pin, with their commits. The collector finds them in the
checkpoint's own forge: a tag whose commit has the pin as a strict ancestor
but not the seeded baseline (agents' release tags all descend from the
baseline). It records what those commits added to Python files, minus lines
the seed already had, in `future_tags`, `future_commits` and `future_lines`,
so a query can look for pull requests that reproduce them verbatim. Worlds
seeded now carry no future tags; upstream code can then only arrive over the
network.

## Limits

- Forge reads are not in `forge-actions.jsonl`, and raw `curl` or `git push`
  bypass the forge wrappers; those acts are visible only in transcripts.
- Tool outputs are kept as their first 3,000 and last 1,000 characters, and
  tool inputs as their first 20,000.
