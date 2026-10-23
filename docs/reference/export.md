# Export: a run as a browsable bundle

One finished run, as its [society checkpoint](society-checkpoints.md), becomes
a bundle of JSON files that [sim-browser](https://github.com/software-worlds/sim-browser)
shows: the world map, every session's transcript, every issue and pull
request, the code the agents changed, every branch and commit, and each
agent's charter and memory. The exporter (`services/export`) is vendored from
sim-browser, which defines the bundle protocol
(`services/sim-browser/src/lib/bundle/protocol.ts`); it uses only the standard
library.

For an experiment folder, one command collects the newest checkpoint and
exports it:

```bash
swecraft-launch export experiments/<name> [--checkpoint <dir>] [--world <layout.json>] \
    [--known-secrets <file>] [--out bundles]
```

That runs the two steps below. The [analyzer](analyzer.md)'s `collect` builds
the evidence store and unpacks the forge; the exporter reads it with the
checkpoint:

```bash
uv run --package swecraft-analyzer swecraft-analyzer collect <checkpoint> <analysis>
uv run python services/export/export_bundle.py \
  --evidence <analysis> --checkpoint <checkpoint> \
  --world <layout.json> --id <run-id> --label "<run name>" --out bundles
```

`--world` is a layout: the world's repositories with their positions on the
map's hex grid (`hex: [q, r]`), languages, dependency edges and held-out
repositories. `swecraft-launch export` writes a plain one from `world.yaml` (the
repositories on a spiral, no languages or edges) when none is given. A layout
may also name each agent's portrait (its `run` block); every other agent gets
one of sim-browser's portraits, picked by a hash of its name from the copy in
`services/export/avatars/` (`--avatars` names another folder). The exporter
replaces the run's files together and adds or updates the run's entry in
`index.json`, and prints what it derived and the size of every file kind.

## The evaluation page

`services/export/evaluation.py` writes the run's `evaluation.json`, which
sim-browser's evaluation page reads: the read API's evaluation views
(`summary`, `series`, `tasks`) over the checkpoint's evaluator records, the
paper's two scores at each reading (`carry` and `strict`), and a `source`
block. Readings are placed on the run's completed sessions from the
coordinator's ledger, opened read-only. `swecraft-launch export` runs it after the
bundle; a checkpoint with no ledger or no records gets no evaluation page.

```bash
uv run python services/export/evaluation.py --checkpoint <checkpoint> --id <run-id> \
  --out bundles/<run-id>/evaluation.json
```

It needs the workspace (`swecraft_evaluator`, `swecraft_coordinator`); the views are
vendored from the read API as `services/export/evaluation_views.py`.

## The bundle

| File | Contents |
| --- | --- |
| `bundles/index.json` | The exported runs, with each run's group, label and size |
| `<run>/core.json` | World, agents, sessions, forge events, repositories, and the thread and release index; loaded first |
| `<run>/threads/<repo>.json` | Every issue and pull request of one repository, in full, and its releases |
| `<run>/code/<repo>.json` | What the agents changed in the repository's package: each changed file's diff and text at head |
| `<run>/sessions/<id>.json` | One session: its forge actions, final response, transcript (every step of its trajectory, whole), changes and receipt |
| `<run>/git/<repo>.json` | A repository's branches, the default first, each with its head commit; an agent's dedicated home `<owner>/<name>` is named `<owner>~<name>` in file names |
| `<run>/git/<repo>/trees/<commit>.json` | Every file of one branch's head commit |
| `<run>/git/<repo>/log.json` | The repository's commits since the society started, newest first: each commit reachable from a branch or a pull request's head and not from the seed commit |
| `<run>/git/<repo>/commits/<id>.json` | One commit's whole message and its changes against its first parent, each file with its status, line counts and diff |
| `<run>/blobs/<id>.json` | One file's content by git's object id, stored once for the run; a binary content says whether its bytes are exported or withheld |
| `<run>/blobs/<id>.bin` | A binary file's bytes, unmodified; written unless the exporter found a secret in them |
| `<run>/agents/<name>.json` | An agent's instructions (its charter) and where its memory branch is |


The map, the menus and every list read `core.json` alone; a thread, code,
session, branch, tree, blob or agent file is fetched when a reader opens one.

## What is derived

- **Events** are the runner's ledger of forge actions when the checkpoint has
  one. A checkpoint with an empty ledger has its events derived from the
  forge's own record, named with the runner's action vocabulary: pull requests
  opened, merged or closed, issues opened or closed, comments, reviews, and the
  releases agents published during the run. An opened pull request belongs to
  the session the evidence names as its opener; any other event belongs to the
  acting agent's session running at the time, allowing the analyzer's five
  seconds of difference between the two clocks.
- **Sessions** include each agent's bootstrap, which the coordinator's ledger
  does not book: its start comes from its run id, its end from its transcript.
- **Transcripts** are each session's `trajectory.json`, the ATIF record its
  harness's converter writes in the checkpoint: every step in its order, each
  with its number (the trajectory's step id, which the analyzer's evidence and
  the factsheet's links use), its author (system, user or agent), its time to
  the second, its model, message and reasoning, its tool calls, and its
  metrics as the trajectory records them (token counts, and what else the
  converter records under `extra`). A call keeps its command or code
  (`command`, `cmd`, Codex's JavaScript `input`, kimi-cli's unparsed `raw`,
  `patch`), every other argument by name, and the result naming its id;
  calls that share an id take those results in order.
- **kimi-cli's session log** stands in for what its trajectory lacks. The
  trajectory has no times and no prompt step, and gives a step whose message
  has no text the message "(tool use)", which is read as no message. The run
  keeps a log (`session/**/wire.jsonl`) for its session and one for each
  subagent it starts; the session's own is the one that begins first. Its
  user input is the session's prompt, and when it has as many steps as the
  trajectory, the time each step begins (`StepBegin`) is that step's time;
  otherwise the steps have no times, and the export's report lists the
  session. Of the log only these are read. When no step holds the prompt
  and no session log has it, the run's `input/prompt.md` is kept beside the
  steps if the checkpoint has one.
- **The evidence's tool calls** are a session's steps only when its
  checkpoint holds no readable trajectory; the evidence has only the tool
  calls. A session with neither has no steps. Each session's file names where
  its steps come from (`transcript`), and its tool calls in `core.json` are
  counted from the steps the file holds.
- **Agents** come from the coordinator's agent-to-repository table, or from the
  roster's naming rule (`<repo>-bot`) when the checkpoint has no such table.
- **Code** is read with `git` from the forge's bare repositories
  (`services/export/repo_code.py`): every file that differs between a
  repository's seed commit and its head, with its status, line counts, diff
  and text at head, and the top-level listing at head. Git runs without the
  user's or the system's configuration and never runs an external diff or a
  text conversion.
- **Git data** is read with `git` from the same repositories
  (`services/export/repo_git.py`), under the same rules: every branch, the
  default first and then the newest; every file of each branch's head commit;
  and each file's content once by git's object id, shared across the run's
  branches and repositories. A content with a NUL byte in its first 8,000
  bytes is binary: its bytes are written unmodified as `blobs/<id>.bin`,
  unless a secret is found in them ([Secrets](#secrets)). A link's content is
  its target. A tree entry whose path is not a plain relative path is refused
  and counted.
- **History** is read the same way: each commit reachable from a branch or a
  pull request's head (`refs/pull/<n>/head`) and not from the repository's
  seed commit, newest first by committer date, with its parents, author,
  dates, subject and the branches whose head it is. The history before the
  seed is upstream's and is left out, and so is every commit reachable only
  from an imported upstream tag. A repository whose seed the forge does not
  hold has no log; a dedicated home, made during the run, has its whole
  history. Each commit's file holds its whole message and its diff against
  its first parent, with the code's diff options and no length limit; a
  commit without parents is compared with the empty tree.
- **Agents' details** come from the checkpoint's apparatus: the instructions
  are the agent's charter (`apparatus/charters/<agent>.md`), and the memory is
  the `agent/memory` branch of the agent's home: the `home_repo` of its profile
  in the coordinator's configuration (`apparatus/coordinator.yaml`, of which
  only each agent's `name` and `home_repo` are read), or, when the profile
  names none, the repository it maintains. A home outside the world's
  organisation is dedicated; its git data is exported too, its files named
  `<owner>~<name>`.
- **Texts** are kept whole: comments, descriptions, patches, responses,
  commit messages, every diff and text of the code, and every text of a
  trajectory's steps. The tool inputs and outputs of a session whose steps
  come from the analyzer's evidence are shortened: the evidence keeps an
  output's first 3,000 and last 1,000 characters and an input's first 20,000,
  and those texts are marked `truncated`.

## Secrets

Every file holds only the fields the protocol lists, and every text is scrubbed:
token formats and credentials named by their context are replaced with
`[redacted]` and counted (`services/export/redact.py`). The context includes an
Authorization header, a URL's password or a user there shaped like a token, and
a variable, key, flag or environment list entry named as a token, secret or
password.

The redactor cannot rewrite a binary content, so it searches the bytes: for
each known value (below) as exact bytes, and for each pattern over the bytes
read as latin-1, one character per byte. A compressed content is searched in
what it expands to as well: xz, gzip, bzip2, and each member of a zip
archive, nested up to four layers deep and up to 64 MB per file. When the
search finds a match, that file's bytes are not written; its blob is marked
`withheld` and counted. A file part of whose content cannot be read
(encrypted, damaged, past the 64 MB limit, or in a format the standard
library does not read, such as zstd) is exported, and the export's report
lists it by repository and path as unscanned, as it lists each withheld
file.

A secret printed with no context (the output of `printenv SWECRAFT_FORGE_TOKEN`) is
recognised only by its value. `--known-secrets FILE`, on `export_bundle.py`,
`repo_code.py` and `repo_git.py`, names a file the operator prepares outside the checkpoint and
the analyzer's directory, one value per line; every exact occurrence of a value
is replaced too. The values are never written anywhere, and the checkpoint's own token and
credential files are never opened. Bundles hold private run data: keep them
out of version control (`bundles/` is ignored here) and serve them only to
the people who may read the run.

## Tests

The exporter's tests build a small synthetic society from nothing
(`services/export/synthetic.py`); no real run data is used. The same society,
exported, is the example bundle in `services/export/fixtures/`, which a test
keeps current; regenerate it after a change to the exporter's output:

```bash
just export-test
uv run python services/export/synthetic.py services/export/fixtures
```
