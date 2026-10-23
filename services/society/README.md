# SWECraft Society Checkpoints

`swecraft-society` captures an idle experiment as one verified private research
dataset and materializes that dataset elsewhere without starting services.
Full protocol and operational details are in
[`docs/reference/society-checkpoints.md`](../../docs/reference/society-checkpoints.md).

## Scope

V1 provides:

- transactional local checkpoint creation;
- exact world baseline and current-head verification from frozen Forge state;
- canonical directory and standalone `tar.gz` transports;
- private directory-native Hugging Face publication and loading;
- pure filesystem load into `.swecraft/loaded/`;
- manifest inspection and verification.

V1 load creates no Docker volume and starts no Forge, coordinator, evaluator,
or runner. Only the separate `resume` command imports state and makes the
loaded destination mutable.

Service-owned adapters preserve boundaries:

- Coordinator parses its own config and captures its own ledger, credentials,
  and runs (traces always; raw Harbor archives by policy).
- Evaluator inspects and captures its own queue and result state.
- Forge owns Docker/Compose lifecycle, volume import/export, copied-service
  startup, and frozen-repository validation.
- Society owns orchestration, the canonical inventory, and transports.

## Configuration

```yaml
schema_version: 1
checkpoint:
  output_dir: .swecraft/checkpoints
  harbor: omit  # traces always captured; 'include' adds raw Harbor archives
  compose_project: swecraft-forge
  actions_wait_seconds: 300
  evaluator_records_dir: .swecraft/evaluator/records
  evaluator_state: .swecraft/evaluator/state.json
load:
  output_dir: .swecraft/loaded
resume:
  compose_project_prefix: swecraft-resumed
huggingface:
  repo_id: software-worlds/experiment-3
  private: true
```

At a boundary, checkpointing lets queued/running Forgejo Actions finish for up
to `actions_wait_seconds`. A timeout skips the checkpoint and leaves CI alone;
`0` restores advisory-only crash-consistent capture.

A checkpoint contains private society-local state. External model, cloud, and
HF credential references are never resolved into it. HF publication confirms
the dataset is private before upload; its generated `README.md` is protocol
content and must not be edited in the HF UI.

## Commands

```bash
swecraft-society checkpoint
swecraft-society checkpoint --archive ./experiment.society.tar.gz
swecraft-society checkpoint --publish
swecraft-society inspect <checkpoint-directory-or-tar.gz>
swecraft-society load <directory-or-tar.gz> [destination]
swecraft-society load hf://software-worlds/<dataset>[@revision] [destination]
swecraft-society resume [loaded-destination]
```

## Development

```bash
just common-check && just common-test
just coordinator-check && just coordinator-test
just evaluator-check && just evaluator-test
just forge-runtime-check && just forge-runtime-test
just society-check && just society-test
just society-e2e  # live checkpoint → archive → pure load → resume
```
