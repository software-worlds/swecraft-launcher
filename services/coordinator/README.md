# swecraft-coordinator

Autonomous per-repository maintainer agents: each configured agent is a
forge bot user that owns one repository, woken on a fixed cadence to decide
for itself what to do — plan, implement, review, merge, converse, or idle.
Built on the one-shot runner (direct-action mode), with its trusted memory
publish path using swecraft-common's isolated Git primitive.

See [docs/reference/coordinator.md](../../docs/reference/coordinator.md) for configuration and
operation, and `just coordinator-help` for the CLI.
